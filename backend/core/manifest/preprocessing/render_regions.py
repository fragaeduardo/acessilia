"""Helpers shared by vision and formula rendering, no Toolbox calls here."""
from __future__ import annotations

import math
from typing import Any, TYPE_CHECKING
from backend.core.manifest.models import ProcessingManifest, is_bottom_left

if TYPE_CHECKING:
    from acessilia_sdk.toolbox.clients.pdf import ClipOrigin


def _source_artifact_id(manifest: ProcessingManifest) -> str | None:
    """The Toolbox artifact of the source document, when the extractor stored one.

    Artifact ids are content addressed, so the id is used only when its digest
    is the manifest's source SHA-256: an id of any other document (a restored
    or reassembled manifest, a wrong id) would have the Toolbox render pixels
    the evidence does not describe. Otherwise the file itself is sent.
    """
    from acessilia_sdk.toolbox import ArtifactRef, ToolboxContractViolation

    value = (manifest.extractor.configuration or {}).get("artifact_id")
    if not isinstance(value, str) or not value:
        return None
    try:
        ref = ArtifactRef.parse(value)
    except ToolboxContractViolation:
        return None
    return value if ref.digest == manifest.source.sha256 else None


def _picture_clip(
    provenance: Any,
) -> tuple[tuple[float, float, float, float] | None, ClipOrigin]:
    """The box of a picture to render, and its origin; ``None`` means the whole page.

    Pictures are best effort, so a box that cannot be used (absent or with a
    non-finite coordinate) degrades to the whole page instead of failing, and
    the origin may be inferred (``is_bottom_left``). Region inspection, which
    certifies its crop, refuses both instead.
    """
    bbox = getattr(provenance, "bbox", None)
    if bbox is None:
        return None, "TOPLEFT"
    left, top, right, bottom = (
        float(bbox.left), float(bbox.top), float(bbox.right), float(bbox.bottom)
    )
    if not all(math.isfinite(value) for value in (left, top, right, bottom)):
        return None, "TOPLEFT"
    origin: ClipOrigin = (
        "BOTTOMLEFT"
        if is_bottom_left(getattr(bbox, "coord_origin", None), top, bottom)
        else "TOPLEFT"
    )
    return (left, top, right, bottom), origin


_RENDERABLE_MEDIA_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg"})
