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
    """Retorna True se o texto parece ser ruído de OCR (muito curto, sem
    estrutura de linguagem natural), não devendo ser considerado como
    conteúdo textual significativo para efeitos de detecção de manifesto
    majoritariamente visual."""
    normalized = text.strip()
    if not normalized:
        return True
    # Texto muito curto (≤4 caracteres) é quase sempre OCR noise
    if len(normalized) <= 4:
        return True
    # Texto sem espaços e com ≤8 caracteres também é suspeito
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
    """Retorna True se o elemento contém texto significativo (não é OCR noise
    nem placeholder). Usado para filtrar elementos do tipo 'picture' que já
    vieram com descrição textual válida do extrator."""
    text = (element.text or "").strip()
    if not text:
        return False
    if _is_placeholder_text(text):
        return False
    if _is_ocr_noise(text):
        return False
    return True
