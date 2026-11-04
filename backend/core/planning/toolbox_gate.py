"""Availability gate: planning methods checked against the live Toolbox catalogue.

A method whose handler calls the Toolbox declares what it needs (capability and
provider) in ``METHOD_TOOLBOX_REQUIREMENTS``. Before planning a document, the
orchestrator takes one catalogue snapshot and asks :func:`evaluate` which of the
requested methods cannot run against it. The catalogue only restricts: methods
without Toolbox requirements are never affected, and a capability outside the
SDK allowlist is never made available.

``TOOLBOX_AVAILABILITY_GATE`` chooses what happens with the answer:

- ``observe`` (default): report it in the logs and the output metadata, plan as
  before. This is how the gate is validated before it changes plans.
- ``enforce``: remove those methods from the plan. Without a usable catalogue
  every method with Toolbox requirements is removed (fail closed), so the
  planner falls back to local methods or ``human-review``.
- ``off``: no check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from acessilia_sdk.toolbox import (
    CatalogCache,
    CatalogSnapshot,
    Requirement,
    auth_headers,
    fetch_catalog,
    missing_requirements,
)
from backend.config.settings import settings
from backend.core.manifest.models import ProcessingManifest
from backend.tools.toolbox_client import toolbox_endpoints

GATE_MODES = ("observe", "enforce", "off")

#: Reading the catalogue must not hold a document for the extraction timeout.
#: Per request: a refresh makes at most four (one capabilities read, up to three
#: providers reads), so ~40s in the worst case.
CATALOG_TIMEOUT_SECONDS = 10.0

_EXTRACT = "document.structure.extract"

#: Method -> Toolbox requirements, resolved when evaluated so provider overrides
#: in the environment apply. Methods not listed run locally.
METHOD_TOOLBOX_REQUIREMENTS: dict[str, Callable[[], list[Requirement]]] = {
    "dual-provider-fusion": lambda: [
        Requirement(_EXTRACT, settings.toolbox_provider),
        Requirement(_EXTRACT, settings.fusion_secondary_provider),
    ],
    "single-provider": lambda: [Requirement(_EXTRACT, settings.toolbox_provider)],
    # Both handlers use ToolboxMathClient.transform_provider, which resolves
    # to TOOLBOX_MATH_PROVIDER (not the recognition provider).
    "mathml": lambda: [
        Requirement("math.convert", settings.toolbox_math_provider)
    ],
    "latex-verbalizer": lambda: [
        Requirement("math.verbalize", settings.toolbox_math_provider)
    ],
    # Methods that render pages or regions; pdf.render@2 also renders raster
    # inputs, so they need it for images too.
    "vision-page-inspection": lambda: [
        Requirement("pdf.render", settings.toolbox_pdf_provider)
    ],
    "pymupdf-region": lambda: [
        Requirement("pdf.render", settings.toolbox_pdf_provider)
    ],
    "vlm-pairwise-judge": lambda: [
        Requirement("document.adjudicate.page", settings.toolbox_adjudicate_provider),
        Requirement("pdf.render", settings.toolbox_pdf_provider),
    ],
}


@dataclass(frozen=True)
class AvailabilityDecision:
    mode: str
    catalog_revision: str | None
    #: Requested methods the catalogue rules out, with the reasons.
    blocked: dict[str, list[str]] = field(default_factory=dict)
    catalog_error: str | None = None

    @property
    def enforced(self) -> bool:
        return self.mode == "enforce"

    @property
    def unavailable_methods(self) -> list[str]:
        """Methods to keep out of the plan (only when enforcing)."""
        return sorted(self.blocked) if self.enforced else []

    def to_metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "catalog_revision": self.catalog_revision,
            "catalog_error": self.catalog_error,
            "blocked_methods": self.blocked,
            "applied": self.enforced and bool(self.blocked),
        }


def gate_mode() -> str:
    mode = settings.toolbox_availability_gate
    return mode if mode in GATE_MODES else "observe"


def requested_methods(manifest: ProcessingManifest) -> set[str]:
    """Methods the manifest may plan with."""
    return {
        method
        for obligation in manifest.obligations
        for method in obligation.admissible_methods
    }


def evaluate(
    methods: Iterable[str],
    snapshot: CatalogSnapshot | None,
    *,
    mode: str,
    catalog_error: str | None = None,
) -> AvailabilityDecision:
    """Which of ``methods`` cannot run against ``snapshot``."""
    blocked: dict[str, list[str]] = {}
    for method in sorted(set(methods)):
        requirements = METHOD_TOOLBOX_REQUIREMENTS.get(method)
        if requirements is None:
            continue
        reasons = missing_requirements(snapshot, requirements())
        if reasons:
            blocked[method] = reasons
    return AvailabilityDecision(
        mode=mode,
        catalog_revision=snapshot.revision if snapshot else None,
        blocked=blocked,
        catalog_error=catalog_error if snapshot is None else None,
    )


def _catalog_endpoint_identity() -> tuple[str, str]:
    """Use the very same first configured Toolbox endpoint as VLM and clients.

    The catalogue is intentionally conservative: if that endpoint fails,
    enforcement fails closed rather than silently consulting an unrelated
    endpoint or another deployment. The SDK may fail over capability calls,
    but this gate does not speculate on another replica's capabilities.
    """
    endpoint = toolbox_endpoints()[0]
    return endpoint.base_url, endpoint.api_key or settings.toolbox_api_key


async def _fetch() -> CatalogSnapshot:
    url, api_key = _catalog_endpoint_identity()
    async with httpx.AsyncClient(
        base_url=url,
        timeout=httpx.Timeout(CATALOG_TIMEOUT_SECONDS),
        headers=auth_headers(api_key),
    ) as http:
        return await fetch_catalog(http)


_cache: CatalogCache | None = None
_cache_identity: tuple[str, str] | None = None


def catalog_cache() -> CatalogCache:
    """Share cache only while the effective Toolbox endpoint/key are unchanged.

    This prevents a dynamically reconfigured worker from using a PROD
    catalogue snapshot to authorize HOMOLOG planning (or vice versa).
    The identity is never recorded in output metadata or log messages.
    """
    global _cache, _cache_identity
    identity = _catalog_endpoint_identity()
    if _cache is None or _cache_identity != identity:
        _cache = CatalogCache(
            _fetch,
            refresh_seconds=settings.toolbox_catalog_refresh_seconds,
            stale_seconds=settings.toolbox_catalog_stale_seconds,
        )
        _cache_identity = identity
    return _cache


async def decide(manifest: ProcessingManifest) -> AvailabilityDecision:
    """Take the catalogue snapshot for one document and evaluate its methods."""
    mode = gate_mode()
    methods = requested_methods(manifest)
    if mode == "off" or not methods & set(METHOD_TOOLBOX_REQUIREMENTS):
        return AvailabilityDecision(mode=mode, catalog_revision=None)
    cache = catalog_cache()
    snapshot = await cache.get()
    return evaluate(methods, snapshot, mode=mode, catalog_error=cache.last_error)


__all__ = [
    "METHOD_TOOLBOX_REQUIREMENTS",
    "AvailabilityDecision",
    "catalog_cache",
    "decide",
    "evaluate",
    "gate_mode",
    "requested_methods",
]
