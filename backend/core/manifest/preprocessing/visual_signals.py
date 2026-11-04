"""Existing visual-content heuristics used before and during PDDL execution.

These predicates retain the original threshold and placeholder rules.
"""

from __future__ import annotations

from backend.core.manifest.builder import is_unstructured_page_placeholder
from backend.core.manifest.models import ManifestElement, ProcessingManifest


def _is_mostly_visual_manifest(manifest: ProcessingManifest) -> bool:
    for element in manifest.elements:
        if element.type in {"heading", "title", "paragraph", "list_item", "table", "code", "formula"}:
            text = (element.text or "").strip()
            if text and not _is_placeholder_text(text) and not _is_ocr_noise(text):
                return False
    return True


def _is_ocr_noise(text: str) -> bool:
    """Return whether text is probably OCR noise, not useful prose.

    Tiny strings or short strings without spaces do not establish meaningful
    natural-language content in a predominantly visual document.
    """
    normalized = text.strip()
    if not normalized:
        return True
    # Very short text (up to 4 characters) is usually OCR noise
    if len(normalized) <= 4:
        return True
    # Short unspaced text (up to 8 characters) is also suspicious
    if " " not in normalized and len(normalized) <= 8:
        return True
    return False


def _is_placeholder_visual_element(element: ManifestElement) -> bool:
    if is_unstructured_page_placeholder(element):
        # Inspected by the planned vision-page-inspection method instead.
        return False
    if element.type not in {"group", "unknown", "paragraph"}:
        return False
    text = (element.text or "").strip()
    raw_label = (element.raw_label or "").strip().lower()
    return (not text) or _is_placeholder_text(text) or _is_ocr_noise(text) or raw_label in {"body", "unspecified", "group"}


def _is_placeholder_text(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"body", "_root_", "root", "unspecified", "group"}


def _has_meaningful_text(element: ManifestElement) -> bool:
    """Whether the extractor supplied substantive picture text.

    Discard OCR noise and structural placeholders while preserving
    meaningful extracted descriptions.
    """
    text = (element.text or "").strip()
    if not text:
        return False
    if _is_placeholder_text(text):
        return False
    if _is_ocr_noise(text):
        return False
    return True
