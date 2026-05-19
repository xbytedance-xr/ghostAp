"""TaskBoardManager — Task lifecycle logic extracted from SlockEngine.

Manages task CRUD, claiming, completion, persistence, and trimming.
The manager does not own the lock — it receives a shared RLock from the engine.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from ..config import get_settings
from .models import SlockTask, TaskStatus

if TYPE_CHECKING:
    import threading

    from .memory_manager import MemoryManager
    from .task_router import TaskRouter

logger = logging.getLogger(__name__)


class TaskBoardManager:
    """Manages the task board for a SlockEngine instance.

    Lifecycle-bound to the engine. Receives shared state references via constructor.
    """

    _MAX_DONE_TASKS = 500

    def __init__(
        self,
        *,
        lock: threading.RLock,
        tasks: list[SlockTask],
        channel_getter: Callable[[], Optional[object]],
        chat_id_getter: Callable[[], str],
        dirty_getter: Callable[[], bool],
        dirty_setter: Callable[[bool], None],
        router: TaskRouter,
        memory: MemoryManager,
        registry_get: Callable[[str], Optional[object]],
        execute_agent_fn: Callable[..., Optional[str]],
    ) -> None:
        self._lock = lock
        self._tasks = tasks
        self._channel_getter = channel_getter
        self._chat_id_getter = chat_id_getter
        self._dirty_getter = dirty_getter
        self._dirty_setter = dirty_setter
        self._router = router
        self._memory = memory
        self._registry_get = registry_get
        self._execute_agent_fn = execute_agent_fn

    def add_task(self, content: str) -> Optional[SlockTask]:
        """Create a new task in the channel.

        Returns:
            SlockTask if successfully created.
            None if the number of open (non-DONE) tasks has reached the
            ``slock_max_open_tasks`` setting limit.

        Callers MUST handle the None return case and present appropriate
        user feedback (e.g. a "team busy" card).
        """
        settings = get_settings()
        with self._lock:
            open_count = sum(1 for t in self._tasks if t.status != TaskStatus.DONE)
            if open_count >= settings.slock_max_open_tasks:
                logger.warning(
                    "Slock open task limit reached (%d/%d), rejecting new task",
                    open_count, settings.slock_max_open_tasks,
                )
                return None
            channel = self._channel_getter()
            task = SlockTask(
                content=content,
                created_in=channel.channel_id if channel else self._chat_id_getter(),
            )
            self._tasks.append(task)
            self._dirty_setter(True)
            snapshot = list(self._tasks)
        self._flush_if_dirty(snapshot)
        return task

    def claim_task(self, task_id: str, agent_id: str) -> bool:
        """Attempt to claim a task for an agent."""
        if not self._router.task_claim.claim(task_id, agent_id):
            return False

        snapshot: list[SlockTask] = []
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id:
                    if task.status != TaskStatus.TODO:
                        self._router.task_claim.release(task_id, agent_id)
                        return False
                    task.status = TaskStatus.IN_PROGRESS
                    task.claimed_by = agent_id
                    task.claimed_at = time.time()
                    self._dirty_setter(True)
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)
            return True
        return False

    def complete_task(self, task_id: str, agent_id: str) -> bool:
        """Mark a task as done."""
        snapshot: list[SlockTask] = []
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id and task.claimed_by == agent_id:
                    task.status = TaskStatus.DONE
                    self._router.task_claim.release(task_id, agent_id)
                    self._dirty_setter(True)
                    self._trim_done_tasks()
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)
            return True
        return False

    def execute_task(
        self,
        task_id: str,
        agent_id: str,
        callbacks=None,
    ) -> Optional[str]:
        """Execute a task end-to-end: claim -> execute -> complete/rollback."""
        with self._lock:
            task = None
            for t in self._tasks:
                if t.task_id == task_id:
                    task = t
                    break
            if task is None:
                return None
            task_content = task.content
            already_claimed = (task.claimed_by == agent_id)

        agent = self._registry_get(agent_id)
        if agent is None:
            return None

        if not already_claimed:
            if not self.claim_task(task_id, agent_id):
                return None

        try:
            result = self._execute_agent_fn(agent, task_content, callbacks)
            if result:
                self.complete_task(task_id, agent_id)
                return result
            else:
                self._rollback_task(task_id, agent_id)
                return None
        except Exception as e:
            logger.error("execute_task failed for task %s agent %s: %s", task_id, agent_id, repr(e))
            self._rollback_task(task_id, agent_id)
            raise

    def _rollback_task(self, task_id: str, agent_id: str) -> None:
        """Rollback a task to TODO state and release its claim."""
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id:
                    task.status = TaskStatus.TODO
                    task.claimed_by = None
                    task.claimed_at = None
                    break
            self._router.task_claim.release(task_id, agent_id)
            self._dirty_setter(True)
            snapshot = list(self._tasks)
        self._flush_if_dirty(snapshot)

    def force_complete_task(self, task_id: str) -> None:
        """Force-mark a task as DONE regardless of claimer."""
        snapshot: list[SlockTask] = []
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id:
                    task.status = TaskStatus.DONE
                    if task.claimed_by:
                        self._router.task_claim.release(task_id, task.claimed_by)
                    self._dirty_setter(True)
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)

    def _persist_task_board(self) -> None:
        """Persist task state for the active channel."""
        channel = self._channel_getter()
        channel_id = channel.channel_id if channel else self._chat_id_getter()
        self._memory.write_task_board(channel_id, self._tasks)

    def _flush_if_dirty(self, snapshot: list[SlockTask]) -> None:
        """Persist task board from a snapshot if dirty flag is set."""
        if not self._dirty_getter():
            return
        try:
            channel = self._channel_getter()
            channel_id = channel.channel_id if channel else self._chat_id_getter()
            self._memory.write_task_board(channel_id, snapshot)
            self._dirty_setter(False)
        except OSError:
            logger.warning("Failed to persist task board (will retry on next mutation)", exc_info=True)

    def _trim_done_tasks(self, max_done: int = _MAX_DONE_TASKS) -> None:
        """Remove oldest DONE tasks when exceeding the cap. Must be called under self._lock."""
        done_tasks = [t for t in self._tasks if t.status == TaskStatus.DONE]
        if len(done_tasks) <= max_done:
            return
        done_tasks.sort(key=lambda t: t.claimed_at or 0)
        to_remove = set(id(t) for t in done_tasks[: len(done_tasks) - max_done])
        self._tasks[:] = [t for t in self._tasks if id(t) not in to_remove]
