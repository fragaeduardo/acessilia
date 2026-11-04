"""Toolbox-backed verified region and whole-page inspection handlers.

The original checked pixels, source digests, and metadata evidence are kept.
No local render fallback. Callers may inject render/model/artifact hooks for
tests, but no alternate capability or schema is created.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agents.output_schemas import PageInspectionOutput, VisionOutput
from backend.agents.vision_agent import VisionAgent
from backend.core.execution.handlers.common import _missing_obligation, _target_elements
from backend.core.execution.models import MethodResult
from backend.core.manifest.builder import is_unstructured_page_placeholder
from backend.core.manifest.models import Artifact, ManifestElement, ProcessingManifest
from backend.core.manifest.preprocessing.render_regions import _source_artifact_id
from backend.tools.logger import logger

if TYPE_CHECKING:
    from acessilia_sdk.toolbox.clients.pdf import ClipOrigin, RenderedPage


def _render_inspection_region(
    source: Path,
    element: ManifestElement,
    *,
    media_type: str | None = None,
    artifact_id: str | None = None,
    render_toolbox=None,
) -> tuple[bytes, int, list[float]]:
    """Render only the requested, validated source region on the Toolbox.

    Crop metadata from the extractor is never trusted: the page identity and
    the box are checked here, and the Toolbox renders exactly that box and
    reports the region it applied. The page range is the document's, as the
    Toolbox reports it, like every other render: a page beyond it is refused
    there. An element without a page is accepted only in a one-page document.
    """
    provenance = element.provenance[0] if element.provenance else None
    number = provenance.page_number if provenance else element.page_number
    if provenance and element.page_number is not None and number != element.page_number:
        raise ValueError("Region page identity is inconsistent")
    if number is not None and (type(number) is not int or number < 1):
        raise ValueError("Region page is missing or out of range")
    clip: tuple[float, float, float, float] | None = None
    origin: ClipOrigin = "TOPLEFT"
    bbox = provenance.bbox if provenance else None
    if bbox is not None:
        values = (bbox.left, bbox.top, bbox.right, bbox.bottom)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Region coordinates must be finite")
        # Unlike ordering, a crop certifies what the vision model describes,
        # so it requires an explicit origin instead of inferring one.
        if bbox.coord_origin not in {"TOPLEFT", "BOTTOMLEFT"}:
            raise ValueError("Region coordinate origin is unknown")
        clip, origin = values, bbox.coord_origin
    page = (render_toolbox or _toolbox_render)(
        source, number or 1, media_type=media_type, dpi=160, clip=clip,
        clip_origin=origin, artifact_id=artifact_id,
    )
    if number is None and page.page_count != 1:
        raise ValueError("Region page is missing or out of range")
    return page.image, number or 1, list(page.clip)

def _inspect_region(image: bytes, number: int, total_pages: int, mode: str, *, vision_cls=None) -> VisionOutput | None:
    async def inspect():
        return await (vision_cls or VisionAgent)(mode=mode).describe_region(
            image_bytes=image, classification="embedded_image", page_num=number,
            total_pages=total_pages, mode=mode, full_budget=True,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(inspect())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(inspect())).result()


def _save_region_artifact(content: bytes, scope: str, kind: str, suffix: str, media_type: str) -> Artifact:
    from backend.config.settings import settings

    digest = hashlib.sha256(content).hexdigest()
    path = settings.data_dir / "artifacts" / "regions" / scope / f"{digest}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return Artifact(
        id=f"artifact-{kind}-{scope[:16]}-{digest[:16]}", kind=kind,
        path=str(path), media_type=media_type, sha256=digest,
    )


def _handle_pymupdf_region_method(manifest: ProcessingManifest, obligation_id: str, *, render_region=None, inspect_region=None, save_artifact=None, source_artifact_id=None) -> MethodResult:
    """Inspect real source pixels and update targets only after every result validates."""
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    try:
        source = Path(manifest.source.path)
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest.source.sha256:
                raise ValueError("Region source differs from the manifest")
        inspected = []
        for element in elements:
            if element.type != "unknown":
                raise ValueError("Region inspection requires an unknown target element")
            image, number, rectangle = (render_region or _render_inspection_region)(
                source,
                element,
                media_type=manifest.source.media_type,
                artifact_id=(source_artifact_id or _source_artifact_id)(manifest),
            )
            declared_pages = {
                page.page_number for page in manifest.pages if element.id in page.element_ids
            }
            if declared_pages and declared_pages != {number}:
                raise ValueError("Region page differs from the manifest page index")
            output = (inspect_region or _inspect_region)(
                image, number, max(len(manifest.pages), number),
                str(manifest.extractor.configuration.get("region_inspection_mode", "medio")),
            )
            if not isinstance(output, VisionOutput):
                raise ValueError(f"Region inspection failed for {element.id}")
            output = VisionOutput.model_validate(output.model_dump())
            text = output.description if output.kind == "description" else normalize_latex(output.formula_latex or "")
            if not text.strip():
                raise ValueError(f"Region inspection returned empty content for {element.id}")
            inspected.append((element, image, number, rectangle, output, text))

        artifacts = []
        replacements = {}
        for element, image, number, rectangle, output, text in inspected:
            scope = hashlib.sha256(json.dumps([
                manifest.manifest_id, manifest.source.sha256, manifest.revision, obligation_id, element.id,
            ]).encode()).hexdigest()
            crop = (save_artifact or _save_region_artifact)(image, scope, "region-crop", "png", "image/png")
            evidence = {
                "source_sha256": manifest.source.sha256, "element_id": element.id,
                "page_number": number, "bbox": rectangle, "crop_sha256": crop.sha256,
                "result": output.model_dump(),
            }
            record = (save_artifact or _save_region_artifact)(
                json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode(),
                scope, "region-inspection", "json", "application/json",
            )
            replacement = element.model_copy(deep=True)
            replacement.metadata.update(
                original_type=element.type, vision_description_complete=True,
                vision_language=output.language, vision_confidence=output.confidence,
                vision_warnings=output.warnings, region_crop=crop.path,
                region_inspection=record.id,
            )
            replacement.text = text
            replacement.type = "formula" if output.kind == "formula" else "picture"
            if replacement.type == "picture":
                replacement.metadata["src"] = (
                    "data:image/png;base64," + base64.b64encode(image).decode("ascii")
                )
            replacement.raw_label = replacement.type
            replacement.page_number = number
            replacements[element.id] = replacement
            artifacts.extend([crop, record])
        manifest.elements = [replacements.get(element.id, element) for element in manifest.elements]
        manifest.summary.element_types = dict(Counter(element.type for element in manifest.elements))
        return MethodResult(
            success=True, validated=True,
            message=f"Inspected {len(elements)} source region(s)", artifacts=artifacts,
        )
    except Exception as exc:
        return MethodResult(
            success=False, validated=False,
            message=f"Region inspection failed: {type(exc).__name__}: {exc}",
        )


#: Inputs ``pdf.render@2`` opens. The pipeline also accepts TIFF, BMP, GIF and
#: WebP; the Toolbox does not render those, and there is no local fallback.
from backend.core.manifest.preprocessing.render_regions import _RENDERABLE_MEDIA_TYPES


from backend.core.manifest.preprocessing.render_regions import _source_artifact_id


def _toolbox_render(
    source: Path,
    page_number: int,
    *,
    media_type: str | None = None,
    dpi: int,
    max_side: int | None = None,
    clip: tuple[float, float, float, float] | None = None,
    clip_origin: ClipOrigin = "TOPLEFT",
    artifact_id: str | None = None,
) -> RenderedPage:
    """Render a page (or a region of it) on the Toolbox with ``pdf.render@2``.

    There is no local rendering (docs/adr/0003): the Toolbox converts, clips,
    bounds and refuses oversized renders, and reports what it applied. With
    ``artifact_id`` the source already stored by the extractor is referenced
    instead of uploaded; the file is sent only if the Toolbox no longer has it.

    The upload is labelled from the file extension; ``media_type`` (the
    manifest's) must agree with that label. Inputs outside
    ``_RENDERABLE_MEDIA_TYPES`` are refused here with a ``ValueError``, before
    any request, so the method fails with that reason and not a Toolbox error.
    """
    from acessilia_sdk.toolbox.clients import ToolboxPdfClient
    from acessilia_sdk.toolbox.transport import media_type_for

    from backend.tools.toolbox_client import make_toolbox_client, run_sync

    sent_as = media_type_for(source)
    if media_type is not None and media_type != sent_as:
        raise ValueError(
            f"Source is {media_type} in the manifest but would be sent as {sent_as}"
        )
    if sent_as not in _RENDERABLE_MEDIA_TYPES:
        raise ValueError(
            f"pdf.render@2 renders PDF, PNG and JPEG inputs, not {sent_as}"
        )

    async def render():
        client = make_toolbox_client(
            ToolboxPdfClient, resend_file_on_missing_artifact=artifact_id is not None
        )
        try:
            return await client.render(
                file_path=source,
                artifact_id=artifact_id,
                page_number=page_number,
                dpi=dpi,
                max_side=max_side,
                clip=clip,
                clip_origin=clip_origin,
            )
        finally:
            await client.close()

    return run_sync(render())


def _render_source_page(
    source: Path, media_type: str, number: int, artifact_id: str | None = None, *, render_toolbox=None
) -> bytes:
    """The PNG of page ``number`` of a PDF, PNG or JPEG input, rendered by the Toolbox."""
    from backend.config.settings import settings

    return (render_toolbox or _toolbox_render)(
        source, number, media_type=media_type, dpi=settings.pdf_split_dpi,
        artifact_id=artifact_id,
    ).image

def _inspect_page(image: bytes, number: int, total_pages: int, mode: str, *, vision_cls=None) -> PageInspectionOutput:
    from backend.tools.toolbox_client import run_sync

    return run_sync(
        (vision_cls or VisionAgent)(mode=mode).inspect_page(
            image_bytes=image, page_num=number, total_pages=total_pages
        )
    )


def _handle_vision_page_inspection_method(
    manifest: ProcessingManifest, obligation_id: str,
    *, render_source_page=None, inspect_page=None, save_artifact=None,
    source_artifact_id=None,
) -> MethodResult:
    """Look at a page the extractor left empty: describe it, or accept it as blank.

    The page is rendered by the Toolbox and judged by the vision model. A page
    with content replaces its placeholder with a ``picture`` that carries the
    page image and its accessible description; a blank page keeps the
    placeholder, marked ``blank_page`` so the canonical document skips it. The
    manifest changes only after every target page validates. Any failure (the
    Toolbox, the model, an invalid answer) fails the method, so a page is never
    dropped silently; the obligation is critical.
    """
    elements = [
        element
        for element in _target_elements(manifest, obligation_id)
        if is_unstructured_page_placeholder(element)
    ]
    if not elements:
        return _missing_obligation(obligation_id)
    try:
        source = Path(manifest.source.path)
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest.source.sha256:
                raise ValueError("Page source differs from the manifest")
        total_pages = max(len(manifest.pages), 1)
        mode = str(manifest.extractor.configuration.get("region_inspection_mode", "medio"))

        inspected = []
        for element in elements:
            number = element.page_number or 1
            image = (render_source_page or _render_source_page)(
                source, manifest.source.media_type, number, (source_artifact_id or _source_artifact_id)(manifest)
            )
            output = (inspect_page or _inspect_page)(image, number, total_pages, mode)
            if not isinstance(output, PageInspectionOutput):
                raise TypeError("Page inspection returned no structured output")
            inspected.append((element, number, image, output))

        artifacts: list[Artifact] = []
        replacements: dict[str, ManifestElement] = {}
        blank = 0
        for element, number, image, output in inspected:
            scope = hashlib.sha256(json.dumps([
                manifest.manifest_id, manifest.source.sha256, manifest.revision,
                obligation_id, element.id,
            ]).encode()).hexdigest()
            page_image = (save_artifact or _save_region_artifact)(image, scope, "page-render", "png", "image/png")
            record = _save_region_artifact(
                json.dumps(
                    {
                        "source_sha256": manifest.source.sha256,
                        "element_id": element.id,
                        "page_number": number,
                        "page_image_sha256": page_image.sha256,
                        "result": output.model_dump(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode(),
                scope, "page-inspection", "json", "application/json",
            )
            artifacts.extend([page_image, record])
            replacement = element.model_copy(deep=True)
            replacement.metadata.update(
                page_inspection=record.id,
                vision_language=output.language,
                vision_confidence=output.confidence,
                vision_warnings=list(output.warnings),
            )
            if output.kind == "blank":
                replacement.metadata["blank_page"] = True
                blank += 1
            else:
                replacement.type = "picture"
                replacement.raw_label = "picture"
                replacement.text = output.description
                replacement.metadata.update(
                    original_type=element.type,
                    vision_description_complete=True,
                    src="data:image/png;base64," + base64.b64encode(image).decode("ascii"),
                )
            replacements[element.id] = replacement

        manifest.elements = [replacements.get(item.id, item) for item in manifest.elements]
        manifest.summary.element_types = dict(Counter(item.type for item in manifest.elements))
        return MethodResult(
            success=True,
            validated=True,
            message=(
                f"Inspected {len(inspected)} unstructured page(s): "
                f"{len(inspected) - blank} described, {blank} blank"
            ),
            artifacts=artifacts,
        )
    except Exception as exc:
        return MethodResult(
            success=False,
            validated=False,
            message=f"Page inspection failed: {type(exc).__name__}: {exc}",
        )
