"""TaskBoardManager — Task lifecycle logic extracted from SlockEngine.

Manages task CRUD, claiming, completion, persistence, and trimming.
The manager does not own the lock — it receives a shared RLock from the engine.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from ..config import get_settings
from .models import AgentIdentity, SlockTask, TaskStatus, TaskTimelineEvent
from .protocols import SlockEngineContext

if TYPE_CHECKING:
    import threading

    from .memory_manager import MemoryManager
    from .observer_queue import TaskStatusNotifier
    from .task_chain_manager import TaskChainManager
    from .task_router import TaskRouter

logger = logging.getLogger(__name__)


@dataclass
class _LegacyTaskBoardContext:
    """Adapter for older tests/callers that passed state getter callbacks."""

    channel_getter: Callable[[], object | None]
    chat_id_getter: Callable[[], str]
    dirty_getter: Callable[[], bool]
    dirty_setter: Callable[[bool], object]
    execute_agent_fn: Callable[..., object] | None = None

    @property
    def channel(self):
        return self.channel_getter()

    @property
    def chat_id(self) -> str:
        return self.chat_id_getter()

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_getter())

    def set_dirty(self, value: bool) -> None:
        self.dirty_setter(value)

    def execute_agent(self, agent: AgentIdentity, content: str, callbacks) -> Optional[str]:
        if self.execute_agent_fn is None:
            return None
        result = self.execute_agent_fn(agent, content, callbacks)
        return result if isinstance(result, str) or result is None else str(result)

    def resolve_agent_for_role(self, role: str, channel_id: str) -> Optional[AgentIdentity]:
        return None

    def execute_task(self, task_id: str, agent_id: str, callbacks) -> Optional[str]:
        return None


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
        context: SlockEngineContext | None = None,
        channel_getter: Callable[[], object | None] | None = None,
        chat_id_getter: Callable[[], str] | None = None,
        dirty_getter: Callable[[], bool] | None = None,
        dirty_setter: Callable[[bool], object] | None = None,
        execute_agent_fn: Callable[..., object] | None = None,
        router: TaskRouter,
        memory: MemoryManager,
        registry_get: Callable[[str], Optional[AgentIdentity]],
        chain_manager: Optional[TaskChainManager] = None,
        notifier: "Optional[TaskStatusNotifier]" = None,
        get_parallel_plan_tasks: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        self._lock = lock
        self._tasks = tasks
        if context is None:
            if channel_getter is None or chat_id_getter is None or dirty_getter is None or dirty_setter is None:
                raise TypeError(
                    "TaskBoardManager requires either context= or legacy "
                    "channel_getter/chat_id_getter/dirty_getter/dirty_setter callbacks"
                )
            context = _LegacyTaskBoardContext(
                channel_getter=channel_getter,
                chat_id_getter=chat_id_getter,
                dirty_getter=dirty_getter,
                dirty_setter=dirty_setter,
                execute_agent_fn=execute_agent_fn,
            )
        self._context = context
        self._router = router
        self._memory = memory
        self._registry_get = registry_get
        self._chain_manager = chain_manager
        self._notifier = notifier
        self._get_parallel_plan_tasks = get_parallel_plan_tasks

    def _notify_status_change(self, task_id: str, old_status: str, new_status: str, agent_id: str = "") -> None:
        """Notify observers of task status change (no-op if notifier not set)."""
        if self._notifier:
            try:
                ch = self._context.channel
                channel_id: str = getattr(ch, 'channel_id', '') or self._context.chat_id
                self._notifier.notify_status_changed(task_id, old_status, new_status, agent_id, channel_id)
            except Exception:
                pass  # Notification failure must not break task flow

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
            channel = self._context.channel
            task = SlockTask(
                content=content,
                created_in=channel.channel_id if channel else self._context.chat_id,
            )
            self._tasks.append(task)
            self._context.set_dirty(True)
            snapshot = list(self._tasks)
        self._flush_if_dirty(snapshot)
        # Notify observers OUTSIDE lock to avoid deadlock (notifier has its own lock)
        if self._notifier:
            try:
                ch = self._context.channel
                channel_id: str = getattr(ch, 'channel_id', '') or self._context.chat_id
                self._notifier.notify_task_created(task.task_id, content, channel_id)
            except Exception:
                logger.debug("notify_task_created failed for task %s", task.task_id[:8], exc_info=True)
        return task

    def claim_task(self, task_id: str, agent_id: str) -> bool:
        """Attempt to claim a task for an agent."""
        if not task_id or not isinstance(task_id, str):
            return False
        if not agent_id or not isinstance(agent_id, str):
            return False
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
                    # Track collaborator participation
                    if agent_id not in task.collaborators:
                        task.collaborators.append(agent_id)
                    task.timeline.append(TaskTimelineEvent(
                        event_type="claimed", agent_id=agent_id,
                        timestamp=time.time(), detail=f"Task claimed by {agent_id}",
                    ))
                    self._context.set_dirty(True)
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)
            self._notify_status_change(task_id, "todo", "in_progress", agent_id)
            return True
        return False

    def complete_task(self, task_id: str, agent_id: str) -> bool:
        """Mark a task as done."""
        snapshot: list[SlockTask] = []
        old_status_value: str = ""
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id and task.claimed_by == agent_id:
                    old_status_value = task.status.value
                    task.status = TaskStatus.DONE
                    self._router.task_claim.release(task_id, agent_id)
                    task.timeline.append(TaskTimelineEvent(
                        event_type="completed", agent_id=agent_id,
                        timestamp=time.time(), detail="Task completed",
                    ))
                    self._context.set_dirty(True)
                    self._trim_done_tasks()
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)
            self._notify_status_change(task_id, old_status_value, "done", agent_id)
            # Check for chain successor
            completed_task = None
            for t in snapshot:
                if t.task_id == task_id and t.status == TaskStatus.DONE:
                    completed_task = t
                    break
            if completed_task:
                self._maybe_spawn_chain_successor(completed_task, agent_id)
            return True
        return False

    def _maybe_spawn_chain_successor(self, task: SlockTask, agent_id: str) -> Optional[SlockTask]:
        """Check if a completed task should spawn a chain successor.

        Creates the successor task AND auto-executes it (no idle wait).
        Returns the new successor task if created, else None.
        """
        if self._chain_manager is None:
            return None

        # Get the completing agent's role
        agent = self._registry_get(agent_id)
        if agent is None:
            return None

        role = getattr(agent, 'role', '')
        if not role:
            return None

        successor_role = self._chain_manager.get_successor_role(role)
        if successor_role is None:
            return None

        # Create follow-up task with chain context
        chain_content = f"[chain:{role}->{successor_role}] {task.content}"
        successor_task = self.add_task(chain_content)
        if successor_task:
            logger.info(
                "Chain successor created: %s -> %s (task=%s -> %s)",
                role, successor_role, task.task_id, successor_task.task_id,
            )
            # Track in chain manager
            self._chain_manager.start_chain(task.task_id, role)
            self._chain_manager.advance_chain(task.task_id, role, successor_task.task_id)

            # Auto-execute: find agent for successor role and dispatch immediately
            channel = self._context.channel
            channel_id = channel.channel_id if channel else self._context.chat_id
            successor_agent = self._context.resolve_agent_for_role(successor_role, channel_id)
            if successor_agent:
                claimed = self.claim_task(successor_task.task_id, successor_agent.agent_id)
                if claimed:
                    logger.info(
                        "Chain successor auto-dispatched: %s claimed by %s",
                        successor_task.task_id, successor_agent.name,
                    )
        return successor_task

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
            result = self._context.execute_agent(agent, task_content, callbacks)
            if result:
                self._mark_task_in_review(task_id, agent_id)
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
        old_status_value = "in_progress"
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id:
                    old_status_value = task.status.value
                    task.status = TaskStatus.TODO
                    task.claimed_by = None
                    task.claimed_at = None
                    break
            self._router.task_claim.release(task_id, agent_id)
            self._context.set_dirty(True)
            snapshot = list(self._tasks)
        self._flush_if_dirty(snapshot)
        self._notify_status_change(task_id, old_status_value, "todo", agent_id)

    def recover_orphan_tasks(self) -> list[SlockTask]:
        """Recover orphan tasks (IN_PROGRESS/IN_REVIEW) by downgrading to TODO.

        Called during channel activation after crash/restart to ensure
        no tasks are stuck in intermediate states.

        Returns:
            List of tasks that were recovered (downgraded to TODO).
        """
        recovered: list[SlockTask] = []
        snapshot: list[SlockTask] = []
        with self._lock:
            for task in self._tasks:
                if task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
                    if task.claimed_by:
                        self._router.task_claim.release(task.task_id, task.claimed_by)
                    task.status = TaskStatus.TODO
                    task.claimed_by = None
                    task.claimed_at = None
                    recovered.append(task)
            if recovered:
                self._context.set_dirty(True)
                snapshot = list(self._tasks)
        if snapshot:
            self._flush_if_dirty(snapshot)
        if recovered:
            logger.info(
                "Recovered %d orphan tasks during channel activation: %s",
                len(recovered),
                [t.task_id for t in recovered],
            )
        return recovered

    def _mark_task_in_review(self, task_id: str, agent_id: str) -> bool:
        """Persist the intermediate review state before marking a task done."""
        snapshot: list[SlockTask] = []
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id and task.claimed_by == agent_id:
                    if task.status != TaskStatus.IN_PROGRESS:
                        return False
                    task.status = TaskStatus.IN_REVIEW
                    self._context.set_dirty(True)
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)
            self._notify_status_change(task_id, "in_progress", "in_review", agent_id)
            return True
        return False

    def force_complete_task(
        self,
        task_id: str,
        *,
        reason: Optional[str] = None,
        actor_id: str = "",
        admin_ids: set[str] | None = None,
        owner_id: str = "",
    ) -> None:
        """Force-mark a task as DONE regardless of claimer.

        Args:
            task_id: The task to force-complete.
            reason: Optional reason for abnormal completion (e.g. "超时中止").
                    Written to task.resolved_reason for differentiated display.
            actor_id: The user performing this action. If provided, permission
                      is checked: actor must be admin, channel owner, or task claimer.
            admin_ids: Set of admin user IDs for permission check.
            owner_id: Channel owner ID for permission check.

        Raises:
            PermissionError: If actor_id is provided but not authorized.
        """
        # Permission check
        if actor_id and actor_id.startswith("system:"):
            # Internal system caller whitelist — always allowed, audit logged
            logger.info(
                "System force_complete: actor=%s task=%s reason=%s",
                actor_id, task_id, reason,
            )
        elif actor_id:
            allowed = False
            if admin_ids and actor_id in admin_ids:
                allowed = True
            elif owner_id and actor_id == owner_id:
                allowed = True
            else:
                # Check if actor is the task claimer
                with self._lock:
                    for task in self._tasks:
                        if task.task_id == task_id and task.claimed_by == actor_id:
                            allowed = True
                            break
            if not allowed:
                logger.warning(
                    "Permission denied: actor %s attempted force_complete on task %s",
                    actor_id, task_id,
                )
                raise PermissionError(
                    f"User {actor_id} is not authorized to force-complete task {task_id}"
                )
        else:
            # Empty actor_id without system: prefix — reject
            logger.warning(
                "force_complete_task called without actor_id for task %s — rejected",
                task_id,
            )
            raise PermissionError(
                f"force_complete_task requires actor_id for task {task_id}"
            )

        snapshot: list[SlockTask] = []
        old_status_value: str = ""
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id:
                    old_status_value = task.status.value
                    task.status = TaskStatus.DONE
                    task.resolved_reason = reason
                    if task.claimed_by:
                        self._router.task_claim.release(task_id, task.claimed_by)
                    task.timeline.append(TaskTimelineEvent(
                        event_type="force_completed", agent_id=actor_id,
                        timestamp=time.time(), detail=reason or "Force completed",
                    ))
                    self._context.set_dirty(True)
                    snapshot = list(self._tasks)
                    break
        if snapshot:
            self._flush_if_dirty(snapshot)
            self._notify_status_change(task_id, old_status_value, "done", "")

    def _persist_task_board(self) -> None:
        """Persist task state for the active channel."""
        channel = self._context.channel
        channel_id = channel.channel_id if channel else self._context.chat_id
        self._memory.write_task_board(channel_id, self._tasks)

    def _flush_if_dirty(self, snapshot: list[SlockTask]) -> None:
        """Persist the latest task board if dirty flag is set."""
        try:
            with self._lock:
                if not self._context.dirty:
                    return
                channel = self._context.channel
                channel_id = channel.channel_id if channel else self._context.chat_id
                latest_snapshot = list(self._tasks)
                self._memory.write_task_board(channel_id, latest_snapshot)
                self._context.set_dirty(False)
        except OSError:
            logger.warning("Failed to persist task board (will retry on next mutation)", exc_info=True)

    def _trim_done_tasks(self, max_done: int = _MAX_DONE_TASKS) -> None:
        """Remove oldest DONE tasks when exceeding the cap. Must be called under self._lock."""
        done_tasks = [t for t in self._tasks if t.status == TaskStatus.DONE]
        if len(done_tasks) <= max_done:
            return
        # Sort by created_at (oldest first) — more reliable than claimed_at which may be None
        done_tasks.sort(key=lambda t: t.created_at)
        to_remove = set(id(t) for t in done_tasks[: len(done_tasks) - max_done])
        self._tasks[:] = [t for t in self._tasks if id(t) not in to_remove]

    # ------------------------------------------------------------------
    # Idle Scan: auto-claim TODO tasks for idle agents
    # ------------------------------------------------------------------

    def start_idle_scan(self) -> None:
        """Start background idle scan loop that auto-claims TODO tasks for idle agents."""
        import threading as _threading

        self._idle_scan_stop = _threading.Event()
        self._idle_scan_thread = _threading.Thread(
            target=self._idle_scan_loop, daemon=True, name="slock-idle-scan"
        )
        self._idle_scan_thread.start()

    def stop_idle_scan(self) -> None:
        """Stop the idle scan loop."""
        if hasattr(self, '_idle_scan_stop'):
            self._idle_scan_stop.set()
            if hasattr(self, '_idle_scan_thread'):
                self._idle_scan_thread.join(timeout=2)

    def _idle_scan_loop(self) -> None:
        """Periodically scan TODO tasks and auto-claim for idle agents."""
        settings = get_settings()
        interval = settings.slock_idle_scan_interval

        while not self._idle_scan_stop.is_set():
            self._idle_scan_stop.wait(interval)
            if self._idle_scan_stop.is_set():
                break
            try:
                self._idle_scan_once()
            except Exception:
                logger.exception("Idle scan iteration failed")

    def _idle_scan_once(self) -> None:
        """Single iteration: match TODO tasks to idle agents by skill.

        Only assigns to agents in IDLE state. Skips agents that are currently
        busy (non-IDLE status) to avoid overwhelming them.
        Uses execute_task for full lifecycle (execute → complete/rollback).
        """
        # Prevent nested triggering (execute_task → notifier → orchestrator → idle scan)
        if getattr(self, '_is_scanning', False):
            return
        self._is_scanning = True
        try:
            self._idle_scan_once_inner()
        finally:
            self._is_scanning = False

    def _idle_scan_once_inner(self) -> None:
        """Inner scan logic (separated to allow _is_scanning guard)."""
        with self._lock:
            todo_tasks = [t for t in self._tasks if t.status == TaskStatus.TODO]

        if not todo_tasks:
            return

        channel = self._context.channel
        channel_id = channel.channel_id if channel else self._context.chat_id

        for task in todo_tasks:
            # Try to find a suitable idle agent based on task content keywords
            if self._chain_manager:
                template = self._chain_manager.find_chain_for_task(task.content)
                if template and template.roles:
                    target_role = template.first_role
                else:
                    target_role = "coder"  # Default fallback
            else:
                target_role = "coder"

            agent = self._context.resolve_agent_for_role(target_role, channel_id)
            if not agent or not hasattr(agent, 'agent_id'):
                continue

            # Pre-dispatch check: verify agent is IDLE before claiming
            agent_status = None
            if self._registry_get:
                # Check agent status via registry — only IDLE agents can accept
                agent_obj = self._registry_get(agent.agent_id)
                if agent_obj and hasattr(agent_obj, 'status'):
                    agent_status = agent_obj.status
            # If we can determine status and agent is not IDLE, skip
            if agent_status is not None and str(agent_status) != "idle":
                logger.debug(
                    "Idle scan: agent %s is %s, skipping for task %s",
                    getattr(agent, 'name', agent.agent_id), agent_status, task.task_id,
                )
                continue

            # Use execute_task for full lifecycle: claim → execute → complete/rollback
            logger.info(
                "Idle scan: dispatching task %s to %s (role=%s)",
                task.task_id, getattr(agent, 'name', agent.agent_id), target_role,
            )
            try:
                self.execute_task(task.task_id, agent.agent_id)
            except Exception:
                logger.exception(
                    "Idle scan: execute_task failed for task %s",
                    task.task_id,
                )
            # Only handle one task per scan iteration to avoid overwhelming
            break
        else:
            # No orphan TODO task was dispatched — check parallel plan tasks
            self._try_claim_parallel_plan_tasks()

    def _try_claim_parallel_plan_tasks(self) -> None:
        """Check collaboration plans for parallel TODO steps and execute them.

        Delegates to get_parallel_plan_tasks callback which returns
        (task_id, agent_id) pairs for ready-to-execute plan tasks.
        """
        if not self._get_parallel_plan_tasks:
            return
        try:
            parallel_tasks = self._get_parallel_plan_tasks()
        except Exception:
            return
        for task_id, agent_id in parallel_tasks[:1]:  # One per iteration
            logger.info("Idle scan: executing parallel plan task %s for agent %s", task_id, agent_id)
            try:
                self.execute_task(task_id, agent_id)
            except Exception:
                logger.exception("Idle scan: parallel plan task %s failed", task_id)
            break
