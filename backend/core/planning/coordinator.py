"""PDDL planning facade: propose, restrict methods and delegate to PlannerAgent.

The internal/Fast Downward solvers and their PDDL domains are unchanged. The
proposal and binding adapters are diagnostics of the existing semantics, not
an alternate problem compiler.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from backend.core.manifest.models import ProcessingManifest
from backend.core.planning.hierarchy import inspect_execution_bindings, observe_planning_intent
from backend.core.planning.models import NominalPlan, PlanningComparison
from backend.core.planning.planner_agent import PlannerAgent
from backend.i18n import t
from backend.log_messages import LOG_PDDL_PREFERRED_PLAN_MISSING
from backend.tools.logger import logger


def unavailable_methods(
    manifest: ProcessingManifest,
    *,
    registered_handlers: Iterable[str],
) -> list[str]:
    """Methods declared by THIS manifest but unsupported by the executor."""
    registered = set(registered_handlers)
    requested = {
        method
        for obligation in manifest.obligations
        for method in obligation.admissible_methods
    }
    return sorted(requested - registered)


def build_nominal_plan(
    *,
    manifest: ProcessingManifest,
    planner: PlannerAgent,
    planner_backend: str,
    preferred_plan: str,
    fast_downward: Path | None,
    fast_downward_alias: str | None,
    fast_downward_search: str,
    registered_handlers: Iterable[str],
    unavailable_handlers: Iterable[str] = (),
    extra_unavailable: Iterable[str] = (),
    selected_roots: Iterable[str] | None = None,
) -> tuple[NominalPlan, PlanningComparison | None]:
    """One existing PDDL synthesis with read-only proposal/binding checks.

    Preserve both-backend comparisons, root selection, solver configuration,
    catalogue restrictions, and all existing PDDL domain versions.
    """
    proposal_intent = observe_planning_intent(manifest)
    logger.debug(
        "PDDL: {} traced proposals, {} evidence items; original solver retained",
        len(proposal_intent),
        sum(len(item.evidence) for item in proposal_intent),
    )
    registered = frozenset(registered_handlers)
    gated = frozenset(extra_unavailable)
    unavailable = sorted(set(unavailable_handlers) | gated)

    if planner_backend == "both":
        _, plans, comparison = planner.compare(
            manifest,
            selected_roots=selected_roots,
            unavailable_methods=unavailable,
            fast_downward=fast_downward,
            fast_downward_alias=fast_downward_alias,
            fast_downward_search=fast_downward_search,
            preferred_backend=preferred_plan,
        )
        if preferred_plan not in plans:
            raise RuntimeError(
                t(LOG_PDDL_PREFERRED_PLAN_MISSING).format(
                    backend=preferred_plan
                )
            )
        plan = plans[preferred_plan]
    else:
        _, plan = planner.plan(
            manifest,
            selected_roots=selected_roots,
            unavailable_methods=unavailable,
            backend=planner_backend,
            fast_downward=fast_downward,
            fast_downward_alias=fast_downward_alias,
            fast_downward_search=fast_downward_search,
        )
        comparison = None

    # The existing executor remains authoritative. A missing handler is a
    # programming error and cannot become an invented Toolbox capability.
    bindings = inspect_execution_bindings(
        plan, registered_handlers=registered,
        blocked_methods=gated, gate_mode="enforce",
    )
    logger.debug(
        "PDDL: {} bindings checked ({} human-review escalations)",
        len(bindings),
        sum(item.status == "human-review" for item in bindings),
    )
    return plan, comparison
