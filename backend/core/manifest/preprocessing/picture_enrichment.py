"""Picture enrichment before PDDL, with bounded concurrent provider calls.

Preserves the original failure reasons, retry budget, slide classification,
pre-enrichment mutations, and shared ProviderBreaker. The facade supplies
late-bound hooks for compatibility with existing tests and integrations.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from backend.agents.output_schemas import VisionOutput
from backend.agents.provider_breaker import ProviderBreaker
from backend.core.manifest.builder import (
    is_unstructured_page_placeholder,
    reconcile_reclassified_element_processing_needs,
)
from backend.core.manifest.models import ManifestElement, ProcessingManifest
from backend.i18n import t
from backend.log_messages import (
    LOG_VISION_ELEMENT_DESCRIPTION, LOG_PDDL_EMPTY_VISION_DESCRIPTION,
    LOG_PDDL_IMAGES_ENRICHED,
)
from backend.services.blocking import run_blocking
from backend.stage_messages import STAGE_DESCRIBING_IMAGE
from backend.tools.logger import logger


@dataclass(frozen=True, slots=True)
class PictureHooks:
    VisionAgent: Callable[..., Any]
    inject_full_page_picture: Callable[..., Any]
    is_mostly_visual_manifest: Callable[..., Any]
    is_placeholder_visual_element: Callable[..., Any]
    extract_picture_bytes: Callable[..., Any]
    source_artifact_id: Callable[..., Any]
    pages_label: Callable[..., Any]
    retry_failed: Callable[..., Any]
    has_meaningful_text: Callable[..., Any]
    record_vision_failure: Callable[..., Any]


#: Why a picture was left undescribed, recorded in metadata["vision_failure_reason"]
#: for the operator: the crop could not be rendered, the vision call failed (its
#: error goes to metadata["vision_failure_detail"]), or the answer had no text.
VisionFailureReason = Literal["crop_unavailable", "model_failed", "empty_description"]


def _record_vision_failure(
    element: ManifestElement, reason: VisionFailureReason, detail: str | None = None
) -> None:
    """Mark ``element`` as undescribed and say why, so a later pass describes it again."""
    element.metadata.pop("vision_description_complete", None)
    element.metadata["vision_failure_reason"] = reason
    if detail:
        element.metadata["vision_failure_detail"] = detail
    else:
        element.metadata.pop("vision_failure_detail", None)


async def _enrich_picture_descriptions(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    mode: str,
    media_inspection: dict[str, Any] | None = None,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    breaker: ProviderBreaker | None = None,
    hooks: PictureHooks,
) -> None:
    from backend.config.settings import settings
    VisionAgent = hooks.VisionAgent
    _inject_full_page_picture = hooks.inject_full_page_picture
    _is_mostly_visual_manifest = hooks.is_mostly_visual_manifest
    _is_placeholder_visual_element = hooks.is_placeholder_visual_element
    _extract_picture_bytes = hooks.extract_picture_bytes
    _source_artifact_id = hooks.source_artifact_id
    _pages_label = hooks.pages_label
    _retry_failed = hooks.retry_failed
    _has_meaningful_text = hooks.has_meaningful_text
    _record_vision_failure = hooks.record_vision_failure

    _inject_full_page_picture(manifest, media_inspection)

    pictures = [
        element
        for element in manifest.elements
        if element.type == "picture"
        and not element.metadata.get("vision_description_complete")
    ]
    if not pictures and _is_mostly_visual_manifest(manifest):
        pictures = [
            element
            for element in manifest.elements
            if _is_placeholder_visual_element(element)
            and not element.metadata.get("vision_description_complete")
        ]

    if (
        not pictures
        and _is_mostly_visual_manifest(manifest)
        and len(manifest.pages) == 1
        and manifest.elements
    ):
        pictures = [element for element in manifest.elements
                    if not element.metadata.get("vision_description_complete")
                    and not is_unstructured_page_placeholder(element)][:1]

    if not pictures:
        return

    vision = VisionAgent(mode=mode)
    breaker = breaker or ProviderBreaker(settings.vision_breaker_threshold)
    total_pages = len(manifest.pages)

    # Descriptions are independent, so they run concurrently, bounded by
    # VISION_CONCURRENCY to stay inside what the model provider accepts (a
    # rate-limited hosted API, or a local Ollama that queues beyond its own
    # parallelism). The progress callback is serialized separately: two
    # admitted tasks would otherwise interleave the state writes behind it
    # (the API's status persistence, the Telegram progress message), and even
    # serialized the emission order may vary with completion, so nothing here
    # promises FIFO delivery.
    slots = asyncio.Semaphore(max(1, settings.vision_concurrency))
    progress_lock = asyncio.Lock()

    async def describe_one(position: int, element: ManifestElement) -> bool:
        """Describe one picture and fold the answer into it; True when enriched."""
        fallback_page_number = element.page_number
        if fallback_page_number is None and len(manifest.pages) == 1:
            fallback_page_number = manifest.pages[0].page_number

        async with slots:
            if status_callback:
                async with progress_lock:
                    await status_callback(
                        t(STAGE_DESCRIBING_IMAGE).format(
                            current=position,
                            total=len(pictures),
                            pages=_pages_label([fallback_page_number], len(manifest.pages)),
                        )
                    )
            if breaker.open:
                _record_vision_failure(element, "model_failed", breaker.skip_detail())
                return False
            image_bytes, page_number = await run_blocking(
                _extract_picture_bytes,
                source_file,
                element,
                fallback_page_number,
                _source_artifact_id(manifest),
                manifest.source.media_type,
            )
            if not image_bytes:
                _record_vision_failure(element, "crop_unavailable")
                return False

            errors: list[str] = []
            vision_output = await vision.describe_region(
                image_bytes=image_bytes,
                # A whole presentation slide gets the page prompt and budget.
                classification=(
                    "full_page_image" if element.metadata.get("slide_visual") else "embedded_image"
                ),
                page_num=page_number,
                total_pages=total_pages,
                mode=mode,
                errors=errors,
            )
            breaker.record(isinstance(vision_output, VisionOutput))

        if not isinstance(vision_output, VisionOutput):
            _record_vision_failure(element, "model_failed", errors[-1] if errors else None)
            return False

        logger.debug(
            t(LOG_VISION_ELEMENT_DESCRIPTION).format(
                element_id=element.id,
                page_num=page_number,
                chars=len(vision_output.description),
                description=vision_output.description,
            )
        )
        element.metadata["vision_language"] = vision_output.language
        element.metadata["vision_confidence"] = vision_output.confidence
        element.metadata["vision_elements"] = list(vision_output.mentioned_elements)
        if vision_output.warnings:
            element.metadata["vision_warnings"] = list(vision_output.warnings)

        if vision_output.kind == "formula":
            element.text = vision_output.formula_latex or ""
            answered = bool(element.text.strip())
            if element.type != "formula":
                previous_type = element.type
                element.metadata["original_type"] = element.type
                element.type = "formula"
                element.raw_label = "formula"
                reconcile_reclassified_element_processing_needs(
                    manifest,
                    element,
                    previous_type=previous_type,
                )
        else:
            # An empty answer keeps text the extractor already had; whether
            # that text is worth keeping is decided below.
            answered = bool(vision_output.description.strip())
            if answered:
                element.text = vision_output.description
            if element.type != "picture":
                previous_type = element.type
                element.metadata["original_type"] = element.type
                element.type = "picture"
                element.raw_label = "picture"
                reconcile_reclassified_element_processing_needs(
                    manifest,
                    element,
                    previous_type=previous_type,
                )
        if element.page_number is None and page_number >= 1:
            element.page_number = page_number
        # The model's own text stands however short ("Logo" is a description);
        # text left over from the extractor after an empty answer must still be
        # meaningful. Otherwise the picture is reported as undescribed, so the
        # operator sees the model refused instead of a silent gap, and stays
        # eligible for a later pass.
        if answered or _has_meaningful_text(element):
            element.metadata["vision_description_complete"] = True
            element.metadata.pop("vision_failure_reason", None)
            element.metadata.pop("vision_failure_detail", None)
            return True
        # What is left is an extractor placeholder ("body") or OCR noise, which
        # must not become the alt text.
        element.text = ""
        _record_vision_failure(element, "empty_description")
        logger.warning(
            t(LOG_PDDL_EMPTY_VISION_DESCRIPTION).format(
                element_id=element.id
            )
        )
        return False

    # The fold above touches only its own element and runs without awaits, so
    # concurrent folds cannot interleave. The TaskGroup cancels the sibling
    # descriptions when one of them fails and re-raises as an ExceptionGroup,
    # matching the sequential stage this replaced — the first failure aborted
    # the stage — without leaving tasks mutating the manifest after it failed.
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(describe_one(position, element))
            for position, element in enumerate(pictures, start=1)
        ]
    enriched = sum(task.result() for task in tasks)
    enriched += await _retry_failed(
        [
            (position, element)
            for position, element in enumerate(pictures, start=1)
            if element.metadata.get("vision_failure_reason") in _RETRIED_VISION_FAILURES
        ],
        describe_one,
        kind="pictures",
        breaker=breaker,
    )

    if enriched:
        logger.info(t(LOG_PDDL_IMAGES_ENRICHED).format(count=enriched))


#: Picture failures a second try may fix: the provider gave no answer, or an
#: empty one (empty answers are never cached, so the try reaches the model). A
#: crop that could not be rendered will not render the second time either.
_RETRIED_VISION_FAILURES = frozenset({"model_failed", "empty_description"})
