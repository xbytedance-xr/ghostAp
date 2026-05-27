"""ObserverLearningQueue — async batch queue for observer skill learning.

Idle agents' observation records are enqueued in-memory and flushed to disk
by a background daemon thread at regular intervals, avoiding I/O blocking
on the worker threads that execute agent tasks.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MemoryBackend(Protocol):
    """Protocol for the memory operations needed by the queue."""

    def record_skill_feedback(
        self, agent_id: str, skill_tags: list[str], *, quality_score: float
    ) -> list:
        ...

    def update_agent_context(self, agent_id: str, entry: str) -> None:
        ...


class SkillProfileSetter(Protocol):
    """Protocol for setting skill profiles on the router."""

    def set_skill_profiles(self, agent_id: str, profiles: list) -> None:
        ...


@dataclass(frozen=True)
class ObservationRecord:
    """A single observation to be flushed."""

    observer_id: str
    actor_id: str
    message_snippet: str
    skill_tags: tuple[str, ...]
    timestamp: float


class ObserverLearningQueue:
    """Thread-safe async queue for observer learning with periodic flush.

    Records are accumulated in an in-memory deque and flushed to disk by a
    background daemon thread every `flush_interval` seconds.
    """

    def __init__(
        self,
        memory: MemoryBackend,
        router: SkillProfileSetter,
        flush_interval: float = 10.0,
        max_queue_size: int = 10000,
        flush_timeout: float = 30.0,
    ) -> None:
        self._memory = memory
        self._router = router
        self._flush_interval = flush_interval
        self._flush_timeout = flush_timeout
        self._queue: deque[ObservationRecord] = deque(maxlen=max_queue_size)
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._flush_loop,
            name="slock-observer-flush",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.flush)

    def enqueue(
        self,
        observer_id: str,
        actor_id: str,
        message: str,
        skill_tags: list[str],
    ) -> None:
        """Add an observation record to the queue (non-blocking)."""
        record = ObservationRecord(
            observer_id=observer_id,
            actor_id=actor_id,
            message_snippet=message[:100],
            skill_tags=tuple(skill_tags),
            timestamp=time.time(),
        )
        with self._lock:
            if self._queue.maxlen and len(self._queue) >= self._queue.maxlen:
                logger.warning(
                    "Observer queue full (maxlen=%d), oldest record will be discarded",
                    self._queue.maxlen,
                )
            self._queue.append(record)

    def flush(self) -> int:
        """Flush all pending records to disk. Returns count of flushed records.

        Respects self._flush_timeout: if the batch processing exceeds the timeout,
        the remaining records are placed back at the front of the queue.
        """
        with self._lock:
            batch = list(self._queue)
            self._queue.clear()

        if not batch:
            return 0

        flushed = 0
        deadline = time.monotonic() + self._flush_timeout
        for index, record in enumerate(batch):
            if time.monotonic() > deadline:
                remaining_records = batch[index:]
                logger.warning(
                    "Observer flush timeout (%.1fs): requeueing %d remaining records",
                    self._flush_timeout,
                    len(remaining_records),
                )
                self._requeue_front(remaining_records)
                break
            try:
                profiles = self._memory.record_skill_feedback(
                    record.observer_id,
                    list(record.skill_tags),
                    quality_score=60.0,
                )
                self._router.set_skill_profiles(record.observer_id, profiles)
                context_entry = (
                    f"[{time.strftime('%Y-%m-%d %H:%M', time.localtime(record.timestamp))}] "
                    f"Observed {record.actor_id} complete: {record.message_snippet}"
                )
                self._memory.update_agent_context(record.observer_id, context_entry)
                flushed += 1
            except Exception:
                logger.exception(
                    "Failed to flush observer record for %s", record.observer_id
                )
        return flushed

    def _requeue_front(self, records: list[ObservationRecord]) -> None:
        """Put unflushed records back before newer queued records."""
        if not records:
            return
        with self._lock:
            current = list(self._queue)
            combined = list(records) + current
            maxlen = self._queue.maxlen
            if maxlen is not None and len(combined) > maxlen:
                dropped = len(combined) - maxlen
                combined = combined[:maxlen]
                logger.warning(
                    "Observer queue full while requeueing timed-out records; dropped %d newest records",
                    dropped,
                )
            self._queue.clear()
            self._queue.extend(combined)

    def shutdown(self) -> None:
        """Stop the background thread and flush remaining records."""
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.flush()

    @property
    def pending_count(self) -> int:
        """Number of records waiting to be flushed."""
        with self._lock:
            return len(self._queue)

    def _flush_loop(self) -> None:
        """Background loop that periodically flushes the queue."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._flush_interval)
            if not self._stop_event.is_set():
                try:
                    self.flush()
                except Exception:
                    logger.exception("Observer flush loop error")
        # Final flush on exit
        try:
            self.flush()
        except Exception:
            logger.exception("Observer final flush error")


class TaskStatusObserver(Protocol):
    """Protocol for observing task status changes.

    Implementers receive notifications when tasks transition between states,
    enabling event-driven role participation (e.g., reviewer auto-starts when
    coder finishes).
    """

    def on_task_status_changed(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
        agent_id: str,
        channel_id: str,
    ) -> None:
        """Called when a task's status changes."""
        ...

    def on_plan_step_completed(
        self,
        plan_id: str,
        step_id: str,
        role: str,
        agent_id: str,
    ) -> None:
        """Called when a collaboration plan step completes."""
        ...

    def on_task_created(
        self,
        task_id: str,
        content: str,
        channel_id: str,
    ) -> None:
        """Called when a new task is created (enables auto-plan triggering)."""
        ...


class TaskStatusNotifier:
    """Manages TaskStatusObserver subscriptions and dispatches events.

    Thread-safe notification dispatcher that fans out task status change
    events to all registered observers. Used by the collaboration orchestrator
    to trigger next-role activation when a predecessor completes.
    """

    def __init__(self) -> None:
        self._observers: list[TaskStatusObserver] = []
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def subscribe(self, observer: TaskStatusObserver) -> None:
        """Register an observer for task status events."""
        with self._lock:
            if observer not in self._observers:
                self._observers.append(observer)

    def unsubscribe(self, observer: TaskStatusObserver) -> None:
        """Remove an observer."""
        with self._lock:
            self._observers = [o for o in self._observers if o is not observer]

    def notify_status_changed(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
        agent_id: str,
        channel_id: str,
    ) -> None:
        """Notify all observers of a task status change."""
        with self._lock:
            observers = list(self._observers)

        for obs in observers:
            try:
                obs.on_task_status_changed(task_id, old_status, new_status, agent_id, channel_id)
            except Exception:
                logger.exception(
                    "TaskStatusObserver error on status change: task=%s observer=%r",
                    task_id, obs,
                )

    def notify_plan_step_completed(
        self,
        plan_id: str,
        step_id: str,
        role: str,
        agent_id: str,
    ) -> None:
        """Notify all observers that a plan step completed."""
        with self._lock:
            observers = list(self._observers)

        for obs in observers:
            try:
                obs.on_plan_step_completed(plan_id, step_id, role, agent_id)
            except Exception:
                logger.exception(
                    "TaskStatusObserver error on plan step: plan=%s step=%s observer=%r",
                    plan_id, step_id, obs,
                )

    def notify_task_created(
        self,
        task_id: str,
        content: str,
        channel_id: str,
    ) -> None:
        """Notify all observers that a new task was created."""
        with self._lock:
            observers = list(self._observers)

        for obs in observers:
            try:
                obs.on_task_created(task_id, content, channel_id)
            except Exception:
                logger.exception(
                    "TaskStatusObserver error on task created: task=%s observer=%r",
                    task_id, obs,
                )

    @property
    def observer_count(self) -> int:
        """Number of registered observers."""
        with self._lock:
            return len(self._observers)
