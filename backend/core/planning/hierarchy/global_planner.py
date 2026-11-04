"""Pure, deterministic consolidation before the existing PDDL planner.

This module never invokes a solver, provider, or Toolbox client, and does
not synthesize or mutate existing manifest obligations.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .proposals import PlanningProposal, proposals_from_manifest


class ProposalConflict(ValueError):
    """One proposal ID was reused for different intent."""


def consolidate_proposals(
    manifest: Any,
    proposals: Iterable[PlanningProposal],
) -> tuple[PlanningProposal, ...]:
    """Preserve proposal order, deduplicate identical IDs, check references.

    Different proposal IDs can address the same element and remain independent:
    merging them implicitly could change the PDDL problem.
    """
    known_elements = {element.id for element in manifest.elements}
    by_id: dict[str, PlanningProposal] = {}
    for proposal in proposals:
        existing = by_id.get(proposal.proposal_id)
        if existing is not None:
            if existing != proposal:
                raise ProposalConflict(
                    f"conflicting proposal_id: {proposal.proposal_id}"
                )
            continue
        missing_targets = set(proposal.target_ids) - known_elements
        if missing_targets:
            raise ValueError(
                f"proposal {proposal.proposal_id} has unknown targets: "
                f"{sorted(missing_targets)}"
            )
        by_id[proposal.proposal_id] = proposal

    known_ids = set(by_id)
    dependents: dict[str, list[str]] = {proposal_id: [] for proposal_id in by_id}
    pending_dependencies: dict[str, int] = {}
    unknown_dependencies: dict[str, set[str]] = {}
    for proposal in by_id.values():
        missing = set(proposal.dependencies) - known_ids
        if missing:
            unknown_dependencies[proposal.proposal_id] = missing
            continue
        pending_dependencies[proposal.proposal_id] = len(proposal.dependencies)
        for dependency in proposal.dependencies:
            dependents[dependency].append(proposal.proposal_id)
    if unknown_dependencies:
        raise ValueError(f"unknown proposal dependencies: {unknown_dependencies}")

    # Iterative dependency traversal avoids recursion limits on long documents.
    # The returned proposals still follow their original order; the traversal
    # checks only acyclicity and never rewrites the execution sequence.
    from collections import deque

    ready = deque(
        proposal_id
        for proposal_id, count in pending_dependencies.items()
        if count == 0
    )
    visited = 0
    while ready:
        proposal_id = ready.popleft()
        visited += 1
        for child in dependents[proposal_id]:
            pending_dependencies[child] -= 1
            if pending_dependencies[child] == 0:
                ready.append(child)
    if visited != len(by_id):
        cycle = next(
            proposal_id
            for proposal_id, count in pending_dependencies.items()
            if count > 0
        )
        raise ValueError(f"cyclic proposal dependency: {cycle}")

    return tuple(by_id.values())


def observe_planning_intent(manifest: Any) -> tuple[PlanningProposal, ...]:
    """Non-invasive bridge from existing obligations to proposals.

    The existing PlannerAgent still owns PDDL compilation and solving.
    """
    from .registry import collect_proposals

    return consolidate_proposals(manifest, collect_proposals(manifest))
