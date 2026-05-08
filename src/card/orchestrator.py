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

import concurrent.futures
import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.card.events import CardEvent, CardEventType
from src.card.hooks import BackfillHook
from src.card.nav_link import format_task_continuation_link
from src.card.task_registry import TaskRegistry, TaskStatus
from src.card.ui_text import UI_TEXT

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
        max_task_cards: int = 8,
    ) -> None:
        self._chat_id = chat_id
        self._registry = registry or TaskRegistry()
        self._session_creator = session_creator
        self._bridge_factory: Callable[[Dispatchable], StreamBridge] | None = bridge_factory
        self._max_task_cards = max_task_cards

        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._sessions: dict[str, SessionRotator | CardSession] = {}
        self._bridges: dict[str, StreamBridge] = {}  # per-task stream bridges
        self._thinking_session: CardSession | None = None
        self._plan_received = threading.Event()
        self._closed_event = threading.Event()
        self._fallback_mode = False
        self._fallback_session: CardSession | None = None
        self._resolver: TaskIdResolver | None = None

        # Debounce state for broadcast
        self._last_broadcast_time: float = 0
        self._pending_broadcast: bool = False
        self._broadcast_timer: threading.Timer | None = None

        # Flood-prevention: overflow task_ids map to the last session's task_id
        self._overflow_target: dict[str, str] = {}  # overflow_task_id → target_task_id
        self._overflow_separator_sent: set[str] = set()  # tracks first dispatch per overflow task

        # Task-level rotation counters (protected by self._lock)
        self._rotation_counts: dict[str, int] = {}

        # Thread pool for timeout-protected close operations
        self._close_executor: concurrent.futures.ThreadPoolExecutor | None = None

        # Subscribe to registry changes for auto-broadcast
        self._registry.subscribe(self._on_registry_status_change)

    @classmethod
    def from_settings(
        cls,
        chat_id: str,
        session_creator: Callable[[str], CardSession],
        thinking_session: SessionRotator | CardSession,
        *,
        bridge_class: type[StreamBridge] | None = None,
    ) -> TaskOrchestrator:
        """Factory: create an orchestrator from project settings.

        Reads `card.task_level_cards_enabled` and `card.max_task_cards` from settings,
        constructs the bridge_factory conditionally, and wires up the thinking session.

        Args:
            chat_id: The chat ID for this execution.
            session_creator: Callable that creates a CardSession given a task_id.
            thinking_session: The pre-plan session (or rotator) to use.
            bridge_class: Optional bridge class to use as factory. If None and
                         task_level_cards_enabled, no per-task bridges are created.
        """
        from src.config import get_settings

        settings = get_settings()
        multi_card_enabled = settings.card.task_level_cards_enabled

        bridge_factory: Callable[[Dispatchable], StreamBridge] | None = None
        if multi_card_enabled and bridge_class is not None:
            bridge_factory = bridge_class

        orchestrator = cls(
            chat_id=chat_id,
            session_creator=session_creator,
            bridge_factory=bridge_factory,
            max_task_cards=settings.card.max_task_cards,
        )
        orchestrator.set_thinking_session(thinking_session)
        return orchestrator

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
        return self._plan_received.is_set()

    def reset(self) -> None:
        """Reset orchestrator state for a new cycle/iteration.

        Archives all active task sessions (sends ARCHIVED), then resets internal
        state to allow fresh plan detection. Used by Spec mode at cycle boundaries.
        """
        with self._lock:
            had_plan = self._plan_received.is_set() and not self._fallback_mode
            sessions_to_close = list(self._sessions.values()) if had_plan else []
            self._sessions.clear()
            self._bridges.clear()
            self._overflow_target.clear()
            self._overflow_separator_sent.clear()
            self._rotation_counts.clear()
            # Reset shared flags under lock to prevent TOCTOU with dispatch_to_task
            self._plan_received.clear()
            self._fallback_mode = False
            self._fallback_session = None
            self._resolver = None

        # Archive sessions OUTSIDE lock (I/O)
        for session in sessions_to_close:
            try:
                session.dispatch(CardEvent.archived("orchestrator_reset"))
            except Exception:
                logger.debug("TaskOrchestrator.reset: error archiving session")

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
        if self._closed_event.is_set():
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
        if self._closed_event.is_set():
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

        self._plan_received.set()

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

        # Create sessions with flood-prevention cap
        max_cards = self._max_task_cards
        overflow_tasks: list[dict] = []
        for i, t in enumerate(valid_tasks):
            if i < max_cards:
                try:
                    self._create_task_session(t["task_id"])
                except Exception:
                    logger.warning(
                        "TaskOrchestrator: _create_task_session failed for task_id=%s, entering fallback mode",
                        t["task_id"], exc_info=True,
                    )
                    self._enter_fallback_mode()
                    return
            else:
                # Overflow: route to the last created session
                last_task_id = valid_tasks[max_cards - 1]["task_id"]
                with self._lock:
                    self._overflow_target[t["task_id"]] = last_task_id
                overflow_tasks.append(t)

        # Finalize thinking session with overflow info
        task_names = [t["name"] for t in valid_tasks]
        self._finalize_thinking_session(task_names, overflow_count=len(overflow_tasks))

        # Notify overflow tasks on last session
        if overflow_tasks:
            last_task_id = valid_tasks[max_cards - 1]["task_id"]
            with self._lock:
                last_session = self._sessions.get(last_task_id)
            if last_session is not None:
                for ot in overflow_tasks:
                    msg = UI_TEXT["orch_flood_merged"].format(task_name=ot["name"])
                    try:
                        block_id = f"_flood_{ot['task_id']}"
                        last_session.dispatch(CardEvent.text_started(block_id))
                        last_session.dispatch(CardEvent.text_delta(block_id, msg))
                        last_session.dispatch(CardEvent.text_done(block_id))
                    except Exception:
                        logger.debug("Error dispatching flood notice for %s", ot["task_id"])

        created_count = min(len(valid_tasks), max_cards)
        logger.info(
            "TaskOrchestrator: created %d task sessions for chat_id=%s (total tasks: %d, max_cards: %d)",
            created_count, self._chat_id, len(valid_tasks), max_cards,
        )

    def dispatch_to_task(self, task_id: str, event: CardEvent) -> None:
        """Route an event to the specific task's session.

        If task_id is unknown, falls back to the most recently active in_progress session
        (or the fallback/thinking session) rather than silently dropping the event.
        In fallback mode, dispatches to the single fallback session.
        When dispatching to an overflow target for the first time, inserts a visual separator.
        """
        if self._closed_event.is_set():
            return

        if self._fallback_mode:
            if self._fallback_session is not None:
                self._fallback_session.dispatch(event)
            return

        # Resolve overflow mapping (flood-prevention)
        resolved_id = self._overflow_target.get(task_id, task_id)
        is_overflow = task_id in self._overflow_target

        with self._lock:
            session = self._sessions.get(resolved_id)
            # Atomically check-then-add overflow separator flag under lock
            should_insert_separator = (
                is_overflow
                and task_id not in self._overflow_separator_sent
                and session is not None
            )
            is_first_overflow = should_insert_separator and len(self._overflow_separator_sent) == 0
            overflow_display_index = len(self._overflow_separator_sent)  # 0-based count before add
            if should_insert_separator:
                self._overflow_separator_sent.add(task_id)

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

        # Insert overflow separator on first dispatch for this overflow task
        # Fold: only display full separator for the first 2 overflow tasks;
        # starting from the 3rd, dispatch a single collapsed count notice instead.
        if should_insert_separator:
            _MAX_VISIBLE_OVERFLOW = 2
            if overflow_display_index < _MAX_VISIBLE_OVERFLOW:
                task_item = self._registry.get(task_id)
                sep_task_name = task_item.name if task_item else task_id
                # Resolve status emoji for the overflow task
                status_key = f"orch_task_status_{task_item.status}" if task_item else "orch_task_status_pending"
                status_emoji = UI_TEXT.get(status_key, "⏳")
                sep_block_id = f"_sep_{task_id}"
                try:
                    session.dispatch(CardEvent(
                        type=CardEventType.SECTION_SEPARATOR,
                        payload={
                            "task_name": sep_task_name,
                            "block_id": sep_block_id,
                            "is_first_overflow": is_first_overflow,
                            "status_emoji": status_emoji,
                        },
                    ))
                except Exception:
                    logger.debug("TaskOrchestrator: error dispatching overflow separator for %s", task_id)
            elif overflow_display_index == _MAX_VISIBLE_OVERFLOW:
                # First folded item: emit collapsed notice with remaining count
                total_overflow = len(self._overflow_target)
                remaining = total_overflow - _MAX_VISIBLE_OVERFLOW
                if remaining > 0:
                    collapsed_msg = UI_TEXT["orch_overflow_collapsed"].format(count=remaining)
                    collapsed_block_id = "_sep_collapsed"
                    try:
                        session.dispatch(CardEvent.text_started(collapsed_block_id))
                        session.dispatch(CardEvent.text_delta(collapsed_block_id, collapsed_msg))
                        session.dispatch(CardEvent.text_done(collapsed_block_id))
                    except Exception:
                        logger.debug("TaskOrchestrator: error dispatching collapsed notice")

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
        if self._closed_event.is_set():
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
        if self._closed_event.is_set():
            return

        from src.acp.models import ACPEventType
        if acp_event.event_type != ACPEventType.PLAN_UPDATE:
            return

        if not acp_event.plan or not acp_event.plan.entries:
            return

        entries = acp_event.plan.entries

        # First PLAN_UPDATE with enough steps: create per-task sessions
        if not self._plan_received.is_set() and not self._fallback_mode:
            from src.card.task_registry import tasks_from_plan_entries
            if len(entries) >= _MIN_TASKS_FOR_MULTI_CARD:
                task_dicts = tasks_from_plan_entries(entries)
                if len(task_dicts) >= _MIN_TASKS_FOR_MULTI_CARD:
                    self.on_plan_received(task_dicts)

        # Broadcast task status changes from plan entries
        if self._plan_received.is_set() and not self._fallback_mode:
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
        if self._closed_event.is_set():
            return

        # Before plan reception or in fallback mode → use fallback bridge
        if not self._plan_received.is_set() or self._fallback_mode:
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

    def route_or_fallback(self, acp_event: ACPEvent, fallback_bridge: StreamBridge) -> bool:
        """Unified routing predicate + dispatch for renderers.

        Encapsulates the repeated condition:
            if has_plan and not is_fallback_mode: route_acp_event(...)
            else: fallback_bridge.on_event(...)

        Returns:
            True if event was routed through the orchestrator (multi-card path).
            False if event was sent to fallback_bridge (single-card path).
        """
        if self._plan_received.is_set() and not self._fallback_mode:
            self.route_acp_event(acp_event, fallback_bridge)
            return True
        fallback_bridge.on_event(acp_event)
        return False

    def _on_registry_status_change(self, task_id: str, new_status: TaskStatus) -> None:
        """Callback from TaskRegistry when status changes — triggers broadcast."""
        self._schedule_broadcast()

    def _schedule_broadcast(self) -> None:
        """Schedule a debounced broadcast of TASK_LIST_UPDATED to all sessions."""
        with self._lock:
            if self._closed_event.is_set():
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
            if self._closed_event.is_set():
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
        if self._closed_event.is_set():
            return False

        with self._lock:
            session = self._sessions.get(task_id)
            old_bridge = self._bridges.get(task_id)
        if session is None:
            return False

        # Freeze old bridge
        if old_bridge is not None:
            try:
                old_bridge.close_open_blocks()
            except Exception:
                logger.debug("Error closing bridge during rotation for task %s", task_id)

        task_item = self._registry.get(task_id)
        task_name = task_item.name if task_item else task_id

        # Phase 1: Create new session (I/O, outside lock)
        new_session = self._create_continuation_session(task_id, task_name, session)
        if new_session is None:
            return False

        # Register backfill callback BEFORE swap (eliminates TOCTOU window)
        rotation_count = self._rotation_counts.get(task_id, 0) + 1
        self._register_backfill_callback(session, new_session, task_name, rotation_count)

        # Phase 2: Atomic swap (inside lock, NO I/O)
        with self._lock:
            self._rotation_counts[task_id] = rotation_count
            self._sessions[task_id] = new_session
            if self._bridge_factory is not None:
                self._bridges[task_id] = self._bridge_factory(new_session)

        # Phase 3: Archive old + initialize new (I/O, outside lock)
        self._archive_old_session(session, task_name, rotation_count)
        self._initialize_continuation_card(new_session, task_id, session, rotation_count)

        logger.info("TaskOrchestrator: rotated task session for task_id=%s (续 %d)", task_id, rotation_count)
        return True

    def _create_continuation_session(
        self,
        task_id: str,
        task_name: str,
        old_session: SessionRotator | CardSession,
    ) -> CardSession | None:
        """Phase 1: Create a new continuation session. Returns None on failure."""
        try:
            return self._session_creator(task_id)
        except Exception:
            logger.warning(
                "TaskOrchestrator: session_creator failed for task_id=%s, rotation aborted",
                task_id, exc_info=True,
            )
            # Dispatch degradation notice on old session
            try:
                degrade_msg = UI_TEXT["orch_rotation_failed_notice"]
                old_session.dispatch(CardEvent.text_started("_rotation_failed"))
                old_session.dispatch(CardEvent.text_delta("_rotation_failed", degrade_msg))
                old_session.dispatch(CardEvent.text_done("_rotation_failed"))
            except Exception:
                logger.warning(
                    "TaskOrchestrator: failed to dispatch rotation degradation for task_id=%s",
                    task_id,
                )
            return None

    def _archive_old_session(
        self,
        old_session: SessionRotator | CardSession,
        task_name: str,
        rotation_count: int,
    ) -> None:
        """Phase 3: Archive old session with continuation navigation text."""
        new_msg_id = ""  # deep-link will be backfilled asynchronously
        msg = format_task_continuation_link(
            task_name=task_name,
            rotation_count=rotation_count,
            new_msg_id=None,
        )
        try:
            old_session.dispatch(CardEvent.text_started("_continuation"))
            old_session.dispatch(CardEvent.text_delta("_continuation", msg))
            old_session.dispatch(CardEvent.text_done("_continuation"))
            old_session.dispatch(CardEvent.archived())
        except Exception:
            logger.debug("Error archiving task session for rotation, task_id=%s (name=%s)", task_name, task_name, exc_info=True)

    def _register_backfill_callback(
        self,
        old_session: SessionRotator | CardSession,
        new_session: CardSession,
        task_name: str,
        rotation_count: int,
    ) -> None:
        """Inject a BackfillHook on new_session to backfill deep-link on old card.

        Must be called BEFORE Phase 2 swap so the hook is registered while
        new_session is not yet exposed to concurrent dispatch (eliminates TOCTOU).
        """
        hook = BackfillHook(
            old_session_ref=weakref.ref(old_session),
            task_name=task_name,
            rotation_count=rotation_count,
        )
        new_session.add_hook(hook)

    def _initialize_continuation_card(
        self,
        new_session: CardSession,
        task_id: str,
        old_session: SessionRotator | CardSession,
        rotation_count: int,
    ) -> None:
        """Dispatch TASK_LIST_UPDATED and continuation hint to the new card."""
        # Task list header
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

        # Back-link hint
        page = rotation_count + 1
        old_msg_id = getattr(old_session, "delivered_message_id", "") or ""
        if old_msg_id:
            hint_msg = UI_TEXT["orch_back_link"].format(msg_id=old_msg_id)
        else:
            hint_msg = UI_TEXT["orch_continuation_hint"].format(page=page)
        try:
            new_session.dispatch(CardEvent.text_started("_continuation_hint"))
            new_session.dispatch(CardEvent.text_delta("_continuation_hint", hint_msg))
            new_session.dispatch(CardEvent.text_done("_continuation_hint"))
        except Exception:
            logger.debug("Error dispatching continuation hint for task_id=%s", task_id, exc_info=True)

    def _enter_fallback_mode(self) -> None:
        """Enter single-session fallback mode (no multi-card).

        If a thinking session exists, it becomes the fallback session.
        Dispatches a visible warning to inform the user.
        """
        self._fallback_mode = True
        self._plan_received.set()  # Prevent further plan processing
        if self._thinking_session is not None and self._fallback_session is None:
            self._fallback_session = self._thinking_session
        # Dispatch visible warning to fallback session
        if self._fallback_session is not None:
            try:
                warn_id = "_fallback_warn"
                self._fallback_session.dispatch(CardEvent.text_started(warn_id))
                self._fallback_session.dispatch(
                    CardEvent.text_delta(warn_id, UI_TEXT["orch_fallback_warning"])
                )
                self._fallback_session.dispatch(CardEvent.text_done(warn_id))
            except Exception:
                logger.debug("Error dispatching fallback warning", exc_info=True)
        logger.info("TaskOrchestrator: fallback mode — using single session")

    def _finalize_thinking_session(self, task_names: list[str], *, overflow_count: int = 0) -> None:
        """Archive the thinking session with a single concise plan summary.

        Merges the former _notify_thinking_of_tasks + _archive_thinking_session
        into one pass to avoid redundant task-list duplication on the card.
        """
        if self._thinking_session is None:
            return
        try:
            task_count = len(task_names)
            # Fold task list when >5 items to save card space
            if task_count > 5:
                visible_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(task_names[:5]))
                task_list = visible_list + f"\n  …及 {task_count - 5} 项更多"
            else:
                task_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(task_names))
            summary = UI_TEXT["orch_plan_archived"].format(
                task_count=task_count,
                task_list=task_list,
            )
            if overflow_count > 0:
                independent_count = task_count - overflow_count
                transition = "\n" + UI_TEXT["orch_plan_transition_hint_overflow"].format(
                    independent_count=independent_count,
                    merged_count=overflow_count,
                )
            else:
                transition = "\n" + UI_TEXT["orch_plan_transition_hint_no_link"]
            block_id = "_plan_summary"
            self._thinking_session.dispatch(CardEvent.text_started(block_id))
            self._thinking_session.dispatch(CardEvent.text_delta(block_id, summary + transition))
            self._thinking_session.dispatch(CardEvent.text_done(block_id))
            self._thinking_session.dispatch(CardEvent.archived())
        except Exception:
            logger.debug("Error finalizing thinking session", exc_info=True)
        self._thinking_session = None

    def set_fallback_session(self, session: CardSession) -> None:
        """Set the fallback session for single-session mode."""
        self._fallback_session = session

    def create_subagent_session(self, task_id: str, name: str) -> None:
        """Create a new session for a detected subagent task.

        Called when TOOL_STARTED with agent/subagent tool name is detected.
        """
        if self._closed_event.is_set() or self._fallback_mode:
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
        if self._closed_event.is_set():
            return
        self._closed_event.set()

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

        # Shutdown the close executor thread pool
        if self._close_executor is not None:
            _executor = self._close_executor
            self._close_executor = None
            # Use a timer to enforce a 2s deadline on shutdown
            _shutdown_done = threading.Event()

            def _timed_shutdown():
                _executor.shutdown(wait=True, cancel_futures=True)
                _shutdown_done.set()

            t = threading.Thread(target=_timed_shutdown, daemon=True)
            t.start()
            if not _shutdown_done.wait(timeout=2.0):
                logger.warning(
                    "TaskOrchestrator: executor shutdown timed out (2s), "
                    "orphan threads may still be running for chat_id=%s",
                    self._chat_id,
                )

        logger.info("TaskOrchestrator: closed for chat_id=%s", self._chat_id)

    def _run_with_timeout(self, fn: Callable[[], None], *, timeout: float) -> None:
        """Run a callable in a managed thread pool with timeout protection.

        Uses a lazy-initialized ThreadPoolExecutor (max_workers=1) that is
        properly shut down in close(). On timeout, the executor is discarded
        so subsequent operations get a fresh thread (the old one is orphaned).
        """
        if self._close_executor is None:
            self._close_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="orch-close"
            )
        future = self._close_executor.submit(fn)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            # Discard blocked executor so next call gets a fresh thread
            self._close_executor.shutdown(wait=False)
            self._close_executor = None
            logger.warning("TaskOrchestrator: close operation timed out after %.1fs", timeout)
            raise TimeoutError(f"Operation timed out after {timeout}s")
