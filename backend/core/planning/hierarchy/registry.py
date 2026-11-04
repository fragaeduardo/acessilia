"""Deterministic single-owner routing for manifest-derived planning proposals.

Specialists propose requirements and never run a second LLM pass or solver.
Unknown obligation kinds remain with the original manifest builder.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from backend.core.planning.universal import formula_code, image, structure, table, text
from .proposals import PlanningProposal, proposals_from_manifest


UNIVERSAL_ANALYZERS = (
    ("structure", structure),
    ("text", text),
    ("image", image),
    ("table", table),
    ("formula_code", formula_code),
)


def analyzer_for_kind(kind: str) -> str | None:
    """Return one universal owner, or keep an unrecognized kind with builder."""
    matches = [name for name, module in UNIVERSAL_ANALYZERS if module.accepts(kind)]
    if len(matches) > 1:
        raise ValueError(f"overlapping universal analyzers for {kind}: {matches}")
    return matches[0] if matches else None


def collect_proposals(manifest: Any) -> tuple[PlanningProposal, ...]:
    """Classify already-issued obligations without changing their order or fields."""
    projected = proposals_from_manifest(manifest)
    result: list[PlanningProposal] = []
    for proposal in projected:
        owner = analyzer_for_kind(proposal.obligation_kind)
        result.append(
            replace(proposal, source=f"universal.{owner}")
            if owner is not None else proposal
        )
    return tuple(result)
