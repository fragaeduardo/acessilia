"""Common manifest geometry and role adapters.

Kept separate from output rendering and the PDDL orchestrator so both
preprocessing and delivery can depend on one geometry implementation.
"""
from __future__ import annotations

from docstruct.geometry.boxes import is_bottom_left

from backend.core.manifest.models import ManifestElement, ProcessingManifest


def _element_bbox(
    element: ManifestElement,
    manifest: ProcessingManifest,
) -> tuple[float, float, float, float] | None:
    """Element bounding box normalized to the unit page expected by docstruct."""
    for item in element.provenance:
        box = getattr(item, "bbox", None)
        if box is None:
            continue
        page = next(
            (
                page
                for page in manifest.pages
                if page.page_number == item.page_number
            ),
            None,
        )
        if page is None or not page.width or not page.height:
            return None
        top, bottom = box.top, box.bottom
        if is_bottom_left(box.coord_origin, top, bottom):
            top, bottom = page.height - top, page.height - bottom
        return (
            box.left / page.width,
            top / page.height,
            box.right / page.width,
            bottom / page.height,
        )
    return None


def _element_role(element: ManifestElement) -> str | None:
    """Fusion block role for an element.

    The manifest builder records page furniture in `element.type`
    (`page_header`/`page_footer`) and does not populate `metadata["role"]`, so the
    role is derived from the type when the metadata does not carry one.
    """
    role = (element.metadata or {}).get("role")
    if role:
        return role
    return {"page_header": "header", "page_footer": "footer"}.get(element.type)


#: Bounding box on a given source page; shared by overlay composition.
def _bbox(element: ManifestElement, page: int | None = None) -> tuple[int, Any] | None:
    """``(page, box)`` of the element: on ``page`` when given, else the first boxed one.

    An element can carry provenances on more than one page; a slash is only
    compared with the part of an element that lies on the slash's own page.
    """
    for provenance in element.provenance or []:
        if provenance.bbox is not None and page in (None, provenance.page_number):
            return provenance.page_number, provenance.bbox
    return None

_OVERLAY_HOST_TOLERANCE = 1.5
