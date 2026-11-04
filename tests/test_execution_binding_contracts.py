"""Execution binding contract uses the actual SDK capability allowlist."""
from types import SimpleNamespace as NS

import pytest

from acessilia_sdk.toolbox import Requirement
from backend.core.planning.hierarchy.bindings import inspect_execution_bindings
from backend.core.planning.toolbox_gate import METHOD_TOOLBOX_REQUIREMENTS
from backend.config.settings import settings


def _plan(method):
    return NS(steps=[
        NS(index=0, action="start-job", method=None, obligation_id=None),
        NS(index=1, action="execute-obligation", method=method, obligation_id="o"),
        NS(index=2, action="complete-job", method=None, obligation_id=None),
    ])


def _requirements(method):
    factory = METHOD_TOOLBOX_REQUIREMENTS.get(method)
    return factory() if factory else ()


def test_math_binding_declares_real_provider_and_exact_sdk_output_schema(monkeypatch):
    monkeypatch.setattr(settings, "toolbox_math_provider", "math-speech")
    bindings = inspect_execution_bindings(
        _plan("mathml"), registered_handlers=["mathml"],
        requirements_for_method=_requirements,
    )
    binding = bindings[0]
    assert binding.validator == "MethodResult.validated"
    assert [(c.capability_id, c.provider, c.contract_version, c.output_schema)
            for c in binding.capabilities] == [
        ("math.convert", "math-speech", 1, "mathml@1")
    ]


def test_adjudication_requires_render_and_judge_capabilities(monkeypatch):
    monkeypatch.setattr(settings, "toolbox_pdf_provider", "pymupdf-pdf")
    monkeypatch.setattr(settings, "toolbox_adjudicate_provider", "qwen3-vl-8b")
    binding, = inspect_execution_bindings(
        _plan("vlm-pairwise-judge"),
        registered_handlers=["vlm-pairwise-judge"],
        requirements_for_method=_requirements,
    )
    assert {c.capability_id for c in binding.capabilities} == {
        "pdf.render", "document.adjudicate.page"
    }
    assert all(c.output_schema for c in binding.capabilities)


def test_human_review_is_terminal_and_does_not_invent_capabilities():
    binding, = inspect_execution_bindings(
        _plan("human-review"), registered_handlers=(),
        requirements_for_method=_requirements,
    )
    assert binding.status == "human-review"
    assert binding.capabilities == ()
    assert binding.validator == "human-review-escalation"


def test_unknown_sdk_capability_fails_even_with_registered_handler():
    with pytest.raises(ValueError, match="no contract"):
        inspect_execution_bindings(
            _plan("some-method"), registered_handlers=["some-method"],
            requirements_for_method=lambda _: [Requirement("not-exposed", "fake")],
        )


def test_capability_binding_has_no_implicit_default_provider():
    with pytest.raises(ValueError, match="missing configured provider"):
        inspect_execution_bindings(
            _plan("some-method"), registered_handlers=["some-method"],
            requirements_for_method=lambda _: [Requirement("math.convert", "")],
        )
