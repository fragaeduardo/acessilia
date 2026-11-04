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
from backend.core.execution.handlers.deterministic import (
    _handle_pandoc_table_method,
    _handle_pandoc_code_method,
    _handle_latex_normalizer_method,
    _handle_norm_fix_method,
)


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


# Pixel verification and region/page handlers live under core.execution.
from backend.core.execution.handlers import region_inspection as _region_handlers
from backend.core.manifest.preprocessing.render_regions import _RENDERABLE_MEDIA_TYPES, _source_artifact_id


def _toolbox_render(
    source: Path, page_number: int, *, media_type: str | None = None,
    dpi: int, max_side: int | None = None,
    clip: tuple[float, float, float, float] | None = None,
    clip_origin: ClipOrigin = "TOPLEFT", artifact_id: str | None = None,
) -> RenderedPage:
    return _region_handlers._toolbox_render(
        source, page_number, media_type=media_type, dpi=dpi, max_side=max_side,
        clip=clip, clip_origin=clip_origin, artifact_id=artifact_id,
    )


_save_region_artifact = _region_handlers._save_region_artifact


def _render_inspection_region(
    source: Path, element: ManifestElement, *,
    media_type: str | None = None, artifact_id: str | None = None,
) -> tuple[bytes, int, list[float]]:
    return _region_handlers._render_inspection_region(
        source, element, media_type=media_type, artifact_id=artifact_id,
        render_toolbox=_toolbox_render,
    )


def _inspect_region(image: bytes, number: int, total_pages: int, mode: str) -> VisionOutput | None:
    return _region_handlers._inspect_region(
        image, number, total_pages, mode, vision_cls=VisionAgent,
    )


def _render_source_page(
    source: Path, media_type: str, number: int, artifact_id: str | None = None,
) -> bytes:
    return _region_handlers._render_source_page(
        source, media_type, number, artifact_id, render_toolbox=_toolbox_render,
    )


def _inspect_page(image: bytes, number: int, total_pages: int, mode: str) -> PageInspectionOutput:
    return _region_handlers._inspect_page(
        image, number, total_pages, mode, vision_cls=VisionAgent,
    )


def _handle_pymupdf_region_method(manifest: ProcessingManifest, obligation_id: str) -> MethodResult:
    return _region_handlers._handle_pymupdf_region_method(
        manifest, obligation_id, render_region=_render_inspection_region,
        inspect_region=_inspect_region, save_artifact=_save_region_artifact,
        source_artifact_id=_source_artifact_id,
    )


def _handle_vision_page_inspection_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    return _region_handlers._handle_vision_page_inspection_method(
        manifest, obligation_id, render_source_page=_render_source_page,
        inspect_page=_inspect_page, save_artifact=_save_region_artifact,
        source_artifact_id=_source_artifact_id,
    )


# Text-oriented PDDL handlers share the deterministic FusionAgent adapter.
from backend.core.execution.handlers.text_methods import (
    _handle_repetition_guard_method,
    _element_blocks,
    _block_kind,
    _handle_extract_line_numbers_method,
    _handle_chunk_verse_method,
    _tailjoin_position,
    _handle_tailjoin_method,
)


# Calibrated provider rules, adjudication and ordering live in execution.
from backend.core.execution.handlers import fusion_selection as _fusion_methods
from backend.core.execution.handlers.fusion_selection import (
    _page_number,
    _load_secondary_extraction,
    _provider_pair_problem,
    _secondary_or_failure,
    _ordered_box,
    _iou,
    _pair_block,
    _handle_table_smart_fusion_method,
    _handle_skeleton_order_method,
    _page_markdown,
    _handle_agreement_rule_method,
    _judge_probability,
    _apply_page_order,
    _refresh_page_element_ids,
    _CALIBRATED_PAIR,
    _TABLE_PREFERENCE,
    _MIN_BLOCK_IOU,
    _MIN_BLOCK_TEXT_RATIO,
    _MIN_SKELETON_MATCH,
    _JUDGE_PAGE_MAX_SIDE,
    _JUDGE_MAX_CHARS,
    _SecondaryExtraction,
    _Box,
)


_render_page_png_impl = _fusion_methods._render_page_png
_judge_page = _fusion_methods._judge_page


def _render_page_png(
    source_path: Path, page_number: int, artifact_id: str | None = None,
    media_type: str | None = None,
) -> bytes:
    return _render_page_png_impl(
        source_path, page_number, artifact_id, media_type,
        render_toolbox=_toolbox_render,
    )


def _handle_vlm_pairwise_judge_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    return _fusion_methods._handle_vlm_pairwise_judge_method(
        manifest, obligation_id, render_page_png=_render_page_png,
        judge_page=_judge_page,
    )


def _handle_xycut_order_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    return _fusion_methods._handle_xycut_order_method(
        manifest, obligation_id, fusion_agent_factory=_fusion_agent,
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


# Fusion execution and evidence persistence are owned by execution.
from backend.core.execution.handlers import fusion_execution as _fusion_execution
from backend.core.execution.handlers.fusion_execution import (
    _reusable_fusion_payload,
    _fused_text_elements,
    _handle_single_provider_method,
    _persist_fusion_payload,
)


def _handle_dual_provider_fusion_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Compatibility facade with late-bound fusion extraction override."""
    return _fusion_execution._handle_dual_provider_fusion_method(
        manifest, obligation_id, extract_fused=_run_extract_fused
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


from backend.core.manifest.preprocessing.picture_crops import _PICTURE_MAX_SIDE
from backend.core.manifest.preprocessing import picture_crops as _picture_crops


def _extract_picture_bytes(
    source_file: Path,
    element: ManifestElement,
    fallback_page_number: int | None,
    artifact_id: str | None = None,
    media_type: str | None = None,
) -> tuple[bytes | None, int]:
    return _picture_crops._extract_picture_bytes(
        source_file, element, fallback_page_number, artifact_id, media_type,
        render_toolbox=_toolbox_render,
    )


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
