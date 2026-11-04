"""Media triage before manifest extraction and PDDL planning.

Preserves media.inspect degradation, presentation detection and the original
full-page image fallback without introducing new provider calls.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from backend.core.manifest.builder import _derive_processing_needs, is_unstructured_page_placeholder
from backend.core.manifest.models import ManifestElement, ProcessingManifest
from backend.i18n import t
from backend.log_messages import LOG_MEDIA_INSPECT_FAILED
from backend.tools.logger import logger
from backend.tools.media_inspect import inspect_source_media


async def _inspect_media(
    source_path: Path, *, inspect_fn=None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Runs ``media.inspect`` and degrades visibly when it fails.

    The inspection feeds the standalone-image heuristic and the presentation
    decision, and neither is required, so no failure of the step fails the
    job: any ``Exception`` (a ``ToolboxError``, but also an SDK bug or a client
    configuration error) makes the run continue without the signal, logs a
    warning and records the cause. Cancellation is not caught, since
    ``asyncio.CancelledError`` derives from ``BaseException``.

    Returns the inspection and the outcome for
    ``manifest.extractor.configuration``, since it runs before the manifest
    exists: ``media_inspection`` is ``toolbox``, ``disabled`` (by
    ``TOOLBOX_MEDIA_INSPECT_ENABLED``), ``source_missing`` or ``failed``, plus
    ``media_inspection_error`` on failure, and ``document_genre`` with its
    ``genre_signals`` when the Toolbox reports them.
    """
    from backend.config.settings import settings

    try:
        media_inspection = await (inspect_fn or inspect_source_media)(source_path)
    except Exception as exc:  # noqa: BLE001 - the step degrades, never fails the job
        cause = f"{type(exc).__name__}: {exc}"
        logger.warning(
            t(LOG_MEDIA_INSPECT_FAILED).format(
                filename=source_path.name, error=cause
            )
        )
        return None, {"media_inspection": "failed", "media_inspection_error": cause}
    if media_inspection is not None:
        status = "toolbox"
    elif not settings.toolbox_media_inspect_enabled:
        status = "disabled"
    else:
        status = "source_missing"
    configuration: dict[str, Any] = {"media_inspection": status}
    if isinstance(media_inspection, dict) and media_inspection.get("document_genre"):
        configuration["document_genre"] = media_inspection["document_genre"]
        configuration["genre_signals"] = list(media_inspection.get("genre_signals") or [])
    return media_inspection, configuration


def _is_presentation(media_inspection: dict[str, Any] | None) -> bool:
    """Whether ``media.inspect`` reported a slide deck.

    A Toolbox older than pure-media-inspect 1.1.0 reports no genre, and a
    failed inspection reports nothing: both are treated as a document.
    """
    return (
        isinstance(media_inspection, dict)
        and media_inspection.get("document_genre") == "presentation"
    )


def _inject_full_page_picture(
    manifest: ProcessingManifest,
    media_inspection: dict[str, Any] | None,
) -> None:
    """Add a synthetic full-page picture for standalone raster images.

    When media.inspect identifies an image that needs visual interpretation,
    and structural extraction finds no picture or formula, create a picture
    element for VisionAgent to describe. Existing unstructured-page
    placeholders already have their own planned inspection method.

    This signal is a preprocessing heuristic, not a change in PDDL goals
    or to the Toolbox media classification domain.
    """
    if not isinstance(media_inspection, dict):
        return
    if not media_inspection.get("is_image"):
        return
    if not media_inspection.get("requires_visual_inspection"):
        return

    has_visual_element = any(
        element.type in {"picture", "formula"} for element in manifest.elements
    )
    if has_visual_element or any(
        is_unstructured_page_placeholder(element) for element in manifest.elements
    ):
        # An empty page is already planned for visual inspection.
        return

    max_reading_order = max(
        (element.reading_order for element in manifest.elements), default=0
    )
    page_number = (
        manifest.pages[0].page_number if manifest.pages else 1
    )

    element = ManifestElement(
        id=f"element-injected-picture-{len(manifest.elements) + 1:06d}",
        type="picture",
        raw_label="picture",
        reading_order=max_reading_order + 1,
        hierarchy_level=1,
        text=None,
        page_number=page_number,
        metadata={
            "injected_picture": True,
            "media_inspection": media_inspection,
        },
    )
    manifest.elements.append(element)

    if manifest.pages:
        manifest.pages[0].element_ids.append(element.id)

    observations, obligations = _derive_processing_needs([element])
    manifest.observations.extend(observations)
    manifest.obligations.extend(obligations)

    manifest.summary.element_count = len(manifest.elements)
    manifest.summary.observation_count = len(manifest.observations)
    manifest.summary.obligation_count = len(manifest.obligations)
    manifest.summary.element_types = dict(
        sorted(Counter(item.type for item in manifest.elements).items())
    )
