"""Shared manifest-to-handler lookups, with existing error semantics."""

from __future__ import annotations

from backend.core.execution.models import MethodResult
from backend.core.manifest.models import ManifestElement, ProcessingManifest


def _target_elements(
    manifest: ProcessingManifest, obligation_id: str
) -> list[ManifestElement]:
    """Elements an obligation points at, in reading order."""
    obligation = next(
        (o for o in manifest.obligations if o.id == obligation_id), None
    )
    if obligation is None:
        return []
    targets = set(obligation.target_ids)
    return sorted(
        (el for el in manifest.elements if el.id in targets),
        key=lambda el: el.reading_order,
    )


def _missing_obligation(obligation_id: str) -> MethodResult:
    return MethodResult(
        success=False,
        validated=False,
        message=f"Obrigação não encontrada: {obligation_id}",
    )


def _handle_human_review_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Record the obligation as escalated to a human reviewer.

    `human-review` is the terminal method every obligation lists as its last
    admissible option. It is not an automated method that can succeed on its own,
    so resolving it must be an explicit decision: an obligation reaching this
    handler is escalated and stays visible in the execution report, instead of
    being marked satisfied by a handler that did nothing.
    """
    obligation = next(
        (o for o in manifest.obligations if o.id == obligation_id), None
    )
    if obligation is None:
        return _missing_obligation(obligation_id)

    return MethodResult(
        success=False,
        validated=False,
        message=(
            "Escalonado para revisão humana: nenhum método automático satisfez "
            f"{obligation.kind} ({obligation_id})"
        ),
    )
