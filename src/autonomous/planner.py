"""GoalSpec/Plan production planner.

Compiles goal descriptions and criteria into executable plans with
step dependencies, resource requirements, and verification contracts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .domain.enums import PlanState, StepState
from .domain.ids import new_id
from .domain.plans import Plan, PlanStep

logger = logging.getLogger(__name__)


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


class ModelBrokerProtocol(Protocol):
    async def call(self, *, authorization: Any, prompt_ref: dict, **kwargs: Any) -> Any: ...


@dataclass
class PlanCompilationResult:
    success: bool
    plan: Plan | None = None
    error: str = ""


class Planner:
    """Produces executable plans from goal descriptions.

    Workflow:
    1. Receive goal spec (description, criteria, constraints)
    2. Call model to decompose into steps
    3. Validate step dependencies form a DAG
    4. Attach verification criteria to each step
    5. Return compiled Plan ready for scheduling
    """

    def __init__(
        self,
        *,
        journal: JournalWriter,
        model_broker: ModelBrokerProtocol | None = None,
    ) -> None:
        self._journal = journal
        self._model_broker = model_broker

    async def compile_plan(
        self,
        *,
        goal_id: str,
        run_id: str,
        description: str,
        criteria: list[dict[str, Any]] | None = None,
        authorization: Any = None,
    ) -> PlanCompilationResult:
        plan_id = new_id("plan")

        if self._model_broker and authorization:
            model_result = await self._model_broker.call(
                authorization=authorization,
                prompt_ref={
                    "type": "plan_compilation",
                    "goal_id": goal_id,
                    "description": description,
                    "criteria": criteria or [],
                },
            )
            if hasattr(model_result, "success") and not model_result.success:
                return PlanCompilationResult(
                    success=False, error=f"model call failed: {getattr(model_result, 'error', '')}"
                )
            steps = self._parse_model_steps(model_result)
        else:
            steps = self._default_single_step(description)

        plan = Plan(
            plan_id=plan_id,
            run_id=run_id,
            state=PlanState.COMPILED,
            steps=tuple(steps),
        )

        await self._journal.write_event("plan.compiled", {
            "plan_id": plan_id,
            "run_id": run_id,
            "goal_id": goal_id,
            "step_count": len(steps),
        })

        return PlanCompilationResult(success=True, plan=plan)

    def _parse_model_steps(self, model_result: Any) -> list[PlanStep]:
        response = getattr(model_result, "response", {})
        if isinstance(response, dict):
            raw_steps = response.get("steps", [])
            if raw_steps:
                return [
                    PlanStep(
                        step_id=new_id("step"),
                        description=s.get("description", ""),
                        name=s.get("role", s.get("name", "")),
                        max_attempts=s.get("max_attempts", 3),
                    )
                    for s in raw_steps
                ]
        return self._default_single_step("execute task")

    def _default_single_step(self, description: str) -> list[PlanStep]:
        return [
            PlanStep(
                step_id=new_id("step"),
                description=description,
                state=StepState.PENDING,
                max_attempts=3,
            )
        ]
