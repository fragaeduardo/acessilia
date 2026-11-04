"""Toolbox-backed picture crop rendering, including validated fallback cases."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.execution.handlers.region_inspection import _toolbox_render
from backend.core.manifest.models import ManifestElement
from backend.core.manifest.preprocessing.render_regions import _picture_clip
from backend.i18n import t
from backend.log_messages import LOG_PDDL_ELEMENT_CROP_FAILED
from backend.tools.logger import logger

if TYPE_CHECKING:
    from acessilia_sdk.toolbox.clients.pdf import ClipOrigin, RenderedPage


#: Longest side of a picture crop sent to the vision model. The render runs at
#: 160 dpi and the Toolbox refuses more than 20 MP, so an unbounded crop of a
#: large standalone image (its page is sized in pixels) is rejected before the
#: model sees it. Bounding the side keeps every source renderable, and the
#: vision model's 8192-token slot would downscale it further anyway.
_PICTURE_MAX_SIDE = 2000


def _extract_picture_bytes(
    source_file: Path,
    element: ManifestElement,
    fallback_page_number: int | None,
    artifact_id: str | None = None,
    media_type: str | None = None,
    *, render_toolbox=None,
) -> tuple[bytes | None, int]:
    """The picture's box rendered by the Toolbox, or its whole page.

    A box the Toolbox refuses (outside the page) or a sliver of one is not a
    picture, so the page is described instead. Any other failure, the Toolbox
    being unavailable included, is not retried with the page: it is logged
    with its cause and the element is left without an image.
    """
    from acessilia_sdk.toolbox import ToolboxCapabilityError, ToolboxIncompatibleEndpoint

    provenance = element.provenance[0] if element.provenance else None
    page_number = provenance.page_number if provenance is not None else (fallback_page_number or 0)
    if page_number < 1:
        return None, 0

    def render(clip=None, origin: ClipOrigin = "TOPLEFT") -> RenderedPage:
        return (render_toolbox or _toolbox_render)(
            source_file, page_number, media_type=media_type, dpi=160,
            max_side=_PICTURE_MAX_SIDE, clip=clip,
            clip_origin=origin, artifact_id=artifact_id,
        )

    clip, origin = _picture_clip(provenance)
    try:
        if clip is None:
            return render().image, page_number
        try:
            page = render(clip, origin)
        except ToolboxIncompatibleEndpoint:
            raise
        except ToolboxCapabilityError:
            # The Toolbox refuses (HTTP 400) a box that does not intersect the page.
            return render().image, page_number
        left, top, right, bottom = page.clip
        if right - left < 4 or bottom - top < 4:
            return render().image, page_number
        return page.image, page_number
    except Exception:
        logger.exception(
            t(LOG_PDDL_ELEMENT_CROP_FAILED).format(element_id=element.id)
        )
        return None, page_number
