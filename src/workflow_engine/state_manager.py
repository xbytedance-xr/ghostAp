"""WorkflowStateManager — thread-safe state mutation for workflow execution.

Extracted from renderer.py to enforce the architectural rule that render stays
pure.  All state transitions go through this class under a single lock, and
the renderer receives immutable snapshots.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from .models import (
    AgentProgress,
    AgentStatus,
    PhaseProgress,
    WorkflowProject,
    WorkflowStatus,
)


class WorkflowStateManager:
    """Owns all mutations to a WorkflowProject during execution.

    Thread-safety: every public method acquires ``_lock`` before mutating.
    Callers that need a consistent view for rendering should call
    ``snapshot()`` which returns the project under the same lock.
    """

    def __init__(self, project: WorkflowProject) -> None:
        self._project = project
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        # O(1) lookup: agent label -> AgentProgress reference.
        # All inserts, lookups and removals happen under ``_lock`` so that the
        # map stays consistent with the project.phases[*].agents lists even
        # under heavy parallel event delivery.
        self._label_to_agent: Dict[str, AgentProgress] = {}
        # AC4: 主 context 增量 token 计数。仅由 engine 在明确需要向主 chat
        # 注入文本的路径上累加（目前唯一合法路径是 workflow 完成时的
        # project.result）。中间结果不得通过其他路径增加此计数器。
        self._delta_context_tokens: int = 0
        self._label_counters: Dict[str, int] = {}
        self._rebuild_indexes()

    # ------------------------------------------------------------------
    # Public: state-changing events
    # ------------------------------------------------------------------

    def on_phase_changed(self, title: str) -> None:
        """Record a new phase start."""
        with self._lock:
            phase = PhaseProgress(title=title, started_at=time.time())
            self._project.phases.append(phase)
            self._project.status = WorkflowStatus.RUNNING

    def on_agent_started(
        self, label: str, tool: str, phase: str, task_summary: str = ""
    ) -> str:
        """Add an agent entry to the current (or matching) phase.

        Returns the effective, UI-visible label. User-generated workflow
        scripts can accidentally reuse labels (for example several
        ``task-analysis`` calls in parallel). State transitions are keyed by
        label, so duplicate labels are disambiguated at the source.
        """
        with self._lock:
            target_phase = self._find_or_create_phase(phase)
            now = time.time()
            effective_label = self._make_unique_label(label or "agent")
            agent = AgentProgress(
                label=effective_label,
                tool=tool,
                task_summary=task_summary,
                status=AgentStatus.RUNNING,
                started_at=now,
            )
            target_phase.agents.append(agent)
            # Map insert must happen atomically with the agent-list append so
            # that concurrent callers of on_agent_done/on_agent_failed always
            # see a consistent view: if it is in `phases[*].agents` it is also
            # in `_label_to_agent`.
            self._label_to_agent[effective_label] = agent
            self._project.metrics.total_agents += 1
            return effective_label

    def on_agent_done(self, label: str, result: dict) -> None:
        """Update agent status to DONE with metrics from result."""
        with self._lock:
            agent = self._label_to_agent.get(label)
            if agent is None:
                # O(n) fallback: keeps backwards-compatible behaviour when an
                # agent was registered through a path that bypassed
                # _label_to_agent (e.g. a misbehaved caller). This branch also
                # acts as a canary for consistency bugs in the map itself.
                agent = self._find_agent(label)
                if agent is None:
                    return
                # Repair the map on hit so subsequent lookups stay O(1).
                self._label_to_agent[label] = agent

            token_usage = result.get("token_usage", 0)
            duration_s = result.get("duration_s", 0.0)
            cached = result.get("cached", False)
            was_terminal = self._is_terminal_agent(agent)
            was_cached = agent.status == AgentStatus.CACHED

            if cached:
                agent.status = AgentStatus.CACHED
                if not was_cached:
                    self._project.metrics.cached_agents += 1
            else:
                agent.status = AgentStatus.DONE

            agent.token_usage = token_usage
            agent.duration_s = duration_s
            agent.error = None
            agent.finished_at = time.time()

            if not was_terminal:
                self._project.metrics.completed_agents += 1
            self._project.metrics.total_tokens += token_usage
            self._project.metrics.total_duration_s += duration_s

    def on_agent_failed(self, label: str, error: str) -> None:
        """Update agent status to FAILED."""
        with self._lock:
            agent = self._label_to_agent.get(label)
            if agent is None:
                # Same O(n) fallback as on_agent_done.
                agent = self._find_agent(label)
                if agent is None:
                    return
                self._label_to_agent[label] = agent

            was_terminal = self._is_terminal_agent(agent)
            was_failed = agent.status == AgentStatus.FAILED
            now = time.time()
            agent.status = AgentStatus.FAILED
            agent.error = error
            agent.finished_at = now
            if agent.duration_s <= 0 and agent.started_at:
                agent.duration_s = max(0.0, now - agent.started_at)
            if not was_failed:
                self._project.metrics.failed_agents += 1
            if not was_terminal:
                self._project.metrics.completed_agents += 1

    def on_workflow_done(self, result: str) -> None:
        """Mark workflow as completed."""
        with self._lock:
            self._project.status = WorkflowStatus.COMPLETED
            self._project.result = result
            self._project.finished_at = time.time()
            # Close last phase
            if self._project.phases:
                last_phase = self._project.phases[-1]
                if last_phase.finished_at is None:
                    last_phase.finished_at = time.time()
            self._project.metrics.phases_completed = len(self._project.phases)

    def on_workflow_failed(self, error: str) -> None:
        """Mark workflow as failed."""
        with self._lock:
            now = time.time()
            self._project.status = WorkflowStatus.FAILED
            self._project.error = error
            self._project.finished_at = now
            self._close_open_agents(
                error=f"Workflow failed before agent completed: {error}",
                finished_at=now,
            )
            for phase in self._project.phases:
                if phase.finished_at is None:
                    phase.finished_at = now

    def on_workflow_cancelled(self, reason: str = "Workflow cancelled") -> None:
        """Mark workflow as cancelled without rewriting it as a failure."""
        with self._lock:
            now = time.time()
            self._project.status = WorkflowStatus.CANCELLED
            self._project.error = reason
            self._project.finished_at = now
            self._close_open_agents(error=reason, finished_at=now)
            for phase in self._project.phases:
                if phase.finished_at is None:
                    phase.finished_at = now

    def add_context_tokens(self, tokens: int) -> None:
        """Increment the main-context token counter (AC4 isolation).

        This counter is *not* a budget gate — it is an audit counter that
        tracks how many tokens-worth of text the workflow intends to inject
        into the main agent chat context.  Only the engine's final-result
        path should call this method; any other path inflating it is a
        regression of the AC4 isolation guarantee.
        """
        with self._lock:
            self._delta_context_tokens += max(0, int(tokens))

    @property
    def delta_context_tokens(self) -> int:
        """Observed main-context token delta for the current workflow run."""
        with self._lock:
            return self._delta_context_tokens

    # ------------------------------------------------------------------
    # Public: snapshot for read-only consumption
    # ------------------------------------------------------------------

    def snapshot(self) -> WorkflowProject:
        """Return a deep copy of the project state for read-only consumption.

        The copy is created under lock, guaranteeing a consistent point-in-time
        view. Callers (e.g. renderer) can safely read the returned object
        without risk of concurrent mutation.
        """
        with self._lock:
            return self._project.model_copy(deep=True)

    @property
    def project(self) -> WorkflowProject:
        """Direct access (for backward compat); prefer snapshot()."""
        return self._project

    # ------------------------------------------------------------------
    # Private helpers (called under lock)
    # ------------------------------------------------------------------

    def _find_or_create_phase(self, phase_title: str) -> PhaseProgress:
        """Find existing phase by title, or fall back to the last active phase.

        When an agent() call omits the phase field (resolved as "default"),
        assign it to the most recently created phase rather than creating a
        spurious "default" phase.
        """
        if phase_title and phase_title != "default":
            for phase in self._project.phases:
                if phase.title == phase_title:
                    return phase
        elif self._project.phases:
            return self._project.phases[-1]
        new_phase = PhaseProgress(title=phase_title or "default", started_at=time.time())
        self._project.phases.append(new_phase)
        return new_phase

    def _find_agent(self, label: str) -> Optional[AgentProgress]:
        """Find an agent by label across all phases (most recent first)."""
        for phase in reversed(self._project.phases):
            for agent in reversed(phase.agents):
                if agent.label == label:
                    return agent
        return None

    def _rebuild_indexes(self) -> None:
        """Build fast lookup indexes from an existing project snapshot."""
        for phase in self._project.phases:
            for agent in phase.agents:
                if not agent.label:
                    continue
                self._label_to_agent[agent.label] = agent

    def _make_unique_label(self, requested: str) -> str:
        """Return a label not already present in the current project."""
        base = requested.strip() or "agent"
        if base not in self._label_to_agent:
            self._label_counters.setdefault(base, 1)
            return base

        next_index = self._label_counters.get(base, 1) + 1
        while True:
            candidate = f"{base} #{next_index}"
            if candidate not in self._label_to_agent:
                self._label_counters[base] = next_index
                return candidate
            next_index += 1

    @staticmethod
    def _is_terminal_agent(agent: AgentProgress) -> bool:
        return agent.status in (
            AgentStatus.DONE,
            AgentStatus.FAILED,
            AgentStatus.CACHED,
        )

    def _close_open_agents(self, *, error: str, finished_at: float) -> None:
        """Move non-terminal agents out of RUNNING/PENDING for terminal cards."""
        for phase in self._project.phases:
            for agent in phase.agents:
                if self._is_terminal_agent(agent):
                    continue
                agent.status = AgentStatus.FAILED
                agent.error = error
                agent.finished_at = finished_at
                if agent.duration_s <= 0 and agent.started_at:
                    agent.duration_s = max(0.0, finished_at - agent.started_at)
                self._project.metrics.failed_agents += 1
                self._project.metrics.completed_agents += 1
