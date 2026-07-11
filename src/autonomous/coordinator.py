"""Lifecycle orchestration — coordinates goal/run/plan state transitions.

Sits between Manager commands and the durable kernel (journal, scheduler,
policy, dispatch). Ensures all state changes flow through the journal
and respects the effective-autonomy contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .domain.enums import GoalState, GoalType, RunState
from .domain.goals import GoalDefinition, GoalSpec, Run
from .domain.ids import new_id

logger = logging.getLogger(__name__)


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


class PolicyGate(Protocol):
    def check_activation(self, principal_id: str, goal_spec: dict) -> bool: ...


@dataclass
class GoalActivationResult:
    success: bool
    goal_id: str = ""
    run_id: str = ""
    error: str = ""


class Coordinator:
    """Orchestrates goal/run lifecycle transitions.

    Responsible for:
    - Goal creation and activation
    - Run lifecycle (start, pause, cancel, replan)
    - Plan compilation delegation
    - Verification and acceptance flow
    """

    def __init__(
        self,
        *,
        journal: JournalWriter,
        policy_gate: PolicyGate | None = None,
    ) -> None:
        self._journal = journal
        self._policy = policy_gate
        self._goals: dict[str, GoalDefinition] = {}
        self._runs: dict[str, Run] = {}

    async def create_goal(
        self,
        *,
        description: str,
        goal_type: GoalType = GoalType.ONE_SHOT,
        owner_principal_id: str,
        tenant_key: str,
        criteria: list[dict[str, Any]] | None = None,
    ) -> GoalActivationResult:
        if self._policy and not self._policy.check_activation(
            owner_principal_id, {"description": description}
        ):
            return GoalActivationResult(success=False, error="activation denied by policy")

        goal_id = new_id("goal")
        goal = GoalDefinition(
            goal_id=goal_id,
            goal_type=goal_type,
            owner_principal_id=owner_principal_id,
            tenant_key=tenant_key,
            spec=GoalSpec(objective=description),
            state=GoalState.ACTIVE,
        )
        self._goals[goal_id] = goal

        run_id = new_id("run")
        run = Run(run_id=run_id, goal_id=goal_id, state=RunState.RECEIVED)
        self._runs[run_id] = run

        await self._journal.write_event("goal.created", {
            "goal_id": goal_id,
            "goal_type": goal_type.value,
            "owner_principal_id": owner_principal_id,
            "tenant_key": tenant_key,
            "description": description,
        })
        await self._journal.write_event("run.created", {
            "run_id": run_id,
            "goal_id": goal_id,
        })

        return GoalActivationResult(success=True, goal_id=goal_id, run_id=run_id)

    async def pause_goal(self, goal_id: str, principal_id: str) -> bool:
        goal = self._goals.get(goal_id)
        if not goal or goal.state is not GoalState.ACTIVE:
            return False
        self._goals[goal_id] = replace(goal, state=GoalState.PAUSED)
        await self._journal.write_event("goal.paused", {
            "goal_id": goal_id, "principal_id": principal_id
        })
        return True

    async def resume_goal(self, goal_id: str, principal_id: str) -> bool:
        goal = self._goals.get(goal_id)
        if not goal or goal.state is not GoalState.PAUSED:
            return False
        self._goals[goal_id] = replace(goal, state=GoalState.ACTIVE)
        await self._journal.write_event("goal.resumed", {
            "goal_id": goal_id, "principal_id": principal_id
        })
        return True

    async def cancel_goal(self, goal_id: str, principal_id: str) -> bool:
        goal = self._goals.get(goal_id)
        if not goal or goal.state in (GoalState.CANCELED, GoalState.EXPIRED):
            return False
        self._goals[goal_id] = replace(goal, state=GoalState.CANCELED)
        await self._journal.write_event("goal.canceled", {
            "goal_id": goal_id, "principal_id": principal_id
        })
        return True

    async def list_goals(self, tenant_key: str) -> list[dict[str, Any]]:
        return [
            {"goal_id": g.goal_id, "state": g.state.value, "description": g.spec.objective}
            for g in self._goals.values()
            if g.tenant_key == tenant_key
        ]

    async def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        goal = self._goals.get(goal_id)
        if not goal:
            return None
        return {"goal_id": goal.goal_id, "state": goal.state.value, "description": goal.spec.objective}

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)
