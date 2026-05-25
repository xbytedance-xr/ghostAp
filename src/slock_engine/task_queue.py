"""Event-driven bounded task queue for the Slock engine.

This module implements a thread-safe bounded task queue that uses
threading.Condition for event-driven synchronization, replacing the
previous sleep-polling QUEUE_WAIT mechanism.

Design:
    - Producers call enqueue() to add tasks; they receive immediate feedback
      on queue position or a TaskQueueFullError if the queue is at capacity.
    - Consumers call wait_for_idle() to block efficiently until an agent
      becomes available, eliminating CPU-wasting spin loops.
    - When an agent finishes work, it calls notify_idle() to wake all
      waiting consumers so one can dispatch the next queued task.
    - All queue mutations are protected by the Condition's internal lock,
      ensuring thread safety without external synchronization.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .exceptions import TaskQueueFullError

logger = logging.getLogger(__name__)

# Backward compatibility alias (deprecated)
QueueFullError = TaskQueueFullError


@dataclass
class QueuedTask:
    """Represents a task waiting in the queue for execution."""

    task_id: str
    text: str
    chat_id: str
    message_id: str
    card_message_id: Optional[str] = None
    enqueue_time: float = field(default_factory=time.time)
    callbacks: Optional[object] = None
    engine: Optional[object] = None
    project: Optional[object] = None
    handler: Optional[object] = None
    bootstrap_pending: bool = False  # True if enqueued before bootstrap completed
    retry_count: int = 0  # Number of times this task has been re-enqueued
    next_retry_at: float = 0.0  # Timestamp before which dispatch should skip this task
    status: str = "pending"  # pending | dispatched | unschedulable
    # Delivery metadata for complete result delivery chain
    origin_message_id: Optional[str] = None  # Original user message ID for reply threading
    final_result_callback: Optional[object] = None  # Callback to deliver final result
    collaboration_plan: Optional[dict] = None  # Multi-role collaboration plan if applicable


class TaskQueue:
    """Event-driven bounded task queue with condition-variable synchronization.

    This queue supports multiple producers (message handlers) and multiple
    consumers (agent dispatch loops). Coordination is achieved via a
    threading.Condition, which allows consumers to sleep efficiently until
    notified that an agent has become idle.

    Args:
        max_size: Maximum number of tasks the queue can hold. Defaults to 8.
    """

    def __init__(self, max_size: int = 8) -> None:
        self._max_size = max_size
        self._condition = threading.Condition()
        self._queue: deque[QueuedTask] = deque()
        logger.info("TaskQueue initialized with max_size=%d", max_size)

    def enqueue(self, task: QueuedTask) -> int:
        """Add a task to the queue.

        Args:
            task: The QueuedTask to enqueue.

        Returns:
            The 1-based position of the task in the queue.

        Raises:
            TaskQueueFullError: If the queue has reached its maximum capacity.
        """
        with self._condition:
            if len(self._queue) >= self._max_size:
                logger.warning(
                    "Queue full (max_size=%d), rejecting task_id=%s",
                    self._max_size,
                    task.task_id,
                )
                raise TaskQueueFullError(
                    f"Queue is at capacity ({self._max_size}). "
                    f"Cannot enqueue task_id={task.task_id}"
                )

            self._queue.append(task)
            position = len(self._queue)
            logger.info(
                "Enqueued task_id=%s at position %d (queue_size=%d)",
                task.task_id,
                position,
                len(self._queue),
            )
            # Notify consumers that a new task is available
            self._condition.notify_all()
            return position

    def dequeue(self) -> Optional[QueuedTask]:
        """Remove and return the oldest task from the queue.

        Returns:
            The oldest QueuedTask, or None if the queue is empty.
        """
        with self._condition:
            if not self._queue:
                return None

            task = self._queue.popleft()
            logger.info(
                "Dequeued task_id=%s (queue_size=%d)",
                task.task_id,
                len(self._queue),
            )
            return task

    def notify_idle(self) -> None:
        """Notify all waiting consumers that an agent has become idle.

        This should be called by an agent after it finishes processing a task,
        signaling that it is available to pick up new work.
        """
        with self._condition:
            logger.debug("notify_idle called, waking waiting consumers")
            self._condition.notify_all()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for an idle notification from an agent.

        Blocks the calling thread until either notify_idle() is called by
        another thread, or the timeout expires.

        Args:
            timeout: Maximum seconds to wait. Defaults to 5.0.

        Returns:
            True if notified before timeout, False if the wait timed out.
        """
        with self._condition:
            notified = self._condition.wait(timeout=timeout)
            if not notified:
                logger.debug("wait_for_idle timed out after %.2fs", timeout)
            return notified

    def cancel(self, task_id: str) -> bool:
        """Remove a task from the queue by its task_id.

        Args:
            task_id: The unique identifier of the task to remove.

        Returns:
            True if the task was found and removed, False otherwise.
        """
        with self._condition:
            for i, task in enumerate(self._queue):
                if task.task_id == task_id:
                    del self._queue[i]
                    logger.info(
                        "Cancelled task_id=%s from position %d (queue_size=%d)",
                        task_id,
                        i + 1,
                        len(self._queue),
                    )
                    return True

            logger.debug("cancel: task_id=%s not found in queue", task_id)
            return False

    def size(self) -> int:
        """Return the current number of tasks in the queue."""
        with self._condition:
            return len(self._queue)

    def get_position(self, task_id: str) -> Optional[int]:
        """Get the 1-based position of a task in the queue.

        Args:
            task_id: The unique identifier of the task to locate.

        Returns:
            The 1-based position if found, or None if the task is not in the queue.
        """
        with self._condition:
            for i, task in enumerate(self._queue):
                if task.task_id == task_id:
                    return i + 1
            return None

    def peek(self) -> Optional[QueuedTask]:
        """Return the oldest task without removing it from the queue.

        Returns:
            The oldest QueuedTask, or None if the queue is empty.
        """
        with self._condition:
            if not self._queue:
                return None
            return self._queue[0]

    def drain_to(self, callback) -> int:
        """Dequeue all tasks and pass each to callback.

        Args:
            callback: A callable accepting a single QueuedTask argument.
                      Called once per task in FIFO order.

        Returns:
            The number of tasks drained.
        """
        count = 0
        with self._condition:
            while self._queue:
                task = self._queue.popleft()
                count += 1
                try:
                    callback(task)
                except Exception:
                    logger.exception(
                        "drain_to callback failed for task_id=%s", task.task_id
                    )
        if count:
            logger.info("Drained %d tasks from queue", count)
        return count

    def snapshot(self) -> list[QueuedTask]:
        """Return a thread-safe snapshot of all pending tasks.

        Returns a shallow copy of the queue contents at the time of the call.
        The returned list is safe to iterate outside the lock.
        """
        with self._condition:
            return list(self._queue)

    def iter_pending(self):
        """Iterator over a thread-safe snapshot of pending tasks.

        Convenience wrapper around snapshot() for for-loop usage.
        """
        return iter(self.snapshot())
