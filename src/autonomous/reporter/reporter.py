"""Reporter and Outbox - reliable delivery of status updates and results.

Handles: progress notifications, completion reports, approval requests,
blocking notifications. Implements retry, dedup, receipts, and dead letter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from ..models import ProgressSnapshot, _new_id


class DeliveryState(Enum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class ReportType(Enum):
    GOAL_ACCEPTED = "goal_accepted"
    RUN_STARTED = "run_started"
    PROGRESS_UPDATE = "progress_update"
    APPROVAL_REQUEST = "approval_request"
    DECISION_REQUEST = "decision_request"
    BLOCKED = "blocked"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    GOAL_CANCELED = "goal_canceled"
    KILL_ACTIVATED = "kill_activated"


@dataclass
class OutboxEntry:
    """A pending delivery in the outbox."""
    entry_id: str = field(default_factory=lambda: _new_id("out"))
    report_type: ReportType = ReportType.PROGRESS_UPDATE
    target: str = ""  # channel/user identifier
    payload: dict = field(default_factory=dict)
    state: DeliveryState = DeliveryState.PENDING
    created_at: float = field(default_factory=time.time)
    last_attempt_at: Optional[float] = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_retry_at: float = 0.0
    delivered_at: Optional[float] = None
    error: str = ""
    idempotency_key: str = ""

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "report_type": self.report_type.value,
            "target": self.target,
            "payload": self.payload,
            "state": self.state.value,
            "created_at": self.created_at,
            "last_attempt_at": self.last_attempt_at,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "next_retry_at": self.next_retry_at,
            "delivered_at": self.delivered_at,
            "error": self.error,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> OutboxEntry:
        return cls(
            entry_id=data["entry_id"],
            report_type=ReportType(data["report_type"]),
            target=data.get("target", ""),
            payload=data.get("payload", {}),
            state=DeliveryState(data.get("state", "pending")),
            created_at=data.get("created_at", 0),
            last_attempt_at=data.get("last_attempt_at"),
            attempt_count=data.get("attempt_count", 0),
            max_attempts=data.get("max_attempts", 5),
            next_retry_at=data.get("next_retry_at", 0),
            delivered_at=data.get("delivered_at"),
            error=data.get("error", ""),
            idempotency_key=data.get("idempotency_key", ""),
        )


class Reporter:
    """Reliable report delivery with retry and dead letter."""

    def __init__(
        self,
        deliver_fn: Callable[[str, dict], bool],
        max_retries: int = 5,
        backoff_base: float = 5.0,
    ):
        self._deliver = deliver_fn
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._outbox: list[OutboxEntry] = []
        self._delivered_keys: set[str] = set()

    async def enqueue(
        self,
        report_type: ReportType,
        target: str,
        payload: dict,
        idempotency_key: str = "",
    ) -> str:
        """Add a report to the outbox. Returns entry_id."""
        if idempotency_key and idempotency_key in self._delivered_keys:
            return ""  # already delivered, skip

        entry = OutboxEntry(
            report_type=report_type,
            target=target,
            payload=payload,
            max_attempts=self._max_retries,
            idempotency_key=idempotency_key,
        )
        self._outbox.append(entry)
        return entry.entry_id

    async def flush(self) -> int:
        """Attempt to deliver all pending entries. Returns count delivered."""
        now = time.time()
        delivered = 0

        for entry in self._outbox:
            if entry.state not in (DeliveryState.PENDING, DeliveryState.FAILED):
                continue
            if entry.next_retry_at > now:
                continue

            entry.state = DeliveryState.SENDING
            entry.last_attempt_at = now
            entry.attempt_count += 1

            try:
                success = self._deliver(entry.target, entry.payload)
            except Exception as exc:
                success = False
                entry.error = str(exc)

            if success:
                entry.state = DeliveryState.DELIVERED
                entry.delivered_at = now
                if entry.idempotency_key:
                    self._delivered_keys.add(entry.idempotency_key)
                delivered += 1
            elif entry.attempt_count >= entry.max_attempts:
                entry.state = DeliveryState.DEAD_LETTER
            else:
                entry.state = DeliveryState.FAILED
                backoff = self._backoff_base * (2 ** (entry.attempt_count - 1))
                entry.next_retry_at = now + backoff

        return delivered

    def get_pending(self) -> list[OutboxEntry]:
        return [e for e in self._outbox if e.state in (DeliveryState.PENDING, DeliveryState.FAILED)]

    def get_dead_letters(self) -> list[OutboxEntry]:
        return [e for e in self._outbox if e.state == DeliveryState.DEAD_LETTER]

    def get_delivered(self) -> list[OutboxEntry]:
        return [e for e in self._outbox if e.state == DeliveryState.DELIVERED]

    async def report_progress(self, target: str, snapshot: ProgressSnapshot) -> str:
        """Convenience: enqueue a progress update."""
        return await self.enqueue(
            ReportType.PROGRESS_UPDATE,
            target,
            snapshot.to_dict(),
            idempotency_key=f"progress_{snapshot.run_id}_{snapshot.updated_at}",
        )

    async def report_completion(self, target: str, run_id: str, result: dict) -> str:
        """Convenience: enqueue a completion report."""
        return await self.enqueue(
            ReportType.RUN_COMPLETED,
            target,
            {"run_id": run_id, **result},
            idempotency_key=f"completed_{run_id}",
        )

    async def report_failure(self, target: str, run_id: str, reason: str) -> str:
        return await self.enqueue(
            ReportType.RUN_FAILED,
            target,
            {"run_id": run_id, "reason": reason},
            idempotency_key=f"failed_{run_id}",
        )

    async def request_approval(self, target: str, approval_id: str, context: dict) -> str:
        return await self.enqueue(
            ReportType.APPROVAL_REQUEST,
            target,
            {"approval_id": approval_id, **context},
        )
