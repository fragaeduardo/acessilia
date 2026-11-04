"""Bounded progress reporting and Toolbox availability diagnostics.

Errors in progress callbacks are non-fatal; failures in the execution
handler remain fatal according to the original execution contract.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from backend.core.execution.executor import StepProgress
from backend.core.execution.recovery import _pages_label
from backend.core.manifest.models import ProcessingManifest
from backend.core.planning.models import PlanStep
from backend.core.planning import toolbox_gate
from backend.i18n import t
from backend.stage_messages import STAGE_EXECUTING_PLAN_STEP
from backend.tools.logger import logger


def _plan_progress(
    manifest: ProcessingManifest,
    status_callback: Callable[[str], Coroutine[Any, Any, None]] | None,
    loop: asyncio.AbstractEventLoop,
) -> StepProgress | None:
    """Report each obligation step, with its pages, through ``status_callback``.

    The executor runs on a worker thread while the callback is a coroutine of
    the event loop, so the report is scheduled on ``loop`` and awaited from the
    thread: the step only starts once its stage is recorded, which is what lets
    a job killed mid-step still say which step and page it was on. A failing
    report, including a broken translation of the label, is logged and never
    fails the step.

    The user sees only the step and its pages; the obligation kind and method
    are internal names, so they go to the debug log.
    """
    if status_callback is None:
        return None
    page_by_element = {element.id: element.page_number for element in manifest.elements}
    pages_by_obligation = {
        obligation.id: [page_by_element.get(target) for target in obligation.target_ids]
        for obligation in manifest.obligations
    }

    def report(step: PlanStep, position: int, total: int) -> None:
        if step.action != "execute-obligation" or step.obligation_id is None:
            return
        pages = pages_by_obligation.get(step.obligation_id, [])
        logger.debug(
            "PDDL: step {}/{} {} via {}, pages {}",
            position,
            total,
            step.obligation_id,
            step.method,
            pages,
        )
        try:
            message = t(STAGE_EXECUTING_PLAN_STEP).format(
                current=position,
                total=total,
                pages=_pages_label(pages, len(manifest.pages)),
            )
            asyncio.run_coroutine_threadsafe(status_callback(message), loop).result(
                timeout=_PROGRESS_REPORT_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - progress must not fail the plan
            logger.warning("PDDL: progress of step {} not recorded: {}", position, exc)

    return report


#: Upper bound on how long a plan step waits for its progress to be recorded.
_PROGRESS_REPORT_TIMEOUT_SECONDS = 10.0


def _log_availability(availability: toolbox_gate.AvailabilityDecision) -> None:
    if not availability.blocked:
        return
    blocked = "; ".join(
        f"{method}: {', '.join(reasons)}"
        for method, reasons in availability.blocked.items()
    )
    if availability.enforced:
        logger.warning(
            "Gate da Toolbox (catálogo {}): métodos fora do plano — {}",
            availability.catalog_revision or "indisponível",
            blocked,
        )
    else:
        logger.warning(
            "Gate da Toolbox em observação (catálogo {}): removeria — {}",
            availability.catalog_revision or "indisponível",
            blocked,
        )
