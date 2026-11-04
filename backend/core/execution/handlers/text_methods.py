"""Deterministic text preservation and format-repair PDDL handlers.

Uses the existing FusionAgent public methods; never calls a model or Toolbox.
"""
from __future__ import annotations

from typing import Any

from docstruct.fusion.tailjoin import is_page_number as _is_page_number
from backend.core.agents.fusion_agent import FusionAgent
from backend.core.execution.handlers.common import _missing_obligation, _target_elements
from backend.core.execution.models import MethodResult
from backend.core.manifest.element_geometry import _element_bbox, _element_role
from backend.core.manifest.models import ManifestElement, ProcessingManifest


def _fusion_agent() -> FusionAgent:
    return FusionAgent()


def _handle_repetition_guard_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Remove repeated token loops from the obligation's current page text."""
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    fusion_agent = _fusion_agent()
    cut_total = 0
    for element in elements:
        result = fusion_agent.process_guard_repetition(element.text or "")
        cut = int(result["tokens_cut"])
        if not cut:
            continue
        element.text = result["text"]
        element.metadata["repetition_guard"] = {"tokens_cut": cut}
        cut_total += cut

    if not cut_total:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma repetição degenerada encontrada nos alvos",
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"Guarda de repetição removeu {cut_total} token(s)",
    )


def _element_blocks(
    elements: list[ManifestElement],
    manifest: ProcessingManifest,
) -> list[dict[str, Any]]:
    """Manifest elements as the fusion stack's block dicts.

    The deterministic primitives in `docstruct` operate on classified blocks, not
    on bare strings, so the elements are adapted once here instead of each handler
    guessing at a signature.
    """
    blocks: list[dict[str, Any]] = []
    for element in elements:
        bbox = _element_bbox(element, manifest)
        blocks.append(
            {
                "md": element.text or "",
                "text": element.text or "",
                "kind": _block_kind(element),
                "type": element.type,
                "role": _element_role(element),
                "box": list(bbox) if bbox else None,
            }
        )
    return blocks


def _block_kind(element: ManifestElement) -> str:
    """Fusion block kind for an element type (`DiffBlock.kind` vocabulary)."""
    if element.type == "heading":
        return "heading"
    if element.type == "table":
        return "table"
    if element.type == "formula":
        return "formula"
    return "text"


def _handle_extract_line_numbers_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Pull marginal verse line numbers out of the target blocks.

    Uses `FusionAgent.process_extract_line_numbers`, which is the supported entry
    point: `docstruct.fusion.line_numbers.extract_line_numbers` needs a block list
    plus a stats counter and a provenance tag, so calling it with a plain string
    raised `TypeError` on every step.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    result = _fusion_agent().process_extract_line_numbers(
        _element_blocks(elements, manifest)
    )
    extracted = int(result.get("stats", {}).get("line-numbers-agent-count", 0))
    if not extracted:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum número de linha marginal encontrado",
        )
    for element in elements:
        element.metadata["line_numbers_extracted"] = extracted
    return MethodResult(
        success=True,
        validated=True,
        message=f"{extracted} número(s) de linha extraído(s)",
    )


def _handle_chunk_verse_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Split verse/record blocks into chunks the evaluator can re-group.

    `docstruct.fusion.verse.chunk_verse` takes already-split lines plus a character
    budget, so the handler goes through `FusionAgent.process_chunk_verse`, which
    detects the verse lines itself.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    result = _fusion_agent().process_chunk_verse(_element_blocks(elements, manifest))
    # `verse_blocks` reports `verse-{tag}-{whole,kept,split,chunks}`; only the
    # blocks it actually split are evidence that the method did anything.
    chunks = result.get("stats", {}).get("verse-agent-split", 0)
    if not chunks:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum bloco de verso elegível para fragmentação",
        )
    for element in elements:
        element.metadata["verse_chunks"] = chunks
    return MethodResult(
        success=True,
        validated=True,
        message=f"Verso fragmentado em {chunks} pedaço(s)",
    )


def _tailjoin_position(manifest: ProcessingManifest) -> str | None:
    """Vertical position ("top"/"bot") of the page number in the manifest.

    `join_tail` only applies the position-conditioned `tj3` rule when it knows
    where the page number sits, which is the distinction between `tailjoin` and
    `tailjoin-positioned`.
    """
    for element in manifest.elements:
        role = _element_role(element)
        if role in {"header", "footer", "page_number"} and _is_page_number(
            element.text or ""
        ):
            bbox = _element_bbox(element, manifest)
            if bbox is None:
                continue
            return "top" if (bbox[1] + bbox[3]) / 2 < 0.5 else "bot"
    return None


def _handle_tailjoin_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Join the trailing short blocks (header/footer/page number).

    `docstruct.fusion.tailjoin.join_tail` requires the tail mode, a maximum short
    block length and the number of blocks to consider, so it cannot be called with
    the text alone. `FusionAgent.process_join_tail` supplies those defaults.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    texts = [(element.text or "") for element in elements]
    merged = _fusion_agent().process_join_tail(
        texts, position=_tailjoin_position(manifest)
    )
    if len(merged) >= len(texts):
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum bloco de cauda elegível para união",
        )
    for element in elements:
        element.metadata["tailjoined"] = True
    return MethodResult(
        success=True,
        validated=True,
        message=(
            f"Blocos de cauda unidos: {len(texts)} -> {len(merged)} bloco(s)"
        ),
    )
