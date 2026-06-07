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
        self._lock = threading.Lock()
        # O(1) lookup: agent label -> AgentProgress reference.
        # All inserts, lookups and removals happen under ``_lock`` so that the
        # map stays consistent with the project.phases[*].agents lists even
        # under heavy parallel event delivery.
        self._label_to_agent: Dict[str, AgentProgress] = {}
        # Tracks the exact amount reserved per agent label so settle() releases
        # the correct amount even if the reservation constant changes.
        self._reservations: Dict[str, int] = {}
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

    def on_agent_started(self, label: str, tool: str, phase: str) -> None:
        """Add an agent entry to the current (or matching) phase."""
        with self._lock:
            target_phase = self._find_or_create_phase(phase)
            agent = AgentProgress(
                label=label,
                tool=tool,
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

    def add_token_usage(self, tokens: int) -> None:
        """Increment budget.used atomically."""
        with self._lock:
            self._project.budget.used += tokens

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

    def mark_budget_exceeded(self, consumed: int) -> None:
        """Sticky flag indicating the budget has been crossed.

        Callers use this to halt / prompt the user about topping up.
        The flag is idempotent: setting it multiple times is safe.
        """
        with self._lock:
            self._project.budget.exceeded = True
            # Ensure ``used`` at least reflects what the accumulator knows.
            self._project.budget.used = max(self._project.budget.used, consumed)

    def try_reserve(
        self,
        estimated: int,
        label: str,
        *,
        tool: str = "",
        phase: str = "default",
    ) -> bool:
        """Atomically reserve *estimated* tokens AND update agent state.

        This is a single-lock atomic operation that eliminates the TOCTOU
        race between budget check and state update. Both operations happen
        under the same ``_lock``, ensuring no concurrent call can slip
        through between the check and the state mutation.

        Args:
            estimated: Number of tokens to reserve.
            label: Agent label for on_agent_started state update.
            tool: Tool name for on_agent_started.
            phase: Phase name for on_agent_started.

        Returns:
            True if reservation succeeded (sufficient headroom),
            False if the budget would be exhausted.
        """
        with self._lock:
            budget = self._project.budget
            # 1. Budget check (atomic with reservation)
            if budget.used + budget.reserved + estimated > budget.total:
                return False
            budget.reserved += estimated
            self._reservations[label] = estimated

            # 2. State update (same lock — no race window)
            target_phase = self._find_or_create_phase(phase)
            from .models import AgentProgress, AgentStatus
            agent = AgentProgress(
                label=label,
                tool=tool,
                status=AgentStatus.RUNNING,
            )
            target_phase.agents.append(agent)
            # Atomically populate the fast-lookup map in the same lock (see
            # on_agent_started for rationale).
            self._label_to_agent[label] = agent
            self._project.metrics.total_agents += 1
            return True

    def settle(self, label: str, actual: int) -> None:
        """Settle a reservation: release the exact amount that was reserved.

        Actual token usage is tracked incrementally via add_token_usage()
        during execution (called from AgentExecutor's on_token_usage callback),
        so this method only releases the reservation headroom. The `actual`
        parameter is accepted for API completeness but not used for accounting.

        Must be called after every successful try_reserve(), regardless of
        whether the agent call succeeded.
        """
        with self._lock:
            budget = self._project.budget
            # Release the exact amount reserved for this label
            reserved_amount = self._reservations.pop(label, 0)
            budget.reserved = max(0, budget.reserved - reserved_amount)

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
        """Find existing phase by title or create a new one."""
        for phase in self._project.phases:
            if phase.title == phase_title:
                return phase
        new_phase = PhaseProgress(title=phase_title, started_at=time.time())
        self._project.phases.append(new_phase)
        return new_phase

    def _find_agent(self, label: str) -> Optional[AgentProgress]:
        """Find an agent by label across all phases (most recent first)."""
        for phase in reversed(self._project.phases):
            for agent in reversed(phase.agents):
                if agent.label == label:
                    return agent
        return None
