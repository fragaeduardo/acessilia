"""Golden-style parity of PDDL planning and execution across the Stage 1 facade.

This is an offline end-to-end path from a realistic manifest through the
actual PDDL compiler and Agno executor; remote provider operations are
pre-enriched or explicitly mocked. It does NOT certify real staging Toolbox.
"""

from copy import deepcopy

from backend.core.execution.models import MethodResult
from backend.core.planning.planner_agent import PlannerAgent
from backend.pipeline.table_ast import table_ast_from_rows
from tests.pddl_support import (
    build_element, build_manifest, build_obligation, build_orchestrator,
)


def _multi_modal_manifest():
    table_ast = table_ast_from_rows([
        ["Name", "Value"], ["Speed", "10"], ["Range", "20"]
    ])
    elements = [
        build_element("image", "picture", text="A red car on the road",
                      metadata={"vision_description_complete": True}, reading_order=1),
        build_element("table", "table", text="", metadata={"table_ast": table_ast},
                      reading_order=2),
        build_element("formula", "formula", text=r"E=mc^2", reading_order=3),
    ]
    kinds_methods = [
        ("describe-image", ["image"], ["vision-description", "human-review"]),
        ("linearize-table", ["table"], ["pandoc-table", "human-review"]),
        ("verbalize-formula", ["formula"], ["mathml", "human-review"]),
    ]
    obligations = []
    for index, (kind, targets, methods) in enumerate(kinds_methods, 1):
        obligation = build_obligation(kind, targets, methods)
        obligation.id = f"ob-{index}"
        obligation.method_costs = {method: (100 if method == "human-review" else 5)
                                   for method in methods}
        obligations.append(obligation)
    return build_manifest(elements, obligations)


def _step_signature(plan):
    return [
        (step.index, step.action, step.obligation_id,
         step.obligation_kind, step.method, step.expected_cost)
        for step in plan.steps
    ]


def test_original_compiler_and_stage1_planner_keep_identical_golden_problem():
    manifest = _multi_modal_manifest()
    orchestrator = build_orchestrator()
    compiler = PlannerAgent()
    original_problem, original_plan = compiler.plan(
        manifest,
        unavailable_methods=orchestrator._unavailable_methods(manifest),
    )
    new_plan, comparison = orchestrator._build_plan(manifest)
    assert comparison is None
    assert original_plan.problem_sha256 == new_plan.problem_sha256
    assert original_plan.domain == new_plan.domain
    assert original_plan.selected_obligations == new_plan.selected_obligations
    assert original_plan.expected_total_cost == new_plan.expected_total_cost
    assert _step_signature(original_plan) == _step_signature(new_plan)
    assert original_problem.sha256 == new_plan.problem_sha256
    assert [s.method for s in new_plan.steps if s.method] == [
        "vision-description", "pandoc-table", "mathml"
    ]


def test_dry_run_and_live_execution_preserve_manifest_and_technical_statuses():
    manifest = _multi_modal_manifest()
    orchestrator = build_orchestrator()
    plan, _ = orchestrator._build_plan(manifest)
    before = deepcopy(manifest.model_dump(mode="json", by_alias=True))
    dry_manifest, dry_report = orchestrator.executor.execute(
        plan, manifest, dry_run=True
    )
    assert dry_manifest.model_dump(mode="json", by_alias=True) == before
    assert dry_report.status == "dry-run-completed"
    assert dry_report.mode == "dry-run"
    assert all(step.status != "failed" for step in dry_report.steps)

    # Only the remote math conversion is stubbed. Image and table handlers are
    # exercised as real methods, against pre-enriched source elements.
    def math_stub(current, obligation_id):
        target = next(o for o in current.obligations if o.id == obligation_id)
        for element in current.elements:
            if element.id in target.target_ids:
                element.metadata["mathml"] = "<math><mi>E</mi></math>"
        return MethodResult(success=True, validated=True, message="valid conversion")

    orchestrator.executor.registry.register("mathml", math_stub)
    live_manifest, live_report = orchestrator.executor.execute(
        plan, manifest, dry_run=False
    )
    assert live_report.mode == "live"
    assert live_report.status == "completed"
    assert all(o.status == "satisfied" for o in live_manifest.obligations)
    assert any(e.metadata.get("mathml") for e in live_manifest.elements)
    assert manifest.model_dump(mode="json", by_alias=True) == before
