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
    """Raised on API failure from Feishu API."""

    # Permanent error codes — retrying won't help
    PERMANENT_CODES = frozenset({
        99992354,  # message_id not exists / invalid
        230099,    # card content invalid (e.g. element exceeds the 200-component limit)
    })

    def __init__(self, message: str = "", *, code: int = 0):
        self.code = code
        super().__init__(message)

    @property
    def is_permanent(self) -> bool:
        """Whether this error is permanent (retrying won't help)."""
        return self.code in self.PERMANENT_CODES
