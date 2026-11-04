"""Shared bounded retry pass for independent image/table enrichments."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from backend.agents.provider_breaker import ProviderBreaker
from backend.core.manifest.models import ManifestElement
from backend.i18n import n
from backend.log_messages import (
    LOG_PDDL_VISION_RETRY_PASS_PICTURES,
    LOG_PDDL_VISION_RETRY_PASS_TABLES,
)
from backend.tools.logger import logger


async def _retry_failed(
    failed: list[tuple[int, ManifestElement]],
    attempt: Callable[[int, ManifestElement], Coroutine[Any, Any, bool]],
    *,
    kind: str,
    breaker: ProviderBreaker,
) -> int:
    """Try once more, one at a time, what a vision stage left without a
    usable answer: the calls the model failed, and the pictures it answered
    empty.

    A provider that failed for a while (006.pdf v5 lost every picture after
    slide 10) may be back after a wait. The breaker lets the first try through
    even when open, and a failure of it opens the breaker again, so a provider
    that is still down costs one call, not one per object. Returns how many
    objects the pass enriched.
    """
    from backend.config.settings import settings

    if not failed:
        return 0
    delay = max(0, settings.vision_retry_pass_delay_seconds)
    kind_msgids = (
        LOG_PDDL_VISION_RETRY_PASS_PICTURES
        if kind == "pictures"
        else LOG_PDDL_VISION_RETRY_PASS_TABLES
    )
    logger.warning(
        n(*kind_msgids, len(failed)).format(count=len(failed), delay=delay)
    )
    await asyncio.sleep(delay)
    breaker.probe()
    enriched = 0
    for position, element in failed:
        enriched += await attempt(position, element)
    return enriched
