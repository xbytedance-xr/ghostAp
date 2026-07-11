"""Plan, step, attempt, and criteria-coverage aggregates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .enums import AttemptState, PlanState, StepState
from .ids import freeze, new_id, strict_float, strict_int, strict_str, thaw


@dataclass(frozen=True)
class PlanStep:
    step_id: str = field(default_factory=lambda: new_id("step"))
    name: str = ""
    description: str = ""
    state: StepState = StepState.PENDING
    depends_on: tuple[str, ...] = ()
    capability: str = ""
    capability_version: str = ""
    arguments_schema: Any = field(default_factory=dict)
    principal_policy: Any = field(default_factory=dict)
    resource_key: str = ""
    verifier_oracle: Any = None
    compensation: str = ""
    assigned_employee: str = ""
    max_attempts: int = 3
    timeout_seconds: float = 600.0
    criterion_ids: tuple[str, ...] = ()
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "criterion_ids", tuple(self.criterion_ids))
        object.__setattr__(self, "arguments_schema", freeze(self.arguments_schema))
        object.__setattr__(self, "principal_policy", freeze(self.principal_policy))
        object.__setattr__(
            self,
            "verifier_oracle",
            None if self.verifier_oracle is None else freeze(self.verifier_oracle),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "description": self.description,
            "state": self.state.value,
            "depends_on": list(self.depends_on),
            "capability": self.capability,
            "capability_version": self.capability_version,
            "arguments_schema": thaw(self.arguments_schema),
            "principal_policy": thaw(self.principal_policy),
            "resource_key": self.resource_key,
            "verifier_oracle": (
                None
                if self.verifier_oracle is None
                else thaw(self.verifier_oracle)
            ),
            "compensation": self.compensation,
            "assigned_employee": self.assigned_employee,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "criterion_ids": list(self.criterion_ids),
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStep:
        return cls(
            step_id=strict_str(data["step_id"], "step_id"),
            name=strict_str(data.get("name", ""), "name"),
            description=strict_str(
                data.get("description", ""),
                "description",
            ),
            state=StepState(data.get("state", StepState.PENDING.value)),
            depends_on=tuple(data.get("depends_on", ())),
            capability=strict_str(
                data.get("capability", ""),
                "capability",
            ),
            capability_version=strict_str(
                data.get("capability_version", ""),
                "capability_version",
            ),
            arguments_schema=data.get("arguments_schema", {}),
            principal_policy=data.get("principal_policy", {}),
            resource_key=strict_str(
                data.get("resource_key", ""),
                "resource_key",
            ),
            verifier_oracle=data.get("verifier_oracle"),
            compensation=strict_str(
                data.get("compensation", ""),
                "compensation",
            ),
            assigned_employee=strict_str(
                data.get("assigned_employee", ""),
                "assigned_employee",
            ),
            max_attempts=strict_int(
                data.get("max_attempts", 3),
                "max_attempts",
                minimum=1,
            ),
            timeout_seconds=strict_float(
                data.get("timeout_seconds", 600.0),
                "timeout_seconds",
            ),
            criterion_ids=tuple(data.get("criterion_ids", ())),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class GoalCriteriaCoverage:
    mappings: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = {
            str(criterion_id): tuple(step_ids)
            for criterion_id, step_ids in dict(self.mappings).items()
        }
        object.__setattr__(self, "mappings", freeze(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {"mappings": thaw(self.mappings)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalCriteriaCoverage:
        return cls(mappings=data.get("mappings", {}))


@dataclass(frozen=True)
class Plan:
    plan_id: str = field(default_factory=lambda: new_id("plan"))
    run_id: str = ""
    tenant_key: str = ""
    state: PlanState = PlanState.DRAFT
    epoch: int = 1
    steps: tuple[PlanStep, ...] = ()
    criteria_coverage: GoalCriteriaCoverage = field(
        default_factory=GoalCriteriaCoverage
    )
    budget_estimate: Any = None
    authorization_id: str = ""
    parent_authorization_id: str = ""
    created_at: float = field(default_factory=time.time)
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(
            self,
            "budget_estimate",
            None if self.budget_estimate is None else freeze(self.budget_estimate),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "tenant_key": self.tenant_key,
            "state": self.state.value,
            "epoch": self.epoch,
            "steps": [step.to_dict() for step in self.steps],
            "criteria_coverage": self.criteria_coverage.to_dict(),
            "budget_estimate": (
                None
                if self.budget_estimate is None
                else thaw(self.budget_estimate)
            ),
            "authorization_id": self.authorization_id,
            "parent_authorization_id": self.parent_authorization_id,
            "created_at": self.created_at,
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(
            plan_id=strict_str(data["plan_id"], "plan_id"),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            state=PlanState(data.get("state", PlanState.DRAFT.value)),
            epoch=strict_int(data.get("epoch", 1), "epoch", minimum=1),
            steps=tuple(
                PlanStep.from_dict(value) for value in data.get("steps", ())
            ),
            criteria_coverage=GoalCriteriaCoverage.from_dict(
                data.get("criteria_coverage", {})
            ),
            budget_estimate=data.get("budget_estimate"),
            authorization_id=strict_str(
                data.get("authorization_id", ""), "authorization_id"
            ),
            parent_authorization_id=strict_str(
                data.get("parent_authorization_id", ""),
                "parent_authorization_id",
            ),
            created_at=strict_float(data.get("created_at", 0), "created_at"),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )

    def get_ready_steps(self) -> list[PlanStep]:
        done_ids = {
            step.step_id
            for step in self.steps
            if step.state is StepState.SUCCEEDED
        }
        return [
            step
            for step in self.steps
            if step.state is StepState.PENDING
            and all(dependency in done_ids for dependency in step.depends_on)
        ]

    def validate_dag(self) -> list[str]:
        step_map = {step.step_id: step for step in self.steps}
        errors: list[str] = []
        visited: set[str] = set()
        active: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in active:
                errors.append(f"Cycle detected involving step {step_id}")
                return
            if step_id in visited:
                return
            step = step_map.get(step_id)
            if step is None:
                errors.append(f"Missing dependency step {step_id}")
                return
            active.add(step_id)
            for dependency in step.depends_on:
                visit(dependency)
            active.remove(step_id)
            visited.add(step_id)

        for step_id in step_map:
            visit(step_id)
        return errors


@dataclass(frozen=True)
class Attempt:
    attempt_id: str = field(default_factory=lambda: new_id("att"))
    tenant_key: str = ""
    step_id: str = ""
    run_id: str = ""
    state: AttemptState = AttemptState.ACTIVE
    lease_id: str = ""
    fencing_token: int = 0
    lease_expires: float = 0.0
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    worker_id: str = ""
    turn_count: int = 0
    checkpoint_blob_ref: Any = None
    checkpoint_path: str = ""
    last_heartbeat: float = field(default_factory=time.time)
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checkpoint_blob_ref",
            (
                None
                if self.checkpoint_blob_ref is None
                else freeze(self.checkpoint_blob_ref)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "tenant_key": self.tenant_key,
            "step_id": self.step_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "lease_expires": self.lease_expires,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "worker_id": self.worker_id,
            "turn_count": self.turn_count,
            "checkpoint_blob_ref": (
                None
                if self.checkpoint_blob_ref is None
                else thaw(self.checkpoint_blob_ref)
            ),
            "checkpoint_path": self.checkpoint_path,
            "last_heartbeat": self.last_heartbeat,
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Attempt:
        return cls(
            attempt_id=strict_str(data["attempt_id"], "attempt_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            step_id=strict_str(data.get("step_id", ""), "step_id"),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            state=AttemptState(data.get("state", AttemptState.ACTIVE.value)),
            lease_id=strict_str(data.get("lease_id", ""), "lease_id"),
            fencing_token=strict_int(
                data.get("fencing_token", 0), "fencing_token", minimum=0
            ),
            lease_expires=strict_float(
                data.get("lease_expires", 0), "lease_expires"
            ),
            started_at=strict_float(data.get("started_at", 0), "started_at"),
            completed_at=data.get("completed_at"),
            worker_id=strict_str(data.get("worker_id", ""), "worker_id"),
            turn_count=strict_int(
                data.get("turn_count", 0), "turn_count", minimum=0
            ),
            checkpoint_blob_ref=data.get("checkpoint_blob_ref"),
            checkpoint_path=strict_str(
                data.get("checkpoint_path", ""), "checkpoint_path"
            ),
            last_heartbeat=strict_float(
                data.get("last_heartbeat", 0), "last_heartbeat"
            ),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )

    def is_lease_valid(self) -> bool:
        return self.lease_expires > time.time()
