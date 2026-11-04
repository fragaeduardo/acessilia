"""The existing extraction -> enrichment -> PDDL -> execution pipeline.

This module preserves the original execution sequence. The public facade
supplies explicit, late-bound hooks so integrations can replace providers
without coupling the entire pipeline to the old monolithic module.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from backend.agents.provider_breaker import ProviderBreaker
from backend.core.execution.models import ExecutionReport
from backend.core.execution.recovery import (
    _blocks_delivery, _replannable_obligation_ids,
    _unsatisfied_selected_obligations, _unsatisfied_summary,
)
from backend.core.manifest.preprocessing.page_limits import (
    StructuralExtractionError, _check_page_limit, _check_page_limit_before_extraction,
)
from backend.core.planning import toolbox_gate
from backend.i18n import t
from backend.services.blocking import run_blocking
from backend.stage_messages import (
    STAGE_ANALYZING_STRUCTURAL, STAGE_ENRICHING_FORMULAS,
    STAGE_ENRICHING_IMAGE_DESCRIPTIONS, STAGE_ENRICHING_TABLES_OCR,
    STAGE_GENERATING_PDDL_PLAN, STAGE_VALIDATING_PLAN_DRY_RUN,
    STAGE_EXECUTING_PLAN, STAGE_STRUCTURAL_EXTRACTION_FAILED,
)
from backend.tools.logger import logger
from backend.tools.toolbox_client import ToolboxError


@dataclass(frozen=True, slots=True)
class PipelineHooks:
    """Explicit extension seam; the orchestrator remains API-compatible."""
    inspect_media: Callable[..., Any]
    is_presentation: Callable[..., Any]
    classify_formula_regions: Callable[..., Any]
    enrich_picture_descriptions: Callable[..., Any]
    TextSpanReader: Callable[..., Any]
    mark_inline_math: Callable[..., Any]
    enrich_formula_latex: Callable[..., Any]
    enrich_table_structures: Callable[..., Any]
    mark_table_inline_math: Callable[..., Any]
    log_availability: Callable[..., Any]
    plan_progress: Callable[..., Any]
    build_pddl_structured_payload: Callable[..., Any]


async def execute_pipeline(
    self,
    file_path: Path,
    tmpdir: Path,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    mode: str | None = None,
    structured_output: bool = False,
    custom_prompt: str | None = None,
    thinking_mode: bool = False,
    *,
    hooks: PipelineHooks,
) -> str | dict[str, Any]:
    _inspect_media = hooks.inspect_media
    _is_presentation = hooks.is_presentation
    _classify_formula_regions = hooks.classify_formula_regions
    _enrich_picture_descriptions = hooks.enrich_picture_descriptions
    _TextSpanReader = hooks.TextSpanReader
    _mark_inline_math = hooks.mark_inline_math
    _enrich_formula_latex = hooks.enrich_formula_latex
    _enrich_table_structures = hooks.enrich_table_structures
    _mark_table_inline_math = hooks.mark_table_inline_math
    _log_availability = hooks.log_availability
    _plan_progress = hooks.plan_progress
    build_pddl_structured_payload = hooks.build_pddl_structured_payload

    effective_mode = mode or "medio"

    if custom_prompt or thinking_mode:
        raise ValueError("Custom instructions and thinking mode are unavailable for this pipeline")

    source_path = file_path.resolve()
    # media.inspect runs first: a slide deck is extracted and structured
    # slide by slide, which has to be decided before extraction.
    media_inspection, media_configuration = await _inspect_media(source_path)
    _check_page_limit_before_extraction(media_inspection)
    presentation = _is_presentation(media_inspection)

    if status_callback:
        await status_callback(t(STAGE_ANALYZING_STRUCTURAL))
    try:
        manifest = await run_blocking(
            self.information_structural.process,
            source_path,
            language="pt-BR",
            presentation=presentation,
        )
    except ToolboxError as exc:
        # The SDK already retried and failed over; there is no local
        # extractor to fall back on (docs/adr/0003), so the job fails here.
        # The message reaches the end user, so it names the capability and
        # the kind of failure only; the full cause, which carries internal
        # Toolbox endpoints, goes to the log and stays chained.
        logger.error("Structural extraction failed on the Toolbox: {}", exc)
        raise StructuralExtractionError(
            t(STAGE_STRUCTURAL_EXTRACTION_FAILED).format(
                capability="document.structure.extract", error=type(exc).__name__
            )
        ) from exc
    _check_page_limit(manifest)
    manifest.extractor.configuration["region_inspection_mode"] = effective_mode
    manifest.extractor.configuration.update(media_configuration)

    if status_callback:
        await status_callback(t(STAGE_ENRICHING_IMAGE_DESCRIPTIONS))
    # Pictures, formulas and tables reach the vision provider from the
    # same pipeline, so one that went down does not get called again for
    # every remaining object.
    from backend.config.settings import settings

    breaker = ProviderBreaker(settings.vision_breaker_threshold)
    await _classify_formula_regions(
        manifest, source_path, mode=effective_mode, breaker=breaker,
    )
    await _enrich_picture_descriptions(
        manifest,
        source_path,
        mode=effective_mode,
        media_inspection=media_inspection,
        status_callback=status_callback,
        breaker=breaker,
    )
    text_spans = _TextSpanReader(manifest, source_path)
    await _mark_inline_math(manifest, source_path, text_spans)
    await _enrich_formula_latex(
        manifest,
        source_path,
        mode=effective_mode,
        status_callback=status_callback,
        breaker=breaker,
    )

    if status_callback:
        await status_callback(t(STAGE_ENRICHING_TABLES_OCR))
    await _enrich_table_structures(
        manifest,
        file_path.resolve(),
        status_callback=status_callback,
        breaker=breaker,
    )
    # After the table enrichment, which can replace a table's cells.
    await _mark_table_inline_math(manifest, source_path, text_spans)

    if status_callback:
        await status_callback(t(STAGE_GENERATING_PDDL_PLAN))
    # One catalogue snapshot per document, so the first plan and every replan
    # see the same Toolbox.
    availability = await toolbox_gate.decide(manifest)
    _log_availability(availability)
    gated = availability.unavailable_methods
    plan, comparison = await run_blocking(
        self._build_plan, manifest, extra_unavailable=gated
    )

    execution_report: ExecutionReport | None = None
    if status_callback:
        await status_callback(
            t(STAGE_VALIDATING_PLAN_DRY_RUN)
            if self.execute_dry_run
            else t(STAGE_EXECUTING_PLAN)
        )
    # `execute_dry_run` selects the *mode* of execution; it used to also gate
    # whether the executor ran at all, so setting it to false produced a plan
    # that was never executed and a report of None. Real execution is now the
    # other branch instead of silence.
    loop = asyncio.get_running_loop()
    manifest, execution_report = await run_blocking(
        self.executor.execute,
        plan,
        manifest,
        dry_run=self.execute_dry_run,
        progress=_plan_progress(manifest, status_callback, loop),
    )

    # A `replan-required` report means the executor recorded `(tried ?o ?m)`
    # for a method that failed and left the obligation pending. Without this
    # loop the status was terminal in production: the pipeline returned a plan
    # it already knew was wrong, and `complete-job` never ran, so no document
    # was produced.
    replans = 0
    while (
        execution_report.replan_required
        and replans < self.max_replans
        and not self.execute_dry_run
    ):
        # Steps after a failure still run, so one execution can leave an
        # obligation exhausted (no method left) next to another that has an
        # alternative. The planner rejects exhausted obligations, so they are
        # left out of the replan; an exhausted critical one fails the job.
        exhausted = [
            obligation for obligation in manifest.obligations
            if obligation.status == "failed"
        ]
        if any(_blocks_delivery(obligation, manifest) for obligation in exhausted):
            break
        roots = _replannable_obligation_ids(manifest)
        if not roots:
            break
        replans += 1
        logger.info(
            "PDDL: replanejando ({}/{}) após {}",
            replans,
            self.max_replans,
            execution_report.status,
        )
        plan, comparison = await run_blocking(
            self._build_plan,
            manifest,
            extra_unavailable=gated,
            selected_roots=roots,
        )
        manifest, execution_report = await run_blocking(
            self.executor.execute,
            plan,
            manifest,
            dry_run=False,
            progress=_plan_progress(manifest, status_callback, loop),
        )

    if execution_report.status not in {"completed", "dry-run-completed"}:
        unsatisfied = _unsatisfied_selected_obligations(manifest)
        critical = [
            obligation for obligation in unsatisfied
            if _blocks_delivery(obligation, manifest)
        ]
        if not unsatisfied or critical:
            # The ids name every target element and run to kilobytes on a
            # long document, so they go to the log; the job error, which
            # reaches the user by e-mail, says what is open and where.
            logger.warning(
                "PDDL: unsatisfied obligations: {}",
                ", ".join(obligation.id for obligation in unsatisfied),
            )
            raise RuntimeError(_unsatisfied_summary(manifest))
        # Only refinements are left open; the extracted content is still
        # complete, so the document is delivered with a technical warning.
        logger.warning(
            "PDDL: concluindo com obrigações não críticas pendentes: {}",
            ", ".join(obligation.id for obligation in unsatisfied),
        )

    payload = build_pddl_structured_payload(
        file_path=file_path,
        manifest=manifest,
        plan=plan,
        planner_backend=self.planner_backend,
        execution_report=execution_report,
        comparison=comparison,
        toolbox_availability=availability.to_metadata(),
    )

    if structured_output:
        return payload
    return payload["text"]


