"""Pre-PDDL formula classification, recognition and PDF inline-math recovery.

The existing heuristics, text-layer agreement rules, provider-breaker limits,
TaskGroup concurrency and metadata formats are unchanged. The old facade
provides explicit hooks for provider calls and mocks.
"""
from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from docstruct.text.equations import preserve_equation_number
from docstruct.text.inline_math import (
    InlineMathDetection, TextSpan, assign_runs_to_cells,
    detect_inline_math, splice_inline_math,
)
from docstruct.text.latex import has_latex_markup, latex_text_agreement
from docstruct.text.negation_overlay import is_detached_overlay
from backend.agents.output_schemas import VisionOutput
from backend.agents.provider_breaker import ProviderBreaker
from backend.core.manifest.builder import (
    TYPE_BOUND_V23_KINDS, reconcile_reclassified_element_processing_needs,
    separate_equation_number,
)
from backend.core.manifest.element_geometry import _bbox, _OVERLAY_HOST_TOLERANCE
from backend.core.manifest.models import ManifestElement, ProcessingManifest
from backend.core.manifest.preprocessing.render_regions import (
    _source_artifact_id, _picture_clip, _RENDERABLE_MEDIA_TYPES,
)
from backend.core.manifest.table_reading import _TABLE_SECTIONS
from backend.i18n import t
from backend.log_messages import (
    LOG_PDDL_FORMULA_RECOGNIZE_EMPTY,
    LOG_PDDL_FORMULA_RECOGNIZE_UNAVAILABLE,
    LOG_PDDL_FORMULA_TEXT_LAYER_DISAGREES,
    LOG_PDDL_FORMULAS_FELL_BACK_TO_VISION,
    LOG_PDDL_FORMULAS_RECOGNIZED,
)
from backend.services.blocking import run_blocking
from backend.stage_messages import STAGE_ENRICHING_FORMULAS, STAGE_RECOGNIZING_FORMULA
from backend.tools.formula_tools import normalize_latex
from backend.tools.logger import logger
from backend.tools.toolbox_math_tools import MathCapabilityUnavailable


@dataclass(frozen=True, slots=True)
class FormulaHooks:
    VisionAgent: Callable[..., Any]
    formula_recognition_crop: Callable[..., Any]
    toolbox_recognize_formula: Callable[..., Any]
    pages_label: Callable[..., Any]
    source_artifact_id: Callable[..., Any]


def _formula_recognition_crop(
    source_file: Path,
    element: ManifestElement,
    *,
    artifact_id: str | None = None,
    media_type: str | None = None,
    render_page: Callable[..., Any],
) -> tuple[bytes | None, int]:
    """Crop to feed the formula recognizer, or ``None`` when there is no usable box.

    A whole page is deliberately not a fallback: the chip models expect a
    formula and would invent one from a page of prose. A saved
    ``region_crop`` is reused when present, so a restored or resumed manifest
    does not pay another render; in the normal flow region inspection runs
    after planning, so a formula reaches this function without one (unless
    its text normalizes to empty). Everything else is rendered from the
    element's provenance box.
    """
    _toolbox_render = render_page

    provenance = element.provenance[0] if element.provenance else None
    page_number = (
        provenance.page_number if provenance is not None else (element.page_number or 0)
    )

    saved = element.metadata.get("region_crop")
    if isinstance(saved, str) and saved:
        try:
            return Path(saved).read_bytes(), page_number
        except OSError:
            logger.warning("Formula crop unreadable for {}: {}", element.id, saved)

    if page_number < 1:
        return None, 0
    clip, origin = _picture_clip(provenance)
    if clip is None:
        return None, page_number
    try:
        page = _toolbox_render(
            source_file,
            page_number,
            media_type=media_type,
            dpi=200,
            clip=clip,
            clip_origin=origin,
            artifact_id=artifact_id,
        )
    except Exception as exc:
        # A DOCX has no page to render, and the Toolbox may refuse the box;
        # both leave the formula without a crop instead of failing the job.
        logger.warning(
            "Formula crop failed for {} ({}): {}", element.id, type(exc).__name__, exc
        )
        return None, page_number
    left, top, right, bottom = page.clip
    if right - left < 4 or bottom - top < 4:
        return None, page_number
    return page.image, page_number


def _ranked_formula_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candidates with LaTeX, normalized, by decreasing confidence.

    ``math.recognize`` returns one entry per detected expression; a chip should
    hold one, but a provider may return several. A confidence that is not a
    finite number (missing, a bool, NaN, infinity) becomes ``None`` in the
    candidate and ranks as 0, so the order stays total and no such value
    reaches the manifest; ties keep the provider's order.
    """
    ranked: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        latex = normalize_latex(str(candidate.get("latex") or ""))
        if not latex:
            continue
        confidence = candidate.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
        ):
            confidence = None
        score = 0.0 if confidence is None else float(confidence)
        ranked.append((score, {**candidate, "latex": latex, "confidence": confidence}))
    ranked.sort(key=lambda pair: -pair[0])
    return [candidate for _, candidate in ranked]


#: Element types whose text the exporters render as prose with inline math.
_INLINE_MATH_ELEMENT_TYPES = frozenset({"paragraph", "list_item", "caption", "footnote"})
#: Points a span's centre may lie outside its element's box.
_INLINE_MATH_BOX_TOLERANCE = 2.0


class _TextSpanReader:
    """The text layer's spans of one document, each page fetched once (#260).

    The prose and table steps of the inline math detection run at different
    points of the pipeline; reading through one reader, a page is requested
    from the Toolbox once, and a capability that failed is not asked again.
    """

    def __init__(self, manifest: ProcessingManifest, source_path: Path) -> None:
        self._manifest = manifest
        self._source_path = source_path
        self._pages: dict[int, dict[str, Any]] = {}
        self._failed = False

    async def pages(self, wanted: Iterable[int]) -> dict[int, dict[str, Any]] | None:
        """The spans of every page read so far, ``wanted`` included; ``None`` when
        the Toolbox cannot read them (recorded as ``unavailable``)."""
        from acessilia_sdk.toolbox.errors import ToolboxError

        from backend.tools.text_spans import fetch_text_spans

        if self._failed:
            return None
        missing = sorted(set(wanted) - set(self._pages))
        if missing:
            try:
                self._pages.update(
                    await fetch_text_spans(
                        self._source_path, missing, artifact_id=_source_artifact_id(self._manifest)
                    )
                )
            except ToolboxError as exc:
                logger.info("Inline math detection skipped: document.text.spans failed ({})", exc)
                self._manifest.extractor.configuration["inline_math_detection"] = "unavailable"
                self._failed = True
                return None
        return self._pages


def _inline_math_enabled(manifest: ProcessingManifest) -> bool:
    from backend.config.settings import settings

    return (
        settings.inline_math_detection == "fonts"
        and manifest.source.media_type == "application/pdf"
    )


def _boxed_pages(elements: Iterable[ManifestElement]) -> list[int]:
    return sorted(
        {
            provenance.page_number
            for element in elements
            for provenance in element.provenance
            if provenance.bbox is not None
        }
    )


async def _mark_inline_math(
    manifest: ProcessingManifest,
    source_path: Path,
    text_spans: _TextSpanReader | None = None,
) -> dict[str, int]:
    """Write the inline math of PDF prose as ``$…$``, from the text layer's fonts (#265).

    Docling reports no inline formula region for a typeset PDF, so
    ``s(t) = t³`` arrives as ``s ( t ) = t 3``. The text layer still has the
    fonts and baselines (ADR 0011): the Toolbox's ``document.text.spans``
    returns them, ``docstruct.text.inline_math`` finds each run of math in an
    element's lines and rebuilds its LaTeX, and splices it into the element's
    text where both character sequences align. The canonical enrichment then
    converts each span (MathML, speech).

    A negation slash the run composed into its relation (``h \neq 0``) is
    recorded on its detached-slash element (``negation_overlay`` outcome
    ``inline-math``), so the later reattachment does not compose another
    ``=`` of the paragraph. Returns ``{elements, marked, unmatched}``; the
    detection is skipped for other media, with ``INLINE_MATH_DETECTION=off``
    and when the Toolbox cannot read the spans.
    """
    counts = {"elements": 0, "marked": 0, "unmatched": 0}
    if not _inline_math_enabled(manifest):
        return counts
    candidates = [
        element
        for element in manifest.elements
        if element.type in _INLINE_MATH_ELEMENT_TYPES
        and (element.text or "").strip()
        and "inline_math_detection" not in element.metadata
        and any(provenance.bbox is not None for provenance in element.provenance)
    ]
    pages = _boxed_pages(candidates)
    if not pages:
        return counts
    spans = await (text_spans or _TextSpanReader(manifest, source_path)).pages(pages)
    if spans is None:
        return counts

    heights = {page.page_number: page.height for page in manifest.pages}
    overlays = [element for element in manifest.elements if is_detached_overlay(element.text)]
    for element in candidates:
        detection = _detect_by_page(element, spans, heights)
        if not detection.runs:
            continue
        result = splice_inline_math(element.text or "", detection)
        counts["elements"] += 1
        counts["marked"] += len(result.marked)
        counts["unmatched"] += len(result.unmatched)
        element.metadata["inline_math_detection"] = {
            "marked": len(result.marked),
            "unmatched": len(result.unmatched),
        }
        if not result.marked:
            continue
        element.text = result.text
        for run in result.marked:
            for slash in run.negations:
                _claim_negation_overlay(overlays, element, slash, run.page, spans, heights)
    manifest.extractor.configuration["inline_math_detection"] = "fonts"
    if counts["elements"]:
        logger.info("Inline math from the text layer's fonts: {}", counts)
    return counts


async def _mark_table_inline_math(
    manifest: ProcessingManifest,
    source_path: Path,
    text_spans: _TextSpanReader | None = None,
) -> dict[str, int]:
    """Write the inline math of PDF table cells as ``$…$`` (#260).

    The prose step's detection over the table's text-layer lines, run after
    the table enrichment so a table read again keeps its marks. A table's
    cells carry no boxes: ``assign_runs_to_cells`` gives each run to the cell
    whose text holds it, and each cell is spliced on its own. A fraction set
    in a header cell (two stacked script-size lines) becomes ``\\frac``.
    Returns ``{elements, marked, unmatched}``, skipping as the prose step does.
    """
    counts = {"elements": 0, "marked": 0, "unmatched": 0}
    if not _inline_math_enabled(manifest):
        return counts
    tables = [
        element
        for element in manifest.elements
        if element.type == "table"
        and isinstance(element.metadata.get("table_ast"), dict)
        and "inline_math_detection" not in element.metadata
        and any(provenance.bbox is not None for provenance in element.provenance)
    ]
    pages = _boxed_pages(tables)
    if not pages:
        return counts
    spans = await (text_spans or _TextSpanReader(manifest, source_path)).pages(pages)
    if spans is None:
        return counts

    heights = {page.page_number: page.height for page in manifest.pages}
    overlays = [element for element in manifest.elements if is_detached_overlay(element.text)]
    for table in tables:
        detection = _detect_by_page(table, spans, heights)
        if not detection.runs:
            continue
        cells = [
            cell
            for section in _TABLE_SECTIONS
            for row in table.metadata["table_ast"].get(section) or []
            if isinstance(row, dict)
            for cell in row.get("cells") or []
            if isinstance(cell, dict) and isinstance(cell.get("text"), str)
        ]
        detections, left_out = assign_runs_to_cells(detection, [cell["text"] for cell in cells])
        marked, unmatched = [], list(left_out)
        for cell, cell_detection in zip(cells, detections):
            if not cell_detection.runs:
                continue
            result = splice_inline_math(cell["text"], cell_detection)
            cell["text"] = result.text
            marked.extend(result.marked)
            unmatched.extend(result.unmatched)
        counts["elements"] += 1
        counts["marked"] += len(marked)
        counts["unmatched"] += len(unmatched)
        table.metadata["inline_math_detection"] = {
            "marked": len(marked),
            "unmatched": len(unmatched),
        }
        for run in marked:
            for slash in run.negations:
                _claim_negation_overlay(overlays, table, slash, run.page, spans, heights)
    if counts["elements"]:
        logger.info("Inline math of table cells from the text layer's fonts: {}", counts)
    return counts


def _top_left_box(
    box: Any, page: int, spans: dict[int, dict[str, Any]], heights: dict[int, float | None]
) -> tuple[float, float, float, float] | None:
    """``(left, top, right, bottom)`` of a manifest box, top-left origin, in points."""
    if box.coord_origin == "BOTTOMLEFT":
        height = (spans.get(page) or {}).get("height") or heights.get(page)
        if not height:
            return None
        return box.left, height - box.top, box.right, height - box.bottom
    if box.coord_origin == "TOPLEFT":
        return box.left, box.top, box.right, box.bottom
    return None


def _detect_by_page(
    element: ManifestElement,
    spans: dict[int, dict[str, Any]],
    heights: dict[int, float | None],
) -> InlineMathDetection:
    """``detect_inline_math`` over the element's lines, page by page, as one detection.

    Each page is read on its own, so a fraction never pairs lines of two pages
    (baselines are per-page coordinates), and each run carries its page, where
    its negation slashes are claimed: a table is the element that spans pages.
    """
    detection = InlineMathDetection()
    pages = dict.fromkeys(p.page_number for p in element.provenance if p.bbox is not None)
    for page in pages:
        read = detect_inline_math(_element_span_lines(element, spans, heights, page))
        offset = len(detection.flat)
        detection.flat += read.flat
        detection.runs.extend(
            replace(run, start=run.start + offset, end=run.end + offset, page=page)
            for run in read.runs
        )
    return detection


def _element_span_lines(
    element: ManifestElement,
    spans: dict[int, dict[str, Any]],
    heights: dict[int, float | None],
    page_number: int,
) -> list[list[TextSpan]]:
    """The element's text-layer lines on a page, in order: spans whose centre lies in its boxes.

    A rotated page is left out: its spans are in the unrotated space, which the
    element's box may not be.
    """
    lines: list[list[TextSpan]] = []
    for provenance in element.provenance:
        if provenance.page_number != page_number:
            continue
        page = spans.get(provenance.page_number)
        if provenance.bbox is None or page is None or page.get("rotation"):
            continue
        box = _top_left_box(provenance.bbox, provenance.page_number, spans, heights)
        if box is None:
            continue
        left, top, right, bottom = box
        top, bottom = min(top, bottom), max(top, bottom)
        margin = _INLINE_MATH_BOX_TOLERANCE
        for line in page.get("lines") or []:
            inside = []
            for span in line.get("spans") or []:
                x0, y0, x1, y1 = span["bbox"]
                x, y = (x0 + x1) / 2, (y0 + y1) / 2
                if left - margin <= x <= right + margin and top - margin <= y <= bottom + margin:
                    inside.append(TextSpan.from_dict(span))
            if inside:
                lines.append(inside)
    return lines


def _claim_negation_overlay(
    overlays: list[ManifestElement],
    host: ManifestElement,
    slash: tuple[float, float, float, float],
    page: int,
    spans: dict[int, dict[str, Any]],
    heights: dict[int, float | None],
) -> None:
    """Record the detached slash at ``slash`` as composed by the inline math of ``host``."""
    x, y = (slash[0] + slash[2]) / 2, (slash[1] + slash[3]) / 2
    for overlay in overlays:
        if "negation_overlay" in overlay.metadata:
            continue
        located = _bbox(overlay, page)
        if located is None:
            continue
        box = _top_left_box(located[1], page, spans, heights)
        if box is None:
            continue
        left, top, right, bottom = box
        top, bottom = min(top, bottom), max(top, bottom)
        margin = _OVERLAY_HOST_TOLERANCE * 2
        if left - margin <= x <= right + margin and top - margin <= y <= bottom + margin:
            overlay.metadata["negation_overlay"] = {"outcome": "inline-math", "host": host.id}
            return


def _reclassify_formula_picture(
    manifest: ProcessingManifest,
    element: ManifestElement,
    output: VisionOutput,
    image: bytes,
    *,
    source: str,
) -> None:
    """Keep the source evidence and reuse a non-mathematical description.

    The crop the model read becomes the picture (``metadata.src``), as for a
    region inspection, so the export shows the drawing it describes.
    ``source`` records which call reclassified it: the candidate inspection
    or the vision fallback of the formula recognition.
    """
    element.metadata["original_type"] = element.type
    element.metadata["original_text"] = element.text
    element.metadata["original_raw_label"] = element.raw_label
    element.type = "picture"
    element.raw_label = "picture"
    element.text = output.description
    element.metadata["vision_description_complete"] = True
    element.metadata["vision_language"] = output.language
    element.metadata["vision_confidence"] = output.confidence
    element.metadata["vision_elements"] = list(output.mentioned_elements)
    element.metadata["vision_warnings"] = list(output.warnings)
    element.metadata["src"] = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    element.metadata["formula_classification"] = {
        "status": "picture",
        "reason": source,
        "confidence": output.confidence,
        "warnings": list(output.warnings),
    }
    reconcile_reclassified_element_processing_needs(manifest, element, previous_type="formula")


def _escalate_unresolved_formula(
    manifest: ProcessingManifest, element: ManifestElement
) -> None:
    """Hand a region the model could not classify to a person, not to the math path.

    Its ``verbalize-formula`` goes to ``human-review``; ``normalize-formula``
    is dropped, since normalizing LaTeX the model could not tell is a formula
    would treat it as one.
    """
    retained = []
    for obligation in manifest.obligations:
        if obligation.target_ids == [element.id]:
            if obligation.kind in TYPE_BOUND_V23_KINDS["formula"]:
                continue
            if obligation.kind == "verbalize-formula":
                obligation.admissible_methods = ["human-review"]
                obligation.method_costs = {"human-review": 100}
        retained.append(obligation)
    manifest.obligations = retained
    manifest.summary.obligation_count = len(manifest.obligations)


async def _classify_formula_regions(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    mode: str,
    breaker: ProviderBreaker,
    hooks: FormulaHooks,
) -> None:
    """Check extractor formula labels before a mathematical recognizer sees them.

    A drawing is described once and reconciled as a picture before planning.
    A candidate the model explicitly cannot classify (``classification_uncertain``)
    keeps its source content and goes to human review. A call that gives no
    answer (open breaker, no crop, model failure, empty description) is no
    evidence against mathematics: the candidate stays ``unclassified`` and
    takes the formula path it took before this check, whose vision fallback
    can still reclassify it.
    """
    VisionAgent = hooks.VisionAgent
    _formula_recognition_crop = hooks.formula_recognition_crop
    _pages_label = hooks.pages_label
    _source_artifact_id = hooks.source_artifact_id

    from backend.config.settings import settings

    if manifest.source.media_type not in _RENDERABLE_MEDIA_TYPES:
        return
    candidates = [
        element for element in manifest.elements
        if element.type == "formula"
        and not has_latex_markup(element.text)
        and not element.metadata.get("latex_markup")
    ]
    if not candidates:
        return
    slots = asyncio.Semaphore(max(1, settings.vision_concurrency))
    vision = VisionAgent(mode=mode)

    async def classify(element: ManifestElement) -> None:
        async with slots:
            output = None
            errors: list[str] = []
            reason = "provider_blocked"
            if not breaker.open:
                reason = "crop_unavailable"
                image, page = await run_blocking(
                    _formula_recognition_crop, source_file, element,
                    artifact_id=_source_artifact_id(manifest),
                    media_type=manifest.source.media_type,
                )
                if image:
                    reason = "model_failed"
                    output = await vision.describe_region(
                        image_bytes=image, classification="formula_candidate",
                        page_num=page, total_pages=len(manifest.pages), mode=mode,
                        full_budget=True, errors=errors,
                    )
                    breaker.record(isinstance(output, VisionOutput))
            status = "unclassified"
            if isinstance(output, VisionOutput):
                if output.classification_uncertain:
                    status, reason = "unresolved", "classification_uncertain"
                elif output.kind == "formula":
                    status, reason = "math", None
                elif output.description.strip():
                    _reclassify_formula_picture(
                        manifest, element, output, image, source="candidate_inspection"
                    )
                    return
                else:
                    reason = "empty_description"
            element.metadata["formula_classification"] = {
                "status": status,
                "reason": reason,
                "confidence": output.confidence if output is not None else None,
                "warnings": list(output.warnings) if output is not None else errors,
            }
            if status == "unresolved":
                _escalate_unresolved_formula(manifest, element)

    async with asyncio.TaskGroup() as group:
        for element in candidates:
            group.create_task(classify(element))


async def _enrich_formula_latex(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    mode: str,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    breaker: ProviderBreaker | None = None,
    hooks: FormulaHooks,
) -> None:
    """Give LaTeX to formulas the extractor delivered without it.

    That covers formulas with no text and, in born-digital PDFs, formulas
    whose text is the PDF text layer (``s(t) = s0 + v0t + 1 2 at2``): it
    keeps the symbols but not the fractions, exponents and limits, so MathML
    and speech built from it lose the structure. ``has_latex_markup`` tells
    the two apart.

    ``math.recognize`` (PP-FormulaNet) runs first; the vision model is the
    fallback, so a Toolbox outage degrades to the behavior the pipeline had
    before instead of leaving the formula without content. Recognition runs
    before planning, like the picture descriptions, so the ``mathml`` and
    ``latex-verbalizer`` methods then work on real LaTeX.

    When the formula has text-layer text, a recognition must agree with it
    (``latex_text_agreement`` ≥ ``FORMULA_TEXT_AGREEMENT_MIN``): PP-FormulaNet
    reports no confidence, and the text layer is the only independent reading
    of the same formula. A recognition below it is a misread; the next one is
    tried, and when none agrees the formula keeps its text-layer version, which
    is flat but faithful. The text layer is the reference, not the truth: a
    garbled one can refuse a correct reading, which ``formula_text_layer`` and
    ``rejected`` leave on record.

    During a ``math.recognize`` outage, text-layer formulas also go to the
    vision model, so an outage now costs one vision call per formula that has
    text, not only per empty one. That is deliberate: on the reference case the
    vision readings took the structure of 16 flat formulas from nothing to a
    98.4 structure-token recall. When vision disagrees with the text layer too,
    the call bought nothing and the formula stays flat.

    A provider can also answer without LaTeX, which is not an outage: a
    page-level one such as ``docling-math`` finds no formula in a chip, so
    every formula goes to vision. Each such fallback is logged at DEBUG, and
    one INFO line per document counts them and names the provider, so the
    misconfiguration reads in one line instead of one per formula.

    The vision fallback shares the job's provider breaker with the picture
    and table stages: a provider those stages declared down gets no call
    from here and each formula degrades to its text-layer text at once.

    Each recognized formula records ``metadata["formula_recognition"]``:
    ``method`` (the capability that gave the LaTeX: ``math.recognize`` or
    ``vision``; ``text-layer`` when nothing agreed), ``confidence``,
    ``text_agreement`` when there was text-layer text, ``rank`` when a
    ``math.recognize`` candidate other than the best-ranked one was used, and
    ``rejected``: refused readings keyed by method, with ``#n`` for the n-th
    ranked candidate (``math.recognize#2``), valued by their agreement
    (``None`` for no evidence). Consumers must read those keys as
    ``method`` or ``method#rank`` strings.

    Crops are independent, so they run concurrently, bounded by
    ``VISION_CONCURRENCY`` for the same reason as the picture descriptions:
    the vision fallback shares that provider budget. An outage is still paid
    once per document: the first ``MathCapabilityUnavailable`` sets a flag the
    remaining formulas read before calling recognition, and the probe itself
    is serialized so concurrent crops cannot each pay the same timeout.

    Two formulas on the same page still pay two renders: the Toolbox applies
    the clip, and caching a whole page to crop it locally would move pixel
    work into the core (ADR-0003), so page-level render reuse stays a
    follow-up.
    """
    VisionAgent = hooks.VisionAgent
    _formula_recognition_crop = hooks.formula_recognition_crop
    toolbox_recognize_formula = hooks.toolbox_recognize_formula
    _pages_label = hooks.pages_label
    _source_artifact_id = hooks.source_artifact_id

    from acessilia_sdk.toolbox.capabilities import provider_default

    from backend.config.settings import settings

    for element in manifest.elements:
        separate_equation_number(element)
    formulas = [
        element
        for element in manifest.elements
        if element.type == "formula"
        and not has_latex_markup(element.text)
        and not element.metadata.get("latex_markup")
        and element.metadata.get("formula_classification", {}).get("status") != "unresolved"
    ]
    if not formulas:
        return
    # Without a page to render there is no crop to recognize. DOCX/PPTX
    # formulas stay as the extractor delivered them instead of paying one
    # failed render per formula.
    if manifest.source.media_type not in _RENDERABLE_MEDIA_TYPES:
        return

    if status_callback:
        await status_callback(t(STAGE_ENRICHING_FORMULAS))

    vision = VisionAgent(mode=mode)
    breaker = breaker or ProviderBreaker(settings.vision_breaker_threshold)
    total_pages = len(manifest.pages)
    artifact_id = _source_artifact_id(manifest)
    slots = asyncio.Semaphore(max(1, settings.vision_concurrency))
    progress_lock = asyncio.Lock()
    math_probe = asyncio.Lock()
    math_ready = asyncio.Event()
    math_available = True
    # Resolved like ``ToolboxCapabilityClient``: an empty setting means the
    # registry default, which is the provider the request actually asked.
    recognize_provider = settings.toolbox_math_recognize_provider or provider_default(
        "toolbox_math_recognize_provider"
    )
    empty_answers = 0

    async def call(image_bytes: bytes) -> list[dict[str, Any]]:
        """One recognition call; an outage turns the flag off and means no LaTeX."""
        nonlocal math_available
        try:
            return await run_blocking(
                toolbox_recognize_formula,
                image_bytes,
                raise_on_unavailable=True,
            )
        except MathCapabilityUnavailable as exc:
            math_available = False
            # The full message names the provider and its HTTP status; the
            # class alone ("ToolboxError") hides a failing sidecar.
            logger.warning(t(LOG_PDDL_FORMULA_RECOGNIZE_UNAVAILABLE).format(error=exc))
            return []

    async def recognize(image_bytes: bytes) -> list[dict[str, Any]]:
        """Recognize one crop, unless the document already found the outage.

        The first call is serialized and every other crop waits for its
        answer, so a batch that starts with the capability down pays the
        outage once instead of once per crop; after it succeeds, the remaining
        calls only read the flag and run concurrently.
        """
        if not math_ready.is_set():
            async with math_probe:
                if not math_ready.is_set():
                    try:
                        return await call(image_bytes)
                    finally:
                        math_ready.set()
        if not math_available:
            return []
        return await call(image_bytes)

    async def enrich_one(position: int, element: ManifestElement) -> bool:
        """Recognize one formula and fold the answer into it; True when filled."""
        nonlocal empty_answers
        async with slots:
            if status_callback:
                async with progress_lock:
                    await status_callback(
                        t(STAGE_RECOGNIZING_FORMULA).format(
                            current=position,
                            total=len(formulas),
                            pages=_pages_label([element.page_number], total_pages),
                        )
                    )
            image_bytes, page_number = await run_blocking(
                _formula_recognition_crop,
                source_file,
                element,
                artifact_id=artifact_id,
                media_type=manifest.source.media_type,
            )
            if not image_bytes:
                return False

            original_text = element.text or ""
            text_layer = normalize_latex(original_text)
            # None records a recognition refused for lack of evidence.
            rejected: dict[str, float | None] = {}
            agreement: float | None = None

            def accept(method: str, latex: str) -> str:
                """``latex`` when it agrees with the text layer, else ``""``.

                With text-layer text but no visible symbol on either side
                (``()``), there is no evidence the recognition read the same
                formula, so it is refused like a disagreement.
                """
                nonlocal agreement
                if not latex or not text_layer:
                    return latex
                expression, _ = split_equation_number(latex)
                score = latex_text_agreement(expression, text_layer)
                if score is None or score < settings.formula_text_agreement_min:
                    rejected[method] = None if score is None else round(score, 3)
                    return ""
                agreement = round(score, 3)
                return latex

            # Every candidate gets its turn at the gate before vision is paid:
            # a lower-ranked one that agrees with the text layer is still a
            # recognition of this crop. Refusals are recorded per rank
            # ("math.recognize", "math.recognize#2", ...).
            method = "math.recognize"
            latex = ""
            confidence = None
            accepted_rank = 1
            candidates = _ranked_formula_candidates(await recognize(image_bytes))
            if not candidates and math_available:
                # An outage already warned once and skips this. An empty list
                # is usually an answer without LaTeX, which nothing else
                # records; a rejected crop or a transport error reaches it too,
                # but those were logged by ``toolbox_recognize_formula``. No
                # await separates ``recognize`` from this check, so the flag
                # and the counter cannot race with sibling crops.
                empty_answers += 1
                logger.debug(
                    t(LOG_PDDL_FORMULA_RECOGNIZE_EMPTY).format(
                        element_id=element.id, page=page_number, provider=recognize_provider,
                    )
                )
            for rank, candidate in enumerate(candidates, start=1):
                latex = accept(method if rank == 1 else f"{method}#{rank}", candidate["latex"])
                if latex:
                    confidence = candidate["confidence"]
                    accepted_rank = rank
                    break

            if not latex:
                method = "vision"
                # The pictures and tables share the breaker, and they run
                # around this stage: once the provider is declared down, the
                # formula keeps its text-layer version (the behavior before
                # the fallback) instead of paying two more 600 s deadlines.
                # Only the vision fallback is gated; ``math.recognize`` is
                # probed once per document through its own flag above.
                output: VisionOutput | None = None
                if not breaker.open:
                    output = await vision.describe_region(
                        image_bytes=image_bytes,
                        classification="formula",
                        page_num=page_number,
                        total_pages=total_pages,
                        mode=mode,
                        full_budget=True,
                    )
                    # A non-mathematical description can also correct the label.
                    breaker.record(output is not None)
                if (
                    output is not None and output.kind == "description"
                    and output.description.strip() and not output.classification_uncertain
                ):
                    _reclassify_formula_picture(
                        manifest, element, output, image_bytes, source="recognition_fallback"
                    )
                    return False
                if output is not None and output.kind == "formula":
                    latex = accept(method, normalize_latex(output.formula_latex or ""))
                    confidence = output.confidence

        if rejected:
            logger.info(
                t(LOG_PDDL_FORMULA_TEXT_LAYER_DISAGREES).format(
                    element_id=element.id, page=page_number, rejected=rejected,
                )
            )
        if not latex:
            if rejected:
                element.metadata["formula_recognition"] = {
                    "method": "text-layer",
                    "rejected": rejected,
                }
            return False
        recognition: dict[str, Any] = {"method": method, "confidence": confidence}
        if method == "math.recognize" and accepted_rank > 1:
            recognition["rank"] = accepted_rank
        if text_layer:
            # The replaced text exactly as the extractor delivered it.
            element.metadata["formula_text_layer"] = original_text
            recognition["text_agreement"] = agreement
        if rejected:
            recognition["rejected"] = rejected
        element.text = preserve_equation_number(latex, element.metadata)
        element.metadata["formula_recognition"] = recognition
        return True

    # The fold above touches only its own element and runs without awaits, so
    # concurrent folds cannot interleave. The TaskGroup cancels the sibling
    # crops when one of them fails and re-raises as an ExceptionGroup, like the
    # picture descriptions; no task keeps mutating the manifest after it failed.
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(enrich_one(position, element))
            for position, element in enumerate(formulas, start=1)
        ]
    recognized = sum(task.result() for task in tasks)

    if recognized:
        logger.info(t(LOG_PDDL_FORMULAS_RECOGNIZED).format(count=recognized))
    if empty_answers:
        logger.info(
            t(LOG_PDDL_FORMULAS_FELL_BACK_TO_VISION).format(
                count=empty_answers, total=len(formulas), provider=recognize_provider,
            )
        )
