"""DurableScheduler: persistent ready queue with leases, retries, and backpressure."""

from __future__ import annotations

import heapq
import random
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

from ..models import PlanStep

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Journal protocol (avoid hard dep on journal module)
# ---------------------------------------------------------------------------

class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LeaseGrant:
    lease_id: str
    attempt_id: str
    step_id: str
    run_id: str
    fencing_token: int
    granted_at: float
    expires_at: float
    worker_id: str


@dataclass
class QueueEntry:
    step_id: str
    run_id: str
    plan_epoch: int
    priority: int = 0
    enqueued_at: float = 0.0
    retry_count: int = 0
    max_retries: int = 3
    next_retry_after: float = 0.0
    backoff_base: float = 5.0

    def __lt__(self, other: QueueEntry) -> bool:
        # Higher priority first, then earlier enqueue
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.enqueued_at < other.enqueued_at


@dataclass
class DeadLetterEntry:
    step_id: str
    run_id: str
    reason: str
    moved_at: float


@dataclass
class SchedulerStats:
    queued: int
    active_leases: int
    dead_letters: int
    total_completed: int
    total_failed: int


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class DurableScheduler:
    def __init__(
        self,
        journal: JournalWriter,
        max_concurrent: int = 10,
        default_lease_seconds: float = 300.0,
    ) -> None:
        self._journal = journal
        self._max_concurrent = max_concurrent
        self._default_lease_seconds = default_lease_seconds

        self._fencing_counter: int = 0
        self._queue: dict[str, QueueEntry] = {}  # step_id -> entry
        self._active_leases: dict[str, LeaseGrant] = {}  # lease_id -> grant
        self._step_to_lease: dict[str, str] = {}  # step_id -> lease_id
        self._dead_letters: dict[str, DeadLetterEntry] = {}  # step_id -> entry
        # Sorted by expiry for efficient expired-lease checks
        self._expiry_heap: list[tuple[float, str]] = []  # (expires_at, lease_id)

        self._total_completed: int = 0
        self._total_failed: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue_step(self, step: PlanStep, run_id: str, plan_epoch: int) -> None:
        entry = QueueEntry(
            step_id=step.step_id,
            run_id=run_id,
            plan_epoch=plan_epoch,
            enqueued_at=time.time(),
            max_retries=step.max_attempts,
            backoff_base=5.0,
        )
        await self._journal.write_event("scheduler.enqueue", {
            "step_id": step.step_id,
            "run_id": run_id,
            "plan_epoch": plan_epoch,
        })
        self._queue[step.step_id] = entry

    async def acquire_lease(self, step_id: str, worker_id: str) -> Optional[LeaseGrant]:
        if step_id not in self._queue:
            return None
        if step_id in self._step_to_lease:
            return None
        if len(self._active_leases) >= self._max_concurrent:
            return None

        entry = self._queue[step_id]
        now = time.time()
        if now < entry.next_retry_after:
            return None

        self._fencing_counter += 1
        lease_id = f"lease_{uuid.uuid4().hex[:12]}"
        attempt_id = f"att_{uuid.uuid4().hex[:12]}"
        expires_at = now + self._default_lease_seconds

        grant = LeaseGrant(
            lease_id=lease_id,
            attempt_id=attempt_id,
            step_id=step_id,
            run_id=entry.run_id,
            fencing_token=self._fencing_counter,
            granted_at=now,
            expires_at=expires_at,
            worker_id=worker_id,
        )

        await self._journal.write_event("scheduler.lease_acquired", {
            "lease_id": lease_id,
            "attempt_id": attempt_id,
            "step_id": step_id,
            "run_id": entry.run_id,
            "fencing_token": self._fencing_counter,
            "worker_id": worker_id,
            "expires_at": expires_at,
        })

        self._active_leases[lease_id] = grant
        self._step_to_lease[step_id] = lease_id
        heapq.heappush(self._expiry_heap, (expires_at, lease_id))
        return grant

    async def renew_lease(self, lease_id: str) -> bool:
        grant = self._active_leases.get(lease_id)
        if grant is None:
            return False
        now = time.time()
        if now > grant.expires_at:
            return False
        new_expires = now + self._default_lease_seconds
        grant.expires_at = new_expires
        await self._journal.write_event("scheduler.lease_renewed", {
            "lease_id": lease_id,
            "new_expires_at": new_expires,
        })
        heapq.heappush(self._expiry_heap, (new_expires, lease_id))
        return True

    async def release_lease(self, lease_id: str, success: bool) -> None:
        grant = self._active_leases.pop(lease_id, None)
        if grant is None:
            return
        self._step_to_lease.pop(grant.step_id, None)

        await self._journal.write_event("scheduler.lease_released", {
            "lease_id": lease_id,
            "step_id": grant.step_id,
            "success": success,
        })

        if success:
            self._queue.pop(grant.step_id, None)
            self._total_completed += 1
        else:
            entry = self._queue.get(grant.step_id)
            if entry:
                entry.retry_count += 1
                if entry.retry_count >= entry.max_retries:
                    await self.mark_dead_letter(grant.step_id, "max_retries_exceeded")
                else:
                    jitter = random.uniform(0, 1) * entry.backoff_base
                    delay = entry.backoff_base * (2 ** entry.retry_count) + jitter
                    entry.next_retry_after = time.time() + delay
            self._total_failed += 1

    async def check_expired_leases(self) -> list[LeaseGrant]:
        now = time.time()
        expired: list[LeaseGrant] = []
        while self._expiry_heap and self._expiry_heap[0][0] <= now:
            exp_time, lease_id = heapq.heappop(self._expiry_heap)
            grant = self._active_leases.get(lease_id)
            if grant is None:
                continue
            # Stale heap entry (lease was renewed)
            if grant.expires_at > now:
                continue
            expired.append(grant)
            self._active_leases.pop(lease_id, None)
            self._step_to_lease.pop(grant.step_id, None)

            await self._journal.write_event("scheduler.lease_expired", {
                "lease_id": lease_id,
                "step_id": grant.step_id,
                "worker_id": grant.worker_id,
            })

            # Schedule retry
            entry = self._queue.get(grant.step_id)
            if entry:
                entry.retry_count += 1
                if entry.retry_count >= entry.max_retries:
                    await self.mark_dead_letter(grant.step_id, "lease_expired_max_retries")
                else:
                    jitter = random.uniform(0, 1) * entry.backoff_base
                    delay = entry.backoff_base * (2 ** entry.retry_count) + jitter
                    entry.next_retry_after = time.time() + delay

        return expired

    async def get_ready_steps(self) -> list[QueueEntry]:
        now = time.time()
        ready = [
            e for e in self._queue.values()
            if e.step_id not in self._step_to_lease
            and e.step_id not in self._dead_letters
            and now >= e.next_retry_after
        ]
        ready.sort()
        return ready

    async def mark_dead_letter(self, step_id: str, reason: str) -> None:
        entry = self._queue.pop(step_id, None)
        run_id = entry.run_id if entry else ""
        self._step_to_lease.pop(step_id, None)

        dl = DeadLetterEntry(
            step_id=step_id,
            run_id=run_id,
            reason=reason,
            moved_at=time.time(),
        )
        self._dead_letters[step_id] = dl

        await self._journal.write_event("scheduler.dead_letter", {
            "step_id": step_id,
            "run_id": run_id,
            "reason": reason,
        })

    def get_stats(self) -> SchedulerStats:
        return SchedulerStats(
            queued=len(self._queue),
            active_leases=len(self._active_leases),
            dead_letters=len(self._dead_letters),
            total_completed=self._total_completed,
            total_failed=self._total_failed,
        )
