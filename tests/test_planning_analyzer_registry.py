"""Architectural contracts for single-owner, passive planning analyzers."""
from types import SimpleNamespace as NS

from backend.core.manifest.builder import OBLIGATION_BY_TYPE, V23_OBLIGATION_BY_KIND
from backend.core.planning.hierarchy.registry import analyzer_for_kind, collect_proposals
from backend.core.planning.hierarchy.global_planner import observe_planning_intent
from backend.core.planning.specialists.registry import SLOTS, enabled_specialists


def _manifest(kinds):
    elements = [NS(id="e")]
    obligations = [
        NS(id=f"o-{i}", kind=kind, target_ids=["e"], dependencies=[],
           admissible_methods=["human-review"], rationale=kind)
        for i, kind in enumerate(kinds)
    ]
    return NS(elements=elements, observations=[], obligations=obligations)


def test_every_supported_builder_obligation_has_exactly_one_universal_owner():
    kinds = {definition[0] for definition in OBLIGATION_BY_TYPE.values()}
    kinds.update(V23_OBLIGATION_BY_KIND)
    kinds.add("repair-heading-hierarchy")
    for kind in kinds:
        assert analyzer_for_kind(kind), kind


def test_universal_routing_does_not_duplicate_or_mutate_obligations():
    m = _manifest(["describe-image", "linearize-table", "verbalize-formula",
                   "normalize-text", "refine-reading-order", "custom-kind"])
    before = [(x.id, list(x.admissible_methods)) for x in m.obligations]
    routed = collect_proposals(m)
    assert len(routed) == len(m.obligations)
    assert [p.proposal_id for p in routed] == [x.id for x in m.obligations]
    assert [p.source for p in routed] == [
        "universal.image", "universal.table", "universal.formula_code",
        "universal.text", "universal.structure", "manifest-builder",
    ]
    assert before == [(x.id, x.admissible_methods) for x in m.obligations]
    assert observe_planning_intent(m) == routed


def test_specialist_slots_are_inert_without_real_detector_binding_validator():
    assert {s.name for s in SLOTS} == {"chemistry", "mathematics", "music"}
    assert enabled_specialists() == ()
    assert not any(slot.ready for slot in SLOTS)
