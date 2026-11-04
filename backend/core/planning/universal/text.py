"""Universal text analyzer routing for existing manifest obligations.

No provider calls, new obligations, or local PDDL solver are introduced.
"""
from __future__ import annotations

OBLIGATION_KINDS: frozenset[str] = frozenset({"normalize-text","repair-legacy-unicode","guard-repetition","join-tail-blocks","chunk-verse","extract-line-numbers"})


def accepts(obligation_kind: str) -> bool:
    return obligation_kind in OBLIGATION_KINDS
