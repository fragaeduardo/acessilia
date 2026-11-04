"""The Toolbox availability gate for planning methods.

Methods whose handlers call the Toolbox are checked against one catalogue
snapshot per document. ``observe`` only reports; ``enforce`` keeps the methods
out of the plan and fails closed without a catalogue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acessilia_sdk.toolbox import snapshot_from_payloads
from backend.config.settings import settings
from backend.core.manifest.builder import DEFAULT_METHOD_COSTS
from backend.core.planning import toolbox_gate
from tests.pddl_support import (
    build_element as _element,
    build_manifest as _manifest,
    build_obligation as _obligation,
    build_orchestrator as _orchestrator,
)

CONTRACT_SNAPSHOT = Path(__file__).parent / "fixtures" / "toolbox-contract.json"
FUSE_METHODS = ["dual-provider-fusion", "single-provider", "human-review"]


def _staging_snapshot():
    """The captured staging catalogue (tests/fixtures/toolbox-contract.json)."""
    payload = json.loads(CONTRACT_SNAPSHOT.read_text(encoding="utf-8"))
    return snapshot_from_payloads(
        payload["capabilities"], payload["providers"], fetched_at=0.0
    )


def _fuse_manifest(methods=FUSE_METHODS):
    obligation = _obligation("fuse-providers", ["el-1"], methods)
    obligation.method_costs = {m: DEFAULT_METHOD_COSTS[m] for m in methods}
    return _manifest([_element("el-1", "paragraph", text="texto")], [obligation])


@pytest.fixture(autouse=True)
def _default_providers(monkeypatch):
    monkeypatch.setattr(settings, "toolbox_provider", "docling")
    monkeypatch.setattr(settings, "fusion_secondary_provider", "mineru")


def test_default_providers_pass_against_the_staging_catalogue() -> None:
    """Expects ``docling`` and ``mineru`` registered in the fixture.

    The fixture is shared with ``test_toolbox_contract_drift.py``: when
    regenerating it from a staging where either provider is not registered,
    this test fails for that reason, not because the gate regressed.
    """
    decision = toolbox_gate.evaluate(
        FUSE_METHODS, _staging_snapshot(), mode="enforce"
    )

    assert decision.blocked == {}
    assert decision.unavailable_methods == []


def test_math_execution_methods_require_the_live_toolbox_catalogue(monkeypatch) -> None:
    """MathML and speech use math.convert/verbalize, not local algorithms."""
    monkeypatch.setattr(settings, "toolbox_math_provider", "math-speech")
    methods = ["mathml", "latex-verbalizer"]
    live = toolbox_gate.evaluate(methods, _staging_snapshot(), mode="enforce")
    assert live.blocked == {}

    missing = toolbox_gate.evaluate(methods, None, mode="enforce")
    assert missing.unavailable_methods == ["latex-verbalizer", "mathml"]

    observing = toolbox_gate.evaluate(methods, None, mode="observe")
    assert set(observing.blocked) == {"mathml", "latex-verbalizer"}
    assert observing.unavailable_methods == []


def test_math_provider_mismatch_is_visible_without_local_fallback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "toolbox_math_provider", "unregistered-math")
    decision = toolbox_gate.evaluate(
        ["mathml", "latex-verbalizer"], _staging_snapshot(), mode="enforce"
    )
    assert set(decision.blocked) == {"mathml", "latex-verbalizer"}


def test_unregistered_secondary_provider_blocks_only_dual_fusion(monkeypatch) -> None:
    monkeypatch.setattr(settings, "fusion_secondary_provider", "qwen3-vl-page")

    decision = toolbox_gate.evaluate(
        FUSE_METHODS, _staging_snapshot(), mode="enforce"
    )

    assert list(decision.blocked) == ["dual-provider-fusion"]
    assert "qwen3-vl-page não registrado" in decision.blocked["dual-provider-fusion"][0]


def test_methods_without_toolbox_requirements_are_never_blocked() -> None:
    decision = toolbox_gate.evaluate(
        ["pandoc-table", "rep-guard", "human-review"], None, mode="enforce"
    )
    assert decision.blocked == {}


def test_observe_reports_without_removing() -> None:
    decision = toolbox_gate.evaluate(FUSE_METHODS, None, mode="observe")

    assert set(decision.blocked) == {"dual-provider-fusion", "single-provider"}
    assert decision.unavailable_methods == []
    assert decision.to_metadata()["applied"] is False


def test_enforce_without_a_catalogue_fails_closed() -> None:
    decision = toolbox_gate.evaluate(
        FUSE_METHODS, None, mode="enforce", catalog_error="ToolboxTimeout: slow"
    )

    assert decision.unavailable_methods == ["dual-provider-fusion", "single-provider"]
    metadata = decision.to_metadata()
    assert metadata["applied"] is True
    assert metadata["catalog_revision"] is None
    assert metadata["catalog_error"] == "ToolboxTimeout: slow"


def test_enforced_gate_moves_the_plan_to_the_next_method() -> None:
    orchestrator = _orchestrator()
    manifest = _fuse_manifest()

    plan, _ = orchestrator._build_plan(
        manifest, extra_unavailable=["dual-provider-fusion"]
    )
    assert [s.method for s in plan.steps if s.method] == ["single-provider"]

    plan, _ = orchestrator._build_plan(
        manifest, extra_unavailable=["dual-provider-fusion", "single-provider"]
    )
    assert [s.method for s in plan.steps if s.method] == ["human-review"]


class _CountingCache:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.calls = 0
        self.last_error = None

    async def get(self):
        self.calls += 1
        return self.snapshot


@pytest.mark.asyncio
async def test_decide_skips_the_catalogue_when_nothing_needs_it(monkeypatch) -> None:
    cache = _CountingCache()
    monkeypatch.setattr(toolbox_gate, "catalog_cache", lambda: cache)
    monkeypatch.setattr(settings, "toolbox_availability_gate", "enforce")
    manifest = _manifest(
        [_element("el-1", "table")],
        [_obligation("linearize-table", ["el-1"], ["pandoc-table", "human-review"])],
    )

    decision = await toolbox_gate.decide(manifest)

    assert cache.calls == 0
    assert decision.blocked == {}


@pytest.mark.asyncio
async def test_decide_is_a_no_op_when_off(monkeypatch) -> None:
    cache = _CountingCache()
    monkeypatch.setattr(toolbox_gate, "catalog_cache", lambda: cache)
    monkeypatch.setattr(settings, "toolbox_availability_gate", "off")

    decision = await toolbox_gate.decide(_fuse_manifest())

    assert cache.calls == 0
    assert decision.mode == "off"
    assert decision.unavailable_methods == []


@pytest.mark.asyncio
async def test_decide_reads_the_cache_once(monkeypatch) -> None:
    cache = _CountingCache(_staging_snapshot())
    monkeypatch.setattr(toolbox_gate, "catalog_cache", lambda: cache)
    monkeypatch.setattr(settings, "toolbox_availability_gate", "enforce")

    decision = await toolbox_gate.decide(_fuse_manifest())

    assert cache.calls == 1
    assert decision.catalog_revision == _staging_snapshot().revision
    assert decision.blocked == {}


def test_unknown_mode_falls_back_to_observe(monkeypatch) -> None:
    monkeypatch.setattr(settings, "toolbox_availability_gate", "strict")
    assert toolbox_gate.gate_mode() == "observe"


def test_every_gated_method_has_a_handler() -> None:
    """A requirement for a method nobody runs would be dead configuration."""
    registered = set(_orchestrator().executor.registry.names())
    assert set(toolbox_gate.METHOD_TOOLBOX_REQUIREMENTS) <= registered


@pytest.mark.asyncio
async def test_catalog_fetch_uses_the_toolbox_client_environment_not_legacy_base_url(
    monkeypatch,
):
    """A stale PROD URL must never select the catalogue when HOMOLOG URLs win."""
    monkeypatch.setattr(settings, "toolbox_base_url", "https://prod-never-used.example")
    monkeypatch.setattr(
        settings, "toolbox_base_urls",
        "https://homolog-primary.example, https://homolog-secondary.example",
    )
    monkeypatch.setattr(settings, "toolbox_api_key", "homolog-key")
    captured = []
    snapshot = _staging_snapshot()

    class FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def fake_catalog(client):
        assert isinstance(client, FakeClient)
        return snapshot

    monkeypatch.setattr(toolbox_gate.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(toolbox_gate, "fetch_catalog", fake_catalog)
    result = await toolbox_gate._fetch()

    assert result is snapshot
    assert len(captured) == 1
    assert captured[0]["base_url"] == "https://homolog-primary.example"
    assert captured[0]["headers"]["Authorization"] == "Bearer homolog-key"


def test_availability_cache_is_not_shared_between_toolbox_environments(monkeypatch):
    monkeypatch.setattr(settings, "toolbox_base_url", "https://unused.example")
    monkeypatch.setattr(settings, "toolbox_base_urls", "https://prod.example")
    monkeypatch.setattr(settings, "toolbox_api_key", "same-key")
    monkeypatch.setattr(toolbox_gate, "_cache", None)
    monkeypatch.setattr(toolbox_gate, "_cache_identity", None)

    production_cache = toolbox_gate.catalog_cache()
    assert toolbox_gate.catalog_cache() is production_cache

    monkeypatch.setattr(settings, "toolbox_base_urls", "https://homolog.example")
    homolog_cache = toolbox_gate.catalog_cache()
    assert homolog_cache is not production_cache

    # A credential rotation also requires a new cache, not a stale snapshot.
    monkeypatch.setattr(settings, "toolbox_api_key", "rotated-key")
    assert toolbox_gate.catalog_cache() is not homolog_cache
