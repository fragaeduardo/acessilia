from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from docstruct.fusion.tailjoin import is_page_number as _is_page_number
from docstruct.tables.ast import (
    raw_rows_from_table_ast as _rows_from_table_ast,
)
from docstruct.tables.garble import table_garble_reason
from docstruct.text.equations import preserve_equation_number, split_equation_number
from docstruct.text.inline_math import (
    InlineMathDetection,
    TextSpan,
    assign_runs_to_cells,
    detect_inline_math,
    splice_inline_math,
)
from docstruct.text.latex import has_latex_markup, latex_text_agreement
from docstruct.text.math_spans import mask_inline_math
from docstruct.text.negation_overlay import (
    compose_negation,
    is_detached_overlay,
    locate_in_grid,
    locate_in_text,
    negates,
)
from docstruct.text.unicode_repair import (
    Detection,
    UnicodeRepairContext,
    detect_legacy_unicode,
    repair_legacy_unicode,
)

from backend.services.blocking import run_blocking
from backend.agents.data_agent import DataAgent
from backend.agents.output_schemas import DataOutput, PageInspectionOutput, VisionOutput
from backend.agents.provider_breaker import ProviderBreaker
from backend.agents.vision_agent import VisionAgent
from backend.core.agents.fusion_agent import FusionAgent
from backend.core.agents.informational_structural import InformationalStructuralAgent
from backend.core.execution.executor import ExecutorAgent, MethodRegistry, StepProgress
from backend.core.execution.models import ExecutionReport, MethodResult
from backend.core.manifest.builder import (
    SECONDARY_EXTRACTION_KIND,
    TYPE_BOUND_V23_KINDS,
    is_unstructured_page_placeholder,
    reconcile_reclassified_element_processing_needs,
    separate_equation_number,
    unicode_repair_text,
)
from backend.core.manifest.models import (
    Artifact,
    ManifestElement,
    PageDescriptor,
    ProcessingManifest,
    is_bottom_left,
)
from backend.core.manifest.toolbox_extractor import ToolboxManifestExtractor
from backend.core.planning.domain_bundle import DomainBundle
from backend.core.planning.models import NominalPlan, PlanningComparison, PlanStep
from backend.core.planning import toolbox_gate
from backend.core.planning.planner_agent import PlannerAgent
from backend.core.planning.coordinator import build_nominal_plan, unavailable_methods
from backend.core.orchestration.pipeline import PipelineHooks, execute_pipeline
from backend.i18n import n, t
from backend.log_messages import (
    LOG_PDDL_ELEMENT_CROP_FAILED,
    LOG_PDDL_EMPTY_VISION_DESCRIPTION,
    LOG_PDDL_FORMULA_RECOGNIZE_EMPTY,
    LOG_PDDL_FORMULA_RECOGNIZE_UNAVAILABLE,
    LOG_PDDL_FORMULA_TEXT_LAYER_DISAGREES,
    LOG_PDDL_FORMULAS_FELL_BACK_TO_VISION,
    LOG_PDDL_FORMULAS_RECOGNIZED,
    LOG_PDDL_IMAGES_ENRICHED,
    LOG_PDDL_TABLES_ENRICHED,
    LOG_PDDL_VISION_RETRY_PASS_PICTURES,
    LOG_PDDL_VISION_RETRY_PASS_TABLES,
    LOG_PDDL_TABLE_REREAD_ACCEPTED,
    LOG_PDDL_TABLE_REREAD_REJECTED,
    LOG_VISION_ELEMENT_DESCRIPTION,
)
from backend.stage_messages import (
    STAGE_ANALYZING_STRUCTURAL,
    STAGE_STRUCTURAL_EXTRACTION_FAILED,
    STAGE_DOCUMENT_TOO_MANY_PAGES,
    STAGE_ENRICHING_FORMULAS,
    STAGE_ENRICHING_IMAGE_DESCRIPTIONS,
    STAGE_ENRICHING_TABLES_OCR,
    STAGE_GENERATING_PDDL_PLAN,
    STAGE_VALIDATING_PLAN_DRY_RUN,
    STAGE_EXECUTING_PLAN,
    STAGE_DESCRIBING_IMAGE,
    STAGE_RECOGNIZING_FORMULA,
    STAGE_RECOGNIZING_TABLE,
    STAGE_EXECUTING_PLAN_STEP,
)
from backend.tools.formula_tools import normalize_latex
from backend.tools.logger import logger
from backend.tools.toolbox_math_tools import (
    MathCapabilityUnavailable,
    toolbox_recognize_formula,
)
from backend.tools.toolbox_client import ToolboxError

if TYPE_CHECKING:
    from acessilia_sdk.toolbox.clients.pdf import ClipOrigin, RenderedPage

# Compatibility API: canonical payload implementation lives in manifest.payload.
from backend.core.manifest.payload import (
    MSG_UNDESCRIBED_PICTURE_ALT,
    MSG_LETTERHEAD_TEXT,
    MSG_PICTURES_NOT_DESCRIBED,
    MSG_TABLES_NOT_READ,
    _visual_failures,
    records_visual_failures,
    records_extraction_degradation,
    _unsatisfied_obligations,
    _is_left_out_of_payload,
    _is_decorated_page_number,
    _is_marked_furniture,
    _is_page_number_in_margin,
    _page_number_ids,
    _is_page_header_line,
    _is_beside,
    _letterhead_groups,
    _letterhead_block,
    _manifest_pages_to_payload,
    _elements_to_blocks,
    _recover_empty_lettered_list_items,
    _callout_titles,
    _merge_list_blocks,
    _text_kept_by_picture,
    _element_to_block,
    _normalize_metadata_links,
    _render_text_from_blocks,
    _render_text_from_pages,
    _RETRYABLE_VISUAL_FAILURES,
    _PAGE_NUMBER_TYPES,
    _PAGE_NUMBER_MARGIN,
    _PAGE_NUMBER_MAX_LEN,
    _LETTERHEAD_TOLERANCE,
    _LETTERHEAD_MAX_GAP,
    _LETTERHEAD_MAX_HEIGHT,
    _LETTERED_LIST_SOURCE_MARKER,
)


# Compatibility: recovery policy is now owned by core.execution.recovery.
from backend.core.execution.recovery import (
    _formula_elements_for_obligation,
    _formula_has_readable_content,
    _blocks_delivery,
    _is_open_obligation,
    _unsatisfied_selected_obligations,
    _replannable_obligation_ids,
    _unsatisfied_summary,
    _pages_label,
    _obligation_pages,
    NON_CRITICAL_OBLIGATION_KINDS,
)


# Math execution handlers live under core.execution.handlers.
from backend.core.execution.handlers.mathematics import _implicit_product_variables
from backend.core.execution.handlers import mathematics as _math_handlers
from backend.core.manifest import payload as _payload_adapter
from backend.core.manifest.preprocessing import media_triage as _media_triage
from backend.tools.media_inspect import inspect_source_media
from backend.tools.toolbox_math_tools import toolbox_convert_latex, toolbox_verbalize_latex


def _handle_mathml_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Compatibility delegate: preserve explicit Toolbox test overrides."""
    return _math_handlers._handle_mathml_method(
        manifest, obligation_id, convert_latex=toolbox_convert_latex
    )


def _handle_latex_verbalizer_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Compatibility delegate: preserve explicit Toolbox test overrides."""
    return _math_handlers._handle_latex_verbalizer_method(
        manifest, obligation_id, verbalize_latex=toolbox_verbalize_latex
    )


async def _inspect_media(
    source_path: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Compatibility delegate preserving the injectable media inspector."""
    return await _media_triage._inspect_media(
        source_path, inspect_fn=inspect_source_media
    )


def build_pddl_structured_payload(
    *,
    file_path: Path,
    manifest: ProcessingManifest,
    plan: NominalPlan,
    planner_backend: str,
    execution_report: ExecutionReport | None,
    comparison: PlanningComparison | None,
    toolbox_availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep the legacy page-builder override without mutable global patching."""
    return _payload_adapter.build_pddl_structured_payload(
        file_path=file_path,
        manifest=manifest,
        plan=plan,
        planner_backend=planner_backend,
        execution_report=execution_report,
        comparison=comparison,
        toolbox_availability=toolbox_availability,
        pages_builder=_manifest_pages_to_payload,
    )


def _run_extract_fused(
    source_path: Path, *, artifact_id: str | None = None, policy=None
) -> dict:
    """Executa ``extract_fused`` respeitando o contexto de event loop.

    Detecta a presença de um event loop **antes** de criar a coroutine,
    evitando criar um objeto coroutine que ficaria sem ``await`` (e
    produziria ``RuntimeWarning: coroutine ... was never awaited``).

    Nota de arquitetura: quando chamado a partir de um thread com event
    loop ativo (Agno Workflow, API), ``.result()`` ainda bloqueia o loop
    chamador até a fusão terminar. O contrato dos handlers permanece
    síncrono; quem invoca o executor a partir de código async deve rodar
    o executor inteiro via ``await run_blocking(...)`` para não
    bloquear o event loop principal.
    """
    import concurrent.futures

    from backend.pipeline.fusion import extract_fused

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Contexto síncrono (executor determinístico, CLI, testes):
        # não existe event loop ativo neste thread.
        return asyncio.run(extract_fused(source_path, artifact_id=artifact_id, policy=policy))

    # Já existe um event loop neste thread; como o handler é síncrono,
    # a coroutine é executada em um thread dedicado com seu próprio loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(
                extract_fused(source_path, artifact_id=artifact_id, policy=policy)
            )
        ).result()


def _fusion_agent() -> FusionAgent:
    """Fusion agent used to reach the deterministic fusion primitives.

    The `docstruct` functions take classified blocks, counters and tuning
    parameters; `FusionAgent.process_*` is the in-repo entry point that supplies
    them, so handlers call that instead of a primitive whose signature they would
    otherwise have to guess.
    """
    return FusionAgent()


from backend.core.manifest.element_geometry import _element_bbox, _element_role


# Legacy names; handler lookup is now shared by execution handlers.
from backend.core.execution.handlers.common import _target_elements, _missing_obligation


from backend.core.execution.handlers.vision_description import _handle_vision_description_method


# Unicode repair and layout composition belong to the execution module.
from backend.core.execution.handlers.unicode_repair import (
    _repair_table_cells,
    _vertical,
    _table_grid,
    _reattach_negation_overlays,
    _compose_in_host,
    _handle_unicode_repair_method,
    _UNICODE_REPAIR_BLOCK_TYPES,
    _UNICODE_STANDALONE_SPACING_MIN,
    _OVERLAY_LINE_HEIGHTS,
    _TABLE_SECTIONS,
    _OVERLAY_HOST_TOLERANCE,
    _bbox,
)


def _render_inspection_region(
    source: Path,
    element: ManifestElement,
    *,
    media_type: str | None = None,
    artifact_id: str | None = None,
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
    page = _toolbox_render(
        source, number or 1, media_type=media_type, dpi=160, clip=clip,
        clip_origin=origin, artifact_id=artifact_id,
    )
    if number is None and page.page_count != 1:
        raise ValueError("Region page is missing or out of range")
    return page.image, number or 1, list(page.clip)

def _inspect_region(image: bytes, number: int, total_pages: int, mode: str) -> VisionOutput | None:
    async def inspect():
        return await VisionAgent(mode=mode).describe_region(
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


def _handle_pymupdf_region_method(manifest: ProcessingManifest, obligation_id: str) -> MethodResult:
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
            image, number, rectangle = _render_inspection_region(
                source,
                element,
                media_type=manifest.source.media_type,
                artifact_id=_source_artifact_id(manifest),
            )
            declared_pages = {
                page.page_number for page in manifest.pages if element.id in page.element_ids
            }
            if declared_pages and declared_pages != {number}:
                raise ValueError("Region page differs from the manifest page index")
            output = _inspect_region(
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
            crop = _save_region_artifact(image, scope, "region-crop", "png", "image/png")
            evidence = {
                "source_sha256": manifest.source.sha256, "element_id": element.id,
                "page_number": number, "bbox": rectangle, "crop_sha256": crop.sha256,
                "result": output.model_dump(),
            }
            record = _save_region_artifact(
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
    source: Path, media_type: str, number: int, artifact_id: str | None = None
) -> bytes:
    """The PNG of page ``number`` of a PDF, PNG or JPEG input, rendered by the Toolbox."""
    from backend.config.settings import settings

    return _toolbox_render(
        source, number, media_type=media_type, dpi=settings.pdf_split_dpi,
        artifact_id=artifact_id,
    ).image

def _inspect_page(image: bytes, number: int, total_pages: int, mode: str) -> PageInspectionOutput:
    from backend.tools.toolbox_client import run_sync

    return run_sync(
        VisionAgent(mode=mode).inspect_page(
            image_bytes=image, page_num=number, total_pages=total_pages
        )
    )


def _handle_vision_page_inspection_method(
    manifest: ProcessingManifest, obligation_id: str
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
            image = _render_source_page(
                source, manifest.source.media_type, number, _source_artifact_id(manifest)
            )
            output = _inspect_page(image, number, total_pages, mode)
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
            page_image = _save_region_artifact(image, scope, "page-render", "png", "image/png")
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


def _handle_repetition_guard_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Remove repeated token loops from the obligation's current page text."""
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    fusion_agent = _fusion_agent()
    cut_total = 0
    for element in elements:
        result = fusion_agent.process_guard_repetition(element.text or "")
        cut = int(result["tokens_cut"])
        if not cut:
            continue
        element.text = result["text"]
        element.metadata["repetition_guard"] = {"tokens_cut": cut}
        cut_total += cut

    if not cut_total:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma repetição degenerada encontrada nos alvos",
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"Guarda de repetição removeu {cut_total} token(s)",
    )


def _element_blocks(
    elements: list[ManifestElement],
    manifest: ProcessingManifest,
) -> list[dict[str, Any]]:
    """Manifest elements as the fusion stack's block dicts.

    The deterministic primitives in `docstruct` operate on classified blocks, not
    on bare strings, so the elements are adapted once here instead of each handler
    guessing at a signature.
    """
    blocks: list[dict[str, Any]] = []
    for element in elements:
        bbox = _element_bbox(element, manifest)
        blocks.append(
            {
                "md": element.text or "",
                "text": element.text or "",
                "kind": _block_kind(element),
                "type": element.type,
                "role": _element_role(element),
                "box": list(bbox) if bbox else None,
            }
        )
    return blocks


def _block_kind(element: ManifestElement) -> str:
    """Fusion block kind for an element type (`DiffBlock.kind` vocabulary)."""
    if element.type == "heading":
        return "heading"
    if element.type == "table":
        return "table"
    if element.type == "formula":
        return "formula"
    return "text"


def _handle_extract_line_numbers_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Pull marginal verse line numbers out of the target blocks.

    Uses `FusionAgent.process_extract_line_numbers`, which is the supported entry
    point: `docstruct.fusion.line_numbers.extract_line_numbers` needs a block list
    plus a stats counter and a provenance tag, so calling it with a plain string
    raised `TypeError` on every step.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    result = _fusion_agent().process_extract_line_numbers(
        _element_blocks(elements, manifest)
    )
    extracted = int(result.get("stats", {}).get("line-numbers-agent-count", 0))
    if not extracted:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum número de linha marginal encontrado",
        )
    for element in elements:
        element.metadata["line_numbers_extracted"] = extracted
    return MethodResult(
        success=True,
        validated=True,
        message=f"{extracted} número(s) de linha extraído(s)",
    )


def _handle_chunk_verse_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Split verse/record blocks into chunks the evaluator can re-group.

    `docstruct.fusion.verse.chunk_verse` takes already-split lines plus a character
    budget, so the handler goes through `FusionAgent.process_chunk_verse`, which
    detects the verse lines itself.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    result = _fusion_agent().process_chunk_verse(_element_blocks(elements, manifest))
    # `verse_blocks` reports `verse-{tag}-{whole,kept,split,chunks}`; only the
    # blocks it actually split are evidence that the method did anything.
    chunks = result.get("stats", {}).get("verse-agent-split", 0)
    if not chunks:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum bloco de verso elegível para fragmentação",
        )
    for element in elements:
        element.metadata["verse_chunks"] = chunks
    return MethodResult(
        success=True,
        validated=True,
        message=f"Verso fragmentado em {chunks} pedaço(s)",
    )


def _tailjoin_position(manifest: ProcessingManifest) -> str | None:
    """Vertical position ("top"/"bot") of the page number in the manifest.

    `join_tail` only applies the position-conditioned `tj3` rule when it knows
    where the page number sits, which is the distinction between `tailjoin` and
    `tailjoin-positioned`.
    """
    for element in manifest.elements:
        role = _element_role(element)
        if role in {"header", "footer", "page_number"} and _is_page_number(
            element.text or ""
        ):
            bbox = _element_bbox(element, manifest)
            if bbox is None:
                continue
            return "top" if (bbox[1] + bbox[3]) / 2 < 0.5 else "bot"
    return None


def _handle_tailjoin_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Join the trailing short blocks (header/footer/page number).

    `docstruct.fusion.tailjoin.join_tail` requires the tail mode, a maximum short
    block length and the number of blocks to consider, so it cannot be called with
    the text alone. `FusionAgent.process_join_tail` supplies those defaults.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    texts = [(element.text or "") for element in elements]
    merged = _fusion_agent().process_join_tail(
        texts, position=_tailjoin_position(manifest)
    )
    if len(merged) >= len(texts):
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum bloco de cauda elegível para união",
        )
    for element in elements:
        element.metadata["tailjoined"] = True
    return MethodResult(
        success=True,
        validated=True,
        message=(
            f"Blocos de cauda unidos: {len(texts)} -> {len(merged)} bloco(s)"
        ),
    )


# --- Methods that choose between the two providers of FUSION_MODE=dual -------
#
# table-smart-fusion, skeleton-order and agreement-rule port Dr.DocBench rules
# (tree_differ_v2.py of the drbench-2026-snapshot tag and
# docstruct.fusion.adjudication.decide_v1). They read the secondary provider's
# extraction that the structure extractor keeps as a manifest artifact. The
# rules were calibrated on the docling + mineru pair, so any other pair fails
# explicitly instead of applying a preference nobody measured.

_CALIBRATED_PAIR = ("docling", "mineru")
#: Accepted Dr.DocBench table preference (TEDS): MinerU's table, unless Docling's
#: has spans or is the only one.
_TABLE_PREFERENCE = "mineru"
#: Minimum intersection over union to pair two blocks by position.
_MIN_BLOCK_IOU = 0.3
#: Minimum text similarity to pair two blocks that could not be paired by position.
_MIN_BLOCK_TEXT_RATIO = 0.8
#: Share of a page's elements that must be paired to follow the skeleton order.
_MIN_SKELETON_MATCH = 0.5

_Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class _SecondaryExtraction:
    provider: str
    #: page number -> raw elements of the secondary provider, in reading order.
    pages: dict[int, list[dict[str, Any]]]
    sizes: dict[int, tuple[float, float]]
    #: page number -> primary elements of a page the manifest took from the
    #: secondary provider (fusion.apply_page_routes), in reading order.
    replaced_primary: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    def bbox(self, element: dict[str, Any]) -> _Box | None:
        """Normalized box of a raw Toolbox element (same frame as _element_bbox)."""
        for item in element.get("provenance") or []:
            if not isinstance(item, dict) or not item.get("bbox"):
                continue
            box = item["bbox"]
            page = _page_number(item.get("page_number", element.get("page_number")))
            width, height = self.sizes.get(page, (0.0, 0.0)) if page else (0.0, 0.0)
            if not width or not height:
                return None
            try:
                left, right = float(box["left"]), float(box["right"])
                top, bottom = float(box["top"]), float(box["bottom"])
            except (KeyError, TypeError, ValueError):
                return None
            if is_bottom_left(box.get("coord_origin"), top, bottom):
                top, bottom = height - top, height - bottom
            return _ordered_box(
                left / width, top / height, right / width, bottom / height
            )
        return None


def _page_number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_secondary_extraction(
    manifest: ProcessingManifest,
) -> tuple[_SecondaryExtraction | None, str]:
    """The secondary provider's extraction, if recorded and unchanged.

    When it cannot be used, the second item says why (missing, replaced or
    unreadable), so the method result can tell those cases apart.
    """
    import json

    artifact = next(
        (a for a in manifest.artifacts if a.kind == SECONDARY_EXTRACTION_KIND), None
    )
    if artifact is None:
        return None, "Extração do provider secundário ausente do manifesto"
    try:
        raw = Path(artifact.path).read_bytes()
    except OSError:
        return None, f"Extração do provider secundário indisponível: {artifact.path}"
    if artifact.sha256 and hashlib.sha256(raw).hexdigest() != artifact.sha256:
        logger.warning(
            "Extração secundária {} não confere com o sha256 registrado",
            artifact.path,
        )
        return None, (
            "Extração do provider secundário alterada: sha256 diverge do "
            f"registrado no manifesto ({artifact.path})"
        )
    unreadable = f"Extração do provider secundário ilegível: {artifact.path}"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, unreadable
    payload = data.get("document") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None, unreadable
    document = payload.get("document", payload)
    sizes: dict[int, tuple[float, float]] = {}
    for page in document.get("pages") or []:
        try:
            sizes[int(page["page_number"])] = (
                float(page.get("width") or 0),
                float(page.get("height") or 0),
            )
        except (KeyError, TypeError, ValueError):
            continue
    pages: dict[int, list[dict[str, Any]]] = {}
    for element in sorted(
        (e for e in document.get("elements") or [] if isinstance(e, dict)),
        key=lambda e: e.get("reading_order") or 0,
    ):
        page = _page_number(element.get("page_number"))
        if page is not None:
            pages.setdefault(page, []).append(element)
    replaced: dict[int, list[dict[str, Any]]] = {}
    for key, rows in (data.get("replaced_primary_pages") or {}).items():
        page = _page_number(key)
        if page is not None and isinstance(rows, list):
            replaced[page] = sorted(
                (row for row in rows if isinstance(row, dict)),
                key=lambda row: row.get("reading_order") or 0,
            )
    return (
        _SecondaryExtraction(str(data.get("provider") or ""), pages, sizes, replaced),
        "",
    )


def _provider_pair_problem(
    manifest: ProcessingManifest, secondary: _SecondaryExtraction
) -> str | None:
    primary = str((manifest.extractor.configuration or {}).get("toolbox_provider") or "")
    if (primary, secondary.provider) == _CALIBRATED_PAIR:
        return None
    return (
        f"Regra calibrada para {'+'.join(_CALIBRATED_PAIR)}; "
        f"par atual: {primary or '?'}+{secondary.provider or '?'}"
    )


def _secondary_or_failure(
    manifest: ProcessingManifest,
) -> tuple[_SecondaryExtraction | None, MethodResult | None]:
    secondary, problem = _load_secondary_extraction(manifest)
    if secondary is None:
        return None, MethodResult(success=False, validated=False, message=problem)
    problem = _provider_pair_problem(manifest, secondary)
    if problem:
        return None, MethodResult(success=False, validated=False, message=problem)
    return secondary, None


def _ordered_box(left: float, top: float, right: float, bottom: float) -> _Box:
    return (min(left, right), min(top, bottom), max(left, right), max(top, bottom))


def _iou(a: _Box, b: _Box) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _pair_block(
    element: ManifestElement,
    manifest: ProcessingManifest,
    candidates: list[dict[str, Any]],
    secondary: _SecondaryExtraction,
    used: set[int],
) -> int | None:
    """Index in ``candidates`` of the block that matches ``element``.

    Greedy: each element takes its best free candidate in the current reading
    order, while Dr.DocBench uses a global (Hungarian) assignment. The two can
    differ only when blocks compete for the same candidate, which is rare above
    the IoU / text thresholds and does not move the skeleton's 0.5 page gate.
    """
    import difflib

    box = _element_bbox(element, manifest)
    if box is not None:
        box = _ordered_box(*box)
        scored = [
            (_iou(box, other), index)
            for index, candidate in enumerate(candidates)
            if index not in used and (other := secondary.bbox(candidate)) is not None
        ]
        best = max(scored, default=(0.0, None))
        if best[0] >= _MIN_BLOCK_IOU:
            return best[1]
    text = " ".join((element.text or "").split())
    if not text:
        return None
    scored_text = [
        (
            difflib.SequenceMatcher(
                None,
                text,
                " ".join(str(candidate["text"]).split()),
                autojunk=False,
            ).ratio(),
            index,
        )
        for index, candidate in enumerate(candidates)
        if index not in used and candidate.get("text")
    ]
    best_text = max(scored_text, default=(0.0, None))
    return best_text[1] if best_text[0] >= _MIN_BLOCK_TEXT_RATIO else None


def _handle_table_smart_fusion_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Pick each table's source between the two providers (Dr.DocBench rule).

    Docling's table is kept when it has spans (MinerU flattens them) or when it
    is the only one; otherwise MinerU's table is adopted. Tables are paired by
    position or text; a page with a single table on each side pairs them only
    when there is no position to compare. The choice and its reason are recorded
    in ``table_source`` / ``table_source_reason``.
    """
    from docstruct.tables.ast import analyze_table_complexity

    tables = [
        element
        for element in _target_elements(manifest, obligation_id)
        if element.type == "table"
    ]
    if not tables:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    primary_name, secondary_name = _CALIBRATED_PAIR
    primary_tables_by_page = Counter(
        element.page_number
        for element in manifest.elements
        if element.type == "table"
    )
    reasons: Counter = Counter()
    for element in tables:
        primary_ast = (element.metadata or {}).get("table_ast")
        candidates = [
            block
            for block in secondary.pages.get(element.page_number or 0, [])
            if block.get("type") == "table"
        ]
        match = _pair_block(element, manifest, candidates, secondary, set())
        if (
            match is None
            and len(candidates) == 1
            and primary_tables_by_page[element.page_number] == 1
            and (
                _element_bbox(element, manifest) is None
                or secondary.bbox(candidates[0]) is None
            )
        ):
            # One table on each side and no position to compare them by: the
            # same table. With both positions known and no overlap, they are not.
            match = 0
        chosen = candidates[match] if match is not None else None
        chosen_meta = (chosen or {}).get("metadata") or {}

        if primary_ast and analyze_table_complexity(primary_ast).has_spans:
            source, reason = primary_name, "primary-has-spans"
        elif not candidates:
            source, reason = primary_name, "no-secondary-table"
        elif chosen is None:
            source, reason = primary_name, "no-secondary-match"
        elif not (chosen.get("text") or chosen_meta.get("table_ast")):
            source, reason = primary_name, "secondary-table-empty"
        else:
            source, reason = _TABLE_PREFERENCE, f"preference-{_TABLE_PREFERENCE}"

        if source == secondary_name and chosen is not None:
            # Docling's table_* keys (AST, row/column counts, header) describe
            # the table being replaced; the output prefers table_ast over the
            # text, so a stale AST would hide the adopted MinerU text.
            for key in [k for k in element.metadata if k.startswith("table_")]:
                del element.metadata[key]
            if chosen.get("text"):
                element.text = chosen["text"]
            for key, value in chosen_meta.items():
                if key.startswith("table_"):
                    element.metadata[key] = value
        element.metadata["table_source"] = source
        element.metadata["table_source_reason"] = reason
        reasons[reason] += 1

    return MethodResult(
        success=True,
        validated=True,
        message="Fonte de tabela escolhida: "
        + ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items())),
    )


def _handle_skeleton_order_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Follow the secondary provider's reading order (MinerU skeleton).

    Page elements are paired with the secondary blocks by position (falling
    back to text); paired elements take the skeleton order and unpaired ones
    stay right after the element that preceded them. A page with fewer than
    half its elements paired is left alone.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    by_page: dict[int, list[ManifestElement]] = {}
    for element in elements:
        if element.page_number is not None:
            by_page.setdefault(element.page_number, []).append(element)

    reordered: set[int] = set()
    confirmed: set[int] = set()
    for page_number, page_elements in by_page.items():
        current = sorted(page_elements, key=lambda element: element.reading_order)
        skeleton = secondary.pages.get(page_number, [])
        used: set[int] = set()
        keys: dict[int, int] = {}
        for index, element in enumerate(current):
            match = _pair_block(element, manifest, skeleton, secondary, used)
            if match is not None:
                used.add(match)
                keys[index] = match
        if len(current) < 2 or len(keys) < _MIN_SKELETON_MATCH * len(current):
            continue
        ranked: list[tuple[float, int, int]] = []
        last = -1.0
        for index in range(len(current)):
            if index in keys:
                last = float(keys[index])
                ranked.append((last, 0, index))
            else:
                ranked.append((last, 1, index))
        order = [index for _, _, index in sorted(ranked)]
        if _apply_page_order(current, order):
            reordered.add(page_number)
        else:
            confirmed.add(page_number)

    if not reordered and not confirmed:
        return MethodResult(
            success=False,
            validated=False,
            message="Esqueleto do provider secundário não casou com a página",
        )
    _refresh_page_element_ids(manifest, reordered)
    parts = []
    if reordered:
        parts.append("aplicada nas páginas " + ", ".join(map(str, sorted(reordered))))
    if confirmed:
        parts.append("confirmada nas páginas " + ", ".join(map(str, sorted(confirmed))))
    return MethodResult(
        success=True,
        validated=True,
        message="Ordem do esqueleto " + "; ".join(parts),
    )


def _page_markdown(items: Iterable[tuple[str, str | None]]) -> str:
    """Page text with the markers the page router looks for."""
    parts = []
    for kind, text in items:
        text = (text or "").strip()
        if kind == "table":
            parts.append(f"<table>{text}</table>")
        elif kind == "formula":
            parts.append(f"$${text}$$")
        elif text:
            parts.append(text)
    return "\n\n".join(parts)


def _handle_agreement_rule_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Deterministic page router between the providers (``decide_v1``).

    When the router keeps the primary (Docling) page the obligation is
    satisfied without changing the text. When it picks the secondary page the
    decision is recorded but not applied: replacing a whole page would reopen
    the manifest mid-plan, and Dr.DocBench measured this router below the
    block-level fusion the core already runs. The method then fails, so the
    obligation goes to ``human-review`` with the route in ``page_route``.
    """
    from docstruct.fusion.adjudication import decide_v1

    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    primary_name = _CALIBRATED_PAIR[0]
    routes: dict[int, tuple[str, str]] = {}
    for page_number in sorted({e.page_number for e in elements if e.page_number}):
        primary_md = _page_markdown(
            (element.type, element.text)
            for element in sorted(manifest.elements, key=lambda e: e.reading_order)
            if element.page_number == page_number
        )
        secondary_md = _page_markdown(
            (str(block.get("type") or ""), block.get("text"))
            for block in secondary.pages.get(page_number, [])
        )
        routes[page_number] = decide_v1(primary_md, secondary_md)

    for element in elements:
        if element.page_number in routes:
            choice, reason = routes[element.page_number]
            element.metadata["page_route"] = {"choice": choice, "reason": reason}

    rerouted = {page: route for page, route in routes.items() if route[0] != primary_name}
    if not routes:
        return MethodResult(
            success=False,
            validated=False,
            message="Obrigação sem página identificada para o roteador",
        )
    if rerouted:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Roteador por página escolhe o provider secundário ("
                + ", ".join(f"p{page}: {reason}" for page, (_, reason) in sorted(rerouted.items()))
                + "); a troca de página não é aplicada automaticamente"
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message="Roteador por página mantém o provider primário nas páginas "
        + ", ".join(map(str, sorted(routes))),
    )


#: Longest side of the page image sent to the judge. Two candidates plus the
#: image must fit one 8192-token slot of the production VLM server.
_JUDGE_PAGE_MAX_SIDE = 1600


def _render_page_png(
    source_path: Path,
    page_number: int,
    artifact_id: str | None = None,
    media_type: str | None = None,
) -> bytes:
    """Page ``page_number`` (1-based) of a PDF, PNG or JPEG as PNG, rendered by the Toolbox.

    At most 144 dpi, with the longest side bounded by ``_JUDGE_PAGE_MAX_SIDE``.
    """
    return _toolbox_render(
        source_path,
        page_number,
        media_type=media_type,
        dpi=144,
        max_side=_JUDGE_PAGE_MAX_SIDE,
        artifact_id=artifact_id,
    ).image

def _judge_page(image: bytes, candidate_a: str, candidate_b: str) -> dict[str, Any]:
    """Call document.adjudicate.page through the Toolbox (sync wrapper)."""
    import tempfile

    from acessilia_sdk.toolbox.clients import ToolboxAdjudicateClient
    from backend.tools.toolbox_client import make_toolbox_client, run_sync

    async def call(path: Path) -> dict[str, Any]:
        client = make_toolbox_client(ToolboxAdjudicateClient)
        try:
            result = await client.judge_page(candidate_a, candidate_b, file_path=path)
        finally:
            await client.close()
        logger.info(
            "Toolbox: page adjudication finished (pB={})",
            result.get("document", {}).get("pB", "?"),
        )
        return result

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.png"
        path.write_bytes(image)
        return run_sync(call(path))


#: Characters per candidate the Toolbox's judge reads (``judge_max_chars`` of
#: ``qwen3-vl-8b`` in its ``providers-config.yaml``); beyond it the candidate is
#: cut on the Toolbox side, so the verdict does not cover the whole page.
_JUDGE_MAX_CHARS = 6000


def _judge_probability(document: dict[str, Any]) -> float | None:
    """``pB`` of a judge answer, or ``None`` when absent or outside [0, 1]."""
    try:
        p_b = float(document["pB"])
    except (KeyError, TypeError, ValueError):
        return None
    return p_b if 0.0 <= p_b <= 1.0 else None


def _handle_vlm_pairwise_judge_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Pairwise VLM judge between the two providers' pages.

    The Toolbox (``document.adjudicate.page``) returns the position-debiased
    probability that the secondary page is the more faithful transcription.
    Candidate A is the page the manifest carries and B the other provider's:
    the secondary page, or, on a page the deterministic router already took
    from the secondary provider (its elements carry that ``metadata.provider``),
    the primary elements it replaced. Such a page without the replaced elements
    in the artifact is skipped: comparing it with the secondary page would
    compare MinerU with itself and report a confirmation. Only pages
    where the two transcriptions diverge are judged (``should_judge``). Of
    docstruct's two-threshold rule (``decide_pair``) only the ``p_new >
    DEFAULT_NEW_THR`` branch applies: there is no previous judge to consult
    here. A confirmed page satisfies the obligation; a preferred alternative is
    recorded in ``page_judgement`` but not swapped in (that would reopen the
    manifest mid-plan), so the method fails and the non-critical obligation
    becomes a technical warning. An answer without a valid ``pB`` fails the
    method instead of counting as a confirmation.
    """
    from docstruct.fusion.judge import DEFAULT_NEW_THR, should_judge

    from backend.tools.toolbox_client import (
        ToolboxAuthenticationError,
        ToolboxContractViolation,
        ToolboxError,
    )

    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    primary_name = _CALIBRATED_PAIR[0]
    source_path = Path(manifest.source.path)
    judgements: dict[int, dict[str, Any]] = {}
    for page_number in sorted({e.page_number for e in elements if e.page_number}):
        page_elements = [
            element
            for element in sorted(manifest.elements, key=lambda e: e.reading_order)
            if element.page_number == page_number
        ]
        adopted = any(
            element.metadata.get("provider") == secondary.provider
            for element in page_elements
        )
        if adopted:
            if page_number not in secondary.replaced_primary:
                logger.warning(
                    "vlm-pairwise-judge: page {} came from {}, but the artifact does not "
                    "keep the {} page it replaced; skipped",
                    page_number,
                    secondary.provider,
                    primary_name,
                )
                continue
            content, other = secondary.provider, primary_name
            other_blocks = secondary.replaced_primary[page_number]
        else:
            content, other = primary_name, secondary.provider
            other_blocks = secondary.pages.get(page_number, [])
        primary_md = _page_markdown(
            (element.type, element.text) for element in page_elements
        )
        secondary_md = _page_markdown(
            (str(block.get("type") or ""), block.get("text"))
            for block in other_blocks
        )
        if not primary_md.strip() or not secondary_md.strip():
            logger.info(
                "vlm-pairwise-judge: página {} sem as duas transcrições; ignorada", page_number
            )
            continue
        if not should_judge(primary_md, secondary_md):
            judgements[page_number] = {
                "content": content, "choice": content, "pB": None, "reason": "agree",
            }
            continue

        try:
            image = _render_page_png(
                source_path,
                page_number,
                _source_artifact_id(manifest),
                manifest.source.media_type,
            )
        except Exception as exc:  # noqa: BLE001 - rendering a broken source
            return MethodResult(
                success=False,
                validated=False,
                message=f"Página {page_number} não pôde ser renderizada: {exc}",
            )
        truncated = max(len(primary_md), len(secondary_md)) > _JUDGE_MAX_CHARS
        if truncated:
            logger.warning(
                "vlm-pairwise-judge: página {} passa de {} caracteres; "
                "o juiz lê a transcrição truncada",
                page_number,
                _JUDGE_MAX_CHARS,
            )
        logger.info("vlm-pairwise-judge: julgando a página {}", page_number)
        try:
            result = _judge_page(image, primary_md, secondary_md)
        except (ToolboxAuthenticationError, ToolboxContractViolation) as exc:
            return MethodResult(
                success=False,
                validated=False,
                message=f"Juiz VLM recusou a chamada: {type(exc).__name__}: {exc}",
            )
        except ToolboxError as exc:
            return MethodResult(
                success=False,
                validated=False,
                message=f"Juiz VLM indisponível: {type(exc).__name__}: {exc}",
            )
        document = result.get("document") or {}
        p_b = _judge_probability(document)
        if p_b is None:
            return MethodResult(
                success=False,
                validated=False,
                message=(
                    f"Juiz VLM não devolveu pB válido para a página {page_number}: "
                    f"{document.get('pB')!r}"
                ),
            )
        judgements[page_number] = {
            "content": content,
            "choice": other if p_b > DEFAULT_NEW_THR else content,
            "pB": p_b,
            "pB_ab": document.get("pB_ab"),
            "pA_ba": document.get("pA_ba"),
            "model": document.get("model"),
            "truncated": truncated,
        }

    if not judgements:
        return MethodResult(
            success=False,
            validated=False,
            message="Sem páginas com as duas transcrições para o juiz VLM",
        )
    for element in elements:
        if element.page_number in judgements:
            element.metadata["page_judgement"] = judgements[element.page_number]

    preferred = {
        page: judgement
        for page, judgement in judgements.items()
        if judgement["choice"] != judgement["content"]
    }
    if preferred:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Juiz VLM prefere o outro provider ("
                + ", ".join(
                    f"p{page}: {j['choice']} pB={j['pB']:.2f}"
                    for page, j in sorted(preferred.items())
                )
                + "); a troca de página não é aplicada automaticamente"
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message="Juiz VLM confirma a página aplicada: "
        + ", ".join(
            f"{page} ({j['content']}, pB={j['pB']:.2f})"
            if j["pB"] is not None
            else f"{page} ({j['content']}, concordam)"
            for page, j in sorted(judgements.items())
        ),
    )


def _apply_page_order(current: list[ManifestElement], order: list[int]) -> bool:
    """Reassign the reading-order slots of ``current`` following ``order``.

    ``order[i]`` is the index in ``current`` of the element that goes to the
    i-th slot. Returns whether anything moved.
    """
    if order == list(range(len(current))):
        return False
    slots = sorted(element.reading_order for element in current)
    for slot, index in zip(slots, order):
        current[index].reading_order = slot
    return True


def _refresh_page_element_ids(
    manifest: ProcessingManifest, page_numbers: set[int]
) -> None:
    manifest.elements.sort(key=lambda element: element.reading_order)
    for page in manifest.pages:
        if page.page_number in page_numbers:
            page.element_ids = [
                element.id
                for element in manifest.elements
                if element.page_number == page.page_number
            ]


def _handle_xycut_order_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Apply XY-cut reading order to the evidenced page elements."""
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    fusion_agent = _fusion_agent()
    reordered_pages: set[int] = set()
    by_page: dict[int, list[ManifestElement]] = {}
    for element in elements:
        if element.page_number is not None:
            by_page.setdefault(element.page_number, []).append(element)

    for page_number, page_elements in by_page.items():
        positioned = [
            element for element in page_elements
            if _element_bbox(element, manifest) is not None
        ]
        if len(positioned) < 2:
            continue
        current = sorted(positioned, key=lambda element: element.reading_order)
        boxes = [_element_bbox(element, manifest) for element in current]
        order = fusion_agent.process_refine_reading_order(
            [list(box) for box in boxes if box is not None]
        )
        if _apply_page_order(current, order):
            reordered_pages.add(page_number)

    if not reordered_pages:
        return MethodResult(
            success=False,
            validated=False,
            message="XY-cut não identificou uma mudança de ordem de leitura",
        )

    _refresh_page_element_ids(manifest, reordered_pages)
    return MethodResult(
        success=True,
        validated=True,
        message=(
            "Ordem de leitura XY-cut aplicada nas páginas "
            + ", ".join(map(str, sorted(reordered_pages)))
        ),
    )


from backend.core.execution.handlers.common import _handle_human_review_method
from backend.core.execution.handlers.deterministic import _handle_deterministic_heading_repair_method


DETERMINISTIC_METHOD_HANDLERS: dict[
    str, Callable[[ProcessingManifest, str], MethodResult]
] = {
    #: Method name -> handler for every method whose work is already done before
    #: planning (or can be done without a model call). A method the manifest can
    #: require but that is absent from here is reported to the planner as
    #: unavailable, so the plan never contains a step that can only fail.
    "vision-description": _handle_vision_description_method,
    "pandoc-table": _handle_pandoc_table_method,
    "pandoc-code": _handle_pandoc_code_method,
    "pymupdf-region": _handle_pymupdf_region_method,
    "vision-page-inspection": _handle_vision_page_inspection_method,
    "latex-normalizer": _handle_latex_normalizer_method,
    "norm-fix": _handle_norm_fix_method,
    "unicode-repair": _handle_unicode_repair_method,
    "rep-guard": _handle_repetition_guard_method,
    "line-number-extractor": _handle_extract_line_numbers_method,
    "verse-chunker": _handle_chunk_verse_method,
    "tailjoin": _handle_tailjoin_method,
    "tailjoin-positioned": _handle_tailjoin_method,
    "xycut-order": _handle_xycut_order_method,
    "skeleton-order": _handle_skeleton_order_method,
    "table-smart-fusion": _handle_table_smart_fusion_method,
    "agreement-rule": _handle_agreement_rule_method,
    "deterministic-heading-repair": _handle_deterministic_heading_repair_method,
}


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
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Funde dois providers (docling+mineru) para a obrigação alvo.

    Reutiliza ``backend.pipeline.fusion.extract_fused`` (Fase 4) para
    produzir o payload fundido, persiste o JSON em ``data_dir/artifacts/
    fusion/`` e o retorna em ``MethodResult.artifacts`` — deixando o
    ``ExecutorAgent`` responsável por incorporá-lo ao manifesto e registrar
    a proveniência na tentativa.

    Em modo single (FUSION_MODE=single) o método não é admissível e não
    deve ser selecionado pelo compilador.

    Reutiliza a fusão inicial quando hash, documento e configuração coincidem.
    Evidência ausente, corrompida ou incompatível provoca reextração dual.
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
                payload = _run_extract_fused(source_path, artifact_id=artifact_id)
            else:
                payload = _run_extract_fused(source_path, artifact_id=artifact_id, policy=policy)
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

    # A ação planejada é dual-provider: se apenas um provider participou,
    # a semântica planejada não foi executada → falha para permitir replanning
    # para um método single-provider.
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


class PddlAccessibilityOrchestrator:
    """Drives the PDDL pipeline: informational-extraction -> planner -> executor.

    Args:
        planner_backend (str): Which planner backend to prefer for nominal-plan generation; defaults to "internal".
        preferred_plan (str): Which plan source to prefer when multiple are available; defaults to "internal".
        execute_dry_run (bool): When True the produced plan is only validated in dry-run instead of executed for real; defaults to True.
        fast_downward (Path | None): Optional filesystem path to a fast-downward binary used when planner_backend is fast-downward; defaults to None.
        fast_downward_alias (str | None): Optional shell alias to invoke fast_downward through instead of the raw path; defaults to None.
        fast_downward_search (str): Fast-downward search heuristic string passed when the backend is active; defaults to "astar(blind())".
        enable_ocr (bool): Whether the docling extraction backend should run its OCR pass; defaults to True.
        extractor_backend (str): Which manifest-extraction backend to use.
            Supported values:
            - "toolbox" — ToolboxManifestExtractor (remote via Acessilia Toolbox)
            - "toolbox-layout" / "toolbox_layout" — mapped to "toolbox" (same REST API)
            Defaults to "toolbox".
    """

    def __init__(
        self,
        *,
        planner_backend: str = "internal",
        preferred_plan: str = "internal",
        execute_dry_run: bool = True,
        fast_downward: Path | None = None,
        fast_downward_alias: str | None = None,
        fast_downward_search: str = "astar(blind())",
        enable_ocr: bool = True,
        extractor_backend: str = "toolbox",
        max_replans: int = 2,
    ) -> None:
        self.planner_backend = planner_backend
        self.preferred_plan = preferred_plan
        self.execute_dry_run = execute_dry_run
        self.max_replans = max(0, max_replans)
        self.fast_downward = fast_downward
        self.fast_downward_alias = fast_downward_alias
        self.fast_downward_search = fast_downward_search
        self.extractor_backend = extractor_backend.strip().lower()

        if self.extractor_backend in ("toolbox", "toolbox-layout", "toolbox_layout"):
            logger.info(
                "PDDL extractor_backend={}: usando ToolboxManifestExtractor",
                self.extractor_backend,
            )
            extractor = ToolboxManifestExtractor(enable_ocr=enable_ocr)
        else:
            raise ValueError(
                f"Extractor local '{self.extractor_backend}' desativado. "
                f"Use extractor_backend='toolbox' para o pipeline PDDL+Toolbox."
            )

        self.information_structural = InformationalStructuralAgent(
            extractor
        )
        self.planner = PlannerAgent()
        self.executor = self._build_executor()

    @staticmethod
    def _build_executor() -> ExecutorAgent:
        registry = MethodRegistry()

        # Model-backed or cross-cutting methods already wired by the pipeline.
        registry.register("mathml", _handle_mathml_method)
        registry.register("latex-verbalizer", _handle_latex_verbalizer_method)
        registry.register(
            "dual-provider-fusion", _handle_dual_provider_fusion_method
        )
        registry.register("single-provider", _handle_single_provider_method)
        registry.register("vlm-pairwise-judge", _handle_vlm_pairwise_judge_method)
        for method, handler in DETERMINISTIC_METHOD_HANDLERS.items():
            registry.register(method, handler)

        # `human-review` has its own semantics: it is the terminal escalation the
        # pipeline performs when no automated method can satisfy an obligation.
        registry.register("human-review", _handle_human_review_method)

        return ExecutorAgent(registry, domain=DomainBundle.load())

    async def executar(
        self,
        file_path: Path,
        tmpdir: Path,
        status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        mode: str | None = None,
        structured_output: bool = False,
        custom_prompt: str | None = None,
        thinking_mode: bool = False,
    ) -> str | dict[str, Any]:
        """Compatibility entrypoint; orchestration lives in one explicit pipeline."""
        return await execute_pipeline(
            self, file_path, tmpdir, status_callback, mode, structured_output,
            custom_prompt, thinking_mode,
            hooks=PipelineHooks(
                inspect_media=_inspect_media,
                is_presentation=_is_presentation,
                classify_formula_regions=_classify_formula_regions,
                enrich_picture_descriptions=_enrich_picture_descriptions,
                TextSpanReader=_TextSpanReader,
                mark_inline_math=_mark_inline_math,
                enrich_formula_latex=_enrich_formula_latex,
                enrich_table_structures=_enrich_table_structures,
                mark_table_inline_math=_mark_table_inline_math,
                log_availability=_log_availability,
                plan_progress=_plan_progress,
                build_pddl_structured_payload=build_pddl_structured_payload,
            ),
        )

    def _build_plan(
        self,
        manifest: ProcessingManifest,
        *,
        extra_unavailable: Iterable[str] = (),
        selected_roots: Iterable[str] | None = None,
    ) -> tuple[NominalPlan, PlanningComparison | None]:
        """Build exactly one nominal plan through the shared PDDL coordinator."""
        return build_nominal_plan(
            manifest=manifest,
            planner=self.planner,
            planner_backend=self.planner_backend,
            preferred_plan=self.preferred_plan,
            fast_downward=self.fast_downward,
            fast_downward_alias=self.fast_downward_alias,
            fast_downward_search=self.fast_downward_search,
            registered_handlers=self.executor.registry.names(),
            unavailable_handlers=self._unavailable_methods(manifest),
            extra_unavailable=extra_unavailable,
            selected_roots=selected_roots,
        )

    def _unavailable_methods(self, manifest: ProcessingManifest) -> list[str]:
        """Compatibility entrypoint for executor registry availability."""
        return unavailable_methods(
            manifest, registered_handlers=self.executor.registry.names()
        )


# Image-specific enrichment implementation resides in manifest.preprocessing.
from backend.core.manifest.preprocessing.picture_enrichment import (
    PictureHooks, VisionFailureReason, _record_vision_failure,
    _RETRIED_VISION_FAILURES,
)


async def _enrich_picture_descriptions(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    mode: str,
    media_inspection: dict[str, Any] | None = None,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    breaker: ProviderBreaker | None = None,
) -> None:
    """Compatibility wrapper; call a single shared enrichment implementation."""
    from backend.core.manifest.preprocessing.picture_enrichment import (
        _enrich_picture_descriptions as implementation,
    )
    return await implementation(
        manifest, source_file, mode=mode,
        media_inspection=media_inspection, status_callback=status_callback,
        breaker=breaker,
        hooks=PictureHooks(
            VisionAgent=VisionAgent,
            inject_full_page_picture=_inject_full_page_picture,
            is_mostly_visual_manifest=_is_mostly_visual_manifest,
            is_placeholder_visual_element=_is_placeholder_visual_element,
            extract_picture_bytes=_extract_picture_bytes,
            source_artifact_id=_source_artifact_id,
            pages_label=_pages_label,
            retry_failed=_retry_failed,
            has_meaningful_text=_has_meaningful_text,
            record_vision_failure=_record_vision_failure,
        ),
    )


from backend.core.manifest.preprocessing.retries import _retry_failed as _retry_failed_impl


async def _retry_failed(
    failed: list[tuple[int, ManifestElement]],
    attempt: Callable[[int, ManifestElement], Coroutine[Any, Any, bool]],
    *,
    kind: str,
    breaker: ProviderBreaker,
) -> int:
    """Preserve the facade's injectable sleep for deterministic retry tests."""
    return await _retry_failed_impl(
        failed, attempt, kind=kind, breaker=breaker, sleep=asyncio.sleep
    )


# Formula algorithms live in one pre-PDDL module with explicit runtime hooks.
from backend.core.manifest.preprocessing.formula_enrichment import (
    FormulaHooks,
    _ranked_formula_candidates,
    _TextSpanReader,
    _inline_math_enabled,
    _boxed_pages,
    _mark_inline_math,
    _mark_table_inline_math,
    _top_left_box,
    _detect_by_page,
    _element_span_lines,
    _claim_negation_overlay,
    _reclassify_formula_picture,
    _escalate_unresolved_formula,
    _INLINE_MATH_ELEMENT_TYPES,
    _INLINE_MATH_BOX_TOLERANCE,
)


def _formula_recognition_crop(
    source_file: Path,
    element: ManifestElement,
    *,
    artifact_id: str | None = None,
    media_type: str | None = None,
) -> tuple[bytes | None, int]:
    from backend.core.manifest.preprocessing.formula_enrichment import (
        _formula_recognition_crop as implementation,
    )
    return implementation(
        source_file, element, artifact_id=artifact_id,
        media_type=media_type, render_page=_toolbox_render,
    )


def _formula_hooks() -> FormulaHooks:
    return FormulaHooks(
        VisionAgent=VisionAgent,
        formula_recognition_crop=_formula_recognition_crop,
        toolbox_recognize_formula=toolbox_recognize_formula,
        pages_label=_pages_label,
        source_artifact_id=_source_artifact_id,
    )


async def _classify_formula_regions(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    mode: str,
    breaker: ProviderBreaker,
) -> None:
    from backend.core.manifest.preprocessing.formula_enrichment import (
        _classify_formula_regions as implementation,
    )
    return await implementation(
        manifest, source_file, mode=mode, breaker=breaker,
        hooks=_formula_hooks(),
    )


async def _enrich_formula_latex(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    mode: str,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    breaker: ProviderBreaker | None = None,
) -> None:
    from backend.core.manifest.preprocessing.formula_enrichment import (
        _enrich_formula_latex as implementation,
    )
    return await implementation(
        manifest, source_file, mode=mode,
        status_callback=status_callback, breaker=breaker,
        hooks=_formula_hooks(),
    )


# Preprocessing before PDDL: keep legacy imports stable.
from backend.core.manifest.preprocessing.media_triage import (
    _is_presentation,
    _inject_full_page_picture,
)


# Table-specific enrichment implementation and failure metadata policy.
from backend.core.manifest.preprocessing.table_enrichment import (
    TableHooks,
    TableReadFailure,
    _record_table_read_failure,
    _table_review_attempts,
    _keep_misread_table,
)


async def _enrich_table_structures(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    breaker: ProviderBreaker | None = None,
) -> None:
    from backend.core.manifest.preprocessing.table_enrichment import (
        _enrich_table_structures as implementation,
    )
    return await implementation(
        manifest, source_file, status_callback=status_callback,
        breaker=breaker,
        hooks=TableHooks(
            DataAgent=DataAgent,
            extract_picture_bytes=_extract_picture_bytes,
            source_artifact_id=_source_artifact_id,
            pages_label=_pages_label,
            retry_failed=_retry_failed,
        ),
    )


# Compatibility imports: the implementations now belong to manifest.table_reading.
from backend.core.manifest.table_reading import (
    _TABLE_REREAD_MIN_SHARED,
    _TABLE_REREAD_ATTEMPTS,
    _TABLE_WORD,
    _table_reread_disqualified,
    _table_element_has_structured_content,
    _table_ast_column_count,
    _backfill_table_from_existing_text,
    _rows_from_visual_table_text,
    _rows_from_pipe_table,
    _rows_from_delimited_lines,
    _looks_like_markdown_separator,
    _score_rows,
    _rows_look_like_placeholder,
    _table_ast_from_metadata,
    _table_ast_from_rows,
)


#: Longest side of a picture crop sent to the vision model. The render runs at
#: 160 dpi and the Toolbox refuses more than 20 MP, so an unbounded crop of a
#: large standalone image (its page is sized in pixels) is rejected before the
#: model sees it. Bounding the side keeps every source renderable, and the
#: vision model's 8192-token slot would downscale it further anyway.
_PICTURE_MAX_SIDE = 2000


def _extract_picture_bytes(
    source_file: Path,
    element: ManifestElement,
    fallback_page_number: int | None,
    artifact_id: str | None = None,
    media_type: str | None = None,
) -> tuple[bytes | None, int]:
    """The picture's box rendered by the Toolbox, or its whole page.

    A box the Toolbox refuses (outside the page) or a sliver of one is not a
    picture, so the page is described instead. Any other failure, the Toolbox
    being unavailable included, is not retried with the page: it is logged
    with its cause and the element is left without an image.
    """
    from acessilia_sdk.toolbox import ToolboxCapabilityError, ToolboxIncompatibleEndpoint

    provenance = element.provenance[0] if element.provenance else None
    page_number = provenance.page_number if provenance is not None else (fallback_page_number or 0)
    if page_number < 1:
        return None, 0

    def render(clip=None, origin: ClipOrigin = "TOPLEFT") -> RenderedPage:
        return _toolbox_render(
            source_file, page_number, media_type=media_type, dpi=160,
            max_side=_PICTURE_MAX_SIDE, clip=clip,
            clip_origin=origin, artifact_id=artifact_id,
        )

    clip, origin = _picture_clip(provenance)
    try:
        if clip is None:
            return render().image, page_number
        try:
            page = render(clip, origin)
        except ToolboxIncompatibleEndpoint:
            raise
        except ToolboxCapabilityError:
            # The Toolbox refuses (HTTP 400) a box that does not intersect the page.
            return render().image, page_number
        left, top, right, bottom = page.clip
        if right - left < 4 or bottom - top < 4:
            return render().image, page_number
        return page.image, page_number
    except Exception:
        logger.exception(
            t(LOG_PDDL_ELEMENT_CROP_FAILED).format(element_id=element.id)
        )
        return None, page_number


from backend.core.manifest.preprocessing.render_regions import _picture_clip


# Visual-content predicates now belong to manifest preprocessing.
from backend.core.manifest.preprocessing.visual_signals import (
    _is_mostly_visual_manifest,
    _is_ocr_noise,
    _is_placeholder_visual_element,
    _is_placeholder_text,
    _has_meaningful_text,
)


def _plan_progress(
    manifest: ProcessingManifest,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None,
    loop: asyncio.AbstractEventLoop,
) -> StepProgress | None:
    """Report each obligation step, with its pages, through ``status_callback``.

    The executor runs on a worker thread while the callback is a coroutine of
    the event loop, so the report is scheduled on ``loop`` and awaited from the
    thread: the step only starts once its stage is recorded, which is what lets
    a job killed mid-step still say which step and page it was on. A failing
    report, including a broken translation of the label, is logged and never
    fails the step.

    The user sees only the step and its pages; the obligation kind and method
    are internal names, so they go to the debug log.
    """
    if status_callback is None:
        return None
    page_by_element = {element.id: element.page_number for element in manifest.elements}
    pages_by_obligation = {
        obligation.id: [page_by_element.get(target) for target in obligation.target_ids]
        for obligation in manifest.obligations
    }

    def report(step: PlanStep, position: int, total: int) -> None:
        if step.action != "execute-obligation" or step.obligation_id is None:
            return
        pages = pages_by_obligation.get(step.obligation_id, [])
        logger.debug(
            "PDDL: step {}/{} {} via {}, pages {}",
            position,
            total,
            step.obligation_id,
            step.method,
            pages,
        )
        try:
            message = t(STAGE_EXECUTING_PLAN_STEP).format(
                current=position,
                total=total,
                pages=_pages_label(pages, len(manifest.pages)),
            )
            asyncio.run_coroutine_threadsafe(status_callback(message), loop).result(
                timeout=_PROGRESS_REPORT_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - progress must not fail the plan
            logger.warning("PDDL: progress of step {} not recorded: {}", position, exc)

    return report


#: Upper bound on how long a plan step waits for its progress to be recorded.
_PROGRESS_REPORT_TIMEOUT_SECONDS = 10.0


def _log_availability(availability: toolbox_gate.AvailabilityDecision) -> None:
    if not availability.blocked:
        return
    blocked = "; ".join(
        f"{method}: {', '.join(reasons)}"
        for method, reasons in availability.blocked.items()
    )
    if availability.enforced:
        logger.warning(
            "Gate da Toolbox (catálogo {}): métodos fora do plano — {}",
            availability.catalog_revision or "indisponível",
            blocked,
        )
    else:
        logger.warning(
            "Gate da Toolbox em observação (catálogo {}): removeria — {}",
            availability.catalog_revision or "indisponível",
            blocked,
        )


# Backward compatibility with the PMV naming.
PmvAccessibilityOrchestrator = PddlAccessibilityOrchestrator
build_pmv_structured_payload = build_pddl_structured_payload
