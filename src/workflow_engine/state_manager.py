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

    # ------------------------------------------------------------------
    # Public: state-changing events
    # ------------------------------------------------------------------

    def on_phase_changed(self, title: str) -> None:
        """Record a new phase start."""
        with self._lock:
            phase = PhaseProgress(title=title, started_at=time.time())
            self._project.phases.append(phase)
            self._project.status = WorkflowStatus.RUNNING

    def on_agent_started(self, label: str, tool: str, phase: str, task_summary: str = "") -> None:
        """Add an agent entry to the current (or matching) phase."""
        with self._lock:
            target_phase = self._find_or_create_phase(phase)
            agent = AgentProgress(
                label=label,
                tool=tool,
                task_summary=task_summary,
                status=AgentStatus.RUNNING,
            )
            target_phase.agents.append(agent)
            # Map insert must happen atomically with the agent-list append so
            # that concurrent callers of on_agent_done/on_agent_failed always
            # see a consistent view: if it is in `phases[*].agents` it is also
            # in `_label_to_agent`.
            self._label_to_agent[label] = agent
            self._project.metrics.total_agents += 1

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

            if cached:
                agent.status = AgentStatus.CACHED
                self._project.metrics.cached_agents += 1
            else:
                agent.status = AgentStatus.DONE

            agent.token_usage = token_usage
            agent.duration_s = duration_s
            agent.error = None

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

            agent.status = AgentStatus.FAILED
            agent.error = error
            self._project.metrics.failed_agents += 1
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
            self._project.status = WorkflowStatus.FAILED
            self._project.error = error
            self._project.finished_at = time.time()

    def on_workflow_cancelled(self, reason: str = "Workflow cancelled") -> None:
        """Mark workflow as cancelled without rewriting it as a failure."""
        with self._lock:
            self._project.status = WorkflowStatus.CANCELLED
            self._project.error = reason
            self._project.finished_at = time.time()
            if self._project.phases:
                last_phase = self._project.phases[-1]
                if last_phase.finished_at is None:
                    last_phase.finished_at = time.time()

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
