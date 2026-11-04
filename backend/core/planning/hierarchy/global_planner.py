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

    unknown_dependencies = {
        proposal.proposal_id: set(proposal.dependencies) - set(by_id)
        for proposal in by_id.values()
        if set(proposal.dependencies) - set(by_id)
    }
    if unknown_dependencies:
        raise ValueError(f"unknown proposal dependencies: {unknown_dependencies}")

    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(proposal_id: str) -> None:
        if proposal_id in visiting:
            raise ValueError(f"cyclic proposal dependency: {proposal_id}")
        if proposal_id in visited:
            return
        visiting.add(proposal_id)
        for dependency in by_id[proposal_id].dependencies:
            visit(dependency)
        visiting.remove(proposal_id)
        visited.add(proposal_id)

    for proposal_id in by_id:
        visit(proposal_id)
    return tuple(by_id.values())


def observe_planning_intent(manifest: Any) -> tuple[PlanningProposal, ...]:
    """Non-invasive bridge from existing obligations to proposals.

    The existing PlannerAgent still owns PDDL compilation and solving.
    """
    return consolidate_proposals(manifest, proposals_from_manifest(manifest))
