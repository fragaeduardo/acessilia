"""Canonical block-to-text serialization and nested metadata link normalization."""

from __future__ import annotations

from typing import Any

from docstruct.blocks.lists import item_label, list_numbering, list_start
from docstruct.tables.ast import raw_rows_from_table_ast as _rows_from_table_ast


def _normalize_metadata_links(metadata: dict[str, Any]) -> dict[str, Any]:
    def normalize_value(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("#/"):
            return value[1:]
        if isinstance(value, list):
            return [normalize_value(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize_value(item) for key, item in value.items()}
        return value

    return {key: normalize_value(value) for key, value in metadata.items()}


def _render_text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "heading":
            level = int(block.get("level", 1))
            prefix = "#" * max(1, min(level, 6))
            lines.append(f"{prefix} {block.get('text', '').strip()}")
        elif block_type == "list":
            numbering = list_numbering(block)
            for number, item in enumerate(block.get("items", []), start=list_start(block)):
                marker = f"{item_label(number, numbering)}." if block.get("ordered") else "-"
                lines.append(f"{marker} {str(item).strip()}")
        elif block_type == "table":
            rows = block.get("rows")
            if not rows and isinstance(block.get("table_ast"), dict):
                rows = _rows_from_table_ast(block["table_ast"])
            for row in rows or []:
                if isinstance(row, list):
                    lines.append(" | ".join(str(cell).strip() for cell in row))
        elif block_type == "image":
            lines.append(block.get("alt_text", "Imagem"))
        else:
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
    return "\n".join(lines).strip()


def _render_text_from_pages(pages: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for page in pages:
        page_number = page.get("page_number", "?")
        text = str(page.get("text", "")).strip()
        if text:
            chunks.append(f"=== Pagina {page_number} ===\n{text}")
    return "\n\n".join(chunks)
