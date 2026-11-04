"""Resolve planned methods to actual handlers and SDK contracts.

Bindings are derived from the existing MethodRegistry, Toolbox gate and SDK
allowlist. They never synthesize providers, wire contracts or executable
operations, and do not change the nominal PDDL plan or execution report.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from acessilia_sdk.toolbox import Requirement
from acessilia_sdk.toolbox.capabilities import TOOLBOX_CAPABILITIES


BindingStatus = Literal["executable", "human-review", "unavailable"]


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    capability_id: str
    provider: str
    contract_version: int
    output_schema: str | None


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    step_index: int
    obligation_id: str
    method: str
    status: BindingStatus
    catalog_blocked: bool
    capabilities: tuple[CapabilityBinding, ...] = ()
    validator: str = "MethodResult.validated"


def inspect_execution_bindings(
    plan: Any,
    *,
    registered_handlers: Iterable[str],
    blocked_methods: Iterable[str] = (),
    gate_mode: Literal["observe", "enforce", "off"] = "observe",
    requirements_for_method: Callable[[str], Iterable[Requirement]] | None = None,
) -> tuple[ExecutionBinding, ...]:
    """Validate handler and SDK binding once, with no extra catalogue calls.

    An empty capability list means the selected handler performs local work
    or validates output from a completed bootstrap step. It is not a license
    to invent a local replacement for a remote capability.
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

        capabilities: list[CapabilityBinding] = []
        if method != "human-review" and requirements_for_method is not None:
            for requirement in requirements_for_method(method):
                spec = TOOLBOX_CAPABILITIES.get(requirement.capability_id)
                if spec is None:
                    raise ValueError(
                        f"SDK has no contract for capability: {requirement.capability_id}"
                    )
                if not requirement.provider:
                    raise ValueError(
                        f"missing configured provider for capability: {spec.id}"
                    )
                capabilities.append(
                    CapabilityBinding(
                        capability_id=spec.id,
                        provider=requirement.provider,
                        contract_version=spec.contract_version,
                        output_schema=spec.output_schema,
                    )
                )
        bindings.append(
            ExecutionBinding(
                step_index=step.index,
                obligation_id=step.obligation_id,
                method=method,
                status=status,
                catalog_blocked=method in blocked,
                capabilities=tuple(capabilities),
                validator=(
                    "human-review-escalation"
                    if method == "human-review" else "MethodResult.validated"
                ),
            )
        )
    return tuple(bindings)
