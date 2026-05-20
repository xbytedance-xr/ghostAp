"""SlockEngine — Multi-Agent collaboration engine (mouthpiece mode).

Inherits BaseEngine lifecycle and integrates AgentRegistry, MemoryManager,
TaskRouter, and Mouthpiece for orchestrating virtual agent teams.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, as_completed
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from typing import Callable, Optional

from ..agent_session import close_session_safely, create_engine_session
from ..config import get_settings
from ..engine_base import BaseEngine, EngineRunState
from .agent_registry import AgentRegistry
from .bounded_executor import BoundedExecutor, QueueFullError
from .card_templates import build_status_panel_card
from .escalation_manager import EscalationManager
from .memory_manager import MemoryManager, default_slock_storage_base
from .models import (
    AgentIdentity,
    AgentStatus,
    DiscussionStatus,
    EscalationLevel,
    EscalationRequest,
    SlockChannel,
    SlockTask,
    TaskStatus,
)
from .mouthpiece import Mouthpiece
from .observer_queue import ObserverLearningQueue
from .task_board_manager import TaskBoardManager
from .task_router import TaskRouter

logger = logging.getLogger(__name__)


@dataclass
class SlockEngineCallbacks:
    """Callbacks for slock engine lifecycle events."""

    on_agent_wake: Optional[Callable[[AgentIdentity], None]] = None
    on_agent_thinking: Optional[Callable[[AgentIdentity], None]] = None
    on_agent_running: Optional[Callable[[AgentIdentity, str], None]] = None
    on_agent_done: Optional[Callable[[AgentIdentity, str], None]] = None
    on_agent_error: Optional[Callable[[AgentIdentity, str], None]] = None
    on_task_claimed: Optional[Callable[[SlockTask, AgentIdentity], None]] = None
    on_message_routed: Optional[Callable[[str, AgentIdentity], None]] = None
    on_escalation: Optional[Callable[[EscalationRequest], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    # Discussion card lifecycle callbacks
    on_card_send: Optional[Callable[[dict], Optional[str]]] = None
    on_card_update: Optional[Callable[[str, dict], bool]] = None


class SlockStreamProcessor:
    """Builds a streaming progress card that updates as agents work.

    Tracks agent state transitions and produces progressive card snapshots
    that a handler can send/update to show real-time team activity.

    Usage:
        processor = SlockStreamProcessor(engine)
        callbacks = processor.build_callbacks()
        engine.execute_parallel(assignments, callbacks)
        # Each callback triggers processor.get_progress_card() update
    """

    def __init__(self, engine: "SlockEngine", *, on_update: Optional[Callable[[dict], None]] = None):
        self._engine = engine
        self._on_update = on_update  # Called with card dict on each state change
        self._start_time = time.time()
        self._agent_activity: dict[str, str] = {}  # agent_id → last activity description
        self._completed_count = 0
        self._error_count = 0
        self._total_tasks = 0

    def build_callbacks(self) -> SlockEngineCallbacks:
        """Build callbacks that feed into this stream processor."""
        return SlockEngineCallbacks(
            on_agent_wake=self._on_wake,
            on_agent_thinking=self._on_thinking,
            on_agent_running=self._on_running,
            on_agent_done=self._on_done,
            on_agent_error=self._on_agent_error,
            on_error=self._on_error,
        )

    def set_total_tasks(self, count: int) -> None:
        """Set the total task count for progress tracking."""
        self._total_tasks = count

    def _on_wake(self, agent: AgentIdentity) -> None:
        self._agent_activity[agent.agent_id] = f"{agent.emoji} {agent.name}: waking..."
        self._emit_update()

    def _on_thinking(self, agent: AgentIdentity) -> None:
        self._agent_activity[agent.agent_id] = f"{agent.emoji} {agent.name}: 💭 thinking..."
        self._emit_update()

    def _on_running(self, agent: AgentIdentity, task: str) -> None:
        short_task = task[:60] + "..." if len(task) > 60 else task
        self._agent_activity[agent.agent_id] = f"{agent.emoji} {agent.name}: 🔄 {short_task}"
        self._emit_update()

    def _on_done(self, agent: AgentIdentity, result: str) -> None:
        self._agent_activity[agent.agent_id] = f"{agent.emoji} {agent.name}: ✅ done"
        self._completed_count += 1
        self._emit_update()

    def _on_agent_error(self, agent: AgentIdentity, error: str) -> None:
        short_err = error[:60] + "..." if len(error) > 60 else error
        self._agent_activity[agent.agent_id] = f"{agent.emoji} {agent.name}: ❌ {short_err}"
        self._error_count += 1
        self._emit_update()

    def _on_error(self, error_msg: str) -> None:
        self._error_count += 1
        self._emit_update()

    def _emit_update(self) -> None:
        """Emit a card update if a callback is registered."""
        if self._on_update:
            self._on_update(self.get_progress_card())

    def get_progress_card(self) -> dict:
        """Build the current progress card snapshot."""
        elapsed = time.time() - self._start_time
        channel = self._engine.channel
        team_name = channel.team_name if channel else "Slock"

        # Progress header
        if self._total_tasks > 0:
            progress_pct = int(self._completed_count / self._total_tasks * 100)
            header_title = f"⚡ {team_name} — {progress_pct}% ({self._completed_count}/{self._total_tasks})"
        else:
            header_title = f"⚡ {team_name} — Running"

        elements: list[dict] = []

        # Agent activity lines
        for activity in self._agent_activity.values():
            elements.append({"tag": "markdown", "content": activity})

        if not elements:
            elements.append({"tag": "markdown", "content": "*Waiting for agents...*"})

        # Footer with stats
        stats_parts = [f"⏱ {elapsed:.0f}s"]
        if self._completed_count:
            stats_parts.append(f"✅ {self._completed_count}")
        if self._error_count:
            stats_parts.append(f"❌ {self._error_count}")
        elements.append({
            "tag": "markdown",
            "content": " | ".join(stats_parts),
            "text_size": "notation",
        })

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": "blue",
            },
            "body": {"elements": elements},
        }


class AgentCancellationError(Exception):
    """Raised when an agent execution is cancelled via cancellation token."""


class SlockEngine(BaseEngine):
    """Multi-Agent collaboration engine using mouthpiece pattern.

    Manages a team of virtual agents within a single Feishu group,
    routing messages, managing tasks, and formatting output through
    the mouthpiece mechanism.

    Lock ordering (always acquire in this order to prevent deadlocks):
        1. self._lock (inherited RLock from BaseEngine)
        2. self._executor_lock (plain threading.Lock)
        3. BoundedExecutor._lock (leaf lock, never held while acquiring above)
    """

    _state_filename = ".slock_engine_state.json"
    _gc_label = "Slock"
    _gc_threshold_default = 85.0

    # Immutable state machine transition table — shared by transition_agent and
    # try_lock_for_move to ensure a single source of truth.  Values are tuples
    # (immutable) to prevent accidental runtime mutation.
    VALID_TRANSITIONS: dict[AgentStatus, tuple[AgentStatus, ...]] = {
        AgentStatus.IDLE: (AgentStatus.WAKING, AgentStatus.MOVING, AgentStatus.DISCUSSING),
        AgentStatus.WAKING: (AgentStatus.THINKING, AgentStatus.IDLE),
        AgentStatus.THINKING: (AgentStatus.RUNNING, AgentStatus.IDLE),
        AgentStatus.RUNNING: (AgentStatus.CHECKING, AgentStatus.IDLE),
        AgentStatus.CHECKING: (AgentStatus.SENDING, AgentStatus.RUNNING, AgentStatus.IDLE),
        AgentStatus.SENDING: (AgentStatus.IDLE,),
        AgentStatus.MOVING: (AgentStatus.IDLE,),
        AgentStatus.DISCUSSING: (AgentStatus.IDLE,),
    }

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str = "coco",
        engine_name: str = "Slock",
        model_name: Optional[str] = None,
        *,
        memory_base_path: str = "",
    ):
        super().__init__(chat_id, root_path, agent_type, engine_name, model_name)

        # Core subsystems
        storage_base_path = memory_base_path or default_slock_storage_base()
        self._registry = AgentRegistry(base_path=storage_base_path)
        self._memory = MemoryManager(base_path=storage_base_path)
        claims_path = os.path.join(storage_base_path, "claims", f"{chat_id}.json")
        self._router = TaskRouter(persist_path=claims_path, memory_backend=self._memory)
        self._observer_queue = ObserverLearningQueue(memory=self._memory, router=self._router)
        self._mouthpiece = Mouthpiece()

        # Thread pool for parallel agent execution
        self._executor: Optional[BoundedExecutor] = None
        self._executor_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._max_parallel_agents = 4

        # Independent executor for inter-agent discussions (decoupled from main agent execution)
        from concurrent.futures import ThreadPoolExecutor as _TPE
        self._discussion_executor = _TPE(max_workers=2, thread_name_prefix="slock_discussion")

        # Channel state
        self._channel: Optional[SlockChannel] = None
        self._tasks: list[SlockTask] = []
        self._dirty = False  # dirty-flag for debounced task board persistence
        self._agent_statuses: dict[str, AgentStatus] = {}
        self._agent_sessions: dict[str, object] = {}
        self._agent_execution_errors: dict[str, str] = {}
        self._escalations: list[EscalationRequest] = []
        self._escalation_retry_counts: dict[str, int] = {}

        # Cancellation tokens for per-agent execution control
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_events_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        # Discussion state: tracks active inter-agent discussions per channel
        self._active_discussions: dict[str, list] = {}  # channel_id -> list[DiscussionThread]
        self._discussions_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._discussion_manager: Optional[object] = None  # Lazy-initialized

        # Composed managers (share lock and state references)
        self._task_mgr = TaskBoardManager(
            lock=self._lock,
            tasks=self._tasks,
            channel_getter=lambda: self._channel,
            chat_id_getter=lambda: self.chat_id,
            dirty_getter=lambda: self._dirty,
            dirty_setter=self._set_dirty,
            router=self._router,
            memory=self._memory,
            registry_get=self._registry.get,
            execute_agent_fn=lambda agent, content, callbacks: self._execute_agent(agent, content, callbacks),
        )
        self._escalation_mgr = EscalationManager(
            lock=self._lock,
            escalations=self._escalations,
            retry_counts=self._escalation_retry_counts,
            channel_getter=lambda: self._channel,
            chat_id_getter=lambda: self.chat_id,
            task_list_getter=lambda: self._tasks,
            dirty_setter=self._set_dirty,
            router=self._router,
            transition_agent=self.transition_agent,
            flush_if_dirty=self._task_mgr._flush_if_dirty,
            execute_task_fn=lambda task_id, agent_id, callbacks: self.execute_task(task_id, agent_id, callbacks),
            rollback_task_fn=self._task_mgr._rollback_task,
            force_complete_task_fn=self._task_mgr.force_complete_task,
            get_executor_fn=self._get_executor,
            escalation_timeout_s=get_settings().slock_escalation_timeout,
        )

        # Register LLM summarization callback for memory compression
        self._memory.set_llm_callback(self._summarize_via_llm)

    def _summarize_via_llm(self, prompt: str) -> Optional[str]:
        """LLM callback for memory summarization via ACP session.

        Creates a lightweight engine session to execute the summarization prompt.
        Must NOT be called while holding self._memory._lock (caller ensures this).
        """
        try:
            session = create_engine_session(
                agent_type=self._agent_type,
                cwd=self.root_path,
                model_name=None,
                thread_id="slock_memory_summarize",
                auto_approve=True,
            )
            if session is None:
                logger.warning("Failed to create session for memory summarization")
                return None
            try:
                result = session.send_prompt(prompt, timeout=60)
                return result.text if result else None
            finally:
                close_session_safely(session)
        except Exception as exc:
            logger.warning("Memory summarization LLM call failed: %s", str(exc))
            return None

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def memory(self) -> MemoryManager:
        return self._memory

    @property
    def router(self) -> TaskRouter:
        return self._router

    @property
    def mouthpiece(self) -> Mouthpiece:
        return self._mouthpiece

    @property
    def channel(self) -> Optional[SlockChannel]:
        return self._channel

    @property
    def tasks(self) -> list[SlockTask]:
        return list(self._tasks)

    # ------------------------------------------------------------------
    # Public API: exposed for DiscussionManager (avoids private access)
    # ------------------------------------------------------------------

    def get_agent(self, agent_id: str) -> Optional[AgentIdentity]:
        """Get agent identity by ID. Public API for discussion/external use."""
        return self._registry.get(agent_id)

    def find_agent_by_name(self, name: str, channel_id: Optional[str] = None) -> Optional[AgentIdentity]:
        """Find agent by display name. Public API for NLI discussion routing."""
        return self._registry.find_by_name(name, channel_id=channel_id)

    def build_agent_prompt(self, agent: AgentIdentity, message: str, memory=None) -> str:
        """Build full prompt for an agent. Public wrapper around _build_agent_prompt.

        If memory is None, reads from memory manager automatically.
        """
        if memory is None:
            memory = self._memory.read_agent_memory(agent.agent_id)
        return self._build_agent_prompt(agent, message, memory)

    def run_agent_session(
        self,
        agent: AgentIdentity,
        prompt: str,
        *,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """Run an ACP session for the agent. Public wrapper around _run_acp_session."""
        return self._run_acp_session(agent, prompt, timeout=timeout)

    # ------------------------------------------------------------------
    # Discussion Helpers: multi-thread parallel support
    # ------------------------------------------------------------------

    @staticmethod
    def build_discussion_config_from_settings(settings=None):
        """Build a DiscussionConfig from current settings. Shared by all discussion paths."""
        from .models import DiscussionConfig
        if settings is None:
            settings = get_settings()
        trigger_rules = [
            r.strip() for r in settings.slock_discussion_trigger_rules.split(",")
            if r.strip()
        ]
        return DiscussionConfig(
            max_rounds=settings.slock_max_discussion_rounds,
            token_budget=settings.slock_discussion_token_budget,
            trigger_rules=trigger_rules or ["coder->reviewer"],
            discussion_timeout=settings.slock_discussion_timeout,
        )

    def _add_discussion(self, channel_id: str, thread) -> bool:
        """Add a discussion thread to the channel. Returns False if at capacity."""
        max_parallel = get_settings().slock_max_parallel_discussions
        with self._discussions_lock:
            discussions = self._active_discussions.setdefault(channel_id, [])
            if len(discussions) >= max_parallel:
                logger.warning(
                    "Channel %s at max parallel discussions (%d), rejecting new discussion",
                    channel_id, max_parallel,
                )
                return False
            discussions.append(thread)
        return True

    def _remove_discussion(self, channel_id: str, thread_id: str) -> None:
        """Remove a completed discussion thread from the channel."""
        with self._discussions_lock:
            discussions = self._active_discussions.get(channel_id, [])
            self._active_discussions[channel_id] = [
                t for t in discussions if t.thread_id != thread_id
            ]
            if not self._active_discussions[channel_id]:
                del self._active_discussions[channel_id]

    def find_active_discussion(self, channel_id: str, thread_id: str):
        """Find an active discussion thread by channel and thread id."""
        with self._discussions_lock:
            for thread in self._active_discussions.get(channel_id, []):
                if getattr(thread, "thread_id", "") == thread_id:
                    return thread
        return None

    def run_council(
        self,
        question: str,
        *,
        participants: Optional[list[AgentIdentity]] = None,
        chairman: Optional[AgentIdentity] = None,
        on_stage: Optional[Callable[[object], None]] = None,
        timeout: float = 300.0,
    ):
        """Run a same-question council flow across multiple Slock agents."""
        from .council_manager import CouncilManager

        channel_id = self._channel.channel_id if self._channel else self.chat_id
        selected = participants or list(self._registry.list_agents(channel_id=channel_id))
        return CouncilManager(engine=self).run(
            question,
            participants=list(selected),
            chairman=chairman,
            on_stage=on_stage,
            timeout=timeout,
        )

    def get_agent_status(self, agent_id: str) -> AgentStatus:
        """Get current status of an agent."""
        with self._lock:
            return self._agent_statuses.get(agent_id, AgentStatus.IDLE)

    def set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        """Update agent status (thread-safe)."""
        with self._lock:
            self._agent_statuses[agent_id] = status
        self._router.set_agent_status(agent_id, status)

    def transition_agent(self, agent_id: str, to_status: AgentStatus) -> bool:
        """Transition agent through valid state machine paths.

        Valid transitions:
            IDLE → WAKING → THINKING → RUNNING → CHECKING → SENDING → IDLE

        Note: This method acquires self._lock (RLock) internally.  It is safe
        to call while self._lock is already held by the same thread (re-entrant),
        which is used by try_lock_for_move for atomic CAS semantics.
        """
        current = self.get_agent_status(agent_id)
        if to_status in self.VALID_TRANSITIONS.get(current, ()):
            self.set_agent_status(agent_id, to_status)
            return True

        logger.warning(
            "Invalid agent transition: %s -> %s (agent=%s)",
            current.value, to_status.value, agent_id,
        )
        return False

    def _transition_agent_or_abort(self, agent_id: str, to_status: AgentStatus) -> bool:
        """Transition during execution, forcing IDLE if the state machine rejects it."""
        if self.transition_agent(agent_id, to_status):
            return True
        logger.error(
            "Aborting Slock agent execution after invalid transition to %s (agent=%s)",
            to_status.value,
            agent_id,
        )
        self.set_agent_status(agent_id, AgentStatus.IDLE)
        return False

    def try_lock_for_move(self, agent_id: str) -> bool:
        """Atomically check IDLE and transition to MOVING.

        Returns True if the agent was IDLE and is now marked MOVING,
        preventing task assignment or other transitions during the move.
        Returns False if the agent is not IDLE (move should be rejected).

        Uses transition_agent internally (RLock is re-entrant) to ensure all
        state changes go through the unified state machine validation path.
        """
        with self._lock:
            current = self._agent_statuses.get(agent_id, AgentStatus.IDLE)
            if current != AgentStatus.IDLE:
                return False
            # Re-entrant call: self._lock is already held, transition_agent
            # acquires it again via get_agent_status/set_agent_status (RLock).
            return self.transition_agent(agent_id, AgentStatus.MOVING)

    def unlock_after_move(self, agent_id: str) -> None:
        """Release MOVING state back to IDLE after move completes or fails.

        Should be called in a finally block to prevent MOVING state leakage.
        """
        with self._lock:
            current = self._agent_statuses.get(agent_id)
            if current == AgentStatus.MOVING:
                self._agent_statuses[agent_id] = AgentStatus.IDLE
        self._router.set_agent_status(agent_id, AgentStatus.IDLE)
        # Defensive L1 memory readability check after move
        self._verify_l1_memory_after_move(agent_id)

    def _verify_l1_memory_after_move(self, agent_id: str) -> str:
        """Verify L1 memory is readable after cross-group move (non-blocking).

        Logs ERROR if memory file cannot be read or if role is unexpectedly
        empty for an established agent — provides observability without
        disrupting the move flow.

        Returns a diagnostic string (empty string means all OK).
        """
        try:
            memory = self._memory.read_agent_memory(agent_id)
            path = self._memory.agent_memory_path(agent_id)
            if not memory.role and not memory.key_knowledge and not memory.active_context:
                logger.debug(
                    "L1 memory empty after move (may be expected for new agents) | agent=%s path=%s",
                    agent_id, path,
                )
                return ""
            if not memory.role and (memory.key_knowledge or memory.active_context):
                # Agent has history but role is missing — persona consistency at risk
                diag = (
                    f"L1 role section empty but agent has history | agent={agent_id} "
                    f"has_knowledge={bool(memory.key_knowledge)} has_context={bool(memory.active_context)} "
                    f"path={path}"
                )
                logger.error(diag)
                return diag
            logger.debug(
                "L1 memory verified after move | agent=%s has_role=%s has_knowledge=%s has_context=%s",
                agent_id, bool(memory.role), bool(memory.key_knowledge), bool(memory.active_context),
            )
            return ""
        except Exception as exc:
            diag = f"L1 memory read FAILED after move — persona consistency at risk | agent={agent_id} error={exc}"
            logger.error(diag)
            return diag

    # ------------------------------------------------------------------
    # Agent Cancellation
    # ------------------------------------------------------------------

    def _get_cancel_event(self, agent_id: str) -> threading.Event:
        """Get or create a cancellation event for an agent."""
        with self._cancel_events_lock:
            if agent_id not in self._cancel_events:
                self._cancel_events[agent_id] = threading.Event()
            return self._cancel_events[agent_id]

    def _clear_cancel_event(self, agent_id: str) -> None:
        """Remove the cancellation event after execution completes."""
        with self._cancel_events_lock:
            self._cancel_events.pop(agent_id, None)

    def cancel_agent(self, agent_id: str) -> bool:
        """Cancel a running agent by setting its cancellation event.

        Returns True if the agent had an active cancel event to set.
        """
        with self._cancel_events_lock:
            event = self._cancel_events.get(agent_id)
            if event is not None:
                event.set()
                logger.info("Cancellation requested for agent %s", agent_id)
                return True
        logger.debug("No active cancel event for agent %s", agent_id)
        return False

    # ------------------------------------------------------------------
    # Engine Lifecycle
    # ------------------------------------------------------------------

    def activate_channel(self, channel: SlockChannel) -> None:
        """Activate slock mode for a channel.

        Creates memory directories and a workspace directory with a marker file.
        """
        self._channel = channel
        self._memory.ensure_directories(channel_id=channel.channel_id)
        self._memory.initialize_team_workspace(channel, project_path=self.root_path)
        persisted_tasks = self._memory.read_task_board(channel.channel_id)
        if persisted_tasks:
            self._tasks = persisted_tasks

        marker_data = {
            "channel_id": channel.channel_id,
            "team_name": channel.team_name,
            "name": channel.name,
            "owner_id": channel.owner_id,
            "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        # Canonical app-level group marker under ~/.ghostap/slock/groups.
        canonical_dir = self._memory.get_group_base_path(channel.channel_id)
        os.makedirs(canonical_dir, exist_ok=True)
        canonical_marker = os.path.join(canonical_dir, ".slock_channel.json")
        self._write_channel_marker(canonical_marker, marker_data)

    @staticmethod
    def _write_channel_marker(marker_path: str, marker_data: dict) -> None:
        """Write or merge a channel marker atomically.

        If the marker file already exists, read the existing JSON and merge:
        - Only fill in fields that are missing or currently empty/None.
        - Never overwrite ``activated_at`` (preserve first activation time).
        Then write atomically via tmp + os.replace.
        """
        import json as _json

        existing: dict = {}
        if os.path.exists(marker_path):
            try:
                with open(marker_path, "r", encoding="utf-8") as f:
                    existing = _json.load(f)
            except (OSError, ValueError):
                existing = {}

        # Merge: for each key in marker_data, update only if the existing
        # value is missing/empty and the new value is non-empty.
        merged = dict(existing)
        for key, new_val in marker_data.items():
            # Never overwrite activated_at — preserve first activation time.
            if key == "activated_at" and key in merged and merged[key]:
                continue
            old_val = merged.get(key)
            if not old_val and new_val:
                merged[key] = new_val

        tmp_path = marker_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            _json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, marker_path)

    def execute(
        self,
        message: str,
        callbacks: Optional[SlockEngineCallbacks] = None,
        *,
        sender_id: str = "",
    ) -> Optional[str]:
        """Process an incoming message through the slock engine.

        Routes the message to the appropriate agent, wakes it,
        executes via ACP session, and returns formatted output.
        """
        with self._lock:
            if self._run_state == EngineRunState.STOPPING:
                return None
            self._run_state = EngineRunState.RUNNING

        try:
            # Get available agents for this channel
            channel_id = self._channel.channel_id if self._channel else self.chat_id
            agents = self._registry.list_agents(channel_id=channel_id)

            if not agents:
                with self._lock:
                    self._run_state = EngineRunState.IDLE
                return None

            # Route message to agent (only IDLE agents considered by router)
            target_agent = self._router.route_message(message, agents)
            if not target_agent:
                return None

            if callbacks and callbacks.on_message_routed:
                callbacks.on_message_routed(message, target_agent)

            # Execute agent lifecycle
            result = self._execute_agent(target_agent, message, callbacks)
            return result

        except Exception as e:
            error_msg = f"Slock engine error: {e}"
            logger.error(error_msg, exc_info=True)
            if callbacks and callbacks.on_error:
                callbacks.on_error(error_msg)
            return None
        finally:
            with self._lock:
                if self._run_state == EngineRunState.RUNNING:
                    self._run_state = EngineRunState.IDLE

    def _execute_agent(
        self,
        agent: AgentIdentity,
        message: str,
        callbacks: Optional[SlockEngineCallbacks],
    ) -> Optional[str]:
        """Execute a single agent's response cycle.

        IDLE → WAKING → THINKING → RUNNING → CHECKING → SENDING → IDLE
        """
        agent_id = agent.agent_id
        channel_id = self._channel.channel_id if self._channel else self.chat_id

        # Set up cancellation event and watchdog timer
        cancel_event = self._get_cancel_event(agent_id)
        settings = get_settings()
        watchdog = threading.Timer(
            settings.slock_agent_execution_timeout,
            cancel_event.set,
        )
        watchdog.daemon = True
        watchdog.start()

        try:
            # IDLE → WAKING (early-return if agent is not IDLE)
            if not self.transition_agent(agent_id, AgentStatus.WAKING):
                logger.info("Agent %s busy, skipping execution", agent_id)
                return None

            self._memory.append_message_archive(
                channel_id,
                sender_type="user",
                content=message,
                agent_id=agent_id,
                agent_name=agent.name,
                metadata={"routed_to": agent_id},
            )

            if callbacks and callbacks.on_agent_wake:
                callbacks.on_agent_wake(agent)

            # Check cancellation
            if cancel_event.is_set():
                raise AgentCancellationError(f"Agent {agent_id} cancelled")

            # Load agent memory context
            memory = self._memory.read_agent_memory(agent_id)

            # WAKING → THINKING
            if not self._transition_agent_or_abort(agent_id, AgentStatus.THINKING):
                return None
            if callbacks and callbacks.on_agent_thinking:
                callbacks.on_agent_thinking(agent)

            # Build prompt with memory context and system prompt
            prompt = self._build_agent_prompt(agent, message, memory)

            # Check cancellation
            if cancel_event.is_set():
                raise AgentCancellationError(f"Agent {agent_id} cancelled")

            # THINKING → RUNNING
            if not self._transition_agent_or_abort(agent_id, AgentStatus.RUNNING):
                return None
            if callbacks and callbacks.on_agent_running:
                callbacks.on_agent_running(agent, message)

            # Execute via ACP session
            try:
                result = self._run_acp_session(agent, prompt)
            except Exception as exc:
                self.set_agent_status(agent_id, AgentStatus.IDLE)
                self.escalate(
                    agent,
                    f"Agent execution failed: {exc}",
                    level=EscalationLevel.BLOCKED,
                    context=message[:1000],
                    callbacks=callbacks,
                )
                return None
            if result is None:
                execution_errors = self._get_agent_execution_errors()
                error_detail = execution_errors.pop(agent_id, "")
                if error_detail:
                    self.set_agent_status(agent_id, AgentStatus.IDLE)
                    self.escalate(
                        agent,
                        f"Agent execution failed: {error_detail}",
                        level=EscalationLevel.BLOCKED,
                        context=message[:1000],
                        callbacks=callbacks,
                    )
                    return None

            # Check cancellation after session
            if cancel_event.is_set():
                raise AgentCancellationError(f"Agent {agent_id} cancelled")

            # RUNNING → CHECKING
            if not self._transition_agent_or_abort(agent_id, AgentStatus.CHECKING):
                return None

            # CHECKING → SENDING
            if not self._transition_agent_or_abort(agent_id, AgentStatus.SENDING):
                return None

            # Format output through mouthpiece
            if result:
                formatted = self._mouthpiece.format_text(agent, result)
                self._memory.append_message_archive(
                    channel_id,
                    sender_type="agent",
                    content=result,
                    agent_id=agent_id,
                    agent_name=agent.name,
                    metadata={"formatted": formatted},
                )
            else:
                formatted = None

            if callbacks and callbacks.on_agent_done:
                callbacks.on_agent_done(agent, result or "")

            # SENDING → IDLE
            if not self._transition_agent_or_abort(agent_id, AgentStatus.IDLE):
                return None

            # Update agent memory with new context
            if result:
                context_entry = f"[{time.strftime('%Y-%m-%d %H:%M')}] Responded to: {message[:100]}"
                self._memory.update_agent_context(agent_id, context_entry)
                skill_tags = self._router.extract_skill_keywords(message)
                profiles = self._memory.record_skill_feedback(agent_id, skill_tags, quality_score=100.0)
                self._router.set_skill_profiles(agent_id, profiles)
                self._record_observer_learning(agent, message, skill_tags)

                # Discussion hook: trigger inter-agent discussion if enabled
                self._maybe_trigger_discussion(agent, result, channel_id, callbacks)

                # Memory enhancement: trigger context summarization if threshold exceeded
                settings_obj = get_settings()
                self._memory.summarize_context(
                    agent_id, threshold=settings_obj.slock_memory_summarize_threshold
                )

            return formatted

        except AgentCancellationError:
            logger.warning("Agent %s execution cancelled", agent_id)
            self.set_agent_status(agent_id, AgentStatus.IDLE)
            # Close any active session
            with self._lock:
                session = self._agent_sessions.pop(agent_id, None)
            if session:
                close_session_safely(session)
            return None
        finally:
            watchdog.cancel()
            self._clear_cancel_event(agent_id)

    def _record_observer_learning(
        self,
        actor: AgentIdentity,
        message: str,
        skill_tags: list[str],
    ) -> None:
        """Let idle team members learn potential skills from successful work."""
        channel_id = self._channel.channel_id if self._channel else self.chat_id
        for observer in self._registry.list_agents(channel_id=channel_id):
            if observer.agent_id == actor.agent_id:
                continue
            if self.get_agent_status(observer.agent_id) != AgentStatus.IDLE:
                continue
            self._observer_queue.enqueue(
                observer_id=observer.agent_id,
                actor_id=actor.agent_id,
                message=message,
                skill_tags=skill_tags,
            )

    def _maybe_trigger_discussion(
        self,
        agent: AgentIdentity,
        result: str,
        channel_id: str,
        callbacks: Optional[SlockEngineCallbacks],
    ) -> None:
        """Post-execution hook: check if inter-agent discussion should be triggered.

        Only runs when slock_discussion_enabled is True. Uses DiscussionManager
        to evaluate trigger conditions and run the discussion loop if triggered.
        Sends discussion status cards for user visibility.
        """
        settings = get_settings()
        if not settings.slock_discussion_enabled:
            return

        # Skip discussion trigger for trivial/short responses
        if len(result.strip()) < 50:
            return

        # Skip if result is just an acknowledgment or status update
        _TRIVIAL_PATTERNS = (
            "已完成", "done", "ok", "好的", "收到", "✅", "已执行",
            "no issues", "没有问题", "通过", "passed",
        )
        result_lower = result.strip().lower()
        if any(result_lower.startswith(p.lower()) for p in _TRIVIAL_PATTERNS):
            return

        from .discussion_manager import DiscussionManager

        # Lazy-init discussion manager using shared config builder
        if self._discussion_manager is None:
            config = self.build_discussion_config_from_settings(settings)
            self._discussion_manager = DiscussionManager(
                engine=self, memory_manager=self._memory, config=config,
            )

        dm: DiscussionManager = self._discussion_manager  # type: ignore[assignment]
        thread = dm.should_trigger_discussion(agent, result)
        if thread is None:
            return

        # Check parallel discussion capacity
        if not self._add_discussion(channel_id, thread):
            logger.info("Discussion rejected: channel %s at max capacity", channel_id)
            return

        logger.info(
            "Discussion triggered | agent=%s channel=%s reason=%s participants=%s",
            agent.agent_id, channel_id, thread.trigger_reason, thread.participants,
        )

        # Set agent state to DISCUSSING (via state machine validation)
        if not self.transition_agent(agent.agent_id, AgentStatus.DISCUSSING):
            logger.info(
                "Discussion skipped: agent %s not in IDLE state, cannot transition to DISCUSSING",
                agent.agent_id,
            )
            self._remove_discussion(channel_id, thread.thread_id)
            return

        # Send "discussion in progress" card to user
        from .card_templates import build_discussion_card_from_thread, build_discussion_summary_card_from_thread
        discussion_card_msg_id = None
        if callbacks and callbacks.on_card_send:
            try:
                card = build_discussion_card_from_thread(thread)
                discussion_card_msg_id = callbacks.on_card_send(card)
            except Exception as card_exc:
                logger.debug("Failed to send discussion start card: %s", card_exc)

        # Define the discussion runner to be submitted to the executor
        def _run_discussion():
            watchdog_timeout = settings.slock_discussion_timeout
            watchdog_fired = threading.Event()

            def _watchdog_trigger():
                watchdog_fired.set()
                thread.status = DiscussionStatus.TIMEOUT
                logger.warning(
                    "Discussion watchdog fired: %s (timeout=%ds)",
                    thread.thread_id, watchdog_timeout,
                )

            watchdog = threading.Timer(watchdog_timeout, _watchdog_trigger)
            watchdog.daemon = True
            watchdog.start()

            try:
                # Set cancellation event on thread for cooperative cancellation
                thread.cancellation_event = watchdog_fired

                def on_round_complete(updated_thread):
                    """Update the discussion card after each round."""
                    if watchdog_fired.is_set():
                        return
                    if discussion_card_msg_id and callbacks and callbacks.on_card_update:
                        try:
                            card = build_discussion_card_from_thread(updated_thread)
                            callbacks.on_card_update(discussion_card_msg_id, card)
                        except Exception as upd_exc:
                            logger.debug("Failed to update discussion card: %s", upd_exc)

                completed_thread = dm.run_discussion(
                    thread, result, on_round_complete=on_round_complete
                )

                # Persist conclusion to L2 shared memory
                if completed_thread.conclusion:
                    self._memory.append_discussion_conclusion(
                        channel_id, completed_thread.conclusion
                    )
                    # Sync conclusion to L1 active_context of all participants
                    for participant_id in completed_thread.participants:
                        conclusion_entry = (
                            f"[{time.strftime('%Y-%m-%d %H:%M')}] "
                            f"Discussion conclusion ({completed_thread.trigger_reason}): "
                            f"{completed_thread.conclusion[:500]}"
                        )
                        self._memory.update_agent_context(participant_id, conclusion_entry)

                # Send discussion summary card
                if callbacks and callbacks.on_card_send:
                    try:
                        summary_card = build_discussion_summary_card_from_thread(completed_thread)
                        callbacks.on_card_send(summary_card)
                    except Exception as card_exc:
                        logger.debug("Failed to send discussion summary card: %s", card_exc)

                if callbacks and callbacks.on_agent_done:
                    callbacks.on_agent_done(
                        agent,
                        f"[Discussion {completed_thread.status.value}] {completed_thread.conclusion[:200]}",
                    )
            except Exception as exc:
                logger.error("Discussion failed: %s", exc, exc_info=True)
            finally:
                watchdog.cancel()
                self.set_agent_status(agent.agent_id, AgentStatus.IDLE)
                self._remove_discussion(channel_id, thread.thread_id)

        # Submit to discussion executor (non-blocking)
        try:
            self._discussion_executor.submit(_run_discussion)
        except Exception as exc:
            logger.warning("Failed to submit discussion to executor: %s", str(exc))
            self.set_agent_status(agent.agent_id, AgentStatus.IDLE)
            self._remove_discussion(channel_id, thread.thread_id)

    def _build_agent_prompt(self, agent: AgentIdentity, message: str, memory) -> str:
        """Build the full prompt for an agent including system prompt and memory."""
        logger.debug(
            "_build_agent_prompt: agent=%s memory_path=%s has_role=%s has_knowledge=%s has_context=%s",
            agent.agent_id,
            self._memory.agent_memory_path(agent.agent_id),
            bool(memory.role),
            bool(memory.key_knowledge),
            bool(memory.active_context),
        )
        parts: list[str] = []

        if agent.system_prompt:
            parts.append(agent.system_prompt)

        # Minimum authorization: inject permitted tools so ACP session scope is bounded.
        if agent.permissions:
            parts.append(
                f"\n# Authorized Tools\n"
                f"You are ONLY permitted to use the following tools: "
                f"{', '.join(agent.permissions)}."
            )

        if memory.role:
            parts.append(f"\n# Your Role\n{memory.role}")

        if memory.key_knowledge:
            parts.append(f"\n# Key Knowledge\n{memory.key_knowledge}")

        # Memory Enhancement: use conversation replay instead of raw truncation
        channel_id = self._channel.channel_id if self._channel else self.chat_id
        settings = get_settings()
        replay_rounds = settings.slock_conversation_replay_rounds
        replay = self._memory.read_conversation_replay(channel_id, replay_rounds)
        if replay:
            replay_lines = []
            for entry in replay:
                sender = entry.get("agent_name") or entry.get("sender_type", "user")
                content = entry.get("content", "")[:500]
                replay_lines.append(f"[{sender}]: {content}")
            parts.append("\n# Recent Conversation\n" + "\n".join(replay_lines))
        elif memory.active_context:
            # Fallback: use summarized/truncated active_context
            parts.append(f"\n# Recent Context\n{memory.active_context[-2000:]}")

        group_memory = self._memory.read_group_memory(channel_id)
        if group_memory:
            parts.append(f"\n# Team Shared Memory\n{group_memory[-2000:]}")

        global_wiki = self._memory.read_global_wiki()
        if global_wiki:
            parts.append(f"\n# Global Knowledge\n{global_wiki[-2000:]}")

        # Discussion context: inject active discussion thread if present (copy-on-read for thread safety)
        if hasattr(self, '_active_discussions') and hasattr(self, '_discussions_lock'):
            from ..utils.redact import redact_sensitive
            with self._discussions_lock:
                active_threads = list(self._active_discussions.get(channel_id, []))
            for active_thread in active_threads:
                if active_thread.is_active and active_thread.messages:
                    disc_lines = []
                    for msg in active_thread.messages[-6:]:  # Last 6 messages for context
                        content = msg.content[:300]
                        if hasattr(active_thread, 'channel_id') and active_thread.channel_id != channel_id:
                            content = redact_sensitive(content)
                        disc_lines.append(f"[Round {msg.round_num}] {msg.sender_agent_id}: {content}")
                    parts.append("\n# Discussion Context\n" + "\n".join(disc_lines))
                    break  # Only inject the most recent active discussion

        parts.append(f"\n# User Message\n{message}")

        return "\n".join(parts)

    def _run_acp_session(
        self,
        agent: AgentIdentity,
        prompt: str,
        *,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """Run an ACP session for the agent. Returns response text or None.

        Security: auto_approve=True suppresses interactive prompts (zero HI).
        Tool authorization is bounded by agent.permissions which is injected
        into the system prompt by _execute_agent, restricting which tools the
        agent may use.
        """
        execution_errors = self._get_agent_execution_errors()
        try:
            execution_errors.pop(agent.agent_id, None)
            thread_id = f"slock_agent_{agent.agent_id}"
            session = create_engine_session(
                agent_type=agent.agent_type,
                cwd=self.root_path,
                model_name=agent.model_name or None,
                thread_id=thread_id,
                auto_approve=True,  # Zero HI; tool scope bounded by agent.permissions
            )
            if session is None:
                logger.warning("Failed to create ACP session for agent %s", agent.name)
                return None

            with self._lock:
                self._agent_sessions[agent.agent_id] = session
            try:
                result = session.send_prompt(
                    prompt,
                    timeout=timeout if timeout is not None else self.settings.coco_execution_timeout,
                )
                return result.text if result else None
            finally:
                with self._lock:
                    if self._agent_sessions.get(agent.agent_id) is session:
                        del self._agent_sessions[agent.agent_id]
                close_session_safely(session)

        except Exception as e:
            execution_errors[agent.agent_id] = str(e)
            logger.error("ACP session error for agent %s: %s", agent.name, str(e))
            return None

    def _get_agent_execution_errors(self) -> dict[str, str]:
        """Return the execution-error cache, initializing old test doubles lazily."""
        execution_errors = getattr(self, "_agent_execution_errors", None)
        if not isinstance(execution_errors, dict):
            execution_errors = {}
            self._agent_execution_errors = execution_errors
        return execution_errors

    # ------------------------------------------------------------------
    # Dirty flag helper
    # ------------------------------------------------------------------

    def _set_dirty(self, value: bool) -> None:
        """Set the dirty flag (used by composed managers)."""
        self._dirty = value

    # ------------------------------------------------------------------
    # Task Management (delegated to TaskBoardManager)
    # ------------------------------------------------------------------

    def add_task(self, content: str) -> Optional[SlockTask]:
        """Create a new task in the channel.

        Returns:
            SlockTask if successfully created.
            None if the channel has reached ``slock_max_open_tasks`` limit.

        Note:
            This is a breaking contract change — callers MUST handle the None
            return case (e.g. display a "team busy" card to the user) instead
            of unconditionally accessing task attributes.
        """
        return self._task_mgr.add_task(content)

    def claim_task(self, task_id: str, agent_id: str) -> bool:
        """Attempt to claim a task for an agent."""
        return self._task_mgr.claim_task(task_id, agent_id)

    def complete_task(self, task_id: str, agent_id: str) -> bool:
        """Mark a task as done."""
        return self._task_mgr.complete_task(task_id, agent_id)

    def execute_task(
        self,
        task_id: str,
        agent_id: str,
        callbacks: Optional[SlockEngineCallbacks] = None,
    ) -> Optional[str]:
        """Execute a task end-to-end: claim → execute → complete/rollback."""
        return self._task_mgr.execute_task(task_id, agent_id, callbacks)

    def _rollback_task(self, task_id: str, agent_id: str) -> None:
        """Rollback a task to TODO state and release its claim."""
        self._task_mgr._rollback_task(task_id, agent_id)

    def _persist_task_board(self) -> None:
        """Persist task state for the active channel."""
        self._task_mgr._persist_task_board()

    def _trim_done_tasks(self) -> None:
        """Remove oldest DONE tasks when exceeding cap."""
        self._task_mgr._trim_done_tasks()

    def _flush_if_dirty(self, snapshot: list[SlockTask]) -> None:
        """Persist task board from a snapshot if dirty flag is set."""
        self._task_mgr._flush_if_dirty(snapshot)

    # ------------------------------------------------------------------
    # Escalation Protocol (delegated to EscalationManager)
    # ------------------------------------------------------------------

    _MAX_ESCALATION_RETRIES = 3

    def escalate(
        self,
        agent: AgentIdentity,
        reason: str,
        *,
        level: EscalationLevel = EscalationLevel.BLOCKED,
        task_id: Optional[str] = None,
        context: str = "",
        options: Optional[list[str]] = None,
        callbacks: Optional["SlockEngineCallbacks"] = None,
    ) -> EscalationRequest:
        """Raise an escalation request."""
        return self._escalation_mgr.escalate(
            agent, reason, level=level, task_id=task_id,
            context=context, options=options, callbacks=callbacks,
        )

    def resolve_escalation(
        self,
        escalation_id: str,
        resolution: str,
    ) -> Optional[EscalationRequest]:
        """Resolve a pending escalation with the admin's decision."""
        return self._escalation_mgr.resolve_escalation(escalation_id, resolution)

    def get_escalation(self, escalation_id: str) -> Optional[EscalationRequest]:
        """Get an escalation by ID."""
        return self._escalation_mgr.get_escalation(escalation_id)

    def get_pending_escalations(self) -> list[EscalationRequest]:
        """Return all unresolved escalations."""
        return self._escalation_mgr.get_pending_escalations()

    def get_escalation_card(self, escalation: EscalationRequest) -> dict:
        """Build the interactive card for an escalation request."""
        return self._escalation_mgr.get_escalation_card(escalation)

    def set_escalation_ui_callbacks(
        self,
        update_card_fn: Callable[[str, str], bool],
        send_text_fn: Callable[[str, str], None],
    ) -> None:
        """Wire UI callbacks for escalation timeout auto-abort notifications."""
        self._escalation_mgr.set_ui_callbacks(update_card_fn, send_text_fn)

    def resume_after_escalation(
        self,
        escalation: EscalationRequest,
        callbacks: Optional[SlockEngineCallbacks] = None,
    ) -> Optional[str]:
        """Resume agent work after an escalation has been resolved."""
        return self._escalation_mgr.resume_after_escalation(escalation, callbacks)

    def _force_complete_task(self, task_id: str) -> None:
        """Force-mark a task as DONE regardless of claimer."""
        self._task_mgr.force_complete_task(task_id)

    def _trim_escalations(self) -> None:
        """Delegate escalation trimming to the escalation manager."""
        self._escalation_mgr._trim_escalations()

    # ------------------------------------------------------------------
    # Parallel Execution
    # ------------------------------------------------------------------

    def _get_executor(self) -> BoundedExecutor:
        """Lazy-initialize the bounded thread pool executor."""
        with self._executor_lock:
            if self._executor is None:
                settings = get_settings()
                self._executor = BoundedExecutor(
                    max_workers=settings.slock_max_parallel_agents,
                    max_queue_size=settings.slock_max_queue_size,
                )
            return self._executor

    def execute_parallel(
        self,
        task_assignments: list[tuple[str, str]],
        callbacks: Optional[SlockEngineCallbacks] = None,
        *,
        timeout: float = 300.0,
    ) -> dict[str, Optional[str]]:
        """Execute multiple tasks in parallel using ThreadPoolExecutor.

        Args:
            task_assignments: List of (task_id, agent_id) tuples to execute concurrently.
            callbacks: Engine lifecycle callbacks.
            timeout: Maximum wall-clock time for the batch (seconds).

        Returns:
            Dict mapping task_id → formatted result (or None on failure).
        """
        with self._lock:
            if self._run_state == EngineRunState.STOPPING:
                return {tid: None for tid, _ in task_assignments}
            self._run_state = EngineRunState.RUNNING

        executor = self._get_executor()
        futures: dict[Future, str] = {}  # future → task_id
        task_to_agent: dict[str, str] = {}  # task_id → agent_id for cancellation
        results: dict[str, Optional[str]] = {}

        try:
            for task_id, agent_id in task_assignments:
                task_to_agent[task_id] = agent_id
                try:
                    future = executor.submit(self.execute_task, task_id, agent_id, callbacks)
                    futures[future] = task_id
                except (QueueFullError, RuntimeError) as e:
                    logger.warning("Failed to submit task %s: %s", task_id, repr(e))
                    results[task_id] = None
                    if callbacks and callbacks.on_error:
                        callbacks.on_error(f"Task {task_id} rejected: {e}")

            for future in as_completed(futures, timeout=timeout):
                task_id = futures[future]
                try:
                    results[task_id] = future.result()
                except Exception as e:
                    logger.error("Parallel task %s failed: %s", task_id, repr(e))
                    results[task_id] = None
                    if callbacks and callbacks.on_error:
                        callbacks.on_error(f"Task {task_id} failed: {e}")

        except TimeoutError:
            logger.warning("Parallel execution timed out after %.1fs", timeout)
            # Cancel incomplete agents via cancellation events
            incomplete_futures = []
            for future, task_id in futures.items():
                if task_id not in results:
                    results[task_id] = None
                    incomplete_futures.append(future)
                    agent_id = task_to_agent.get(task_id)
                    if agent_id:
                        self.cancel_agent(agent_id)
            # Grace period: wait for cancelled agents to finish cleanup (event-driven)
            if incomplete_futures:
                futures_wait(incomplete_futures, timeout=5.0)
            if callbacks and callbacks.on_error:
                callbacks.on_error(f"Parallel execution timed out after {timeout}s")
        finally:
            with self._lock:
                if self._run_state == EngineRunState.RUNNING:
                    self._run_state = EngineRunState.IDLE

        return results

    def dispatch_pending_tasks(
        self,
        callbacks: Optional[SlockEngineCallbacks] = None,
        *,
        max_concurrent: Optional[int] = None,
    ) -> dict[str, Optional[str]]:
        """Auto-assign and execute all pending TODO tasks in parallel.

        For each TODO task, uses the TaskRouter to find the best available agent,
        then dispatches all assignments concurrently via execute_parallel.

        Args:
            callbacks: Engine lifecycle callbacks.
            max_concurrent: Override max parallel tasks (defaults to _max_parallel_agents).

        Returns:
            Dict mapping task_id → formatted result (or None on failure/skip).
        """
        channel_id = self._channel.channel_id if self._channel else self.chat_id
        agents = self._registry.list_agents(channel_id=channel_id)

        if not agents:
            return {}

        # Snapshot pending tasks under lock to avoid TOCTOU race
        with self._lock:
            pending = [t for t in self._tasks if t.status == TaskStatus.TODO]
        if not pending:
            return {}

        limit = max_concurrent or self._max_parallel_agents
        assignments: list[tuple[str, str]] = []
        assigned_agents: set[str] = set()

        for task in pending[:limit]:
            # Route task content to best available agent (not already assigned)
            available = [a for a in agents if a.agent_id not in assigned_agents]
            target = self._router.route_message(task.content, available)
            if not target:
                continue  # no idle agent available for this task
            assignments.append((task.task_id, target.agent_id))
            assigned_agents.add(target.agent_id)

        if not assignments:
            return {}

        return self.execute_parallel(assignments, callbacks)

    # ------------------------------------------------------------------
    # Status & Cleanup
    # ------------------------------------------------------------------

    def get_status_card(self, team_name: str = "") -> dict:
        """Build the status panel card for all agents in this channel."""
        channel_id = self._channel.channel_id if self._channel else self.chat_id
        agents = self._registry.list_agents(channel_id=channel_id)
        agent_statuses = [(a, self.get_agent_status(a.agent_id)) for a in agents]
        current_tasks = {
            task.claimed_by: task
            for task in self._tasks
            if task.claimed_by and task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)
        }
        return build_status_panel_card(
            agent_statuses,
            team_name=team_name,
            channel_id=channel_id,
            current_tasks=current_tasks,
        )

    def pause(self) -> None:
        """Pause the engine."""
        with self._lock:
            self._run_state = EngineRunState.STOPPING
            session = self._session  # snapshot under lock to avoid TOCTOU
            agent_sessions = list(self._agent_sessions.values())
            self._agent_sessions.clear()
        if session:
            try:
                session.cancel()
            except Exception:
                pass
        for agent_session in agent_sessions:
            try:
                agent_session.cancel()
            except Exception:
                pass

    def resume(self, callbacks: Optional[SlockEngineCallbacks] = None) -> None:
        """Resume the engine from paused state."""
        with self._lock:
            self._run_state = EngineRunState.IDLE

    def cleanup(self) -> None:
        """Clean up engine resources."""
        # Flush and stop observer learning queue
        self._observer_queue.shutdown()

        # Cancel escalation timeout timers
        self._escalation_mgr.shutdown_timers()

        # Shutdown thread pool executor
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

        # Shutdown discussion executor
        self._discussion_executor.shutdown(wait=False)

        # Reset all agent statuses
        with self._lock:
            for agent_id in list(self._agent_statuses.keys()):
                self._agent_statuses[agent_id] = AgentStatus.IDLE
            agent_sessions = list(self._agent_sessions.values())
            self._agent_sessions.clear()
        for agent_session in agent_sessions:
            try:
                agent_session.cancel()
            except Exception:
                pass
        super().cleanup()

    def deactivate(self) -> None:
        """Deactivate slock mode for this channel.

        Stops the engine, resets all agent statuses, and clears channel binding.
        After deactivation, the engine will refuse to execute new messages.
        """
        with self._lock:
            self._run_state = EngineRunState.STOPPING
            # Reset all agents to IDLE
            for agent_id in list(self._agent_statuses.keys()):
                self._agent_statuses[agent_id] = AgentStatus.IDLE
            self._channel = None
            session = self._session  # snapshot under lock to avoid TOCTOU
            agent_sessions = list(self._agent_sessions.values())
            self._agent_sessions.clear()

        # Cancel any running session using the snapshot
        if session:
            try:
                session.cancel()
            except Exception:
                pass
        for agent_session in agent_sessions:
            try:
                agent_session.cancel()
            except Exception:
                pass

        # Cancel escalation timeout timers
        self._escalation_mgr.shutdown_timers()

        # Flush and stop observer learning queue
        self._observer_queue.shutdown()

        # Release thread pool resources
        with self._executor_lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False)
                self._executor = None

        # Shutdown discussion executor
        self._discussion_executor.shutdown(wait=False)

        logger.info("SlockEngine deactivated for chat %s", self.chat_id)

    def stop_agent(self, agent_id: str) -> bool:
        """Stop a single agent: cancel its ACP session and reset status to IDLE.

        Returns True if the agent was found and stopped, False otherwise.
        Does not affect other agents or the engine's overall state.
        """
        with self._lock:
            if agent_id not in self._agent_statuses:
                return False
            self._agent_statuses[agent_id] = AgentStatus.IDLE
            agent_session = self._agent_sessions.pop(agent_id, None)

        # Signal cancellation to the executing thread
        self.cancel_agent(agent_id)

        if agent_session:
            try:
                agent_session.cancel()
            except Exception:
                pass

        logger.info("Stopped agent %s in chat %s", agent_id, self.chat_id)
        return True

    @property
    def is_active(self) -> bool:
        """Check if the engine is active (has a channel and is not stopping)."""
        with self._lock:
            return self._channel is not None and self._run_state != EngineRunState.STOPPING
