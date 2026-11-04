"""Canonical PDDL document delivery and legacy payload compatibility.

The functions here were moved unchanged from pddl_orchestrator: output
semantics, warnings, metadata and canonical block order must remain stable.
The orchestrator imports and re-exports the previous public/private names.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from docstruct.blocks.callouts import dedupe_callout_titles as _dedupe_callout_titles
from docstruct.blocks.lists import marker_numbering
from docstruct.fusion.similarity import PAGENUM_RE
from docstruct.fusion.tailjoin import is_page_number as _is_page_number
from docstruct.geometry import reading_role
from docstruct.tables.ast import raw_rows_from_table_ast as _rows_from_table_ast

from backend.core.execution.models import ExecutionReport
from backend.core.execution.recovery import (
    NON_CRITICAL_OBLIGATION_KINDS,
    _is_open_obligation,
    _unsatisfied_summary,
)
from backend.core.manifest.builder import is_unstructured_page_placeholder
from backend.core.manifest.element_geometry import _element_bbox, _element_role
from backend.core.manifest.models import ManifestElement, ProcessingManifest
from backend.core.manifest.table_reading import _table_ast_from_metadata, _table_ast_from_rows
from backend.core.planning.models import NominalPlan, PlanningComparison
from backend.i18n import locale_for_user, n, reset_request_locale, set_request_locale, t
from backend.log_messages import LOG_PDDL_EMPTY_LIST_ITEMS_DROPPED, MSG_STRUCTURAL_EXTRACTION_DEGRADED
from backend.tools.code_tools import normalize_code_text
from backend.tools.logger import logger


#: Canonical English msgid for the alt text of a picture the vision model left
#: undescribed; why it was left so goes to the operator, not the reader.
MSG_UNDESCRIBED_PICTURE_ALT: str = "Image without a text description."

#: Canonical English msgid for an institutional header read as one paragraph:
#: the logo, then its header lines; {lines} is substituted by callers after lookup.
MSG_LETTERHEAD_TEXT: str = "Logo. {lines}."

#: Canonical English singular/plural msgids for the document's warning on the
#: pictures and tables a failure left without a description or a reading.
MSG_PICTURES_NOT_DESCRIBED: tuple[str, str] = (
    "{count} image could not be described.",
    "{count} images could not be described.",
)
MSG_TABLES_NOT_READ: tuple[str, str] = (
    "{count} table could not be read.",
    "{count} tables could not be read.",
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
    pages_payload = _manifest_pages_to_payload(manifest)
    text_output = _render_text_from_pages(pages_payload)
    configuration = manifest.extractor.configuration
    media_inspection_status = configuration.get("media_inspection")
    fusion_status = configuration.get("fusion") or {}
    unsatisfied_obligations = _unsatisfied_obligations(manifest)

    technical_warnings: list[str] = []
    if execution_report is None:
        technical_warnings.append(
            "Executor nao foi executado; somente manifesto e plano foram gerados."
        )
    elif execution_report.status in {
        "failed", "replan-required",
    } or unsatisfied_obligations:
        # A replan that leaves an exhausted refinement out can still report
        # `completed`; the open obligation must reach the document anyway. The
        # ids are in `pddl_unsatisfied_obligations`, so the warning stays short.
        technical_warnings.append(_unsatisfied_summary(manifest))

    if comparison is not None:
        technical_warnings.append(
            f"Comparacao de planners: {comparison.comparison.verdict}."
        )

    visual_failures = _visual_failures(manifest)
    for kind, msgids in (("pictures", MSG_PICTURES_NOT_DESCRIBED), ("tables", MSG_TABLES_NOT_READ)):
        if count := sum((visual_failures or {}).get(kind, {}).values()):
            technical_warnings.append(n(*msgids, count).format(count=count))

    observations = getattr(manifest, "observations", [])
    obligations = getattr(manifest, "obligations", [])
    if any(
        observation.kind == "repair-legacy-unicode-detected"
        for observation in observations
    ) and not any(
        obligation.kind == "repair-legacy-unicode"
        for obligation in obligations
    ):
        # Only the observation exists: LEGACY_UNICODE_REPAIR=detect. The
        # warning names the switch because the job delivers the text as
        # extracted, NFD and dotless-i included.
        technical_warnings.append(
            "Texto com sinais de Unicode legado (NFD, i sem ponto ou acento espaçador); "
            "reparo desabilitado (LEGACY_UNICODE_REPAIR=detect)."
        )

    extraction_degraded = records_extraction_degradation(
        configuration.get("extraction_degraded")
    )
    if extraction_degraded:
        technical_warnings.append(
            t(MSG_STRUCTURAL_EXTRACTION_DEGRADED).format(
                provider=configuration.get("toolbox_provider", manifest.extractor.name)
            )
        )

    return {
        "text": text_output,
        "pages": pages_payload,
        "page_count": len(pages_payload),
        "mode": f"pddl-{planner_backend}",
        "source_path": str(file_path),
        "canonical_metadata": {
            "pipeline_engine": "pddl",
            "pddl_validation_only": True,
            "accessibility_verified": False,
            "pddl_manifest_id": manifest.manifest_id,
            "pddl_manifest_revision": manifest.revision,
            "pddl_plan_id": plan.plan_id,
            "pddl_planner": plan.planner,
            "pddl_expected_total_cost": plan.expected_total_cost,
            "pddl_execution_id": execution_report.execution_id
            if execution_report
            else None,
            "pddl_execution_status": execution_report.status
            if execution_report
            else None,
            "pddl_comparison_verdict": comparison.comparison.verdict
            if comparison
            else None,
            "pddl_unsatisfied_obligations": unsatisfied_obligations,
            # The open obligations a delivered document still has, for the
            # result e-mail: the job page and e-mail show no technical warning.
            "pddl_unsatisfied_summary": _unsatisfied_summary(manifest)
            if unsatisfied_obligations
            else None,
            "pddl_toolbox_availability": toolbox_availability,
            "pddl_fusion": fusion_status or None,
            **(
                {
                    "pddl_extraction_degraded": True,
                    "pddl_extraction_failures": configuration.get("extraction_failures", {}),
                }
                if extraction_degraded else {}
            ),
            # Pictures and tables left undescribed or unread, by reason; a rerun
            # once the model provider or the Toolbox is back may deliver them.
            "pddl_visual_failures": visual_failures,
            "pddl_media_inspection": {
                "status": media_inspection_status,
                "error": configuration.get("media_inspection_error"),
                # Why a deck was structured slide by slide (or not).
                "document_genre": configuration.get("document_genre"),
                "genre_signals": configuration.get("genre_signals", []),
            }
            if media_inspection_status is not None
            else None,
        },
        "technical_warnings": technical_warnings,
        # A step degraded by a Toolbox or model failure must not be replayed
        # from the cache once the service is back.
        "cacheable": (
            media_inspection_status != "failed"
            and fusion_status.get("status") != "degraded"
            and execution_report is not None
            and execution_report.status in {"completed", "dry-run-completed"}
            and not any(
                item["recovery"] != "human-review" for item in unsatisfied_obligations
            )
            and not records_visual_failures(visual_failures)
            and not extraction_degraded
        ),
    }


#: Why a picture or table left without a description or a reading may come
#: out differently on a rerun: the model provider or the Toolbox was down. An
#: empty description is the model's answer, as is a refused table reading, and
#: would come back the same, so it neither counts nor keeps a run uncached.
_RETRYABLE_VISUAL_FAILURES = frozenset({"crop_unavailable", "model_failed"})


def _visual_failures(manifest: ProcessingManifest) -> dict[str, dict[str, int]] | None:
    """Pictures left undescribed and tables left unread, counted by reason.

    A picture counts whatever its type: the vision stage also describes the
    placeholders of a mostly visual document, and an element becomes a
    ``picture`` only once the model answers. None when nothing failed.
    """
    elements = getattr(manifest, "elements", [])
    counts = {
        "pictures": Counter(
            reason for element in elements
            if (reason := element.metadata.get("vision_failure_reason")) in _RETRYABLE_VISUAL_FAILURES
        ),
        "tables": Counter(
            reason for element in elements
            if (reason := element.metadata.get("table_read_failure")) in _RETRYABLE_VISUAL_FAILURES
        ),
    }
    failures = {kind: dict(by_reason) for kind, by_reason in counts.items() if by_reason}
    return failures or None


def records_visual_failures(failures: Any) -> bool:
    """Whether ``canonical_metadata.pddl_visual_failures`` records any failure.

    A run that does is not cached, and a cached entry that does is not
    replayed; both sides read the record through this one check.
    """
    return isinstance(failures, dict) and any(
        isinstance(by_reason, dict)
        and any(isinstance(count, int) and count > 0 for count in by_reason.values())
        for by_reason in failures.values()
    )


def records_extraction_degradation(flag: Any) -> bool:
    """Whether the structural extraction came from one provider of a dual run.

    ``flag`` is the extractor's ``extraction_degraded`` or, on a cached entry,
    ``canonical_metadata.pddl_extraction_degraded``. A run that records it is
    not cached, and a cached entry that does is not replayed.
    """
    return flag is True


def _unsatisfied_obligations(manifest: ProcessingManifest) -> list[dict[str, Any]]:
    """Selected obligations the execution left unsatisfied.

    `pddl_execution_status` alone does not say which obligations were exhausted
    or escalated, so each one is reported with its last attempt. An obligation
    whose last attempt is `human-review` is a pending escalation.
    """
    unsatisfied: list[dict[str, Any]] = []
    for obligation in manifest.obligations:
        if not _is_open_obligation(obligation):
            continue
        last = obligation.attempts[-1] if obligation.attempts else None
        unsatisfied.append(
            {
                "id": obligation.id,
                "kind": obligation.kind,
                "status": obligation.status,
                "target_ids": list(obligation.target_ids),
                "last_method": last.method if last else None,
                "last_message": last.message if last else None,
                "escalated": bool(last and last.method == "human-review"),
                "recovery": (
                    "human-review" if last and last.method == "human-review"
                    and obligation.kind in NON_CRITICAL_OBLIGATION_KINDS else "retry"
                ),
            }
        )
    return unsatisfied


def _is_left_out_of_payload(element: ManifestElement) -> bool:
    """A page accepted as blank, or a placeholder not yet inspected, adds nothing.

    Nor does a detached negation slash: it was given back to its relation, or
    could not be, and alone it says nothing to a reader.

    An inspected page with content is a ``picture``, no longer a placeholder.
    A placeholder still present means the inspection did not run (dry-run), so
    the page contributes no block rather than its raw label.
    """
    return (
        bool(element.metadata.get("blank_page"))
        or bool(element.metadata.get("negation_overlay"))
        or is_unstructured_page_placeholder(element)
    )


#: Types a page number can arrive as. A number inside a table, list, heading
#: or formula is content, wherever it sits on the page.
_PAGE_NUMBER_TYPES = frozenset({"paragraph", "page_header", "page_footer", "unknown"})
#: Share of the page height at the top and at the bottom where a lone number
#: is read as a page number (the margin `docstruct.fusion.noise.decor_role`
#: uses for the same decision).
_PAGE_NUMBER_MARGIN = 0.12
#: Longest furniture text read as a decorated page number (``- 5 -``,
#: ``Page 5``), as in `decor_role`.
_PAGE_NUMBER_MAX_LEN = 16


def _is_decorated_page_number(text: str) -> bool:
    """A page number with the decoration or repetition extractors leave on it.

    Accepts ``- 5 -``, ``5.`` and ``Page 5`` (``PAGENUM_RE``, the matcher of
    `decor_role`) and the same number repeated (``281 281``), but only digits
    and the Roman letters `is_page_number` knows, so ``mid`` is not a number.
    """
    text = text.strip()
    match = PAGENUM_RE.match(text) if len(text) <= _PAGE_NUMBER_MAX_LEN else None
    if match and _is_page_number(match.group(1)):
        return True
    tokens = set(text.split())
    return len(tokens) == 1 and _is_page_number(tokens.pop())


def _is_marked_furniture(element: ManifestElement) -> bool:
    """Whether the extractor marked ``element`` as page header/footer.

    Read from the type, the raw label (an element typed ``paragraph`` may keep
    ``page_footer`` there) or Docling's ``content_layer``.
    """
    return (
        _element_role(element) in {"header", "footer", "page_number"}
        or reading_role(str(element.type), str(element.raw_label)) != "body"
        or element.metadata.get("content_layer") == "furniture"
    )


def _is_page_number_in_margin(
    element: ManifestElement, manifest: ProcessingManifest
) -> bool:
    """A bare number in the top or bottom margin, which may be a page number.

    A single uppercase Roman letter (``I``, ``V``, ``X``...) is read as text.
    """
    text = (element.text or "").strip()
    if not _is_page_number(text) or (len(text) == 1 and text.isupper()):
        return False
    bbox = _element_bbox(element, manifest)
    if bbox is None:
        return False
    center = (bbox[1] + bbox[3]) / 2
    return not _PAGE_NUMBER_MARGIN <= center <= 1 - _PAGE_NUMBER_MARGIN


def _page_number_ids(manifest: ProcessingManifest) -> set[str]:
    """Elements that are page numbers rather than body text.

    A screen reader would announce them in the middle of the text. A number
    the extractor marked as furniture is one. Otherwise only its position in
    the top or bottom margin tells it apart from a number of the body, and a
    page has one page number: the margin decides only on a page without a
    marked one and with a single candidate, so chart axis labels or a footnote
    marker next to the page number stay.
    """
    marked: set[str] = set()
    #: page number -> its page number candidates, marked or in the margin.
    candidates: dict[int, list[str]] = {}
    for element in manifest.elements:
        if element.type not in _PAGE_NUMBER_TYPES:
            continue
        page = element.page_number or 1
        if _is_marked_furniture(element):
            if _is_decorated_page_number(element.text or ""):
                marked.add(element.id)
                candidates.setdefault(page, []).append(element.id)
        elif _is_page_number_in_margin(element, manifest):
            candidates.setdefault(page, []).append(element.id)
    for ids in candidates.values():
        if len(ids) == 1:
            marked.update(ids)
    return marked


#: Height share of the page by which a picture may miss a header line and
#: still sit beside it (the line's box is tighter than the logo's).
_LETTERHEAD_TOLERANCE = 0.01
#: Width share of the page a header line may stand away from the picture and
#: still be beside it; a running header across the page is not.
_LETTERHEAD_MAX_GAP = 0.05
#: Tallest picture, as a share of the page height, read as a logo: a figure
#: crossing the header band is content, and keeps its description.
_LETTERHEAD_MAX_HEIGHT = _PAGE_NUMBER_MARGIN


def _is_page_header_line(
    element: ManifestElement, bbox: tuple[float, ...]
) -> bool:
    """A non-empty line the extractor marked as page header.

    Read from the type and raw label, like the page number filter. Docling's
    ``content_layer`` does not tell header from footer, so a ``furniture``
    line counts only in the top margin.
    """
    if not (element.text or "").strip():
        return False
    if (
        _element_role(element) == "header"
        or reading_role(str(element.type), str(element.raw_label)) == "header"
    ):
        return True
    return (
        element.metadata.get("content_layer") == "furniture"
        and (bbox[1] + bbox[3]) / 2 < _PAGE_NUMBER_MARGIN
    )


def _is_beside(picture_box: tuple[float, ...], line_box: tuple[float, ...]) -> bool:
    """Whether a header line overlaps the picture's height and stands next to it."""
    horizontal_gap = max(line_box[0] - picture_box[2], picture_box[0] - line_box[2])
    return (
        line_box[1] <= picture_box[3] + _LETTERHEAD_TOLERANCE
        and picture_box[1] <= line_box[3] + _LETTERHEAD_TOLERANCE
        and horizontal_gap <= _LETTERHEAD_MAX_GAP
    )


def _letterhead_groups(
    manifest: ProcessingManifest, page_numbers: set[str]
) -> dict[str, list[ManifestElement]]:
    """Logos and the page header lines beside them, keyed by the first in reading order.

    An institutional header ("University of X / Department of Y" next to the
    logo) reaches the manifest as page_header lines and an unrelated picture,
    so the reader got the lines apart and a long description of a logo that
    repeats them. A logo-sized picture joins the header lines beside it; two
    pictures form one group only when they share a line, so two institutions'
    headers on a page stay apart. A page header without a picture is left as is.
    """
    #: page number -> its header lines and logo-sized pictures, with their boxes.
    lines: dict[int, list[tuple[ManifestElement, tuple[float, ...]]]] = {}
    pictures: dict[int, list[tuple[ManifestElement, tuple[float, ...]]]] = {}
    for element in manifest.elements:
        if _is_left_out_of_payload(element) or element.id in page_numbers:
            continue
        bbox = _element_bbox(element, manifest)
        if bbox is None:
            continue
        page = element.page_number or 1
        if element.type == "picture":
            if bbox[3] - bbox[1] <= _LETTERHEAD_MAX_HEIGHT:
                pictures.setdefault(page, []).append((element, bbox))
        elif _is_page_header_line(element, bbox):
            lines.setdefault(page, []).append((element, bbox))

    groups: dict[str, list[ManifestElement]] = {}
    for page, page_pictures in pictures.items():
        components: list[dict[str, ManifestElement]] = []
        for picture, picture_box in page_pictures:
            beside = [
                line
                for line, line_box in lines.get(page, [])
                if _is_beside(picture_box, line_box)
            ]
            if not beside:
                continue
            component = {picture.id: picture}
            component.update((line.id, line) for line in beside)
            for other in [item for item in components if item.keys() & component.keys()]:
                component.update(other)
                components.remove(other)
            components.append(component)
        for component in components:
            ordered = sorted(component.values(), key=lambda item: item.reading_order)
            groups[ordered[0].id] = ordered
    return groups


def _letterhead_block(
    members: list[ManifestElement], language: str | None
) -> dict[str, Any]:
    """One paragraph for a logo and its header lines, in place of the first of them.

    The paragraph replaces the picture too: the outputs never showed the
    logo's pixels (Pandoc writes an image block as a paragraph of its alt
    text), only the description this paragraph stands for. The text is in
    the document's language, not the interface's. ``role`` and
    ``letterhead_members`` record the merge for audit; no renderer reads
    them, so the PDF/UA tag stays ``P``.
    """
    block = _element_to_block(members[0])
    lines = [
        (member.text or "").strip().rstrip(".,;")
        for member in members
        if member.type != "picture"
    ]
    token = set_request_locale(locale_for_user(language))
    try:
        template = t(MSG_LETTERHEAD_TEXT)
    finally:
        reset_request_locale(token)
    block["type"] = "paragraph"
    block["text"] = template.format(lines=", ".join(lines))
    block.pop("alt_text", None)
    block["metadata"]["role"] = "header"
    block["metadata"]["letterhead_members"] = [member.id for member in members]
    return block


def _manifest_pages_to_payload(manifest: ProcessingManifest) -> list[dict[str, Any]]:
    elements_by_id = {element.id: element for element in manifest.elements}
    page_numbers = _page_number_ids(manifest)
    letterheads = _letterhead_groups(manifest, page_numbers)
    #: Letterhead members other than the first, which carries the whole group.
    merged = {
        member.id
        for members in letterheads.values()
        for member in members[1:]
    }
    letterhead_blocks = {
        key: _letterhead_block(members, manifest.language)
        for key, members in letterheads.items()
    }
    pages: list[dict[str, Any]] = []

    if manifest.pages:
        sorted_pages = sorted(manifest.pages, key=lambda page: page.page_number)
        for page in sorted_pages:
            block_elements = [
                elements_by_id[element_id]
                for element_id in page.element_ids
                if element_id in elements_by_id
            ]
            if not block_elements:
                # Some extractors can fill in pages without element_ids; in that
                # case we fall back to page_number so no content is lost.
                block_elements = [
                    element
                    for element in manifest.elements
                    if (element.page_number or 1) == page.page_number
                ]
            block_elements = [
                item
                for item in block_elements
                if not _is_left_out_of_payload(item)
                and item.id not in page_numbers
                and item.id not in merged
            ]
            block_elements.sort(key=lambda item: item.reading_order)
            blocks = _elements_to_blocks(block_elements, letterhead_blocks)
            _dedupe_callout_titles(blocks)
            pages.append(
                {
                    "page_number": page.page_number,
                    "text": _render_text_from_blocks(blocks),
                    "blocks": blocks,
                }
            )
        return pages

    grouped: dict[int, list[ManifestElement]] = {}
    for element in manifest.elements:
        if (
            _is_left_out_of_payload(element)
            or element.id in page_numbers
            or element.id in merged
        ):
            continue
        page_number = element.page_number or 1
        grouped.setdefault(page_number, []).append(element)

    for page_number in sorted(grouped):
        page_elements = sorted(
            grouped[page_number],
            key=lambda item: item.reading_order,
        )
        blocks = _elements_to_blocks(page_elements, letterhead_blocks)
        _dedupe_callout_titles(blocks)
        pages.append(
            {
                "page_number": page_number,
                "text": _render_text_from_blocks(blocks),
                "blocks": blocks,
            }
        )

    return pages


def _elements_to_blocks(
    elements: list[ManifestElement], replacements: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Blocks of ``elements`` in order, with the text an undescribed picture held.

    An element in ``replacements`` (the first member of a letterhead) gives
    way to its block, which already stands for the picture's text.
    """
    callout_titles = _callout_titles(elements)
    recovered_items, consumed_text, preambles = _recover_empty_lettered_list_items(elements)
    blocks: list[dict[str, Any]] = []
    dropped_empty_items: list[ManifestElement] = []
    for element in elements:
        if element.id in replacements:
            blocks.append(replacements[element.id])
            continue
        if preamble := preambles.get(element.id):
            blocks.append(_element_to_block(preamble, callout_titles))
        text = consumed_text.get(element.id)
        if text is not None:
            if not text:
                continue
            element = element.model_copy(update={"text": text})
        recovered = recovered_items.get(element.id)
        if recovered is not None:
            item_text, marker = recovered
            metadata = {**element.metadata, "enumerated": True, "marker": marker}
            element = element.model_copy(
                update={"text": item_text, "metadata": metadata}
            )
        block = _element_to_block(element, callout_titles)
        if block.get("type") != "list" or any(
            isinstance(item, str) and item.strip()
            for item in block.get("items", [])
        ):
            blocks.append(block)
        else:
            dropped_empty_items.append(element)
        blocks.extend(_element_to_block(kept) for kept in _text_kept_by_picture(element))
    if dropped_empty_items:
        # The extractor found list items it could not read; the document
        # goes without them, which the operator should be able to see.
        logger.warning(
            t(LOG_PDDL_EMPTY_LIST_ITEMS_DROPPED).format(
                count=len(dropped_empty_items),
                page=dropped_empty_items[0].page_number,
                element_ids=", ".join(item.id for item in dropped_empty_items),
            )
        )
    return _merge_list_blocks(blocks)


# List recovery and callout grouping are owned by the list adapter.
from backend.core.manifest.payload_lists import (
    _LETTERED_LIST_SOURCE_MARKER,
    _recover_empty_lettered_list_items,
    _callout_titles,
    _merge_list_blocks,
)


def _text_kept_by_picture(element: ManifestElement) -> list[ManifestElement]:
    """The text a picture took from the slide, as paragraphs, when it went undescribed.

    Slide structuring hands text to a picture: a slide read as one picture
    keeps the text it replaced (``replaced_text``), a text slide's labels are
    folded into the picture they lie on or beside (``figure_labels``). Its
    description was to carry that text; without one, the text goes back to
    the slide after the picture, as it stood before, instead of being lost or
    listed as the picture's alt text (006.pdf v5, slides 16 and 31).

    Only the vision model's description carries that text: an alt the
    extractor gave the picture, which a failed call leaves in place, does not.
    The paragraphs carry the picture's source location; the labels' own boxes
    left with the elements they were folded from.
    """
    if element.type != "picture" or (
        element.metadata.get("vision_description_complete") and (element.text or "").strip()
    ):
        return []
    replaced = element.metadata.get("replaced_text")
    texts = (
        [line for line in replaced.splitlines() if line.strip()]
        if isinstance(replaced, str) and replaced.strip()
        else list(element.metadata.get("figure_labels") or [])
    )
    return [
        ManifestElement(
            id=f"{element.id}-kept-{index:03d}",
            type="paragraph",
            raw_label="paragraph",
            reading_order=element.reading_order,
            hierarchy_level=element.hierarchy_level,
            text=text,
            page_number=element.page_number,
            provenance=list(element.provenance),
            metadata={"kept_from_undescribed_picture": element.id},
        )
        for index, text in enumerate(texts, start=1)
    ]


def _element_to_block(
    element: ManifestElement, callout_titles: dict[str, str] | None = None
) -> dict[str, Any]:
    provenance = element.provenance[0] if element.provenance else None
    metadata: dict[str, Any] = {
        "raw_label": element.raw_label,
        "source_ref": element.source_ref,
        "parent_id": element.parent_id,
        "confidence": element.confidence,
        "manifest_element_id": element.id,
    }
    if element.metadata:
        metadata.update(element.metadata)
    metadata = _normalize_metadata_links(metadata)

    source_location: dict[str, Any] = {}
    if provenance is not None:
        source_location["page_number"] = provenance.page_number
        if provenance.bbox is not None:
            source_location["bbox"] = {
                "left": provenance.bbox.left,
                "top": provenance.bbox.top,
                "right": provenance.bbox.right,
                "bottom": provenance.bbox.bottom,
            }

    text = element.text or ""
    callout_id = metadata.get("callout_id")
    callout_role = str(metadata.get("callout_role", "")).lower()
    callout_type = str(metadata.get("callout_type", "note")).lower()
    callout_title = (callout_titles or {}).get(str(callout_id)) or str(
        metadata.get("callout_title", "")
    ).strip()
    callout_block_type = "warning" if callout_type in {"warning", "aviso", "importante", "caution"} else "note"
    block: dict[str, Any] = {
        "id": element.id,
        "source_location": source_location,
        "metadata": metadata,
    }

    if (
        callout_id
        and callout_role != "title"
        and metadata.get("slide_role") not in {"slide_heading", "deck_title"}
    ):
        block.update(
            {
                "type": callout_block_type,
                "title": callout_title,
                "text": text,
            }
        )
        return block

    if element.type in {"title", "heading"}:
        block.update(
            {
                "type": "heading",
                "level": 1 if element.type == "title" else max(1, element.hierarchy_level),
                "text": text or element.raw_label,
            }
        )
    elif element.type == "list_item":
        ordered = bool(metadata.get("enumerated"))
        block.update(
            {
                "type": "list",
                "ordered": ordered,
                "items": [text] if text else [],
            }
        )
        numbering = marker_numbering(metadata.get("marker")) if ordered else None
        if numbering is not None:
            start, style = numbering
            if start > 1:
                block["start"] = start
            if style != "decimal":
                block["numbering"] = style
    elif element.type == "table":
        table_ast = _table_ast_from_metadata(metadata)
        rows = _rows_from_table_ast(table_ast) if table_ast else None
        if not rows:
            rows = [[text]] if text else [["Tabela detectada"]]
            if table_ast is None:
                table_ast = _table_ast_from_rows(rows)
        block.update(
            {
                "type": "table",
                "rows": rows,
                "table_ast": table_ast,
            }
        )
    elif element.type == "picture":
        block.update(
            {
                "type": "image",
                "alt_text": text or t(MSG_UNDESCRIBED_PICTURE_ALT),
            }
        )
    elif element.type == "code":
        block.update({"type": "code", "text": normalize_code_text(text)})
    elif element.type == "formula":
        if text:
            block.update({"type": "math", "text": text})
        else:
            block.update({"type": "paragraph", "text": "Formula detectada."})
    else:
        block.update({"type": "paragraph", "text": text or element.raw_label})

    return block


# Text formatting and metadata link normalization are separate from layout.
from backend.core.manifest.payload_text import (
    _normalize_metadata_links,
    _render_text_from_blocks,
    _render_text_from_pages,
)
