"""Math-specific PDDL handlers: normalization, conversion and speech.

All operations retain their Toolbox-backed semantics and the existing
error/validation behavior. No local fallback or new provider is introduced.
"""

from __future__ import annotations

from docstruct.text.implicit_products import implicit_product_variables, mark_implicit_products
from docstruct.text.math_spans import block_prose, inline_math_sources

from backend.core.execution.models import MethodResult
from backend.core.execution.recovery import _formula_elements_for_obligation
from backend.core.manifest.models import ProcessingManifest
from backend.core.manifest.payload import _element_to_block, _text_kept_by_picture
from backend.i18n import t
from backend.tools.formula_tools import label_spoken_formula, normalize_latex
from backend.tools.logger import logger
from backend.tools.toolbox_math_tools import (
    MSG_MATHML_UNAVAILABLE,
    MSG_SPEECH_UNAVAILABLE,
    MathCapabilityUnavailable,
    toolbox_convert_latex,
    toolbox_verbalize_latex,
)


def _implicit_product_variables(manifest: ProcessingManifest) -> frozenset[str]:
    """The letters the document uses as variables, from all its formulas.

    Read from every formula element and every inline span of the manifest, the
    same sources the canonical build reads, so a formula converted here and
    one converted there mark the same products.
    """
    sources: list[str] = []
    for element in manifest.elements:
        blocks = [
            _element_to_block(item)
            for item in [element, *_text_kept_by_picture(element)]
        ]
        for block in blocks:
            if block.get("type") == "math":
                sources.append(str(block.get("text") or ""))
            for text in block_prose(block):
                sources.extend(inline_math_sources(text))
    return implicit_product_variables(sources)


def _handle_mathml_method(
    manifest: ProcessingManifest, obligation_id: str, *,
    convert_latex=None,
) -> MethodResult:
    """Converte o LaTeX das fórmulas alvo em MathML (metadata.mathml) via math.convert.

    Uma math.convert indisponível falha o método explicitamente (sem conversão
    local, ADR-0007), para o executor tentar outro método ou replanejar.
    """
    elements = _formula_elements_for_obligation(manifest, obligation_id)
    if not elements:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma fórmula encontrada para a obrigação",
        )
    converted = 0
    variables = _implicit_product_variables(manifest)
    for element in elements:
        latex = mark_implicit_products(normalize_latex(element.text or ""), variables)
        try:
            mathml = (convert_latex or toolbox_convert_latex)(latex) if latex else ""
        except MathCapabilityUnavailable as exc:
            logger.warning("Handler mathml: {}", exc)
            return MethodResult(
                success=False,
                validated=False,
                message=t(MSG_MATHML_UNAVAILABLE).format(reason=exc.reason),
            )
        if mathml:
            element.metadata["mathml"] = mathml
            converted += 1
    if converted == len(elements):
        return MethodResult(
            success=True,
            validated=True,
            message=f"MathML gerado para {converted} fórmula(s)",
        )
    return MethodResult(
        success=False,
        validated=False,
        message=f"MathML gerado para {converted}/{len(elements)} fórmula(s)",
    )


def _handle_latex_verbalizer_method(
    manifest: ProcessingManifest, obligation_id: str, *,
    verbalize_latex=None,
) -> MethodResult:
    """Gera a verbalização pt-BR das fórmulas (metadata.verbalization) via math.verbalize.

    Uma math.verbalize indisponível falha o método explicitamente (sem
    verbalizador local, ADR-0007).
    """
    elements = _formula_elements_for_obligation(manifest, obligation_id)
    if not elements:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma fórmula encontrada para a obrigação",
        )
    verbalized = 0
    variables = _implicit_product_variables(manifest)
    for element in elements:
        latex = mark_implicit_products(normalize_latex(element.text or ""), variables)
        try:
            spoken = label_spoken_formula((verbalize_latex or toolbox_verbalize_latex)(latex)) if latex else ""
        except MathCapabilityUnavailable as exc:
            logger.warning("Handler latex-verbalizer: {}", exc)
            return MethodResult(
                success=False,
                validated=False,
                message=t(MSG_SPEECH_UNAVAILABLE).format(reason=exc.reason),
            )
        if spoken:
            element.metadata["verbalization"] = spoken
            verbalized += 1
    if verbalized == len(elements):
        return MethodResult(
            success=True,
            validated=True,
            message=f"Verbalização gerada para {verbalized} fórmula(s)",
        )
    return MethodResult(
        success=False,
        validated=False,
        message=f"Verbalização gerada para {verbalized}/{len(elements)} fórmula(s)",
    )
