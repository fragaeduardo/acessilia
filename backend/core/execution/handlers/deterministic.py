"""Deterministic validation/normalization handlers for canonical blocks.

These are existing methods from MethodRegistry, not new capabilities.
"""

from __future__ import annotations

from docstruct.tables.ast import linearize_table_for_text, raw_rows_from_table_ast as _rows_from_table_ast
from docstruct.text.norm_fix import norm_fix

from backend.core.execution.handlers.common import _missing_obligation, _target_elements
from backend.core.execution.models import MethodResult
from backend.core.manifest.models import ProcessingManifest
from backend.core.manifest.table_reading import _table_element_has_structured_content
from backend.tools.code_tools import normalize_code_text
from backend.tools.formula_tools import normalize_latex


def _handle_pandoc_table_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Require the table targets to carry a structured AST."""
    elements = [
        el for el in _target_elements(manifest, obligation_id) if el.type == "table"
    ]
    if not elements:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma tabela encontrada para a obrigação",
        )

    missing = []
    for element in elements:
        try:
            usable = _table_element_has_structured_content(element)
            table_ast = element.metadata.get("table_ast")
            if usable:
                usable = any(
                    value.strip() for row in _rows_from_table_ast(table_ast) for value in row
                )
                linearize_table_for_text({"table_ast": table_ast})
        except (TypeError, ValueError, AttributeError):
            usable = False
        if not usable:
            missing.append(element)
    if missing:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Tabela sem estrutura tabular: "
                + ", ".join(el.id for el in missing)
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"Estrutura tabular validada para {len(elements)} tabela(s)",
    )


def _handle_pandoc_code_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Normalize code targets in place and record the normalized language."""
    elements = [
        el for el in _target_elements(manifest, obligation_id) if el.type == "code"
    ]
    if not elements:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum bloco de código encontrado para a obrigação",
        )

    normalized = 0
    for element in elements:
        text = normalize_code_text(element.text or "")
        if not text:
            continue
        element.text = text
        element.metadata["code_normalized"] = True
        normalized += 1

    if normalized != len(elements):
        return MethodResult(
            success=False,
            validated=False,
            message=(
                f"Código normalizado em {normalized}/{len(elements)} bloco(s)"
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"Código normalizado em {normalized} bloco(s)",
    )


def _handle_latex_normalizer_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Normalize the LaTeX of every formula target in place."""
    elements = [
        el
        for el in _target_elements(manifest, obligation_id)
        if el.type == "formula"
    ]
    if not elements:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhuma fórmula encontrada para a obrigação",
        )

    normalized = 0
    for element in elements:
        cleaned = normalize_latex(element.text or "")
        if not cleaned:
            continue
        element.metadata["latex_normalized"] = cleaned
        normalized += 1

    if normalized != len(elements):
        return MethodResult(
            success=False,
            validated=False,
            message=(
                f"LaTeX normalizado em {normalized}/{len(elements)} fórmula(s)"
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"LaTeX normalizado em {normalized} fórmula(s)",
    )


def _handle_norm_fix_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Apply the deterministic text cleanups (ligatures, hyphenation, NBSP)."""
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    fixed = 0
    for element in elements:
        current = element.text or ""
        if not current:
            continue
        cleaned = norm_fix(current)
        if cleaned != current:
            element.text = cleaned
            element.metadata["norm_fixed"] = True
        fixed += 1

    if not fixed:
        return MethodResult(
            success=False,
            validated=False,
            message="Nenhum texto elegível para normalização",
        )
    return MethodResult(
        success=True,
        validated=True,
        message=f"Normalização determinística aplicada em {fixed} elemento(s)",
    )
