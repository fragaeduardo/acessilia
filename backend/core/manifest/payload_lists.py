"""List recovery, grouping and callout titles for canonical delivery.

Algorithms were extracted verbatim from manifest.payload. Keep their
strict ambiguity checks: recovering partially extracted lists twice would
duplicate content and change accessible reading order.
"""

from __future__ import annotations

import re
from typing import Any

from docstruct.blocks.lists import marker_numbering
from backend.core.manifest.models import ManifestElement


#: ``a)`` at the start of the text or after whitespace: a letter after any
#: other character (``(a)``, ``x(a)``) is an enumeration inside prose.
_LETTERED_LIST_SOURCE_MARKER = re.compile(
    r"(?:^|(?<=\s))(?P<marker>[A-Za-z])\)(?=\s)"
)


def _recover_empty_lettered_list_items(
    elements: list[ManifestElement],
) -> tuple[dict[str, tuple[str, str]], dict[str, str], dict[str, ManifestElement]]:
    """Recover a fully extracted lettered list from a sibling text element.

    Some extractors emit empty ``list_item`` elements while leaving the actual
    ``a) ... b) ... c)`` text in a paragraph on the same page. Move that text
    only when one paragraph in the same heading section has the exact marker
    sequence of one fully empty list group. Explicit non-alphabetic markers,
    ambiguous matches and partial matches stay untouched.

    Returns item-id -> (text, marker) and source-paragraph-id -> remaining
    prefix, and first-item-id -> preamble to emit before that item. The
    paragraph's list text is consumed once, avoiding duplicate output. A
    preamble after the first item moves before it, keeping the exercise's
    introduction ahead of its questions.

    The last item runs to the end of the paragraph: a sentence after it (a
    closing remark) stays in that item, since a question can itself span
    several sentences and no marker says where it ends.
    """
    sections: dict[str, int] = {}
    section = 0
    groups: dict[str, list[ManifestElement]] = {}
    for element in elements:
        if element.type in {"title", "heading"}:
            section += 1
        sections[element.id] = section
        if element.type == "list_item" and element.parent_id:
            groups.setdefault(element.parent_id, []).append(element)

    empty_groups = {
        parent_id: sorted(items, key=lambda item: item.reading_order)
        for parent_id, items in groups.items()
        if len(items) >= 2
        and all(not (item.text or "").strip() for item in items)
        and len({sections[item.id] for item in items}) == 1
    }
    if not empty_groups:
        return {}, {}, {}

    sources = [
        element
        for element in elements
        if element.type in {"paragraph", "unknown"} and (element.text or "").strip()
    ]
    matches_by_group: dict[
        str, list[tuple[ManifestElement, list[tuple[str, str]], str]]
    ] = {parent_id: [] for parent_id in empty_groups}

    for parent_id, items in empty_groups.items():
        known_markers: list[tuple[int, str] | None] = []
        for item in items:
            marker = str(item.metadata.get("marker") or "").strip()
            parsed = marker_numbering(marker) if marker else None
            if marker and (parsed is None or not parsed[1].endswith("-alpha")):
                known_markers = []
                break
            known_markers.append(parsed)
        if not known_markers:
            continue

        for source in sources:
            if sections[source.id] != sections[items[0].id]:
                continue
            source_text = source.text or ""
            markers = list(_LETTERED_LIST_SOURCE_MARKER.finditer(source_text))
            if len(markers) != len(items):
                continue
            parsed_markers = [
                marker_numbering(f"{match.group('marker')})") for match in markers
            ]
            if any(
                marker is None or not marker[1].endswith("-alpha")
                for marker in parsed_markers
            ):
                continue
            typed_markers = [marker for marker in parsed_markers if marker is not None]
            style = typed_markers[0][1]
            first_number = typed_markers[0][0]
            if any(marker[1] != style for marker in typed_markers) or [
                marker[0] for marker in typed_markers
            ] != list(range(first_number, first_number + len(items))):
                continue
            if any(
                known is not None and known != actual
                for known, actual in zip(known_markers, typed_markers, strict=True)
            ):
                continue

            item_texts = []
            for index, match in enumerate(markers):
                end = markers[index + 1].start() if index + 1 < len(markers) else len(source_text)
                item_texts.append(source_text[match.end() : end].strip())
            if not all(item_texts):
                continue
            prefix = source_text[: markers[0].start()].strip()
            marker_texts = [match.group("marker") + ")" for match in markers]
            matches_by_group[parent_id].append(
                (source, list(zip(marker_texts, item_texts, strict=True)), prefix)
            )

    group_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for parent_id, matches in matches_by_group.items():
        group_counts[parent_id] = len(matches)
        for source, _, _ in matches:
            source_counts[source.id] = source_counts.get(source.id, 0) + 1

    recovered: dict[str, tuple[str, str]] = {}
    consumed: dict[str, str] = {}
    preambles: dict[str, ManifestElement] = {}
    positions = {element.id: index for index, element in enumerate(elements)}
    for parent_id, matches in matches_by_group.items():
        if group_counts[parent_id] != 1:
            continue
        source, marked_texts, prefix = matches[0]
        if source_counts[source.id] != 1:
            continue
        for item, (marker, text) in zip(
            empty_groups[parent_id], marked_texts, strict=True
        ):
            recovered[item.id] = (text, marker)
        first_item = empty_groups[parent_id][0]
        if prefix and positions[source.id] > positions[first_item.id]:
            preambles[first_item.id] = source.model_copy(update={"text": prefix})
            prefix = ""
        consumed[source.id] = prefix

    return recovered, consumed, preambles


def _callout_titles(elements: list[ManifestElement]) -> dict[str, str]:
    """Each callout's title, read from its title element's text as it stands now.

    The Toolbox copies the title into ``metadata.callout_title`` when it groups
    the callout, before any repair; the text-repair methods then fix the
    title element's text only, so the copy keeps the broken accents
    ("observa¸ c˜ oes").

    Read per page: the Toolbox groups callouts page by page, so a callout's
    title and content never sit on different pages.
    """
    return {
        str(element.metadata["callout_id"]): (element.text or "").strip()
        for element in elements
        if element.metadata.get("callout_id")
        and str(element.metadata.get("callout_role", "")).lower() == "title"
        and (element.text or "").strip()
    }


def _merge_list_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks with each run of items from one source list joined into one list.

    Each list item becomes a list block of its own. Consecutive ones that share
    their Docling list (``parent_id``) and kind are one list; an item kept
    apart by a formula or a paragraph stays a list of its own, whose ``start``
    carries the numbering on.

    The joined list keeps the first item's identity and provenance (``id``,
    ``manifest_element_id``, ``source_location``); the later items lend it
    their text only. Nothing reads provenance per item.
    """
    merged: list[dict[str, Any]] = []
    for block in blocks:
        previous = merged[-1] if merged else None
        parent_id = (block.get("metadata") or {}).get("parent_id")
        if (
            previous is not None
            and parent_id
            and block.get("type") == previous.get("type") == "list"
            and block.get("ordered") == previous.get("ordered")
            and (previous.get("metadata") or {}).get("parent_id") == parent_id
        ):
            previous["items"].extend(block.get("items", []))
            continue
        merged.append(block)
    return merged
