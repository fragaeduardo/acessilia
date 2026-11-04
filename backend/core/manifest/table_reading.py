"""Table reading and normalization helpers extracted from the PDDL orchestrator.

Algorithms are deliberately byte-for-byte retained from the original
implementation to preserve table fallbacks, heuristics, and metadata.
The orchestration layer keeps compatibility re-exports.
"""

from __future__ import annotations

import re
from typing import Any

from docstruct.tables.ast import effective_section_width, raw_rows_from_table_ast as _rows_from_table_ast
from docstruct.tables.garble import table_garble_reason

from backend.core.manifest.models import ManifestElement


#: Share of the words two readings of a table must share, each way, for the
#: new one to replace the misread one: it is the same table read better, so
#: most of its words were there, misplaced (a crop of the wrong region, or an
#: invented table, shares few), and it must keep most of the misread one's
#: words (on 006.pdf the model once returned only the header row).
_TABLE_REREAD_MIN_SHARED = 0.5

#: Readings asked for a misread table before it is kept as it is.
_TABLE_REREAD_ATTEMPTS = 3

_TABLE_WORD = re.compile(r"\w{2,}")


def _table_reread_disqualified(
    misread_ast: dict[str, Any], rows: list[list[str]]
) -> str | None:
    """Why a new reading of a misread table must not replace it, or None."""
    reason = table_garble_reason(rows)
    if reason:
        return f"the new reading is misread too: {reason}"
    misread_rows = _rows_from_table_ast(misread_ast)
    width = max((len(row) for row in rows), default=0)
    misread_width = max((len(row) for row in misread_rows), default=0)
    if width < misread_width:
        return f"the new reading has {width} column(s), the table {misread_width}"
    words = {word for row in rows for cell in row for word in _TABLE_WORD.findall(cell.casefold())}
    if not words:
        return "the new reading has no text"
    misread = {
        word for row in misread_rows for cell in row for word in _TABLE_WORD.findall(cell.casefold())
    }
    shared = len(words & misread) / len(words)
    if shared < _TABLE_REREAD_MIN_SHARED:
        return f"only {shared:.0%} of its words are in the table it would replace"
    kept = len(words & misread) / len(misread) if misread else 1.0
    if kept < _TABLE_REREAD_MIN_SHARED:
        return f"it keeps only {kept:.0%} of the words of the table it would replace"
    return None


def _table_element_has_structured_content(element: ManifestElement) -> bool:
    metadata = element.metadata or {}
    table_ast = metadata.get("table_ast")
    if not isinstance(table_ast, dict):
        return False

    rows = _rows_from_table_ast(table_ast)
    if not rows:
        return False

    width = max((len(row) for row in rows), default=0)
    if len(rows) >= 2 and width >= 2:
        return True

    return not _rows_look_like_placeholder(rows)


def _table_ast_column_count(table_ast: dict[str, Any]) -> int:
    """Return the widest logical section, including cell spans."""
    widths: list[int] = []
    for section_name in ("header", "body", "footer"):
        section = table_ast.get(section_name)
        if isinstance(section, list):
            widths.append(effective_section_width(section))
    return max(widths, default=0)


def _backfill_table_from_existing_text(element: ManifestElement) -> bool:
    text = (element.text or "").strip()
    if not text:
        return False

    rows = _rows_from_visual_table_text(text)
    if not rows:
        return False

    table_ast = _table_ast_from_rows(rows)
    if table_ast is None:
        return False

    element.metadata["table_ast"] = table_ast
    element.metadata["table_linearization_hint"] = "text-heuristic"
    element.metadata["table_row_count"] = len(rows)
    element.metadata["table_column_count"] = max((len(row) for row in rows), default=0)
    element.metadata["table_has_header"] = bool(rows and len(rows[0]) >= 2)
    element.text = ""
    return True


def _rows_from_visual_table_text(raw_text: str) -> list[list[str]]:
    text = raw_text.strip()
    if not text:
        return []

    rows = _rows_from_pipe_table(text)
    if rows:
        return rows

    rows = _rows_from_delimited_lines(text, delimiter="\t")
    if rows:
        return rows

    semicolon_rows = _rows_from_delimited_lines(text, delimiter=";")
    comma_rows = _rows_from_delimited_lines(text, delimiter=",")
    if _score_rows(semicolon_rows) >= _score_rows(comma_rows):
        return semicolon_rows
    return comma_rows


def _rows_from_pipe_table(text: str) -> list[list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows: list[list[str]] = []
    for line in lines:
        if "|" not in line:
            continue
        if _looks_like_markdown_separator(line):
            continue
        cleaned = line.strip().strip("|")
        cells = [cell.strip() for cell in cleaned.split("|")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            rows.append(cells)
    if _score_rows(rows) >= 4:
        return rows
    return []


def _rows_from_delimited_lines(text: str, *, delimiter: str) -> list[list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows: list[list[str]] = []
    for line in lines:
        if delimiter not in line:
            continue
        if "|" in line and delimiter != "|":
            continue
        cells = [cell.strip() for cell in line.split(delimiter)]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            rows.append(cells)
    if _score_rows(rows) >= 4:
        return rows
    return []


def _looks_like_markdown_separator(line: str) -> bool:
    cleaned = line.strip().strip("|").replace(" ", "")
    if not cleaned:
        return False
    parts = cleaned.split("|")
    if not parts:
        return False
    return all(re.fullmatch(r":?-{2,}:?", part or "") for part in parts)


def _score_rows(rows: list[list[str]]) -> int:
    if not rows:
        return 0
    row_count = len(rows)
    width = max((len(row) for row in rows), default=0)
    return row_count * width


def _rows_look_like_placeholder(rows: list[list[str]]) -> bool:
    if len(rows) != 1 or len(rows[0]) != 1:
        return False
    return rows[0][0].strip().lower() == "tabela detectada"


def _table_ast_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("table_ast")
    if not isinstance(raw, dict):
        return None

    table_ast: dict[str, Any] = {}
    caption = raw.get("caption")
    if isinstance(caption, str) and caption.strip():
        table_ast["caption"] = caption.strip()

    for section_name in ("header", "body", "footer"):
        section = raw.get(section_name)
        if not isinstance(section, list):
            continue
        normalized_rows: list[dict[str, Any]] = []
        for row in section:
            if not isinstance(row, dict):
                continue
            cells = row.get("cells")
            if not isinstance(cells, list):
                continue
            normalized_cells: list[dict[str, Any]] = []
            for cell in cells:
                if not isinstance(cell, dict) or "text" not in cell:
                    continue
                text = str(cell.get("text", "")).strip()
                normalized_cell: dict[str, Any] = {"text": text}
                if isinstance(cell.get("header"), bool):
                    normalized_cell["header"] = cell["header"]
                scope = cell.get("scope")
                if isinstance(scope, str) and scope in {
                    "none",
                    "row",
                    "col",
                    "rowgroup",
                    "colgroup",
                }:
                    normalized_cell["scope"] = scope
                for span_key in ("rowspan", "colspan"):
                    span = cell.get(span_key)
                    if isinstance(span, int) and span >= 1:
                        normalized_cell[span_key] = span
                normalized_cells.append(normalized_cell)
            if normalized_cells:
                normalized_rows.append({"cells": normalized_cells})
        if normalized_rows:
            table_ast[section_name] = normalized_rows

    metadata_obj = raw.get("metadata")
    if isinstance(metadata_obj, dict):
        table_ast["metadata"] = metadata_obj

    return table_ast if table_ast.get("body") else None


def _table_ast_from_rows(rows: list[list[str]]) -> dict[str, Any]:
    body: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        cells = []
        for cell in row:
            text = str(cell).strip()
            cells.append({"text": text})
        if cells:
            body.append({"cells": cells})
    return {"body": body}
