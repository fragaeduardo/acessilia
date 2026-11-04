"""Evidence-bound dual/single-provider fusion execution handlers.

These handlers keep the original Toolbox extraction, provenance verification,
reuse policy and pre/postconditions. No local provider or pipeline replacement.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import tempfile

from backend.core.execution.handlers.common import _missing_obligation
from backend.core.execution.models import MethodResult
from backend.core.manifest.models import Artifact, ManifestElement, PageDescriptor, ProcessingManifest
from backend.core.manifest.preprocessing.render_regions import _source_artifact_id
from backend.tools.logger import logger
from backend.pipeline.fusion import extract_fused as _extract_fused_async
from backend.tools.toolbox_client import run_sync


def _run_extract_fused(source_path: Path, *, artifact_id: str | None = None, policy=None) -> dict:
    """Preserve the existing synchronous Toolbox-backed fusion path."""
    import asyncio
    import concurrent.futures
    from backend.pipeline.fusion import extract_fused
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(extract_fused(source_path, artifact_id=artifact_id, policy=policy))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(
                extract_fused(source_path, artifact_id=artifact_id, policy=policy)
            )
        ).result()


def _reusable_fusion_payload(manifest: ProcessingManifest) -> dict | None:
    from dataclasses import asdict

    from docstruct.policy import FusionPolicy

    from backend.services.cache import processing_cache_configuration

    ref = manifest.extractor.configuration.get("fusion_reuse") or {}
    try:
        content = Path(ref["path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != ref["sha256"]:
            return None
        record = json.loads(content)
        if record["source_sha256"] != manifest.source.sha256:
            return None
        expected_policy = manifest.extractor.configuration.get("fusion_policy")
        if expected_policy is None:
            expected_policy = asdict(FusionPolicy())
        if record.get("policy") != expected_policy:
            return None
        if json.dumps(record["configuration"], sort_keys=True) != json.dumps(processing_cache_configuration(), sort_keys=True):
            return None
        payload = record["payload"]
        return payload if isinstance(payload, dict) else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _handle_dual_provider_fusion_method(
    manifest: ProcessingManifest, obligation_id: str, *, extract_fused=None
) -> MethodResult:
    """Fuse Docling and MinerU for the target obligation.

    Reuse ``backend.pipeline.fusion.extract_fused`` to
    produce the fused payload, persist the JSON under ``data_dir/artifacts/
    fusion/`` and return it in ``MethodResult.artifacts``; the
    ``ExecutorAgent`` incorporates it into the manifest and records
    the attempt's artifact provenance.

    In single mode (FUSION_MODE=single) this method is not admissible and
    must not be selected by the compiler.

    Reuse previous fusion when source hash, document and settings match.
    Absent, corrupted or incompatible evidence forces a new dual extraction.
    """
    obligation = next(
        (o for o in manifest.obligations if o.id == obligation_id), None
    )
    if obligation is None:
        return MethodResult(
            success=False,
            validated=False,
            message=f"Obrigação não encontrada: {obligation_id}",
        )
    source_path = Path(manifest.source.path)
    # Reuse the document the structure extractor already stored in the Toolbox.
    artifact_id = _source_artifact_id(manifest)
    try:
        payload = _reusable_fusion_payload(manifest)
        if payload is None:
            from docstruct.policy import FusionPolicy

            policy_config = manifest.extractor.configuration.get("fusion_policy")
            policy = FusionPolicy.from_dict(policy_config) if policy_config else None
            if policy is None:
                payload = (extract_fused or _run_extract_fused)(source_path, artifact_id=artifact_id)
            else:
                payload = (extract_fused or _run_extract_fused)(source_path, artifact_id=artifact_id, policy=policy)
    except Exception as exc:  # noqa: BLE001
        return MethodResult(
            success=False,
            validated=False,
            message=f"Fusão dual-provider falhou: {type(exc).__name__}: {exc}",
        )
    if payload.get("status") != "succeeded":
        return MethodResult(
            success=False,
            validated=False,
            message="Fusão dual-provider não concluiu com sucesso",
        )

    # The planned action is dual-provider: one provider is insufficient,
    # so fail explicitly to permit replanning using another method.
    # The single-provider method remains an explicit fallback.
    provider = str(payload.get("provider") or "")
    if "+" not in provider:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Fusão dual-provider executou com um único provider "
                f"({provider or 'desconhecido'}); replanning necessário"
            ),
        )

    try:
        elements = _fused_text_elements(manifest, obligation.target_ids, payload)
    except (TypeError, ValueError) as exc:
        return MethodResult(success=False, validated=False, message=str(exc))

    artifact = _persist_fusion_payload(
        manifest,
        payload,
        obligation_id,
        kind="fusion-payload",
    )

    manifest.elements = elements
    page_ids = {element.page_number or 1 for element in elements}
    existing_pages = {page.page_number: page for page in manifest.pages}
    manifest.pages = [
        PageDescriptor(
            page_number=number,
            width=existing_pages[number].width if number in existing_pages else None,
            height=existing_pages[number].height if number in existing_pages else None,
            element_ids=[element.id for element in elements if (element.page_number or 1) == number],
        )
        for number in sorted(page_ids)
    ]
    manifest.summary.page_count = len(manifest.pages)
    manifest.summary.element_count = len(elements)
    manifest.summary.element_types = dict(Counter(element.type for element in elements))
    return MethodResult(
        success=True,
        validated=True,
        message="Fusão dual-provider concluída com sucesso",
        artifacts=[artifact],
    )


def _fused_text_elements(
    manifest: ProcessingManifest, target_ids: list[str], payload: dict[str, Any]
) -> list[ManifestElement]:
    # The fusion adapter currently emits text-only blocks. It cannot safely
    # replace a structured document or a subset with a whole-document result.
    if any(element.type != "paragraph" for element in manifest.elements) or set(target_ids) != {
        element.id for element in manifest.elements
    }:
        raise ValueError("Text fusion requires all paragraphs of a text-only manifest")
    raw_elements = payload.get("document", {}).get("elements")
    if not isinstance(raw_elements, list) or not raw_elements:
        raise ValueError("Fusion returned no usable document elements")
    originals = sorted(manifest.elements, key=lambda element: element.reading_order)
    by_page: dict[int, list[dict[str, Any]]] = {}
    for raw in raw_elements:
        if not isinstance(raw, dict) or raw.get("type") != "paragraph":
            raise ValueError("Fusion returned unsupported structured content")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Fusion returned an empty text block")
        page = raw.get("page_number", raw.get("page"))
        if page is None:
            if len(manifest.pages) > 1:
                raise ValueError("Fusion omitted page identity for a multipage document")
            page = manifest.pages[0].page_number if manifest.pages else 1
        if type(page) is not int or page < 1:
            raise ValueError("Fusion returned an invalid page number")
        by_page.setdefault(page, []).append(raw)
    original_pages = {element.page_number or 1 for element in originals}
    if not original_pages.issubset(by_page):
        raise ValueError("Fusion omitted a source page")

    result = []
    for number, rows in sorted(by_page.items()):
        old = [element for element in originals if (element.page_number or 1) == number]
        # Keep every existing ID and reference; ambiguous mappings fail closed.
        if len(rows) < len(old):
            raise ValueError("Fusion cannot safely map fewer blocks to existing element references")
        for index, raw in enumerate(rows):
            if index < len(old):
                element = old[index].model_copy(deep=True)
                element.text = raw["text"]
                element.page_number = number
            else:
                identifier = hashlib.sha256(json.dumps([
                    manifest.manifest_id, number, index, raw["text"],
                ]).encode()).hexdigest()[:24]
                element = ManifestElement(
                    id=f"element-fusion-{identifier}", type="paragraph", raw_label="paragraph",
                    reading_order=len(result) + 1, hierarchy_level=0,
                    page_number=number, text=raw["text"],
                )
            element.metadata["fusion_provider"] = payload["provider"]
            element.reading_order = len(result) + 1
            result.append(element)
    return result


def _handle_single_provider_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Satisfy ``fuse-providers`` with the extraction the manifest already carries.

    There is nothing to re-run: the manifest's elements are the configured
    provider's extraction, with the pages the deterministic router took from
    the secondary provider in dual mode (``fusion.apply_page_routes``). The
    method does not claim a single-provider document; it checks only that the
    manifest was extracted by the configured provider and has content. It used
    to extract the whole document again, uploaded anew and with the Toolbox
    cache off, once per ``fuse-providers`` step, and changed nothing in the
    manifest: a full extraction of cost for no content.
    """
    from backend.config.settings import settings

    obligation = next(
        (item for item in manifest.obligations if item.id == obligation_id), None
    )
    if obligation is None:
        return _missing_obligation(obligation_id)
    if obligation.kind != "fuse-providers":
        return MethodResult(
            success=False,
            validated=False,
            message="single-provider só é válido para fuse-providers",
        )

    provider = str(manifest.extractor.configuration.get("toolbox_provider") or "")
    if not provider:
        return MethodResult(
            success=False,
            validated=False,
            message="The manifest does not record the provider it was extracted by",
        )
    if provider != settings.toolbox_provider:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                f"The manifest was extracted by {provider}, not by the configured "
                f"provider ({settings.toolbox_provider})"
            ),
        )
    if not manifest.elements:
        return MethodResult(
            success=False,
            validated=False,
            message="The manifest extraction has no elements",
        )
    return MethodResult(
        success=True,
        validated=True,
        message=(
            f"The manifest already carries the extraction ({provider}); "
            "nothing to re-extract"
        ),
    )


def _persist_fusion_payload(
    manifest: ProcessingManifest,
    payload: dict[str, Any],
    obligation_id: str,
    *,
    kind: str,
) -> Artifact:
    """Write the payload atomically, addressed by its manifest scope and content."""
    from backend.config.settings import settings

    content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    scope = hashlib.sha256(json.dumps([
        manifest.manifest_id, manifest.source.document_id, manifest.source.sha256,
        manifest.revision, obligation_id,
    ]).encode()).hexdigest()
    output_path = settings.data_dir / "artifacts" / "fusion" / scope / f"{digest}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return Artifact(
        id=f"artifact-{kind.removesuffix('-payload')}-{scope[:16]}-{digest[:16]}",
        kind=kind,
        path=str(output_path),
        media_type="application/json",
        sha256=digest,
    )


from backend.core.manifest.preprocessing.page_limits import (
    StructuralExtractionError,
    DocumentTooLargeError,
    _check_page_count,
    _check_page_limit_before_extraction,
    _check_page_limit,
)
