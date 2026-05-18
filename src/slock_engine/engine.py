"""SlockEngine — Multi-Agent collaboration engine (mouthpiece mode).

Inherits BaseEngine lifecycle and integrates AgentRegistry, MemoryManager,
TaskRouter, and Mouthpiece for orchestrating virtual agent teams.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..agent_session import close_session_safely, create_engine_session
from ..engine_base import BaseEngine, EngineRunState
from .agent_registry import AgentRegistry
from .card_templates import build_escalation_card, build_status_panel_card
from .memory_manager import MemoryManager, default_slock_storage_base
from .models import (
    AgentIdentity,
    AgentStatus,
    EscalationLevel,
    EscalationRequest,
    SlockChannel,
    SlockTask,
    TaskStatus,
)
from .mouthpiece import Mouthpiece
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
    on_error: Optional[Callable[[str], None]] = None


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
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": " | ".join(stats_parts)}],
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


class SlockEngine(BaseEngine):
    """Multi-Agent collaboration engine using mouthpiece pattern.

    Manages a team of virtual agents within a single Feishu group,
    routing messages, managing tasks, and formatting output through
    the mouthpiece mechanism.
    """

    _state_filename = ".slock_engine_state.json"
    _gc_label = "Slock"
    _gc_threshold_default = 85.0

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
        self._router = TaskRouter(persist_path=claims_path)
        self._mouthpiece = Mouthpiece()

        # Thread pool for parallel agent execution
        self._executor: Optional[ThreadPoolExecutor] = None
        self._max_parallel_agents = 4

        # Channel state
        self._channel: Optional[SlockChannel] = None
        self._tasks: list[SlockTask] = []
        self._agent_statuses: dict[str, AgentStatus] = {}
        self._agent_sessions: dict[str, object] = {}
        self._escalations: list[EscalationRequest] = []

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
    # Agent Status Machine
    # ------------------------------------------------------------------

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
        """
        valid_transitions: dict[AgentStatus, list[AgentStatus]] = {
            AgentStatus.IDLE: [AgentStatus.WAKING],
            AgentStatus.WAKING: [AgentStatus.THINKING, AgentStatus.IDLE],
            AgentStatus.THINKING: [AgentStatus.RUNNING, AgentStatus.IDLE],
            AgentStatus.RUNNING: [AgentStatus.CHECKING, AgentStatus.IDLE],
            AgentStatus.CHECKING: [AgentStatus.SENDING, AgentStatus.RUNNING, AgentStatus.IDLE],
            AgentStatus.SENDING: [AgentStatus.IDLE],
        }

        current = self.get_agent_status(agent_id)
        if to_status in valid_transitions.get(current, []):
            self.set_agent_status(agent_id, to_status)
            return True

        logger.warning(
            "Invalid agent transition: %s -> %s (agent=%s)",
            current.value, to_status.value, agent_id,
        )
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
            "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        # Canonical app-level group marker under ~/.ghostap/slock/groups.
        canonical_dir = self._memory.get_group_base_path(channel.channel_id)
        os.makedirs(canonical_dir, exist_ok=True)
        canonical_marker = os.path.join(canonical_dir, ".slock_channel.json")
        self._write_channel_marker(canonical_marker, marker_data)

    @staticmethod
    def _write_channel_marker(marker_path: str, marker_data: dict) -> None:
        """Write a channel marker atomically if it does not already exist."""
        if os.path.exists(marker_path):
            return
        import json as _json

        tmp_path = marker_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            _json.dump(marker_data, f, ensure_ascii=False, indent=2)
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
            self._sync_skill_profiles(agents)

            if not agents:
                with self._lock:
                    self._run_state = EngineRunState.IDLE
                return None

            # Route message to agent
            target_agent = self._router.route_message(message, agents)
            if not target_agent:
                target_agent = agents[0]  # fallback to first agent

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

        self._memory.append_message_archive(
            channel_id,
            sender_type="user",
            content=message,
            agent_id=agent_id,
            agent_name=agent.name,
            metadata={"routed_to": agent_id},
        )

        # IDLE → WAKING
        self.transition_agent(agent_id, AgentStatus.WAKING)
        if callbacks and callbacks.on_agent_wake:
            callbacks.on_agent_wake(agent)

        # Load agent memory context
        memory = self._memory.read_agent_memory(agent_id)

        # WAKING → THINKING
        self.transition_agent(agent_id, AgentStatus.THINKING)
        if callbacks and callbacks.on_agent_thinking:
            callbacks.on_agent_thinking(agent)

        # Build prompt with memory context and system prompt
        prompt = self._build_agent_prompt(agent, message, memory)

        # THINKING → RUNNING
        self.transition_agent(agent_id, AgentStatus.RUNNING)
        if callbacks and callbacks.on_agent_running:
            callbacks.on_agent_running(agent, message)

        # Execute via ACP session
        result = self._run_acp_session(agent, prompt)

        # RUNNING → CHECKING
        self.transition_agent(agent_id, AgentStatus.CHECKING)

        # CHECKING → SENDING
        self.transition_agent(agent_id, AgentStatus.SENDING)

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
        self.transition_agent(agent_id, AgentStatus.IDLE)

        # Update agent memory with new context
        if result:
            context_entry = f"[{time.strftime('%Y-%m-%d %H:%M')}] Responded to: {message[:100]}"
            self._memory.update_agent_context(agent_id, context_entry)
            skill_tags = self._router.extract_skill_keywords(message)
            profiles = self._memory.record_skill_feedback(agent_id, skill_tags, quality_score=100.0)
            self._router.set_skill_profiles(agent_id, profiles)
            self._record_observer_learning(agent, message, skill_tags)

        return formatted

    def _sync_skill_profiles(self, agents: list[AgentIdentity]) -> None:
        """Load persisted skill profiles into the router before assignment."""
        for agent in agents:
            profiles = self._memory.read_skill_profiles(agent.agent_id)
            if profiles:
                self._router.set_skill_profiles(agent.agent_id, profiles)

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
            profiles = self._memory.record_skill_feedback(
                observer.agent_id,
                skill_tags,
                quality_score=60.0,
            )
            self._router.set_skill_profiles(observer.agent_id, profiles)
            context_entry = (
                f"[{time.strftime('%Y-%m-%d %H:%M')}] "
                f"Observed {actor.agent_id} complete: {message[:100]}"
            )
            self._memory.update_agent_context(observer.agent_id, context_entry)

    def _build_agent_prompt(self, agent: AgentIdentity, message: str, memory) -> str:
        """Build the full prompt for an agent including system prompt and memory."""
        parts: list[str] = []

        if agent.system_prompt:
            parts.append(agent.system_prompt)

        if memory.role:
            parts.append(f"\n# Your Role\n{memory.role}")

        if memory.key_knowledge:
            parts.append(f"\n# Key Knowledge\n{memory.key_knowledge}")

        if memory.active_context:
            parts.append(f"\n# Recent Context\n{memory.active_context[-2000:]}")

        parts.append(f"\n# User Message\n{message}")

        return "\n".join(parts)

    def _run_acp_session(self, agent: AgentIdentity, prompt: str) -> Optional[str]:
        """Run an ACP session for the agent. Returns response text or None."""
        try:
            thread_id = f"slock_agent_{agent.agent_id}"
            session = create_engine_session(
                agent_type=agent.agent_type,
                cwd=self.root_path,
                model_name=agent.model_name or None,
                thread_id=thread_id,
                auto_approve=True,  # Human interaction suppression
            )
            if session is None:
                logger.warning("Failed to create ACP session for agent %s", agent.name)
                return None

            with self._lock:
                self._agent_sessions[agent.agent_id] = session
            try:
                result = session.send_prompt(prompt, timeout=self.settings.coco_execution_timeout)
                return result.text if result else None
            finally:
                with self._lock:
                    if self._agent_sessions.get(agent.agent_id) is session:
                        del self._agent_sessions[agent.agent_id]
                close_session_safely(session)

        except Exception as e:
            logger.error("ACP session error for agent %s: %s", agent.name, str(e))
            return None

    # ------------------------------------------------------------------
    # Task Management
    # ------------------------------------------------------------------

    def add_task(self, content: str) -> SlockTask:
        """Create a new task in the channel."""
        task = SlockTask(
            content=content,
            created_in=self._channel.channel_id if self._channel else self.chat_id,
        )
        self._tasks.append(task)
        self._persist_task_board()
        return task

    def claim_task(self, task_id: str, agent_id: str) -> bool:
        """Attempt to claim a task for an agent."""
        if not self._router.task_claim.claim(task_id, agent_id):
            return False

        # Update task status
        for task in self._tasks:
            if task.task_id == task_id:
                task.status = TaskStatus.IN_PROGRESS
                task.claimed_by = agent_id
                task.claimed_at = time.time()
                self._persist_task_board()
                return True
        return False

    def complete_task(self, task_id: str, agent_id: str) -> bool:
        """Mark a task as done."""
        for task in self._tasks:
            if task.task_id == task_id and task.claimed_by == agent_id:
                task.status = TaskStatus.DONE
                self._router.task_claim.release(task_id, agent_id)
                self._persist_task_board()
                return True
        return False

    def execute_task(
        self,
        task_id: str,
        agent_id: str,
        callbacks: Optional[SlockEngineCallbacks] = None,
    ) -> Optional[str]:
        """Execute a task end-to-end: claim → execute → complete/rollback.

        Returns the formatted agent output on success, or None on failure.
        On failure, the task is rolled back to TODO and the claim is released.
        """
        # Find the task
        task = None
        for t in self._tasks:
            if t.task_id == task_id:
                task = t
                break
        if task is None:
            return None

        agent = self._registry.get(agent_id)
        if agent is None:
            return None

        # Claim (may already be claimed by assign_task caller)
        if task.claimed_by != agent_id:
            if not self.claim_task(task_id, agent_id):
                return None

        # Execute agent with the task content as message
        try:
            result = self._execute_agent(agent, task.content, callbacks)
            if result:
                self.complete_task(task_id, agent_id)
                return result
            else:
                # Execution produced no output — rollback
                self._rollback_task(task_id, agent_id)
                return None
        except Exception as e:
            logger.error("execute_task failed for task %s agent %s: %s", task_id, agent_id, repr(e))
            self._rollback_task(task_id, agent_id)
            raise

    def _rollback_task(self, task_id: str, agent_id: str) -> None:
        """Rollback a task to TODO state and release its claim."""
        for task in self._tasks:
            if task.task_id == task_id:
                task.status = TaskStatus.TODO
                task.claimed_by = None
                task.claimed_at = None
                break
        self._router.task_claim.release(task_id, agent_id)
        self._persist_task_board()

    def _persist_task_board(self) -> None:
        """Persist task state for the active channel."""
        channel_id = self._channel.channel_id if self._channel else self.chat_id
        self._memory.write_task_board(channel_id, self._tasks)

    # ------------------------------------------------------------------
    # Escalation Protocol
    # ------------------------------------------------------------------

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
        """Raise an escalation request — pauses the agent and requests admin decision.

        The agent is transitioned to IDLE (paused) and the escalation is stored.
        Returns the EscalationRequest for the handler to send as a card.

        Args:
            agent: The agent requesting escalation.
            reason: Human-readable description of the blocker.
            level: Severity level (WARNING, BLOCKED, CRITICAL).
            task_id: Optional task being worked on.
            context: Additional context (error details, conversation snippet).
            options: Resolution choices for the admin (defaults to Retry/Skip/Abort).
            callbacks: Engine callbacks for error reporting.
        """
        escalation = EscalationRequest(
            agent_id=agent.agent_id,
            agent_name=agent.name,
            task_id=task_id,
            level=level,
            reason=reason,
            context=context[:2000],  # Truncate to prevent oversized cards
            options=options or ["Retry", "Skip", "Abort"],
        )

        with self._lock:
            self._escalations.append(escalation)

        # Pause the agent
        self.transition_agent(agent.agent_id, AgentStatus.IDLE)

        logger.warning(
            "Escalation raised: agent=%s level=%s reason=%s",
            agent.name, level.value, reason[:100],
        )

        if callbacks and callbacks.on_error:
            callbacks.on_error(f"Escalation [{level.value}] from {agent.name}: {reason}")

        return escalation

    def resolve_escalation(
        self,
        escalation_id: str,
        resolution: str,
    ) -> Optional[EscalationRequest]:
        """Resolve a pending escalation with the admin's decision.

        Returns the resolved EscalationRequest, or None if not found.
        """
        with self._lock:
            for esc in self._escalations:
                if esc.escalation_id == escalation_id and not esc.resolved:
                    esc.resolved = True
                    esc.resolution = resolution
                    esc.resolved_at = time.time()
                    logger.info(
                        "Escalation resolved: id=%s resolution=%s",
                        escalation_id, resolution,
                    )
                    return esc
        return None

    def get_pending_escalations(self) -> list[EscalationRequest]:
        """Return all unresolved escalations."""
        with self._lock:
            return [e for e in self._escalations if not e.resolved]

    def get_escalation_card(self, escalation: EscalationRequest) -> dict:
        """Build the interactive card for an escalation request."""
        channel_id = self._channel.channel_id if self._channel else self.chat_id
        return build_escalation_card(escalation, channel_id=channel_id)

    # ------------------------------------------------------------------
    # Parallel Execution
    # ------------------------------------------------------------------

    def _get_executor(self) -> ThreadPoolExecutor:
        """Lazy-initialize the thread pool executor."""
        if self._executor is None or self._executor._shutdown:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_parallel_agents,
                thread_name_prefix="slock-agent",
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
        results: dict[str, Optional[str]] = {}

        try:
            for task_id, agent_id in task_assignments:
                future = executor.submit(self.execute_task, task_id, agent_id, callbacks)
                futures[future] = task_id

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
            # Collect whatever completed and mark the rest as None
            for future, task_id in futures.items():
                if task_id not in results:
                    results[task_id] = None
                    future.cancel()
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
        self._sync_skill_profiles(agents)

        if not agents:
            return {}

        # Gather TODO tasks and assign agents
        pending = [t for t in self._tasks if t.status == TaskStatus.TODO]
        if not pending:
            return {}

        limit = max_concurrent or self._max_parallel_agents
        assignments: list[tuple[str, str]] = []

        for task in pending[:limit]:
            # Route task content to best agent
            target = self._router.route_message(task.content, agents)
            if not target:
                target = agents[0]
            assignments.append((task.task_id, target.agent_id))

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
        return build_status_panel_card(agent_statuses, team_name=team_name, channel_id=channel_id)

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
        # Shutdown thread pool executor
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

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

        logger.info("SlockEngine deactivated for chat %s", self.chat_id)

    @property
    def is_active(self) -> bool:
        """Check if the engine is active (has a channel and is not stopping)."""
        with self._lock:
            return self._channel is not None and self._run_state != EngineRunState.STOPPING
