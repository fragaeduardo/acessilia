"""Gated specialist slots, not additional solvers or Toolbox handlers.

Domain specialists can be activated only after there is a real detector,
provider/capability binding and output validator. No specialist is active
at this architectural refactor stage; the universal formula analyzer
already owns mathematics-related existing obligations.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecialistSlot:
    name: str
    detector: str | None = None
    provider_binding: str | None = None
    validator: str | None = None
    enabled: bool = False

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled and self.detector
            and self.provider_binding and self.validator
        )


SLOTS: tuple[SpecialistSlot, ...] = (
    SpecialistSlot("chemistry"),
    SpecialistSlot("mathematics"),
    SpecialistSlot("music"),
)


def enabled_specialists() -> tuple[SpecialistSlot, ...]:
    """Never infer provider availability from a domain name."""
    return tuple(slot for slot in SLOTS if slot.ready)
