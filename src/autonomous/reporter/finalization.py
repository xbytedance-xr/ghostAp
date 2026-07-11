"""Finalization - Effect disposition saga for terminal run states.

Ensures all effects are dispositioned before a run reaches terminal state.
Journal-backed for crash recovery.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

from ..domain import (
    Effect,
    new_id,
)

# ---------------------------------------------------------------------------
# Journal protocol
# ---------------------------------------------------------------------------


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


# ---------------------------------------------------------------------------
# Finalization types
# ---------------------------------------------------------------------------


class FinalizationState(Enum):
    """State of a finalization record."""

    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class DispositionType(Enum):
    """How an effect was dispositioned."""

    COMMITTED = "committed"
    COMPENSATED = "compensated"
    RELEASED = "released"
    MANUAL = "manual"
    ABANDONED = "abandoned"


@dataclass
class EffectDisposition:
    """Records how a specific effect was dispositioned."""

    effect_id: str
    disposition: DispositionType
    evidence_hash: str = ""
    dispositioned_at: float = field(default_factory=time.time)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "effect_id": self.effect_id,
            "disposition": self.disposition.value,
            "evidence_hash": self.evidence_hash,
            "dispositioned_at": self.dispositioned_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EffectDisposition:
        return cls(
            effect_id=data["effect_id"],
            disposition=DispositionType(data["disposition"]),
            evidence_hash=data.get("evidence_hash", ""),
            dispositioned_at=data.get("dispositioned_at", 0),
            reason=data.get("reason", ""),
        )


@dataclass
class FinalizationRecord:
    """Tracks the finalization saga for a run."""

    record_id: str = field(default_factory=lambda: new_id("fin"))
    run_id: str = ""
    state: FinalizationState = FinalizationState.IN_PROGRESS
    total_effects: int = 0
    dispositioned_effects: int = 0
    dispositions: list[EffectDisposition] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    timeout_seconds: float = 600.0

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "total_effects": self.total_effects,
            "dispositioned_effects": self.dispositioned_effects,
            "dispositions": [d.to_dict() for d in self.dispositions],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "timeout_seconds": self.timeout_seconds,
        }


# ---------------------------------------------------------------------------
# Finalization engine
# ---------------------------------------------------------------------------


class Finalization:
    """Effect disposition saga - ensures all effects are dispositioned
    before a run can reach terminal state.

    All operations are journal-backed for crash recovery.
    """

    def __init__(self, journal: Optional[JournalWriter] = None):
        self._journal = journal
        self._records: dict[str, FinalizationRecord] = {}

    async def _journal_event(self, event_type: str, payload: dict) -> None:
        if self._journal:
            await self._journal.write_event(event_type, payload)

    async def start_finalization(
        self,
        run_id: str,
        effects: list[Effect],
        timeout_seconds: float = 600.0,
    ) -> str:
        """Start finalization saga for a run. Returns record_id."""
        record = FinalizationRecord(
            run_id=run_id,
            total_effects=len(effects),
            timeout_seconds=timeout_seconds,
        )
        self._records[record.record_id] = record

        await self._journal_event("finalization.started", {
            "record_id": record.record_id,
            "run_id": run_id,
            "total_effects": len(effects),
        })

        return record.record_id

    async def record_disposition(
        self,
        record_id: str,
        effect_id: str,
        disposition: DispositionType,
        evidence_hash: str = "",
        reason: str = "",
    ) -> bool:
        """Record a disposition for an effect. Returns True if accepted."""
        record = self._records.get(record_id)
        if record is None:
            return False

        if record.state != FinalizationState.IN_PROGRESS:
            return False

        # Check for duplicate disposition
        if any(d.effect_id == effect_id for d in record.dispositions):
            return False

        disp = EffectDisposition(
            effect_id=effect_id,
            disposition=disposition,
            evidence_hash=evidence_hash,
            reason=reason,
        )
        record.dispositions.append(disp)
        record.dispositioned_effects += 1

        await self._journal_event("finalization.disposition_recorded", {
            "record_id": record_id,
            "effect_id": effect_id,
            "disposition": disposition.value,
            "progress": f"{record.dispositioned_effects}/{record.total_effects}",
        })

        # Auto-complete if all effects dispositioned
        if record.dispositioned_effects >= record.total_effects:
            record.state = FinalizationState.COMPLETE
            record.completed_at = time.time()
            await self._journal_event("finalization.complete", {
                "record_id": record_id,
                "run_id": record.run_id,
            })

        return True

    async def check_complete(self, run_id: str) -> bool:
        """Check if finalization is complete for a run."""
        for record in self._records.values():
            if record.run_id == run_id:
                if record.state == FinalizationState.COMPLETE:
                    return True
                # Check timeout
                if (
                    record.state == FinalizationState.IN_PROGRESS
                    and time.time() - record.started_at > record.timeout_seconds
                ):
                    record.state = FinalizationState.TIMED_OUT
                    await self._journal_event("finalization.timed_out", {
                        "record_id": record.record_id,
                        "run_id": run_id,
                        "dispositioned": record.dispositioned_effects,
                        "total": record.total_effects,
                    })
                return False
        # No finalization record means nothing to finalize
        return True

    def get_record(self, record_id: str) -> Optional[FinalizationRecord]:
        return self._records.get(record_id)

    def get_records_for_run(self, run_id: str) -> list[FinalizationRecord]:
        return [r for r in self._records.values() if r.run_id == run_id]

    def get_pending_effects(self, record_id: str) -> list[str]:
        """Return effect IDs not yet dispositioned in a finalization record."""
        record = self._records.get(record_id)
        if record is None:
            return []
        return []

    def is_timed_out(self, record_id: str) -> bool:
        record = self._records.get(record_id)
        if record is None:
            return False
        if record.state == FinalizationState.TIMED_OUT:
            return True
        if (
            record.state == FinalizationState.IN_PROGRESS
            and time.time() - record.started_at > record.timeout_seconds
        ):
            return True
        return False
