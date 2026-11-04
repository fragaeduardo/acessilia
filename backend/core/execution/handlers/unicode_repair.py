"""Legacy Unicode and negation overlay repair handler.

This is the original deterministic algorithm relocated out of the
PDDL orchestrator with unchanged extracted data, metadata and messages.
"""
from __future__ import annotations

from typing import Any

from docstruct.text.negation_overlay import (
    compose_negation, is_detached_overlay, locate_in_grid, locate_in_text, negates,
)
from docstruct.text.unicode_repair import (
    Detection, UnicodeRepairContext, detect_legacy_unicode, repair_legacy_unicode,
)
from backend.core.execution.handlers.common import _missing_obligation, _target_elements
from backend.core.execution.models import MethodResult
from backend.core.manifest.builder import unicode_repair_text
from backend.core.manifest.models import ManifestElement, ProcessingManifest
from backend.i18n import t
from backend.tools.logger import logger


#: Manifest element type -> block type accepted by the Unicode repair. Types
#: absent here (formula, code, table, picture) are never rewritten.
_UNICODE_REPAIR_BLOCK_TYPES = {
    "paragraph": "prose",
    "heading": "heading",
    "title": "heading",
    "list_item": "prose",
    "caption": "prose",
    "footnote": "prose",
    "page_header": "prose",
    "page_footer": "prose",
}

#: Sections of a table AST whose cells carry text.
from backend.core.manifest.table_reading import _TABLE_SECTIONS

#: Word-internal spaced accents needed before standalone ones (an accent
#: separated from its word) are treated as the same extraction defect. A
#: document that merely mentions an accent has none of the word-internal shape,
#: so it keeps its text; a legacy TeX extraction has hundreds.
_UNICODE_STANDALONE_SPACING_MIN = 5


def _repair_table_cells(
    element: ManifestElement, context: UnicodeRepairContext
) -> tuple[bool, set[str], int, bool]:
    """Repair a table caption and every cell in place.

    Returns ``(edited, rules, edits, needs_review)``. The table keeps its
    structure; only the text of the caption and of the cells changes.
    """
    table_ast = element.metadata.get("table_ast")
    if not isinstance(table_ast, dict):
        return False, set(), 0, False

    edited = False
    rules: set[str] = set()
    edits = 0
    review = False

    def repair_text(current: str) -> str:
        nonlocal edited, edits, review
        decision = repair_legacy_unicode(current, context=context)
        if decision.repaired != current:
            edited = True
        rules.update(edit.rule for edit in decision.edits)
        edits += len(decision.edits)
        review = review or decision.needs_review
        return decision.repaired

    caption = table_ast.get("caption")
    if isinstance(caption, str) and caption:
        table_ast["caption"] = repair_text(caption)
    for section_name in _TABLE_SECTIONS:
        section = table_ast.get(section_name)
        if not isinstance(section, list):
            continue
        for row in section:
            if not isinstance(row, dict):
                continue
            cells = row.get("cells")
            if not isinstance(cells, list):
                continue
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                text = cell.get("text")
                if isinstance(text, str) and text:
                    cell["text"] = repair_text(text)
    return edited, rules, edits, review


#: Slack, in PDF points, when finding the element a negation slash lies in.
from backend.core.manifest.element_geometry import _OVERLAY_HOST_TOLERANCE
#: A text line is about this many slash heights tall (glyph plus leading).
_OVERLAY_LINE_HEIGHTS = 1.25


from backend.core.manifest.element_geometry import _bbox


def _vertical(box: Any) -> tuple[float, float]:
    """``(upper, lower)`` edges in a downward axis, whatever the box's origin."""
    if box.coord_origin == "BOTTOMLEFT":
        return -box.top, -box.bottom
    return min(box.top, box.bottom), max(box.top, box.bottom)


def _table_grid(element: ManifestElement) -> list[list[tuple[str, dict[str, Any] | None]]]:
    """The table's rows on its visual column grid: a cell spanning ``n`` columns
    takes ``n`` slots, its text in the first, so a slash's column position
    matches the cell under it. Row spans are not expanded (the slash's row is
    estimated from the AST rows), and every column is taken as equally wide —
    the AST carries no widths; ``locate_in_grid``'s margin refuses close calls.
    """
    table_ast = element.metadata.get("table_ast")
    if not isinstance(table_ast, dict):
        return []
    grid: list[list[tuple[str, dict[str, Any] | None]]] = []
    for section in _TABLE_SECTIONS:
        for row in table_ast.get(section) or []:
            if not isinstance(row, dict):
                continue
            slots: list[tuple[str, dict[str, Any] | None]] = []
            for cell in row.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                span = max(1, int(cell.get("colspan") or 1))
                slots.append((str(cell.get("text") or ""), cell))
                slots.extend(("", None) for _ in range(span - 1))
            grid.append(slots)
    return grid


def _reattach_negation_overlays(manifest: ProcessingManifest) -> dict[str, int]:
    """Give each detached ``≠`` slash back to the relation it negates.

    A legacy TeX text layer may extract the slash TeX lays over ``=`` as its own
    element (``U+0338`` alone), leaving ``h = 0`` where the page says
    ``h ≠ 0``. The slash's box is the only link back: the element containing it
    is found on the same page, the slash's position inside it is estimated, and
    ``docstruct.text.negation_overlay`` picks the relation only when the choice
    is unambiguous — composing the wrong ``=`` would invert a true statement.
    A formula whose text is already LaTeX (recognized, #253) is not touched.

    Every slash element is then left out of the payload (``negation_overlay``
    metadata, with the outcome: ``composed``, ``host-is-latex``, ``ambiguous``,
    ``no-candidate``, ``ambiguous-host`` or ``no-host``), since a lone
    combining mark says nothing to a reader. A slash already recorded is
    skipped, so running the handler again (a plan retry) keeps the first
    outcome instead of re-reading a host whose text it already changed.
    Returns the count per outcome.

    This runs only with the repair obligation (``LEGACY_UNICODE_REPAIR=repair``).
    ``detect`` keeps its meaning — report, deliver as extracted — so there the
    slash elements stay in the outputs, like every other legacy artifact.
    """
    outcomes: dict[str, int] = {}
    overlays = [
        element
        for element in manifest.elements
        if is_detached_overlay(element.text) and "negation_overlay" not in element.metadata
    ]
    for overlay in overlays:
        located = _bbox(overlay)
        outcome, host_id = "no-host", None
        if located is not None:
            page, box = located
            x = (box.left + box.right) / 2
            upper, lower = _vertical(box)
            y = (upper + lower) / 2
            slash_height = (lower - upper) or 12.0
            hosts = []
            for element in manifest.elements:
                if element is overlay or is_detached_overlay(element.text):
                    continue
                other = _bbox(element, page)
                if other is None:
                    continue
                top, bottom = _vertical(other[1])
                if (
                    other[1].left - _OVERLAY_HOST_TOLERANCE <= x <= other[1].right + _OVERLAY_HOST_TOLERANCE
                    and top - _OVERLAY_HOST_TOLERANCE <= y <= bottom + _OVERLAY_HOST_TOLERANCE
                ):
                    hosts.append((element, other[1], top, bottom))
            if len(hosts) > 1:
                outcome = "ambiguous-host"
            elif hosts:
                host, host_box, top, bottom = hosts[0]
                host_id = host.id
                height = max(bottom - top, 1e-6)
                width = max(host_box.right - host_box.left, 1e-6)
                # The host tolerance lets a slash sit just outside the box:
                # clamp once, so the text and the grid paths see the same span.
                y_fraction = min(max((y - top) / height, 0.0), 1.0)
                x_fraction = min(max((x - host_box.left) / width, 0.0), 1.0)
                outcome = _compose_in_host(host, x_fraction, y_fraction, height, slash_height)
                if outcome == "composed":
                    host.metadata["negation_overlays"] = host.metadata.get("negation_overlays", 0) + 1
        overlay.metadata["negation_overlay"] = {"outcome": outcome, "host": host_id}
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    if outcomes:
        logger.info("Detached negation slashes: {}", outcomes)
    return outcomes


def _compose_in_host(
    host: ManifestElement,
    x_fraction: float,
    y_fraction: float,
    height: float,
    slash_height: float,
) -> str:
    """Negate the relation under the slash in ``host``; returns the outcome.

    A relation inside an inline ``$…$`` span is not a candidate: the font
    detection built that span from the text layer, slash included (#260).
    """
    if host.type == "table":
        grid = _table_grid(host)
        texts = [[mask_inline_math(text) for text, _ in row] for row in grid]
        if not any(negates(char) for row in texts for text in row for char in text):
            return "no-candidate"
        columns = max((len(row) for row in grid), default=0)
        found = locate_in_grid(
            texts,
            row_position=y_fraction * len(grid),
            column_position=x_fraction * columns,
        )
        if found is None:
            return "ambiguous"
        row, column, index = found
        cell = grid[row][column][1]
        if cell is None:
            return "ambiguous"
        cell["text"] = compose_negation(str(cell["text"]), index)
        return "composed"
    if host.type == "formula" and has_latex_markup(host.text):
        return "host-is-latex"
    text = host.text or ""
    prose = mask_inline_math(text)
    if not any(negates(char) for char in prose):
        return "no-candidate"
    lines = max(1, round(height / (slash_height * _OVERLAY_LINE_HEIGHTS)))
    index = locate_in_text(
        prose, lines=lines, line_position=y_fraction * lines, x_fraction=x_fraction
    )
    if index is None:
        return "ambiguous"
    host.text = compose_negation(text, index)
    return "composed"


def _handle_unicode_repair_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Repair legacy Unicode: NFD, dotless i, spacing diacritics, the crase and split ligatures.

    Only selected, prose-like elements are touched. Word-internal spacing
    accents are repaired whenever the element shows them; standalone accents
    are only repaired when the document has enough word-internal evidence
    (recorded in the manifest observation). The decision metadata records the
    edits and keeps ``needs_review`` for signals that remain, without copying
    the whole text into the manifest.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    # Before the per-element repair, so the reattached ``≠`` is in the text
    # the repair reads, and the slash elements are not counted for review.
    _reattach_negation_overlays(manifest)

    language = manifest.language or "pt-BR"
    eligible: list[tuple[ManifestElement, str]] = []
    detections: dict[str, Detection] = {}
    for element in elements:
        text = unicode_repair_text(element)
        if not text or is_detached_overlay(element.text):
            continue
        if element.type == "table":
            block_type = "table"
        else:
            block_type = _UNICODE_REPAIR_BLOCK_TYPES.get(element.type)
            if block_type is None:
                continue
        eligible.append((element, block_type))
        detections[element.id] = detect_legacy_unicode(text)

    if not eligible:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum texto elegível para reparo Unicode",
        )

    # The threshold is document-level. The builder emits one obligation for all
    # eligible elements, and records the count in its observation; read it from
    # there so a future split of the obligation cannot change the decision.
    observation = next(
        (
            item
            for item in getattr(manifest, "observations", [])
            if item.kind == "repair-legacy-unicode-detected"
        ),
        None,
    )
    if observation is not None and "spacing_word_internal" in observation.evidence:
        word_internal = int(observation.evidence["spacing_word_internal"])
    else:
        word_internal = sum(
            detection.spacing_word_internal for detection in detections.values()
        )
    allow_standalone = word_internal >= _UNICODE_STANDALONE_SPACING_MIN
    inspected = len(eligible)
    repaired = 0
    review = 0
    for element, block_type in eligible:
        detection = detections[element.id]
        original = element.text or ""
        # A word-internal spacing accent (``C´ alculo``) is legacy extraction
        # evidence. Standalone accents (separated from their word) are only
        # included when the document has enough of the word-internal shape;
        # the lib never touches protected spans. The crase and split-ligature
        # rules rest on that same document-level evidence alone: an element
        # may carry ``` a`` or ``fi ca`` without any spaced accent of its own.
        allow_spacing = detection.spacing_diacritics > 0
        context = UnicodeRepairContext(
            language=language,
            block_type="prose" if block_type == "table" else block_type,
            page=element.page_number,
            allow_spacing_diacritics=allow_spacing,
            allow_standalone_spacing_diacritics=allow_spacing and allow_standalone,
            allow_legacy_tex_artifacts=allow_standalone,
        )
        if block_type == "table":
            edited, rules, edits, needs_review = _repair_table_cells(
                element, context
            )
            if edited:
                repaired += 1
            if needs_review:
                review += 1
            element.metadata["unicode_repair"] = {
                "edited": edited,
                "rules": sorted(rules),
                "edits": edits,
                "needs_review": needs_review,
                "confidence": None,
            }
            continue
        decision = repair_legacy_unicode(original, context=context)
        if decision.repaired != original:
            element.text = decision.repaired
            repaired += 1
        element.metadata["unicode_repair"] = {
            "edited": decision.repaired != original,
            "rules": sorted({edit.rule for edit in decision.edits}),
            "edits": len(decision.edits),
            "needs_review": decision.needs_review,
            "confidence": decision.confidence,
        }
        if decision.needs_review:
            review += 1
    return MethodResult(
        success=True,
        validated=True,
        message=(
            f"Reparo Unicode aplicado em {repaired}/{inspected} elemento(s); "
            f"{review} para revisão"
        ),
    )
