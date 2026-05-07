"""TaskOrchestrator: manages task-level card sessions for multi-task execution.

Responsibilities:
- Parse plan info to identify tasks
- Create independent CardSession (wrapped in SessionRotator) per task
- Route ACP events to the correct task's session
- Broadcast task status changes to all active sessions
- Handle fallback to single-session mode when plan parsing fails
- Resolve which task_id an ACP event belongs to (TaskIdResolver)
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.card.events import CardEvent, CardEventType
from src.card.task_registry import TaskRegistry, TaskStatus

if TYPE_CHECKING:
    from src.acp.models import ACPEvent
    from src.card.protocols import Dispatchable, StreamBridge
    from src.card.session.core import CardSession
    from src.card.session.rotator import SessionRotator

logger = logging.getLogger(__name__)

# Debounce window for broadcast (100ms) to coalesce rapid status changes
_BROADCAST_DEBOUNCE_MS = 100

# Minimum number of tasks for multi-card split
_MIN_TASKS_FOR_MULTI_CARD = 2


class TaskIdResolver:
    """Resolves which task_id an ACP event belongs to.

    Strategy:
    1. Track active plan step indices based on PLAN_UPDATE status transitions
    2. Support multiple concurrently active tasks (subagent scenarios)
    3. When resolving, prefer the most recently activated task
    4. Fall back to the most recently active task_id if inference fails

    Thread-safe: mutable state protected by a lock.
    """

    def __init__(self, task_ids: list[str]) -> None:
        self._task_ids = list(task_ids)
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._active_index: int = 0  # primary active index
        self._last_active_id: str = task_ids[0] if task_ids else ""
        self._active_task_ids: set[str] = set()  # tracks all currently in_progress tasks
        self._last_activated_time: dict[str, float] = {}  # task_id → monotonic timestamp

    @property
    def current_task_id(self) -> str:
        """The currently active task_id (most recently activated)."""
        with self._lock:
            return self._last_active_id

    @property
    def active_task_ids(self) -> set[str]:
        """Set of all currently in_progress task_ids."""
        with self._lock:
            return set(self._active_task_ids)

    def advance_to(self, index: int) -> None:
        """Advance active task to the given index (from plan step updates)."""
        with self._lock:
            self._advance_to_unlocked(index)

    def _advance_to_unlocked(self, index: int) -> None:
        """Internal advance without lock — caller must hold self._lock."""
        if 0 <= index < len(self._task_ids):
            task_id = self._task_ids[index]
            self._active_index = index
            self._last_active_id = task_id
            self._active_task_ids.add(task_id)
            self._last_activated_time[task_id] = time.monotonic()

    def resolve(self, acp_event: ACPEvent | None = None) -> str:
        """Resolve the task_id for a given ACP event.

        Uses plan step status transitions to infer which task is currently active.
        If the event contains a PLAN_UPDATE, uses the first in_progress entry index.
        When multiple tasks are active, returns the most recently activated one.

        Falls back to last known active task_id.
        """
        with self._lock:
            if acp_event is not None:
                from src.acp.models import ACPEventType
                if acp_event.event_type == ACPEventType.PLAN_UPDATE and acp_event.plan:
                    # Find all in_progress entries and advance to the latest one
                    for idx, entry in enumerate(acp_event.plan.entries):
                        if entry.status == "in_progress":
                            self._advance_to_unlocked(idx)
                            # Don't break — track all active entries

            return self._last_active_id

    def mark_active(self, task_id: str) -> None:
        """Explicitly mark a task_id as active."""
        with self._lock:
            if task_id in self._task_ids:
                self._last_active_id = task_id
                self._active_index = self._task_ids.index(task_id)
                self._active_task_ids.add(task_id)
                self._last_activated_time[task_id] = time.monotonic()

    def mark_inactive(self, task_id: str) -> None:
        """Mark a task_id as no longer active (completed/failed).

        If the deactivated task was the last_active_id, falls back to
        the most recently activated remaining task, or the first task_id.
        """
        with self._lock:
            self._active_task_ids.discard(task_id)
            self._last_activated_time.pop(task_id, None)
            if self._last_active_id == task_id:
                # Fall back to most recently activated remaining task
                if self._active_task_ids:
                    best = max(
                        self._active_task_ids,
                        key=lambda tid: self._last_activated_time.get(tid, 0),
                    )
                    self._last_active_id = best
                    if best in self._task_ids:
                        self._active_index = self._task_ids.index(best)
                elif self._task_ids:
                    # No active tasks — keep last known index
                    pass


class TaskOrchestrator:
    """Orchestrates task-level card sessions for a single engine execution.

    Not a singleton — one instance per engine execution (e.g., per Deep/Loop/Spec run).
    """

    def __init__(
        self,
        chat_id: str,
        session_creator: Callable[[str], CardSession],
        registry: TaskRegistry | None = None,
        *,
        bridge_factory: Callable[[Dispatchable], StreamBridge] | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._registry = registry or TaskRegistry()
        self._session_creator = session_creator
        self._bridge_factory: Callable[[Dispatchable], StreamBridge] | None = bridge_factory

        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._sessions: dict[str, SessionRotator | CardSession] = {}
        self._bridges: dict[str, StreamBridge] = {}  # per-task stream bridges
        self._thinking_session: CardSession | None = None
        self._plan_received = False
        self._closed = False
        self._fallback_mode = False
        self._fallback_session: CardSession | None = None
        self._resolver: TaskIdResolver | None = None

        # Debounce state for broadcast
        self._last_broadcast_time: float = 0
        self._pending_broadcast: bool = False
        self._broadcast_timer: threading.Timer | None = None

        # Subscribe to registry changes for auto-broadcast
        self._registry.subscribe(self._on_registry_status_change)

    @property
    def registry(self) -> TaskRegistry:
        """Access the task registry."""
        return self._registry

    @property
    def is_fallback_mode(self) -> bool:
        """Whether orchestrator is in single-session fallback mode."""
        return self._fallback_mode

    @property
    def has_plan(self) -> bool:
        """Whether a plan has been received (task sessions created)."""
        return self._plan_received

    def reset(self) -> None:
        """Reset orchestrator state for a new iteration (e.g. Loop mode).

        If a plan was previously received (sessions created), closes all sessions.
        Otherwise, simply resets the plan-detection flag to allow fresh detection.

        This is the public API for iteration boundary reset — callers should NOT
        access internal attributes like _plan_received directly.
        """
        if self._plan_received and not self._fallback_mode:
            self.close()
        else:
            self._plan_received = False
            self._fallback_mode = False
            self._fallback_session = None

    @property
    def resolver(self) -> TaskIdResolver | None:
        """Access the task_id resolver (available after on_plan_received)."""
        return self._resolver

    @property
    def active_session_count(self) -> int:
        """Number of active task sessions."""
        with self._lock:
            return len(self._sessions)

    def get_bridge(self, task_id: str) -> StreamBridge | None:
        """Get the stream bridge for a specific task (if bridge_factory was set)."""
        with self._lock:
            return self._bridges.get(task_id)

    def set_thinking_session(self, session: CardSession) -> None:
        """Set the thinking-phase session for pre-plan event routing.

        This session receives all events until on_plan_received() is called,
        at which point it is archived (completed) and per-task sessions take over.
        """
        self._thinking_session = session

    def dispatch_to_thinking(self, event: CardEvent) -> None:
        """Route an event to the thinking-phase session.

        Used before on_plan_received() is called. After plan reception,
        callers should use dispatch_to_task() instead.
        """
        if self._closed:
            return
        if self._thinking_session is not None:
            self._thinking_session.dispatch(event)

    def on_plan_received(self, plan_tasks: list[dict]) -> None:
        """Handle plan reception — parse tasks and create sessions.

        Archives the thinking session (if set) and creates per-task sessions.
        Falls back to single-session mode if plan is empty/invalid — in that case,
        the thinking session becomes the fallback session.

        Args:
            plan_tasks: List of dicts with at least 'task_id' and 'name' keys.
                       Falls back to single-session mode if empty or invalid.
        """
        if self._closed:
            return

        if not plan_tasks or not isinstance(plan_tasks, list):
            logger.info("TaskOrchestrator: no valid tasks in plan, entering fallback mode")
            self._enter_fallback_mode()
            return

        # Validate task format
        valid_tasks = []
        for t in plan_tasks:
            if isinstance(t, dict) and t.get("task_id") and t.get("name"):
                valid_tasks.append(t)

        if not valid_tasks:
            logger.info("TaskOrchestrator: no parseable tasks, entering fallback mode")
            self._enter_fallback_mode()
            return

        self._plan_received = True

        # Append explanation text to thinking session before archiving
        task_names = [t["name"] for t in valid_tasks]
        self._notify_thinking_of_tasks(task_names)

        # Archive the thinking session (complete it)
        self._archive_thinking_session(task_names)

        # Register all tasks
        for t in valid_tasks:
            self._registry.register(
                task_id=t["task_id"],
                name=t["name"],
                status=t.get("status", "pending"),
            )

        # Create resolver for task_id inference
        task_ids = [t["task_id"] for t in valid_tasks]
        self._resolver = TaskIdResolver(task_ids)

        # Create a session for each task
        for t in valid_tasks:
            self._create_task_session(t["task_id"])

        logger.info(
            "TaskOrchestrator: created %d task sessions for chat_id=%s",
            len(valid_tasks), self._chat_id,
        )

    def dispatch_to_task(self, task_id: str, event: CardEvent) -> None:
        """Route an event to the specific task's session.

        If task_id is unknown, falls back to the most recently active in_progress session
        (or the fallback/thinking session) rather than silently dropping the event.
        In fallback mode, dispatches to the single fallback session.
        """
        if self._closed:
            return

        if self._fallback_mode:
            if self._fallback_session is not None:
                self._fallback_session.dispatch(event)
            return

        with self._lock:
            session = self._sessions.get(task_id)

        if session is None:
            # Fallback: route to most recently active in_progress task
            logger.warning(
                "TaskOrchestrator.dispatch_to_task: unknown task_id=%s, routing to active session",
                task_id,
            )
            fallback_session = self._find_active_session()
            if fallback_session is not None:
                fallback_session.dispatch(event)
            return

        session.dispatch(event)

    def _find_active_session(self) -> SessionRotator | CardSession | None:
        """Find the most recently active (in_progress) task session for fallback routing."""
        if self._resolver is not None:
            active_ids = self._resolver.active_task_ids
            with self._lock:
                for tid in active_ids:
                    if tid in self._sessions:
                        return self._sessions[tid]
        # Last resort: any session or thinking session
        with self._lock:
            if self._sessions:
                return next(iter(self._sessions.values()))
        return self._thinking_session

    def broadcast_status_change(self, task_id: str, new_status: TaskStatus) -> None:
        """Update a task's status and broadcast to all active sessions.

        Uses debounce to coalesce rapid consecutive status changes.
        """
        if self._closed:
            return

        self._registry.update_status(task_id, new_status)
        # The actual broadcast is triggered via the subscribe callback

    def handle_plan_update(self, acp_event: ACPEvent, fallback_bridge: StreamBridge) -> None:
        """Unified plan detection + status broadcast entry point for renderers.

        Encapsulates the full plan-detection logic that was previously duplicated
        in Deep/Loop/Spec renderers:
        1. Check if PLAN_UPDATE event with sufficient entries
        2. Convert to task dicts and call on_plan_received() if threshold met
        3. Broadcast status changes for all entries

        Renderers only need to call this single method on every PLAN_UPDATE event.

        Args:
            acp_event: The ACP event (should be PLAN_UPDATE type).
            fallback_bridge: The bridge for fallback routing (unused here, kept for interface consistency).
        """
        if self._closed:
            return

        from src.acp.models import ACPEventType
        if acp_event.event_type != ACPEventType.PLAN_UPDATE:
            return

        if not acp_event.plan or not acp_event.plan.entries:
            return

        entries = acp_event.plan.entries

        # First PLAN_UPDATE with enough steps: create per-task sessions
        if not self._plan_received and not self._fallback_mode:
            from src.card.task_registry import tasks_from_plan_entries
            if len(entries) >= _MIN_TASKS_FOR_MULTI_CARD:
                task_dicts = tasks_from_plan_entries(entries)
                if len(task_dicts) >= _MIN_TASKS_FOR_MULTI_CARD:
                    self.on_plan_received(task_dicts)

        # Broadcast task status changes from plan entries
        if self._plan_received and not self._fallback_mode:
            for idx, entry in enumerate(entries):
                entry_task_id = f"step_{idx}"
                if entry.status == "in_progress":
                    self.broadcast_status_change(entry_task_id, "in_progress")
                elif entry.status == "completed":
                    self.broadcast_status_change(entry_task_id, "completed")

    def route_acp_event(self, acp_event: ACPEvent, fallback_bridge: StreamBridge) -> None:
        """Unified ACP event routing — resolve task_id and dispatch to the correct bridge.

        This is the single entry point for renderers to route ACP events in multi-card mode.
        Internally: resolve task_id → find per-task bridge → bridge.on_event(acp_event).

        If orchestrator has no plan yet or is in fallback mode, the event goes to fallback_bridge.
        If per-task bridges are not configured (no bridge_factory), dispatches the converted
        CardEvent directly to the task session.

        Args:
            acp_event: The raw ACP event to route.
            fallback_bridge: The bridge to use when routing cannot be resolved
                            (pre-plan phase or fallback mode).
        """
        if self._closed:
            return

        # Before plan reception or in fallback mode → use fallback bridge
        if not self._plan_received or self._fallback_mode:
            fallback_bridge.on_event(acp_event)
            return

        # Resolve which task this event belongs to
        if self._resolver is None:
            fallback_bridge.on_event(acp_event)
            return

        task_id = self._resolver.resolve(acp_event)
        if not task_id:
            fallback_bridge.on_event(acp_event)
            return

        # Route to per-task bridge if available
        with self._lock:
            bridge = self._bridges.get(task_id)

        if bridge is not None:
            bridge.on_event(acp_event)
        else:
            # No per-task bridge — dispatch converted CardEvent directly to session
            from src.card.events import card_event_from_acp
            card_evt = card_event_from_acp(acp_event)
            self.dispatch_to_task(task_id, card_evt)

    def _on_registry_status_change(self, task_id: str, new_status: TaskStatus) -> None:
        """Callback from TaskRegistry when status changes — triggers broadcast."""
        self._schedule_broadcast()

    def _schedule_broadcast(self) -> None:
        """Schedule a debounced broadcast of TASK_LIST_UPDATED to all sessions."""
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            elapsed_ms = (now - self._last_broadcast_time) * 1000

            if elapsed_ms >= _BROADCAST_DEBOUNCE_MS:
                # Enough time has passed, broadcast immediately (release lock first)
                pass
            else:
                # Schedule delayed broadcast
                if self._broadcast_timer is not None:
                    self._broadcast_timer.cancel()
                remaining = (_BROADCAST_DEBOUNCE_MS - elapsed_ms) / 1000
                self._broadcast_timer = threading.Timer(remaining, self._do_broadcast)
                self._broadcast_timer.daemon = True
                self._broadcast_timer.start()
                return

        # Immediate broadcast (outside lock to avoid deadlock with session.dispatch)
        self._do_broadcast()

    def _do_broadcast(self) -> None:
        """Actually broadcast TASK_LIST_UPDATED to all active sessions."""
        with self._lock:
            if self._closed:
                return
            self._last_broadcast_time = time.monotonic()
            sessions = list(self._sessions.items())

        snapshot = self._registry.get_snapshot()
        tasks_payload = [
            {"task_id": s.task_id, "name": s.name, "status": s.status}
            for s in snapshot
        ]

        for task_id, session in sessions:
            event: CardEvent = CardEvent(
                type=CardEventType.TASK_LIST_UPDATED,
                payload={"tasks": tasks_payload, "current_task_id": task_id},
            )
            try:
                session.dispatch(event)
            except Exception:
                logger.debug("Broadcast to task_id=%s failed", task_id, exc_info=True)

    def _create_task_session(self, task_id: str) -> None:
        """Create a CardSession for a task and bind it."""
        session = self._session_creator(task_id)

        with self._lock:
            self._sessions[task_id] = session
            # Create per-task bridge if bridge_factory is configured
            if self._bridge_factory is not None:
                self._bridges[task_id] = self._bridge_factory(session)

        # Dispatch initial TASK_LIST_UPDATED so the card renders the task list header
        snapshot = self._registry.get_snapshot()
        tasks_payload = [
            {"task_id": s.task_id, "name": s.name, "status": s.status}
            for s in snapshot
        ]
        event: CardEvent = CardEvent(
            type=CardEventType.TASK_LIST_UPDATED,
            payload={"tasks": tasks_payload, "current_task_id": task_id},
        )
        session.dispatch(event)

    def rotate_task_session(self, task_id: str) -> bool:
        """Trigger continuation card for a task (when content exceeds byte_budget).

        Freezes the current task session (dispatches ARCHIVED), creates a new continuation
        session, and re-dispatches TASK_LIST_UPDATED to the new session.

        Returns True if rotation succeeded, False if task_id not found or already closed.
        """
        if self._closed:
            return False

        with self._lock:
            session = self._sessions.get(task_id)
            old_bridge = self._bridges.get(task_id)
        if session is None:
            return False

        # Freeze old session: close open blocks on bridge, then archive
        if old_bridge is not None:
            try:
                old_bridge.close_open_blocks()
            except Exception:
                logger.debug("Error closing bridge during rotation for task %s", task_id)

        # Build continuation message with task name and sequence number
        task_item = self._registry.get(task_id)
        task_name = task_item.name if task_item else task_id
        # Count how many times this task has been rotated (simple counter via session re-creation)
        rotation_count = getattr(self, f"_rotation_count_{task_id}", 0) + 1
        setattr(self, f"_rotation_count_{task_id}", rotation_count)
        msg = f"\n\n---\n📄 任务「{task_name}」内容续 (续 {rotation_count}) →"
        try:
            session.dispatch(CardEvent.text_started("_continuation"))
            session.dispatch(CardEvent.text_delta("_continuation", msg))
            session.dispatch(CardEvent.text_done("_continuation"))
            session.dispatch(CardEvent.archived())
        except Exception:
            logger.debug("Error archiving task session for rotation, task_id=%s", task_id, exc_info=True)

        # Create new continuation session
        new_session = self._session_creator(task_id)
        with self._lock:
            self._sessions[task_id] = new_session
            if self._bridge_factory is not None:
                self._bridges[task_id] = self._bridge_factory(new_session)

        # Dispatch TASK_LIST_UPDATED to initialize the new card's header
        snapshot = self._registry.get_snapshot()
        tasks_payload = [
            {"task_id": s.task_id, "name": s.name, "status": s.status}
            for s in snapshot
        ]
        event: CardEvent = CardEvent(
            type=CardEventType.TASK_LIST_UPDATED,
            payload={"tasks": tasks_payload, "current_task_id": task_id},
        )
        new_session.dispatch(event)

        # Add continuation hint in new card
        hint_msg = "⬆ 承接上方卡片内容"
        try:
            new_session.dispatch(CardEvent.text_started("_continuation_hint"))
            new_session.dispatch(CardEvent.text_delta("_continuation_hint", hint_msg))
            new_session.dispatch(CardEvent.text_done("_continuation_hint"))
        except Exception:
            logger.debug("Error dispatching continuation hint for task_id=%s", task_id, exc_info=True)

        logger.info("TaskOrchestrator: rotated task session for task_id=%s (续 %d)", task_id, rotation_count)
        return True

    def _enter_fallback_mode(self) -> None:
        """Enter single-session fallback mode (no multi-card).

        If a thinking session exists, it becomes the fallback session.
        Dispatches a visible warning to inform the user.
        """
        self._fallback_mode = True
        self._plan_received = True  # Prevent further plan processing
        if self._thinking_session is not None and self._fallback_session is None:
            self._fallback_session = self._thinking_session
        # Dispatch visible warning to fallback session
        if self._fallback_session is not None:
            try:
                warn_id = "_fallback_warn"
                self._fallback_session.dispatch(CardEvent.text_started(warn_id))
                self._fallback_session.dispatch(
                    CardEvent.text_delta(warn_id, "⚠️ 任务拆分失败，已切换为单卡模式")
                )
                self._fallback_session.dispatch(CardEvent.text_done(warn_id))
            except Exception:
                logger.debug("Error dispatching fallback warning", exc_info=True)
        logger.info("TaskOrchestrator: fallback mode — using single session")

    def _archive_thinking_session(self, task_names: list[str] | None = None) -> None:
        """Archive the thinking session with task summary.

        Uses archived() (not completed()) to distinguish from actual task completion.
        Includes a summary of identified sub-task names.
        """
        if self._thinking_session is None:
            return
        try:
            # Build task name summary
            if task_names:
                task_list = " ".join(
                    f"{i+1}. {name}" for i, name in enumerate(task_names)
                )
                summary = f"✅ 分析规划完成，任务执行中 ↓\n{task_list}"
                block_id = "_archive_summary"
                self._thinking_session.dispatch(CardEvent.text_started(block_id))
                self._thinking_session.dispatch(CardEvent.text_delta(block_id, summary))
                self._thinking_session.dispatch(CardEvent.text_done(block_id))
            self._thinking_session.dispatch(CardEvent.archived())
        except Exception:
            logger.debug("Error archiving thinking session", exc_info=True)
        self._thinking_session = None

    def _notify_thinking_of_tasks(self, task_names: list[str]) -> None:
        """Append a summary text to the thinking session before archiving.

        Includes task names so users see the full plan at a glance.
        """
        if self._thinking_session is None:
            return
        block_id = "_plan_summary"
        task_count = len(task_names)
        if task_names:
            names_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(task_names))
            msg = f"\n\n---\n📋 已识别 {task_count} 个子任务，分别展示如下\n{names_list}"
        else:
            msg = f"\n\n---\n📋 已识别 {task_count} 个子任务，分别展示如下"
        try:
            self._thinking_session.dispatch(CardEvent.text_started(block_id))
            self._thinking_session.dispatch(CardEvent.text_delta(block_id, msg))
            self._thinking_session.dispatch(CardEvent.text_done(block_id))
        except Exception:
            logger.debug("Error notifying thinking session of tasks", exc_info=True)

    def set_fallback_session(self, session: CardSession) -> None:
        """Set the fallback session for single-session mode."""
        self._fallback_session = session

    def create_subagent_session(self, task_id: str, name: str) -> None:
        """Create a new session for a detected subagent task.

        Called when TOOL_STARTED with agent/subagent tool name is detected.
        """
        if self._closed or self._fallback_mode:
            return

        # Register the new subtask
        self._registry.register(task_id=task_id, name=name, status="in_progress")
        self._create_task_session(task_id)

    def close(self) -> None:
        """Close all sessions and clean up.

        Includes timeout protection: bridge.close_open_blocks() and session.dispatch()
        are each given a 5s timeout. On timeout, the operation is skipped to prevent
        blocking the caller indefinitely.
        """
        if self._closed:
            return
        self._closed = True

        # Cancel pending broadcast timer
        with self._lock:
            if self._broadcast_timer is not None:
                self._broadcast_timer.cancel()
                self._broadcast_timer = None

        # Unsubscribe from registry
        self._registry.unsubscribe(self._on_registry_status_change)

        # Close all task sessions and bridges
        with self._lock:
            sessions = list(self._sessions.values())
            bridges = list(self._bridges.values())
            self._sessions.clear()
            self._bridges.clear()

        # Close open blocks on all bridges (with timeout protection)
        for bridge in bridges:
            try:
                self._run_with_timeout(bridge.close_open_blocks, timeout=5.0)
            except Exception:
                logger.debug("Error closing bridge", exc_info=True)

        completed_event = CardEvent.completed()
        for session in sessions:
            try:
                self._run_with_timeout(
                    lambda s=session: s.dispatch(completed_event),  # type: ignore[misc]
                    timeout=5.0,
                )
            except Exception:
                logger.debug("Error closing task session", exc_info=True)

        self._fallback_session = None
        logger.info("TaskOrchestrator: closed for chat_id=%s", self._chat_id)

    @staticmethod
    def _run_with_timeout(fn: Callable[[], None], *, timeout: float) -> None:
        """Run a callable in a thread with timeout protection.

        If the callable doesn't complete within `timeout` seconds, raises TimeoutError.
        The thread is left as a daemon (will be cleaned up on process exit).
        """
        result: list[Exception | None] = [None]

        def _wrapper() -> None:
            try:
                fn()
            except Exception as e:
                result[0] = e

        t = threading.Thread(target=_wrapper, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            logger.warning("TaskOrchestrator: close operation timed out after %.1fs", timeout)
            raise TimeoutError(f"Operation timed out after {timeout}s")
        if result[0] is not None:
            raise result[0]
