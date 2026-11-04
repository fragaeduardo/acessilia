"""Table structure enrichment before PDDL, preserving visual re-read policy.

Table retries, garble scoring, accepted replacements and fallback metadata are
identical to the previous orchestrator. The data provider is injected so
callers can replace it without changing this module's domain logic.
"""
from __future__ import annotations
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from docstruct.tables.ast import raw_rows_from_table_ast as _rows_from_table_ast
from docstruct.tables.garble import table_garble_reason
from backend.agents.output_schemas import DataOutput
from backend.agents.provider_breaker import ProviderBreaker
from backend.core.manifest.models import ManifestElement, ProcessingManifest
from backend.core.manifest.table_reading import (
    _table_element_has_structured_content, _backfill_table_from_existing_text,
    _table_reread_disqualified, _table_ast_column_count, _TABLE_REREAD_ATTEMPTS,
)
from backend.i18n import t
from backend.log_messages import LOG_PDDL_TABLES_ENRICHED, LOG_PDDL_TABLE_REREAD_ACCEPTED, LOG_PDDL_TABLE_REREAD_REJECTED
from backend.services.blocking import run_blocking
from backend.stage_messages import STAGE_RECOGNIZING_TABLE
from backend.tools.logger import logger


@dataclass(frozen=True, slots=True)
class TableHooks:
    DataAgent: Callable[..., Any]
    extract_picture_bytes: Callable[..., Any]
    source_artifact_id: Callable[..., Any]
    pages_label: Callable[..., Any]
    retry_failed: Callable[..., Any]


async def _enrich_table_structures(
    manifest: ProcessingManifest,
    source_file: Path,
    *,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    breaker: ProviderBreaker | None = None,
    hooks: TableHooks,
) -> None:
    from backend.config.settings import settings
    DataAgent = hooks.DataAgent
    _extract_picture_bytes = hooks.extract_picture_bytes
    _source_artifact_id = hooks.source_artifact_id
    _pages_label = hooks.pages_label
    _retry_failed = hooks.retry_failed

    # A table read in the wrong shape (rows collapsed into one, cells doubled)
    # has structure but not the right one; it is read again from its image.
    garbled: dict[str, str] = {}
    if settings.table_visual_review:
        for element in manifest.elements:
            if element.type == "table" and _table_element_has_structured_content(element):
                reason = table_garble_reason(
                    _rows_from_table_ast(element.metadata["table_ast"])
                )
                if reason:
                    garbled[element.id] = reason
    table_elements = [
        element
        for element in manifest.elements
        if element.type == "table"
        and (element.id in garbled or not _table_element_has_structured_content(element))
    ]
    if not table_elements:
        return

    data_agent = DataAgent()
    breaker = breaker or ProviderBreaker(settings.vision_breaker_threshold)

    async def read_one(position: int, element: ManifestElement) -> bool:
        """Read one table from its image, or keep it as it is; True when enriched."""
        reason = garbled.get(element.id)
        if reason is None and _backfill_table_from_existing_text(element):
            return True

        fallback_page_number = element.page_number
        if fallback_page_number is None and len(manifest.pages) == 1:
            fallback_page_number = manifest.pages[0].page_number

        if status_callback:
            await status_callback(
                t(STAGE_RECOGNIZING_TABLE).format(
                    current=position,
                    total=len(table_elements),
                    pages=_pages_label([fallback_page_number], len(manifest.pages)),
                )
            )
        # The breaker is consulted before the crop: a provider the pictures
        # declared down must not cost one Toolbox render per remaining table.
        # The second pass calls this anyway for its probe, because the probe
        # is let through before any `read_one` sees the breaker.
        if breaker.open:
            if reason is not None:
                _keep_misread_table(
                    element,
                    fallback_page_number or 0,
                    reason,
                    breaker.skip_detail(),
                    _table_review_attempts(element, 0),
                )
            _record_table_read_failure(element, "model_failed")
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
            _record_table_read_failure(element, "crop_unavailable")
            if reason is not None:
                _keep_misread_table(
                    element, page_number, reason, "no image of the table",
                    _table_review_attempts(element, 0),
                )
            return False

        # A misread table gets a few readings: the model may drop columns or
        # rows on one try and read the table whole on the next.
        attempts = _TABLE_REREAD_ATTEMPTS if reason is not None else 1
        table_output = None
        answered = False
        why = "no table was read"
        attempt = 0
        for attempt in range(1, attempts + 1):
            if breaker.open:
                why = breaker.skip_detail()
                attempt -= 1  # this try was not made
                break
            candidate = await data_agent.process_region(
                image_bytes=image_bytes,
                classification="table",
                page_num=page_number,
                fallback_text=element.text or "",
            )
            # The data agent returns None for any failure, an unusable answer
            # included, so the breaker may count one of those too.
            breaker.record(candidate is not None)
            answered = answered or isinstance(candidate, DataOutput)
            if not isinstance(candidate, DataOutput) or candidate.kind != "table":
                continue
            if reason is not None:
                why = _table_reread_disqualified(
                    element.metadata["table_ast"], candidate.table_rows()
                )
                if why:
                    continue
            table_output = candidate
            break
        if table_output is None:
            # A reading refused as worse than the table, or an answer that it
            # is not a table, is the model's answer; only no answer at all is
            # a failure a rerun may not repeat.
            if answered:
                # The provider is up and answered; a failure an earlier pass
                # recorded on this element is no longer true.
                element.metadata.pop("table_read_failure", None)
            else:
                _record_table_read_failure(element, "model_failed")
            if reason is not None:
                _keep_misread_table(
                    element, page_number, reason, why, _table_review_attempts(element, attempt)
                )
            return False

        element.metadata.pop("table_read_failure", None)
        rows = table_output.table_rows()
        table_ast = table_output.table_ast()
        if reason is not None:
            element.metadata["table_ast_replaced"] = element.metadata["table_ast"]
            element.metadata["table_review"] = {
                "reason": reason, "outcome": "replaced",
                "attempts": _table_review_attempts(element, attempt),
            }
            logger.info(
                t(LOG_PDDL_TABLE_REREAD_ACCEPTED).format(
                    element_id=element.id, page=page_number, reason=reason,
                )
            )

        element.metadata["table_ast"] = table_ast
        element.metadata["table_linearization_hint"] = "data-agent-structured"
        element.metadata["table_row_count"] = len(rows)
        element.metadata["table_column_count"] = _table_ast_column_count(table_ast)
        element.metadata["table_has_header"] = any(
            cell.header or cell.scope != "none"
            for row in table_output.rows
            for cell in row.cells
        )
        element.metadata["data_language"] = table_output.language
        element.metadata["data_confidence"] = table_output.confidence
        if table_output.warnings:
            element.metadata["data_warnings"] = list(table_output.warnings)
        element.text = ""
        if element.page_number is None and page_number >= 1:
            element.page_number = page_number
        return True

    enriched = 0
    for position, element in enumerate(table_elements, start=1):
        enriched += await read_one(position, element)
    enriched += await _retry_failed(
        [
            (position, element)
            for position, element in enumerate(table_elements, start=1)
            if element.metadata.get("table_read_failure") == "model_failed"
        ],
        read_one,
        kind="tables",
        breaker=breaker,
    )

    if enriched:
        logger.info(t(LOG_PDDL_TABLES_ENRICHED).format(count=enriched))


#: Why a table was left unread, recorded in metadata["table_read_failure"]: the
#: crop could not be rendered, or the data agent returned no answer at all.
TableReadFailure = Literal["crop_unavailable", "model_failed"]


def _record_table_read_failure(element: ManifestElement, reason: TableReadFailure) -> None:
    """Mark ``element`` as a table no reading came back for, and say why."""
    element.metadata["table_read_failure"] = reason


def _table_review_attempts(element: ManifestElement, attempts: int) -> int:
    """How many readings a table has had, with ``attempts`` more.

    Every pass that reads a table again stores the count it made, so the
    metadata says how many readings the table got in total (3 on one pass, 1
    more on the next), not only the last pass's.
    """
    review = element.metadata.get("table_review")
    previous = review.get("attempts", 0) if isinstance(review, dict) else 0
    return previous + attempts


def _keep_misread_table(
    element: ManifestElement, page_number: int, reason: str, why: str, attempts: int
) -> None:
    """Record that a misread table stays as it was read, and why."""
    element.metadata["table_review"] = {
        "reason": reason, "outcome": "kept", "why": why, "attempts": attempts,
    }
    logger.warning(
        t(LOG_PDDL_TABLE_REREAD_REJECTED).format(
            element_id=element.id, page=page_number, reason=reason, why=why,
        )
    )
