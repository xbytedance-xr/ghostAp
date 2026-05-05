"""Shared types for the card delivery layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MutationOutcome:
    """Result of a card mutation attempt."""

    kind: Literal["applied", "reconcile", "skipped", "rejected"]
    message: str = ""


class SequenceConflictError(Exception):
    """Raised when Feishu returns 300317 (sequence conflict)."""

    def __init__(self, next_floor: int = 0):
        self.next_floor = next_floor
        super().__init__(f"Sequence conflict, floor={next_floor}")


class TransportError(Exception):
    """Raised on 5xx / timeout from Feishu API."""
    pass
