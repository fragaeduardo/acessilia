"""Universal structure analyzer routing for existing manifest obligations.

No provider calls, new obligations, or local PDDL solver are introduced.
"""
from __future__ import annotations

OBLIGATION_KINDS: frozenset[str] = frozenset({"review-structure","repair-heading-hierarchy","refine-reading-order","inspect-unstructured-page","fuse-providers","rescue-omitted-block","adjudicate-page"})


def accepts(obligation_kind: str) -> bool:
    return obligation_kind in OBLIGATION_KINDS
