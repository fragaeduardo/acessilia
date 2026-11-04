"""Read-only, traceable planning proposals projected from existing manifest obligations.

The manifest builder remains authoritative until golden regressions establish
that proposal-driven planning is semantically equivalent to the current PDDL.
No extraction, obligation emission, or solver behavior changes here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProposalEvidence:
    observation_id: str
    kind: str
    severity: str
    message: str


@dataclass(frozen=True, slots=True)
class PlanningProposal:
    """An analyzer's intent; NOT a Toolbox capability or executable handler."""

    proposal_id: str
    obligation_kind: str
    target_ids: tuple[str, ...]
    evidence: tuple[ProposalEvidence, ...]
    dependencies: tuple[str, ...]
    admissible_methods: tuple[str, ...]
    source: str
    priority: int
    rationale: str

    def __post_init__(self) -> None:
        if not self.proposal_id.strip():
            raise ValueError("proposal_id cannot be empty")
        if not self.obligation_kind.strip():
            raise ValueError("obligation_kind cannot be empty")
        if not self.source.strip():
            raise ValueError("proposal source cannot be empty")
        if not self.rationale.strip():
            raise ValueError("proposal rationale cannot be empty")
        if not self.target_ids or len(set(self.target_ids)) != len(self.target_ids):
            raise ValueError("proposal target_ids must be nonempty and distinct")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("proposal dependencies must be distinct")
        if len(set(self.admissible_methods)) != len(self.admissible_methods):
            raise ValueError("admissible_methods must be distinct")
        if self.priority < 0:
            raise ValueError("priority must be nonnegative")
        if self.proposal_id in self.dependencies:
            raise ValueError("a proposal cannot depend on itself")


def proposals_from_manifest(manifest: Any) -> tuple[PlanningProposal, ...]:
    """Project obligations without mutating the manifest or changing method order.

    Attach only observations explicitly targeting an obligation's elements.
    Zero priority means unspecified; do not invent ranking in this adapter.
    """
    observations = tuple(manifest.observations)
    result: list[PlanningProposal] = []
    for obligation in manifest.obligations:
        targets = frozenset(obligation.target_ids)
        evidence = tuple(
            ProposalEvidence(
                observation_id=observation.id,
                kind=observation.kind,
                severity=observation.severity,
                message=observation.message,
            )
            for observation in observations
            if targets.intersection(observation.target_ids)
        )
        result.append(
            PlanningProposal(
                proposal_id=obligation.id,
                obligation_kind=obligation.kind,
                target_ids=tuple(obligation.target_ids),
                evidence=evidence,
                dependencies=tuple(obligation.dependencies),
                admissible_methods=tuple(obligation.admissible_methods),
                source="manifest-builder",
                priority=0,
                rationale=obligation.rationale,
            )
        )
    return tuple(result)
