"""Calibrated provider choice, page adjudication and reading-order methods.

These algorithms reuse the original Docling/MinerU calibration and the
Toolbox's documented page adjudication capability. Provider and render hooks
are injected only through the original facade, preserving existing tests.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import math

from backend.core.agents.fusion_agent import FusionAgent
from backend.core.execution.handlers.common import _target_elements, _missing_obligation
from backend.core.execution.models import MethodResult
from backend.core.manifest.builder import SECONDARY_EXTRACTION_KIND
from backend.core.manifest.element_geometry import _element_bbox
from backend.core.manifest.models import ManifestElement, ProcessingManifest, is_bottom_left
from backend.core.manifest.preprocessing.render_regions import _source_artifact_id
from backend.core.execution.handlers.region_inspection import _toolbox_render
from backend.tools.logger import logger


def _fusion_agent() -> FusionAgent:
    return FusionAgent()


# --- Methods that choose between the two providers of FUSION_MODE=dual -------
#
# table-smart-fusion, skeleton-order and agreement-rule port Dr.DocBench rules
# (tree_differ_v2.py of the drbench-2026-snapshot tag and
# docstruct.fusion.adjudication.decide_v1). They read the secondary provider's
# extraction that the structure extractor keeps as a manifest artifact. The
# rules were calibrated on the docling + mineru pair, so any other pair fails
# explicitly instead of applying a preference nobody measured.

_CALIBRATED_PAIR = ("docling", "mineru")
#: Accepted Dr.DocBench table preference (TEDS): MinerU's table, unless Docling's
#: has spans or is the only one.
_TABLE_PREFERENCE = "mineru"
#: Minimum intersection over union to pair two blocks by position.
_MIN_BLOCK_IOU = 0.3
#: Minimum text similarity to pair two blocks that could not be paired by position.
_MIN_BLOCK_TEXT_RATIO = 0.8
#: Share of a page's elements that must be paired to follow the skeleton order.
_MIN_SKELETON_MATCH = 0.5

_Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class _SecondaryExtraction:
    provider: str
    #: page number -> raw elements of the secondary provider, in reading order.
    pages: dict[int, list[dict[str, Any]]]
    sizes: dict[int, tuple[float, float]]
    #: page number -> primary elements of a page the manifest took from the
    #: secondary provider (fusion.apply_page_routes), in reading order.
    replaced_primary: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    def bbox(self, element: dict[str, Any]) -> _Box | None:
        """Normalized box of a raw Toolbox element (same frame as _element_bbox)."""
        for item in element.get("provenance") or []:
            if not isinstance(item, dict) or not item.get("bbox"):
                continue
            box = item["bbox"]
            page = _page_number(item.get("page_number", element.get("page_number")))
            width, height = self.sizes.get(page, (0.0, 0.0)) if page else (0.0, 0.0)
            if not width or not height:
                return None
            try:
                left, right = float(box["left"]), float(box["right"])
                top, bottom = float(box["top"]), float(box["bottom"])
            except (KeyError, TypeError, ValueError):
                return None
            if is_bottom_left(box.get("coord_origin"), top, bottom):
                top, bottom = height - top, height - bottom
            return _ordered_box(
                left / width, top / height, right / width, bottom / height
            )
        return None


def _page_number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_secondary_extraction(
    manifest: ProcessingManifest,
) -> tuple[_SecondaryExtraction | None, str]:
    """The secondary provider's extraction, if recorded and unchanged.

    When it cannot be used, the second item says why (missing, replaced or
    unreadable), so the method result can tell those cases apart.
    """
    import json

    artifact = next(
        (a for a in manifest.artifacts if a.kind == SECONDARY_EXTRACTION_KIND), None
    )
    if artifact is None:
        return None, "Extração do provider secundário ausente do manifesto"
    try:
        raw = Path(artifact.path).read_bytes()
    except OSError:
        return None, f"Extração do provider secundário indisponível: {artifact.path}"
    if artifact.sha256 and hashlib.sha256(raw).hexdigest() != artifact.sha256:
        logger.warning(
            "Extração secundária {} não confere com o sha256 registrado",
            artifact.path,
        )
        return None, (
            "Extração do provider secundário alterada: sha256 diverge do "
            f"registrado no manifesto ({artifact.path})"
        )
    unreadable = f"Extração do provider secundário ilegível: {artifact.path}"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, unreadable
    payload = data.get("document") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None, unreadable
    document = payload.get("document", payload)
    sizes: dict[int, tuple[float, float]] = {}
    for page in document.get("pages") or []:
        try:
            sizes[int(page["page_number"])] = (
                float(page.get("width") or 0),
                float(page.get("height") or 0),
            )
        except (KeyError, TypeError, ValueError):
            continue
    pages: dict[int, list[dict[str, Any]]] = {}
    for element in sorted(
        (e for e in document.get("elements") or [] if isinstance(e, dict)),
        key=lambda e: e.get("reading_order") or 0,
    ):
        page = _page_number(element.get("page_number"))
        if page is not None:
            pages.setdefault(page, []).append(element)
    replaced: dict[int, list[dict[str, Any]]] = {}
    for key, rows in (data.get("replaced_primary_pages") or {}).items():
        page = _page_number(key)
        if page is not None and isinstance(rows, list):
            replaced[page] = sorted(
                (row for row in rows if isinstance(row, dict)),
                key=lambda row: row.get("reading_order") or 0,
            )
    return (
        _SecondaryExtraction(str(data.get("provider") or ""), pages, sizes, replaced),
        "",
    )


def _provider_pair_problem(
    manifest: ProcessingManifest, secondary: _SecondaryExtraction
) -> str | None:
    primary = str((manifest.extractor.configuration or {}).get("toolbox_provider") or "")
    if (primary, secondary.provider) == _CALIBRATED_PAIR:
        return None
    return (
        f"Regra calibrada para {'+'.join(_CALIBRATED_PAIR)}; "
        f"par atual: {primary or '?'}+{secondary.provider or '?'}"
    )


def _secondary_or_failure(
    manifest: ProcessingManifest,
) -> tuple[_SecondaryExtraction | None, MethodResult | None]:
    secondary, problem = _load_secondary_extraction(manifest)
    if secondary is None:
        return None, MethodResult(success=False, validated=False, message=problem)
    problem = _provider_pair_problem(manifest, secondary)
    if problem:
        return None, MethodResult(success=False, validated=False, message=problem)
    return secondary, None


def _ordered_box(left: float, top: float, right: float, bottom: float) -> _Box:
    return (min(left, right), min(top, bottom), max(left, right), max(top, bottom))


def _iou(a: _Box, b: _Box) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _pair_block(
    element: ManifestElement,
    manifest: ProcessingManifest,
    candidates: list[dict[str, Any]],
    secondary: _SecondaryExtraction,
    used: set[int],
) -> int | None:
    """Index in ``candidates`` of the block that matches ``element``.

    Greedy: each element takes its best free candidate in the current reading
    order, while Dr.DocBench uses a global (Hungarian) assignment. The two can
    differ only when blocks compete for the same candidate, which is rare above
    the IoU / text thresholds and does not move the skeleton's 0.5 page gate.
    """
    import difflib

    box = _element_bbox(element, manifest)
    if box is not None:
        box = _ordered_box(*box)
        scored = [
            (_iou(box, other), index)
            for index, candidate in enumerate(candidates)
            if index not in used and (other := secondary.bbox(candidate)) is not None
        ]
        best = max(scored, default=(0.0, None))
        if best[0] >= _MIN_BLOCK_IOU:
            return best[1]
    text = " ".join((element.text or "").split())
    if not text:
        return None
    scored_text = [
        (
            difflib.SequenceMatcher(
                None,
                text,
                " ".join(str(candidate["text"]).split()),
                autojunk=False,
            ).ratio(),
            index,
        )
        for index, candidate in enumerate(candidates)
        if index not in used and candidate.get("text")
    ]
    best_text = max(scored_text, default=(0.0, None))
    return best_text[1] if best_text[0] >= _MIN_BLOCK_TEXT_RATIO else None


def _handle_table_smart_fusion_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Pick each table's source between the two providers (Dr.DocBench rule).

    Docling's table is kept when it has spans (MinerU flattens them) or when it
    is the only one; otherwise MinerU's table is adopted. Tables are paired by
    position or text; a page with a single table on each side pairs them only
    when there is no position to compare. The choice and its reason are recorded
    in ``table_source`` / ``table_source_reason``.
    """
    from docstruct.tables.ast import analyze_table_complexity

    tables = [
        element
        for element in _target_elements(manifest, obligation_id)
        if element.type == "table"
    ]
    if not tables:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    primary_name, secondary_name = _CALIBRATED_PAIR
    primary_tables_by_page = Counter(
        element.page_number
        for element in manifest.elements
        if element.type == "table"
    )
    reasons: Counter = Counter()
    for element in tables:
        primary_ast = (element.metadata or {}).get("table_ast")
        candidates = [
            block
            for block in secondary.pages.get(element.page_number or 0, [])
            if block.get("type") == "table"
        ]
        match = _pair_block(element, manifest, candidates, secondary, set())
        if (
            match is None
            and len(candidates) == 1
            and primary_tables_by_page[element.page_number] == 1
            and (
                _element_bbox(element, manifest) is None
                or secondary.bbox(candidates[0]) is None
            )
        ):
            # One table on each side and no position to compare them by: the
            # same table. With both positions known and no overlap, they are not.
            match = 0
        chosen = candidates[match] if match is not None else None
        chosen_meta = (chosen or {}).get("metadata") or {}

        if primary_ast and analyze_table_complexity(primary_ast).has_spans:
            source, reason = primary_name, "primary-has-spans"
        elif not candidates:
            source, reason = primary_name, "no-secondary-table"
        elif chosen is None:
            source, reason = primary_name, "no-secondary-match"
        elif not (chosen.get("text") or chosen_meta.get("table_ast")):
            source, reason = primary_name, "secondary-table-empty"
        else:
            source, reason = _TABLE_PREFERENCE, f"preference-{_TABLE_PREFERENCE}"

        if source == secondary_name and chosen is not None:
            # Docling's table_* keys (AST, row/column counts, header) describe
            # the table being replaced; the output prefers table_ast over the
            # text, so a stale AST would hide the adopted MinerU text.
            for key in [k for k in element.metadata if k.startswith("table_")]:
                del element.metadata[key]
            if chosen.get("text"):
                element.text = chosen["text"]
            for key, value in chosen_meta.items():
                if key.startswith("table_"):
                    element.metadata[key] = value
        element.metadata["table_source"] = source
        element.metadata["table_source_reason"] = reason
        reasons[reason] += 1

    return MethodResult(
        success=True,
        validated=True,
        message="Fonte de tabela escolhida: "
        + ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items())),
    )


def _handle_skeleton_order_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Follow the secondary provider's reading order (MinerU skeleton).

    Page elements are paired with the secondary blocks by position (falling
    back to text); paired elements take the skeleton order and unpaired ones
    stay right after the element that preceded them. A page with fewer than
    half its elements paired is left alone.
    """
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    by_page: dict[int, list[ManifestElement]] = {}
    for element in elements:
        if element.page_number is not None:
            by_page.setdefault(element.page_number, []).append(element)

    reordered: set[int] = set()
    confirmed: set[int] = set()
    for page_number, page_elements in by_page.items():
        current = sorted(page_elements, key=lambda element: element.reading_order)
        skeleton = secondary.pages.get(page_number, [])
        used: set[int] = set()
        keys: dict[int, int] = {}
        for index, element in enumerate(current):
            match = _pair_block(element, manifest, skeleton, secondary, used)
            if match is not None:
                used.add(match)
                keys[index] = match
        if len(current) < 2 or len(keys) < _MIN_SKELETON_MATCH * len(current):
            continue
        ranked: list[tuple[float, int, int]] = []
        last = -1.0
        for index in range(len(current)):
            if index in keys:
                last = float(keys[index])
                ranked.append((last, 0, index))
            else:
                ranked.append((last, 1, index))
        order = [index for _, _, index in sorted(ranked)]
        if _apply_page_order(current, order):
            reordered.add(page_number)
        else:
            confirmed.add(page_number)

    if not reordered and not confirmed:
        return MethodResult(
            success=False,
            validated=False,
            message="Esqueleto do provider secundário não casou com a página",
        )
    _refresh_page_element_ids(manifest, reordered)
    parts = []
    if reordered:
        parts.append("aplicada nas páginas " + ", ".join(map(str, sorted(reordered))))
    if confirmed:
        parts.append("confirmada nas páginas " + ", ".join(map(str, sorted(confirmed))))
    return MethodResult(
        success=True,
        validated=True,
        message="Ordem do esqueleto " + "; ".join(parts),
    )


def _page_markdown(items: Iterable[tuple[str, str | None]]) -> str:
    """Page text with the markers the page router looks for."""
    parts = []
    for kind, text in items:
        text = (text or "").strip()
        if kind == "table":
            parts.append(f"<table>{text}</table>")
        elif kind == "formula":
            parts.append(f"$${text}$$")
        elif text:
            parts.append(text)
    return "\n\n".join(parts)


def _handle_agreement_rule_method(
    manifest: ProcessingManifest, obligation_id: str
) -> MethodResult:
    """Deterministic page router between the providers (``decide_v1``).

    When the router keeps the primary (Docling) page the obligation is
    satisfied without changing the text. When it picks the secondary page the
    decision is recorded but not applied: replacing a whole page would reopen
    the manifest mid-plan, and Dr.DocBench measured this router below the
    block-level fusion the core already runs. The method then fails, so the
    obligation goes to ``human-review`` with the route in ``page_route``.
    """
    from docstruct.fusion.adjudication import decide_v1

    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    primary_name = _CALIBRATED_PAIR[0]
    routes: dict[int, tuple[str, str]] = {}
    for page_number in sorted({e.page_number for e in elements if e.page_number}):
        primary_md = _page_markdown(
            (element.type, element.text)
            for element in sorted(manifest.elements, key=lambda e: e.reading_order)
            if element.page_number == page_number
        )
        secondary_md = _page_markdown(
            (str(block.get("type") or ""), block.get("text"))
            for block in secondary.pages.get(page_number, [])
        )
        routes[page_number] = decide_v1(primary_md, secondary_md)

    for element in elements:
        if element.page_number in routes:
            choice, reason = routes[element.page_number]
            element.metadata["page_route"] = {"choice": choice, "reason": reason}

    rerouted = {page: route for page, route in routes.items() if route[0] != primary_name}
    if not routes:
        return MethodResult(
            success=False,
            validated=False,
            message="Obrigação sem página identificada para o roteador",
        )
    if rerouted:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Roteador por página escolhe o provider secundário ("
                + ", ".join(f"p{page}: {reason}" for page, (_, reason) in sorted(rerouted.items()))
                + "); a troca de página não é aplicada automaticamente"
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message="Roteador por página mantém o provider primário nas páginas "
        + ", ".join(map(str, sorted(routes))),
    )


#: Longest side of the page image sent to the judge. Two candidates plus the
#: image must fit one 8192-token slot of the production VLM server.
_JUDGE_PAGE_MAX_SIDE = 1600


def _render_page_png(
    source_path: Path,
    page_number: int,
    artifact_id: str | None = None,
    media_type: str | None = None,
    *, render_toolbox=None,
) -> bytes:
    """Page ``page_number`` (1-based) of a PDF, PNG or JPEG as PNG, rendered by the Toolbox.

    At most 144 dpi, with the longest side bounded by ``_JUDGE_PAGE_MAX_SIDE``.
    """
    return (render_toolbox or _toolbox_render)(
        source_path,
        page_number,
        media_type=media_type,
        dpi=144,
        max_side=_JUDGE_PAGE_MAX_SIDE,
        artifact_id=artifact_id,
    ).image

def _judge_page(image: bytes, candidate_a: str, candidate_b: str) -> dict[str, Any]:
    """Call document.adjudicate.page through the Toolbox (sync wrapper)."""
    import tempfile

    from acessilia_sdk.toolbox.clients import ToolboxAdjudicateClient
    from backend.tools.toolbox_client import make_toolbox_client, run_sync

    async def call(path: Path) -> dict[str, Any]:
        client = make_toolbox_client(ToolboxAdjudicateClient)
        try:
            result = await client.judge_page(candidate_a, candidate_b, file_path=path)
        finally:
            await client.close()
        logger.info(
            "Toolbox: page adjudication finished (pB={})",
            result.get("document", {}).get("pB", "?"),
        )
        return result

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.png"
        path.write_bytes(image)
        return run_sync(call(path))


#: Characters per candidate the Toolbox's judge reads (``judge_max_chars`` of
#: ``qwen3-vl-8b`` in its ``providers-config.yaml``); beyond it the candidate is
#: cut on the Toolbox side, so the verdict does not cover the whole page.
_JUDGE_MAX_CHARS = 6000


def _judge_probability(document: dict[str, Any]) -> float | None:
    """``pB`` of a judge answer, or ``None`` when absent or outside [0, 1]."""
    try:
        p_b = float(document["pB"])
    except (KeyError, TypeError, ValueError):
        return None
    return p_b if 0.0 <= p_b <= 1.0 else None


def _handle_vlm_pairwise_judge_method(
    manifest: ProcessingManifest, obligation_id: str,
    *, render_page_png=None, judge_page=None,
) -> MethodResult:
    """Pairwise VLM judge between the two providers' pages.

    The Toolbox (``document.adjudicate.page``) returns the position-debiased
    probability that the secondary page is the more faithful transcription.
    Candidate A is the page the manifest carries and B the other provider's:
    the secondary page, or, on a page the deterministic router already took
    from the secondary provider (its elements carry that ``metadata.provider``),
    the primary elements it replaced. Such a page without the replaced elements
    in the artifact is skipped: comparing it with the secondary page would
    compare MinerU with itself and report a confirmation. Only pages
    where the two transcriptions diverge are judged (``should_judge``). Of
    docstruct's two-threshold rule (``decide_pair``) only the ``p_new >
    DEFAULT_NEW_THR`` branch applies: there is no previous judge to consult
    here. A confirmed page satisfies the obligation; a preferred alternative is
    recorded in ``page_judgement`` but not swapped in (that would reopen the
    manifest mid-plan), so the method fails and the non-critical obligation
    becomes a technical warning. An answer without a valid ``pB`` fails the
    method instead of counting as a confirmation.
    """
    from docstruct.fusion.judge import DEFAULT_NEW_THR, should_judge

    from backend.tools.toolbox_client import (
        ToolboxAuthenticationError,
        ToolboxContractViolation,
        ToolboxError,
    )

    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)
    secondary, failure = _secondary_or_failure(manifest)
    if failure is not None:
        return failure

    primary_name = _CALIBRATED_PAIR[0]
    source_path = Path(manifest.source.path)
    judgements: dict[int, dict[str, Any]] = {}
    for page_number in sorted({e.page_number for e in elements if e.page_number}):
        page_elements = [
            element
            for element in sorted(manifest.elements, key=lambda e: e.reading_order)
            if element.page_number == page_number
        ]
        adopted = any(
            element.metadata.get("provider") == secondary.provider
            for element in page_elements
        )
        if adopted:
            if page_number not in secondary.replaced_primary:
                logger.warning(
                    "vlm-pairwise-judge: page {} came from {}, but the artifact does not "
                    "keep the {} page it replaced; skipped",
                    page_number,
                    secondary.provider,
                    primary_name,
                )
                continue
            content, other = secondary.provider, primary_name
            other_blocks = secondary.replaced_primary[page_number]
        else:
            content, other = primary_name, secondary.provider
            other_blocks = secondary.pages.get(page_number, [])
        primary_md = _page_markdown(
            (element.type, element.text) for element in page_elements
        )
        secondary_md = _page_markdown(
            (str(block.get("type") or ""), block.get("text"))
            for block in other_blocks
        )
        if not primary_md.strip() or not secondary_md.strip():
            logger.info(
                "vlm-pairwise-judge: página {} sem as duas transcrições; ignorada", page_number
            )
            continue
        if not should_judge(primary_md, secondary_md):
            judgements[page_number] = {
                "content": content, "choice": content, "pB": None, "reason": "agree",
            }
            continue

        try:
            image = (render_page_png or _render_page_png)(
                source_path,
                page_number,
                _source_artifact_id(manifest),
                manifest.source.media_type,
            )
        except Exception as exc:  # noqa: BLE001 - rendering a broken source
            return MethodResult(
                success=False,
                validated=False,
                message=f"Página {page_number} não pôde ser renderizada: {exc}",
            )
        truncated = max(len(primary_md), len(secondary_md)) > _JUDGE_MAX_CHARS
        if truncated:
            logger.warning(
                "vlm-pairwise-judge: página {} passa de {} caracteres; "
                "o juiz lê a transcrição truncada",
                page_number,
                _JUDGE_MAX_CHARS,
            )
        logger.info("vlm-pairwise-judge: julgando a página {}", page_number)
        try:
            result = (judge_page or _judge_page)(image, primary_md, secondary_md)
        except (ToolboxAuthenticationError, ToolboxContractViolation) as exc:
            return MethodResult(
                success=False,
                validated=False,
                message=f"Juiz VLM recusou a chamada: {type(exc).__name__}: {exc}",
            )
        except ToolboxError as exc:
            return MethodResult(
                success=False,
                validated=False,
                message=f"Juiz VLM indisponível: {type(exc).__name__}: {exc}",
            )
        document = result.get("document") or {}
        p_b = _judge_probability(document)
        if p_b is None:
            return MethodResult(
                success=False,
                validated=False,
                message=(
                    f"Juiz VLM não devolveu pB válido para a página {page_number}: "
                    f"{document.get('pB')!r}"
                ),
            )
        judgements[page_number] = {
            "content": content,
            "choice": other if p_b > DEFAULT_NEW_THR else content,
            "pB": p_b,
            "pB_ab": document.get("pB_ab"),
            "pA_ba": document.get("pA_ba"),
            "model": document.get("model"),
            "truncated": truncated,
        }

    if not judgements:
        return MethodResult(
            success=False,
            validated=False,
            message="Sem páginas com as duas transcrições para o juiz VLM",
        )
    for element in elements:
        if element.page_number in judgements:
            element.metadata["page_judgement"] = judgements[element.page_number]

    preferred = {
        page: judgement
        for page, judgement in judgements.items()
        if judgement["choice"] != judgement["content"]
    }
    if preferred:
        return MethodResult(
            success=False,
            validated=False,
            message=(
                "Juiz VLM prefere o outro provider ("
                + ", ".join(
                    f"p{page}: {j['choice']} pB={j['pB']:.2f}"
                    for page, j in sorted(preferred.items())
                )
                + "); a troca de página não é aplicada automaticamente"
            ),
        )
    return MethodResult(
        success=True,
        validated=True,
        message="Juiz VLM confirma a página aplicada: "
        + ", ".join(
            f"{page} ({j['content']}, pB={j['pB']:.2f})"
            if j["pB"] is not None
            else f"{page} ({j['content']}, concordam)"
            for page, j in sorted(judgements.items())
        ),
    )


def _apply_page_order(current: list[ManifestElement], order: list[int]) -> bool:
    """Reassign the reading-order slots of ``current`` following ``order``.

    ``order[i]`` is the index in ``current`` of the element that goes to the
    i-th slot. Returns whether anything moved.
    """
    if order == list(range(len(current))):
        return False
    slots = sorted(element.reading_order for element in current)
    for slot, index in zip(slots, order):
        current[index].reading_order = slot
    return True


def _refresh_page_element_ids(
    manifest: ProcessingManifest, page_numbers: set[int]
) -> None:
    manifest.elements.sort(key=lambda element: element.reading_order)
    for page in manifest.pages:
        if page.page_number in page_numbers:
            page.element_ids = [
                element.id
                for element in manifest.elements
                if element.page_number == page.page_number
            ]


def _handle_xycut_order_method(
    manifest: ProcessingManifest, obligation_id: str,
    *, fusion_agent_factory=None,
) -> MethodResult:
    """Apply XY-cut reading order to the evidenced page elements."""
    elements = _target_elements(manifest, obligation_id)
    if not elements:
        return _missing_obligation(obligation_id)

    fusion_agent = (fusion_agent_factory or _fusion_agent)()
    reordered_pages: set[int] = set()
    by_page: dict[int, list[ManifestElement]] = {}
    for element in elements:
        if element.page_number is not None:
            by_page.setdefault(element.page_number, []).append(element)

    for page_number, page_elements in by_page.items():
        positioned = [
            element for element in page_elements
            if _element_bbox(element, manifest) is not None
        ]
        if len(positioned) < 2:
            continue
        current = sorted(positioned, key=lambda element: element.reading_order)
        boxes = [_element_bbox(element, manifest) for element in current]
        order = fusion_agent.process_refine_reading_order(
            [list(box) for box in boxes if box is not None]
        )
        if _apply_page_order(current, order):
            reordered_pages.add(page_number)

    if not reordered_pages:
        return MethodResult(
            success=False,
            validated=False,
            message="XY-cut não identificou uma mudança de ordem de leitura",
        )

    _refresh_page_element_ids(manifest, reordered_pages)
    return MethodResult(
        success=True,
        validated=True,
        message=(
            "Ordem de leitura XY-cut aplicada nas páginas "
            + ", ".join(map(str, sorted(reordered_pages)))
        ),
    )
