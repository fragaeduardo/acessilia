"""Validate source picture descriptions before applying the existing PDDL plan.

The actual VisionAgent work happens before planning, so this handler validates
the manifest rather than calling a second model provider.
"""
from __future__ import annotations

from backend.core.execution.handlers.common import _target_elements
from backend.core.execution.models import MethodResult
from backend.core.manifest.models import ProcessingManifest
from backend.core.manifest.preprocessing.visual_signals import _has_meaningful_text


def _handle_vision_description_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Require every picture target to already carry a description.

    The description itself is produced before planning (`_enrich_picture_descriptions`
    asks the vision model), so this method validates that the work the planner is
    asking for actually happened instead of pretending to perform it. A picture
    that is still undescribed is a real failure and keeps the next admissible
    method available; `describe-image` is non-critical, so it does not fail the job.
    """
    elements = [
        el
        for el in _target_elements(manifest, obligation_id)
        if el.type == "picture"
    ]
    if not elements:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma imagem encontrada para a obrigação",
        )

    undescribed = [
        el for el in elements
        if not (
            bool((el.text or "").strip())
            if el.metadata.get("vision_description_complete")
            else _has_meaningful_text(el)
        )
    ]
    if undescribed:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Imagem sem descrição textual: "
                + ", ".join(
                    f"{el.id} ({reason})"
                    if (reason := el.metadata.get("vision_failure_reason"))
                    else el.id
                    for el in undescribed
                )
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"Descrição validada para {len(elements)} imagem(ns)",
    )


# Deterministic block handlers now have their own execution module.
from backend.core.execution.handlers.deterministic import (
    _handle_pandoc_table_method,
    _handle_pandoc_code_method,
    _handle_latex_normalizer_method,
    _handle_norm_fix_method,
)
