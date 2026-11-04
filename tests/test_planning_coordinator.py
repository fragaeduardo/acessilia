"""Focused contract tests for the Stage 1 nominal planning coordinator."""

from types import SimpleNamespace as NS

import pytest

from backend.core.planning.coordinator import build_nominal_plan, unavailable_methods


class _Planner:
    def __init__(self, plan):
        self.result = plan
        self.kwargs = None
        self.compared = False

    def plan(self, manifest, **kwargs):
        self.kwargs = kwargs
        return None, self.result

    def compare(self, manifest, **kwargs):
        self.compared = True
        self.kwargs = kwargs
        return None, {"internal": self.result, "fast-downward": self.result}, NS(backend="both")


def _manifest():
    return NS(
        elements=[NS(id="el-1")],
        observations=[NS(id="obs-1", kind="needs-description", severity="warning", message="missing alt", target_ids=["el-1"])],
        obligations=[
            NS(id="ob-1", kind="describe-image", target_ids=["el-1"],
               dependencies=[], admissible_methods=["vision", "human-review"],
               rationale="missing alt", selected=True, status="candidate")
        ],
    )


def _plan(method="vision"):
    return NS(steps=[
        NS(index=0, action="start-job", obligation_id=None, method=None),
        NS(index=1, action="execute-obligation", obligation_id="ob-1", method=method),
        NS(index=2, action="complete-job", obligation_id=None, method=None),
    ])


def _build(planner, *, registered=("vision", "human-review"), blocked=(), backend="internal", roots=None):
    return build_nominal_plan(
        manifest=_manifest(),
        planner=planner,
        planner_backend=backend,
        preferred_plan="internal",
        fast_downward=None,
        fast_downward_alias=None,
        fast_downward_search="astar(blind())",
        registered_handlers=registered,
        unavailable_handlers=unavailable_methods(_manifest(), registered_handlers=registered),
        extra_unavailable=blocked,
        selected_roots=roots,
    )


def test_coordinator_delegates_to_existing_planner_with_unchanged_options():
    planner = _Planner(_plan())
    plan, comparison = _build(planner, roots=["ob-1"])
    assert plan is planner.result
    assert comparison is None
    assert planner.kwargs["selected_roots"] == ["ob-1"]
    assert planner.kwargs["backend"] == "internal"
    assert planner.kwargs["fast_downward_search"] == "astar(blind())"
    assert planner.kwargs["unavailable_methods"] == []


def test_unregistered_method_is_filtered_before_compilation():
    manifest = _manifest()
    assert unavailable_methods(manifest, registered_handlers=["human-review"]) == ["vision"]


def test_catalog_blocked_methods_are_forwarded_unchanged():
    planner = _Planner(_plan(method="human-review"))
    _build(planner, blocked=("vision",))
    assert planner.kwargs["unavailable_methods"] == ["vision"]


def test_missing_real_handler_is_rejected_after_solver():
    with pytest.raises(ValueError, match="no real handler"):
        _build(_Planner(_plan("unknown-method")))


def test_human_review_remains_distinct_from_automatic_success():
    planner = _Planner(_plan("human-review"))
    plan, _ = _build(planner, registered=())
    assert plan is planner.result


def test_comparison_uses_same_planner_instance_and_preferred_backend():
    planner = _Planner(_plan())
    plan, comparison = _build(planner, backend="both")
    assert plan is planner.result
    assert planner.compared is True
    assert planner.kwargs["preferred_backend"] == "internal"
    assert comparison.backend == "both"


def test_enforced_catalog_method_cannot_escape_filter():
    with pytest.raises(ValueError, match="catalog-blocked"):
        _build(_Planner(_plan()), blocked=("vision",))
