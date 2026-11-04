"""Characterize the Stage 1 planning-intent adapters before PDDL integration.

These unit tests are intentionally independent of Toolbox services and the
live PDDL solver; full integration regression is still required.
"""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from backend.core.planning.hierarchy import (
    ProposalConflict,
    consolidate_proposals,
    inspect_execution_bindings,
    observe_planning_intent,
    proposals_from_manifest,
)


def _obligation(ident: str, kind: str, target: str, *, depends=(), methods=()):
    return NS(
        id=ident, kind=kind, target_ids=[target],
        dependencies=list(depends), admissible_methods=list(methods),
        rationale=f"need to {kind}",
    )


def _manifest():
    return NS(
        elements=[NS(id="img"), NS(id="table"), NS(id="formula")],
        observations=[
            NS(id="obs-image", kind="missing-alt", severity="warning", message="Alt", target_ids=["img"]),
            NS(id="obs-math", kind="formula-risk", severity="error", message="Math", target_ids=["formula"]),
            NS(id="obs-global", kind="language", severity="info", message="global", target_ids=[]),
        ],
        obligations=[
            _obligation("o-image", "describe-image", "img", methods=("vision-description", "human-review")),
            _obligation("o-table", "linearize-table", "table", methods=("pandoc-table", "human-review")),
            _obligation("o-math", "verbalize-formula", "formula", depends=("o-image",), methods=("mathml", "human-review")),
        ],
    )


def test_projection_tracks_image_table_formula_and_preserves_method_order():
    manifest = _manifest()
    before = [tuple(x.admissible_methods) for x in manifest.obligations]
    proposals = observe_planning_intent(manifest)
    assert [p.proposal_id for p in proposals] == ["o-image", "o-table", "o-math"]
    assert [p.obligation_kind for p in proposals] == [
        "describe-image", "linearize-table", "verbalize-formula",
    ]
    assert proposals[0].admissible_methods == ("vision-description", "human-review")
    assert proposals[2].dependencies == ("o-image",)
    assert [e.observation_id for e in proposals[0].evidence] == ["obs-image"]
    assert [e.observation_id for e in proposals[1].evidence] == []
    assert [e.observation_id for e in proposals[2].evidence] == ["obs-math"]
    assert [tuple(x.admissible_methods) for x in manifest.obligations] == before


def test_projection_does_not_attach_global_observation_by_guessing():
    proposals = proposals_from_manifest(_manifest())
    assert "obs-global" not in {
        evidence.observation_id for p in proposals for evidence in p.evidence
    }


def test_conflicting_repeated_id_fails_instead_of_silent_overwrite():
    manifest = _manifest()
    proposal = proposals_from_manifest(manifest)[0]
    with pytest.raises(ProposalConflict, match="conflicting proposal_id"):
        consolidate_proposals(manifest, [proposal, replace(proposal, rationale="other")])


def test_identical_repeat_is_deduplicated_stably():
    manifest = _manifest()
    proposals = proposals_from_manifest(manifest)
    assert consolidate_proposals(manifest, [proposals[0], proposals[0], proposals[1]]) == proposals[:2]


def test_two_distinct_obligations_can_target_same_element():
    manifest = _manifest()
    p = proposals_from_manifest(manifest)[0]
    assert len(consolidate_proposals(manifest, [p, replace(p, proposal_id="other")])) == 2


def test_unknown_target_fails_before_compilation():
    manifest = _manifest()
    proposal = proposals_from_manifest(manifest)[0]
    with pytest.raises(ValueError, match="unknown targets"):
        consolidate_proposals(manifest, [replace(proposal, target_ids=("deleted",))])


def test_unknown_dependency_fails_before_compilation():
    manifest = _manifest()
    proposal = proposals_from_manifest(manifest)[0]
    with pytest.raises(ValueError, match="unknown proposal dependencies"):
        consolidate_proposals(manifest, [replace(proposal, dependencies=("other",))])


def test_cyclic_dependencies_fail_before_compilation():
    manifest = _manifest()
    first, second, *_ = proposals_from_manifest(manifest)
    with pytest.raises(ValueError, match="cyclic proposal dependency"):
        consolidate_proposals(manifest, [
            replace(first, dependencies=("o-table",)),
            replace(second, dependencies=("o-image",)),
        ])


def test_empty_or_self_dependent_proposal_fails():
    proposal = proposals_from_manifest(_manifest())[0]
    with pytest.raises(ValueError, match="cannot depend on itself"):
        replace(proposal, dependencies=(proposal.proposal_id,))
    with pytest.raises(ValueError, match="nonempty"):
        replace(proposal, target_ids=())


def _plan(*methods):
    steps = [NS(index=0, action="start-job", method=None, obligation_id=None)]
    steps.extend(
        NS(index=i+1, action="execute-obligation", method=m, obligation_id=f"o-{i}")
        for i, m in enumerate(methods)
    )
    steps.append(NS(index=len(methods)+1, action="complete-job", method=None, obligation_id=None))
    return NS(steps=steps)


def test_human_review_is_terminal_escalation_not_automated_success():
    bindings = inspect_execution_bindings(_plan("human-review"), registered_handlers=())
    assert bindings[0].status == "human-review"


def test_missing_method_handler_fails_explicitly():
    with pytest.raises(ValueError, match="no real handler"):
        inspect_execution_bindings(_plan("not-implemented"), registered_handlers=())


def test_gate_observe_reports_but_enforce_blocks_method():
    plan = _plan("mathml")
    observed = inspect_execution_bindings(
        plan, registered_handlers={"mathml"}, blocked_methods={"mathml"}, gate_mode="observe"
    )
    assert observed[0].status == "executable"
    assert observed[0].catalog_blocked
    with pytest.raises(ValueError, match="catalog-blocked"):
        inspect_execution_bindings(
            plan, registered_handlers={"mathml"}, blocked_methods={"mathml"}, gate_mode="enforce"
        )
    off = inspect_execution_bindings(
        plan, registered_handlers={"mathml"}, blocked_methods={"mathml"}, gate_mode="off"
    )
    assert not off[0].catalog_blocked


def test_invalid_gate_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown gate_mode"):
        inspect_execution_bindings(_plan("mathml"), registered_handlers={"mathml"}, gate_mode="bad")
