"""Read-only execution binding diagnostics for nominal PDDL steps.

No operation IDs, provider selection or wire schemas are invented here.
Those belong to the deferred Stage 0. Inspect registered handlers and
catalogue restrictions without modifying a plan.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal


BindingStatus = Literal["executable", "human-review", "unavailable"]


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    step_index: int
    obligation_id: str
    method: str
    status: BindingStatus
    catalog_blocked: bool


def inspect_execution_bindings(
    plan: Any,
    *,
    registered_handlers: Iterable[str],
    blocked_methods: Iterable[str] = (),
    gate_mode: Literal["observe", "enforce", "off"] = "observe",
) -> tuple[ExecutionBinding, ...]:
    """Check planned methods; do not rewrite a plan or execute a handler.

    observe: report blocked methods without changing the plan.
    enforce: fail if a blocked method nevertheless reaches a plan.
    human-review: a terminal escalation, never automated success.
    """
    if gate_mode not in {"observe", "enforce", "off"}:
        raise ValueError(f"unknown gate_mode: {gate_mode}")
    registered = frozenset(registered_handlers)
    blocked = frozenset(blocked_methods) if gate_mode != "off" else frozenset()
    bindings: list[ExecutionBinding] = []
    for step in plan.steps:
        if step.action != "execute-obligation":
            continue
        method = step.method
        if not method or not step.obligation_id:
            raise ValueError(f"incomplete executable step at index {step.index}")
        if method == "human-review":
            status: BindingStatus = "human-review"
        elif method not in registered:
            status = "unavailable"
        else:
            status = "executable"
        if status == "unavailable":
            raise ValueError(f"no real handler for planned method: {method}")
        if gate_mode == "enforce" and method in blocked:
            raise ValueError(f"catalog-blocked method in enforced plan: {method}")
        bindings.append(
            ExecutionBinding(
                step_index=step.index,
                obligation_id=step.obligation_id,
                method=method,
                status=status,
                catalog_blocked=method in blocked,
            )
        )
    return tuple(bindings)
