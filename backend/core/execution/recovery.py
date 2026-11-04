"""Delivery/recovery policies for PDDL execution.

This module owns the existing rules for critical obligations, exhausted
methods, replan eligibility, and user-facing page summaries. All algorithms
were moved unchanged from pddl_orchestrator; it does not call a solver.
"""

from __future__ import annotations

from collections.abc import Iterable

from backend.core.manifest.models import ManifestElement, Obligation, ProcessingManifest
from backend.core.execution.messages import EXE_COMPLETED_OBLIGATIONS_UNSATISFIED
from backend.i18n import t
from backend.stage_messages import STAGE_PAGE, STAGE_PAGE_OF, STAGE_PAGES, STAGE_PAGE_UNKNOWN
from backend.tools.formula_tools import normalize_latex


def _formula_elements_for_obligation(
    manifest: ProcessingManifest, obligation_id: str
) -> list[ManifestElement]:
    obligation = next(
        (o for o in manifest.obligations if o.id == obligation_id), None
    )
    if obligation is None:
        return []
    targets = set(obligation.target_ids)
    return [
        el for el in manifest.elements if el.id in targets and el.type == "formula"
    ]


#: Leaving one open (every automatic method failed and it was escalated to
#: human review) does not lose content, so the job finishes with a warning
#: instead of failing. Heading levels are also normalized by the canonical
#: builder, which keeps the exported document valid without the repair.
#: `refine-reading-order` is deliberately absent: a wrong reading order is what
#: a screen reader user hits first, so an open one still fails the job.
NON_CRITICAL_OBLIGATION_KINDS = frozenset(
    {
        # The adjudication verdict is recorded but cannot be applied (a page
        # swap would reopen the manifest mid-plan), and the extracted content
        # is complete either way, so a verdict for the secondary provider must
        # not fail the job.
        "adjudicate-page",
        "repair-heading-hierarchy",
        "join-tail-blocks",
        "normalize-text",
        "repair-legacy-unicode",
        "chunk-verse",
        "extract-line-numbers",
        # No handler implements `rescue-omitted` yet, so its only path is
        # human review: as a critical kind it failed every dual-fusion job
        # with an omitted block. The content the primary provider extracted
        # is delivered and the result e-mail names the pages of the omitted
        # block. Remove the kind from here once the handler lands (#236).
        "rescue-omitted-block",
        # The vision model is the only automatic method, and a degenerate
        # answer repeats on every try: in 006.pdf two images out of 40 failed
        # a 42-page document. The picture is exported with the generic alt
        # text and the result e-mail names its page. Unless no picture at all
        # was described: see _blocks_delivery.
        "describe-image",
    }
)


def _formula_has_readable_content(
    obligation: Obligation, manifest: ProcessingManifest
) -> bool:
    """Whether every target formula still carries something readable.

    A formula the Toolbox could not verbalize or normalize is exported as its
    LaTeX: the canonical builder and the renderers fall back to it when
    ``math.verbalize`` is unavailable or rejects the input, so the document
    keeps the content. That is a degradation, not a lost document. A formula
    with neither LaTeX, a normalized LaTeX, MathML nor a verbalization has
    nothing to deliver, so its obligation stays critical.
    """
    targets = _formula_elements_for_obligation(manifest, obligation.id)
    return bool(targets) and all(
        bool(normalize_latex(element.text or ""))
        or bool(str(element.metadata.get("latex_normalized") or "").strip())
        or bool(str(element.metadata.get("mathml") or "").strip())
        or bool(str(element.metadata.get("verbalization") or "").strip())
        for element in targets
    )


def _blocks_delivery(obligation: Obligation, manifest: ProcessingManifest) -> bool:
    """Whether an open obligation fails the job instead of leaving a warning.

    ``describe-image`` is non-critical while some picture was described: a few
    degenerate answers are sampling (2 of 40 in 006.pdf). With none described,
    the model is unreachable or misconfigured (``describe_region`` turns a
    timeout or an auth error into ``None``), and every picture would silently
    get the generic alt text, so the job fails. A document with a single
    picture its one description failed on fails too.

    ``verbalize-formula`` and ``normalize-formula`` are non-critical while the
    target formulas keep their LaTeX: without MathML or spoken text the exported
    document still shows the formula source, and the result e-mail names the
    page. With no readable content at all there is nothing to deliver, so the
    obligation fails the job.
    """
    if obligation.kind == "describe-image":
        return not any(
            other.kind == "describe-image" and other.status == "satisfied"
            for other in manifest.obligations
        )
    if obligation.kind in {"verbalize-formula", "normalize-formula"}:
        return not _formula_has_readable_content(obligation, manifest)
    return obligation.kind not in NON_CRITICAL_OBLIGATION_KINDS


def _is_open_obligation(obligation: Obligation) -> bool:
    """Whether the job selected the obligation and left it unsatisfied.

    An exhausted obligation is left out of later replans, which clears its
    `selected` flag; its `failed` status keeps it open.
    """
    return obligation.status != "satisfied" and (
        obligation.selected or obligation.status == "failed"
    )


def _unsatisfied_selected_obligations(manifest: ProcessingManifest) -> list:
    return [
        obligation for obligation in manifest.obligations
        if _is_open_obligation(obligation)
    ]


def _replannable_obligation_ids(manifest: ProcessingManifest) -> list[str]:
    """Open selected obligations that a new plan can still satisfy.

    Exhausted obligations, and those depending on one, are excluded: the planner
    rejects an obligation without an untried admissible method.
    """
    by_id = {obligation.id: obligation for obligation in manifest.obligations}

    def blocked(obligation_id: str, seen: set[str]) -> bool:
        if obligation_id in seen:
            return False
        seen.add(obligation_id)
        obligation = by_id[obligation_id]
        if obligation.status == "failed":
            return True
        return any(blocked(dependency, seen) for dependency in obligation.dependencies)

    return [
        obligation.id for obligation in manifest.obligations
        if obligation.selected
        and obligation.status != "satisfied"
        and not blocked(obligation.id, set())
    ]


def _unsatisfied_summary(manifest: ProcessingManifest) -> str:
    """The open obligations grouped by kind with their pages.

    "refine-reading-order (pages 1, 7, 11); describe-image (page 18)" instead of
    one id per obligation, each listing every target element.
    """
    pages: dict[str, set[int]] = {}
    unknown: set[str] = set()
    for obligation in _unsatisfied_selected_obligations(manifest):
        numbers = {
            page for page in _obligation_pages(manifest, obligation.id) if page
        }
        pages.setdefault(obligation.kind, set()).update(numbers)
        if not numbers:
            unknown.add(obligation.kind)
    groups = []
    for kind, numbers in pages.items():
        labels = [_pages_label(numbers)] if numbers else []
        if kind in unknown:
            labels.append(t(STAGE_PAGE_UNKNOWN))
        groups.append(f"{kind} ({', '.join(labels)})")
    return t(EXE_COMPLETED_OBLIGATIONS_UNSATISFIED).format(unsatisfied="; ".join(groups))


def _pages_label(
    pages: Iterable[int | None], total_pages: int | None = None
) -> str:
    """Localized location of a unit of work, e.g. "page 3" or "pages 3, 4".

    When the document's page count is known and the work sits on a single page,
    the label also names the total, e.g. "page 3 of 12": progress messages pair
    that with image or step counters ("image 11 of 37 (page 18 of 42)"), and
    without the total users could read 37 as the page count. Single-page
    documents keep the plain "page 1" label because "of 1" adds nothing.
    """
    numbers = sorted({page for page in pages if page})
    if not numbers:
        return t(STAGE_PAGE_UNKNOWN)
    if len(numbers) == 1:
        if total_pages is not None and total_pages > 1:
            return t(STAGE_PAGE_OF).format(page=numbers[0], total_pages=total_pages)
        return t(STAGE_PAGE).format(page=numbers[0])
    return t(STAGE_PAGES).format(pages=", ".join(str(page) for page in numbers))


def _obligation_pages(manifest: ProcessingManifest, obligation_id: str) -> list[int | None]:
    obligation = next(
        (item for item in manifest.obligations if item.id == obligation_id), None
    )
    if obligation is None:
        return []
    page_by_element = {element.id: element.page_number for element in manifest.elements}
    return [page_by_element.get(target) for target in obligation.target_ids]
