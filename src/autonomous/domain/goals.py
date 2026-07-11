"""Goal, Run, criteria, and trigger aggregates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .enums import (
    AutonomyMode,
    GoalState,
    GoalType,
    MisfirePolicy,
    OracleType,
    OverlapPolicy,
    RunState,
)
from .ids import (
    canonical_hash,
    freeze,
    new_id,
    strict_bool,
    strict_float,
    strict_int,
    strict_str,
    thaw,
)


@dataclass(frozen=True)
class EpochSet:
    definition_version: int = 1
    admission_epoch: int = 1
    revocation_epoch: int = 0
    run_control_epoch: int = 0
    plan_epoch: int = 0
    kill_epoch: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "definition_version": self.definition_version,
            "admission_epoch": self.admission_epoch,
            "revocation_epoch": self.revocation_epoch,
            "run_control_epoch": self.run_control_epoch,
            "plan_epoch": self.plan_epoch,
            "kill_epoch": self.kill_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EpochSet:
        return cls(
            **{
                name: int(data[name])
                for name in cls.__dataclass_fields__
                if name in data
            }
        )


@dataclass(frozen=True)
class GoalCriterion:
    criterion_id: str = field(default_factory=lambda: new_id("crit"))
    description: str = ""
    oracle_type: OracleType = OracleType.COMMAND
    oracle_config: Any = field(default_factory=dict)
    criterion_hash: str = ""

    def __post_init__(self) -> None:
        config = freeze(self.oracle_config)
        object.__setattr__(self, "oracle_config", config)
        computed = canonical_hash(
            {
                "criterion_id": self.criterion_id,
                "description": self.description,
                "oracle_type": self.oracle_type.value,
                "oracle_config": thaw(config),
            }
        )
        if self.criterion_hash and self.criterion_hash != computed:
            raise ValueError("criterion hash does not match criterion content")
        object.__setattr__(self, "criterion_hash", computed)

    def compute_hash(self) -> str:
        return self.criterion_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "description": self.description,
            "oracle_type": self.oracle_type.value,
            "oracle_config": thaw(self.oracle_config),
            "criterion_hash": self.criterion_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalCriterion:
        return cls(
            criterion_id=strict_str(data["criterion_id"], "criterion_id"),
            description=strict_str(
                data.get("description", ""), "description"
            ),
            oracle_type=OracleType(data.get("oracle_type", OracleType.COMMAND.value)),
            oracle_config=data.get("oracle_config", {}),
            criterion_hash=strict_str(
                data.get("criterion_hash", ""), "criterion_hash"
            ),
        )


@dataclass(frozen=True)
class GoalSpec:
    objective: str = ""
    deliverables: tuple[str, ...] = ()
    scope: str = ""
    constraints: tuple[str, ...] = ()
    deadline: float | None = None
    criteria: tuple[GoalCriterion, ...] = ()
    data_sources: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    budget: Any = None
    risks: tuple[str, ...] = ()
    notification_policy: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "deliverables", tuple(self.deliverables))
        object.__setattr__(self, "constraints", tuple(self.constraints))
        object.__setattr__(self, "criteria", tuple(self.criteria))
        object.__setattr__(self, "data_sources", tuple(self.data_sources))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(
            self,
            "budget",
            None if self.budget is None else freeze(self.budget),
        )
        object.__setattr__(
            self,
            "notification_policy",
            freeze(self.notification_policy),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "deliverables": list(self.deliverables),
            "scope": self.scope,
            "constraints": list(self.constraints),
            "deadline": self.deadline,
            "criteria": [criterion.to_dict() for criterion in self.criteria],
            "data_sources": list(self.data_sources),
            "tools": list(self.tools),
            "budget": None if self.budget is None else thaw(self.budget),
            "risks": list(self.risks),
            "notification_policy": thaw(self.notification_policy),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalSpec:
        return cls(
            objective=strict_str(data.get("objective", ""), "objective"),
            deliverables=tuple(data.get("deliverables", ())),
            scope=strict_str(data.get("scope", ""), "scope"),
            constraints=tuple(data.get("constraints", ())),
            deadline=data.get("deadline"),
            criteria=tuple(
                GoalCriterion.from_dict(value)
                for value in data.get("criteria", ())
            ),
            data_sources=tuple(data.get("data_sources", ())),
            tools=tuple(data.get("tools", ())),
            budget=data.get("budget"),
            risks=tuple(data.get("risks", ())),
            notification_policy=data.get("notification_policy", {}),
        )


@dataclass(frozen=True)
class GoalDefinition:
    goal_id: str = field(default_factory=lambda: new_id("goal"))
    tenant_key: str = ""
    owner_principal_id: str = ""
    owner_id: str = ""
    goal_type: GoalType = GoalType.ONE_SHOT
    state: GoalState = GoalState.DRAFT
    spec: GoalSpec = field(default_factory=GoalSpec)
    epochs: EpochSet = field(default_factory=EpochSet)
    autonomy_mode: AutonomyMode = AutonomyMode.SUPERVISED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    standing_order: Any = None
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "standing_order",
            None if self.standing_order is None else freeze(self.standing_order),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "tenant_key": self.tenant_key,
            "owner_principal_id": self.owner_principal_id,
            "owner_id": self.owner_id,
            "goal_type": self.goal_type.value,
            "state": self.state.value,
            "spec": self.spec.to_dict(),
            "epochs": self.epochs.to_dict(),
            "autonomy_mode": self.autonomy_mode.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "standing_order": (
                None if self.standing_order is None else thaw(self.standing_order)
            ),
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalDefinition:
        return cls(
            goal_id=strict_str(data["goal_id"], "goal_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            owner_principal_id=strict_str(
                data.get("owner_principal_id", ""), "owner_principal_id"
            ),
            owner_id=strict_str(data.get("owner_id", ""), "owner_id"),
            goal_type=GoalType(data.get("goal_type", GoalType.ONE_SHOT.value)),
            state=GoalState(data.get("state", GoalState.DRAFT.value)),
            spec=GoalSpec.from_dict(data.get("spec", {})),
            epochs=EpochSet.from_dict(data.get("epochs", {})),
            autonomy_mode=AutonomyMode(
                data.get("autonomy_mode", AutonomyMode.SUPERVISED.value)
            ),
            created_at=strict_float(data.get("created_at", 0), "created_at"),
            updated_at=strict_float(data.get("updated_at", 0), "updated_at"),
            standing_order=data.get("standing_order"),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class Run:
    run_id: str = field(default_factory=lambda: new_id("run"))
    tenant_key: str = ""
    goal_id: str = ""
    goal_version: int = 1
    run_definition_version: int = 1
    root_run_lineage: str = ""
    occurrence_key: str = ""
    trigger_event_id: str = ""
    state: RunState = RunState.QUEUED
    plan_epoch: int = 0
    budget_ledger_id: str = ""
    created_at: float = field(default_factory=time.time)
    deadline: float | None = None
    supersedes_run_id: str | None = None
    retry_of_run_id: str | None = None
    revision_of_run_id: str | None = None
    authorization_snapshot_id: str = ""
    run_control_epoch: int = 0
    revocation_epoch: int = 0
    target_terminal_state: RunState | None = None
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        if not self.root_run_lineage:
            object.__setattr__(self, "root_run_lineage", self.run_id)
        if self.target_terminal_state not in {
            None,
            RunState.CANCELED,
            RunState.FAILED,
            RunState.EXPIRED,
        }:
            raise ValueError(
                "target terminal target must be canceled, failed, or expired"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_key": self.tenant_key,
            "goal_id": self.goal_id,
            "goal_version": self.goal_version,
            "run_definition_version": self.run_definition_version,
            "root_run_lineage": self.root_run_lineage,
            "occurrence_key": self.occurrence_key,
            "trigger_event_id": self.trigger_event_id,
            "state": self.state.value,
            "plan_epoch": self.plan_epoch,
            "budget_ledger_id": self.budget_ledger_id,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "supersedes_run_id": self.supersedes_run_id,
            "retry_of_run_id": self.retry_of_run_id,
            "revision_of_run_id": self.revision_of_run_id,
            "authorization_snapshot_id": self.authorization_snapshot_id,
            "run_control_epoch": self.run_control_epoch,
            "revocation_epoch": self.revocation_epoch,
            "target_terminal_state": (
                self.target_terminal_state.value
                if self.target_terminal_state is not None
                else None
            ),
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        target = data.get("target_terminal_state")
        return cls(
            run_id=strict_str(data["run_id"], "run_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            goal_id=strict_str(data.get("goal_id", ""), "goal_id"),
            goal_version=strict_int(
                data.get("goal_version", 1),
                "goal_version",
                minimum=1,
            ),
            run_definition_version=strict_int(
                data.get(
                    "run_definition_version",
                    data.get("goal_version", 1),
                ),
                "run_definition_version",
                minimum=1,
            ),
            root_run_lineage=strict_str(
                data.get("root_run_lineage", ""),
                "root_run_lineage",
            ),
            occurrence_key=strict_str(
                data.get("occurrence_key", ""),
                "occurrence_key",
            ),
            trigger_event_id=strict_str(
                data.get("trigger_event_id", ""),
                "trigger_event_id",
            ),
            state=RunState(data.get("state", RunState.QUEUED.value)),
            plan_epoch=strict_int(
                data.get("plan_epoch", 0),
                "plan_epoch",
                minimum=0,
            ),
            budget_ledger_id=strict_str(
                data.get("budget_ledger_id", ""),
                "budget_ledger_id",
            ),
            created_at=strict_float(data.get("created_at", 0), "created_at"),
            deadline=data.get("deadline"),
            supersedes_run_id=data.get("supersedes_run_id"),
            retry_of_run_id=data.get("retry_of_run_id"),
            revision_of_run_id=data.get("revision_of_run_id"),
            authorization_snapshot_id=strict_str(
                data.get("authorization_snapshot_id", ""),
                "authorization_snapshot_id",
            ),
            run_control_epoch=strict_int(
                data.get("run_control_epoch", 0),
                "run_control_epoch",
                minimum=0,
            ),
            revocation_epoch=strict_int(
                data.get("revocation_epoch", 0),
                "revocation_epoch",
                minimum=0,
            ),
            target_terminal_state=RunState(target) if target else None,
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class ScheduleCursor:
    last_occurrence: str | None = None
    next_planned: str | None = None
    last_success_at: float | None = None
    watermark: str | None = None
    misfire_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_occurrence": self.last_occurrence,
            "next_planned": self.next_planned,
            "last_success_at": self.last_success_at,
            "watermark": self.watermark,
            "misfire_count": self.misfire_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleCursor:
        return cls(
            last_occurrence=data.get("last_occurrence"),
            next_planned=data.get("next_planned"),
            last_success_at=data.get("last_success_at"),
            watermark=data.get("watermark"),
            misfire_count=strict_int(
                data.get("misfire_count", 0), "misfire_count", minimum=0
            ),
        )


@dataclass(frozen=True)
class TriggerSubscription:
    subscription_id: str = field(default_factory=lambda: new_id("trig"))
    tenant_key: str = ""
    goal_id: str = ""
    timezone: str = "UTC"
    cron_expr: str = ""
    event_rule: Any = None
    misfire_policy: MisfirePolicy = MisfirePolicy.RUN_LATEST
    overlap_policy: OverlapPolicy = OverlapPolicy.FORBID
    cursor: ScheduleCursor = field(default_factory=ScheduleCursor)
    definition_version: int = 1
    admission_epoch: int = 1
    active: bool = True
    delivery_semantics: str = ""
    replay_supported: bool = False
    cursor_format: str = ""
    gap_detection: bool = False
    max_recovery_window_seconds: int = 0
    heartbeat_seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_rule",
            None if self.event_rule is None else freeze(self.event_rule),
        )

    @property
    def is_autonomous_eligible(self) -> bool:
        return (
            self.active
            and (
                self.delivery_semantics in {"durable_ack", "exactly_once"}
                or self.replay_supported
            )
            and bool(self.cursor_format)
            and self.gap_detection
            and self.max_recovery_window_seconds > 0
            and self.heartbeat_seconds > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "tenant_key": self.tenant_key,
            "goal_id": self.goal_id,
            "timezone": self.timezone,
            "cron_expr": self.cron_expr,
            "event_rule": None if self.event_rule is None else thaw(self.event_rule),
            "misfire_policy": self.misfire_policy.value,
            "overlap_policy": self.overlap_policy.value,
            "cursor": self.cursor.to_dict(),
            "definition_version": self.definition_version,
            "admission_epoch": self.admission_epoch,
            "active": self.active,
            "delivery_semantics": self.delivery_semantics,
            "replay_supported": self.replay_supported,
            "cursor_format": self.cursor_format,
            "gap_detection": self.gap_detection,
            "max_recovery_window_seconds": self.max_recovery_window_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TriggerSubscription:
        return cls(
            subscription_id=strict_str(
                data["subscription_id"], "subscription_id"
            ),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            goal_id=strict_str(data.get("goal_id", ""), "goal_id"),
            timezone=strict_str(data.get("timezone", "UTC"), "timezone"),
            cron_expr=strict_str(data.get("cron_expr", ""), "cron_expr"),
            event_rule=data.get("event_rule"),
            misfire_policy=MisfirePolicy(
                data.get("misfire_policy", MisfirePolicy.RUN_LATEST.value)
            ),
            overlap_policy=OverlapPolicy(
                data.get("overlap_policy", OverlapPolicy.FORBID.value)
            ),
            cursor=ScheduleCursor.from_dict(data.get("cursor", {})),
            definition_version=strict_int(
                data.get("definition_version", 1),
                "definition_version",
                minimum=1,
            ),
            admission_epoch=strict_int(
                data.get("admission_epoch", 1), "admission_epoch", minimum=0
            ),
            active=strict_bool(data.get("active", True), "active"),
            delivery_semantics=strict_str(
                data.get("delivery_semantics", ""), "delivery_semantics"
            ),
            replay_supported=strict_bool(
                data.get("replay_supported", False),
                "replay_supported",
            ),
            cursor_format=strict_str(
                data.get("cursor_format", ""), "cursor_format"
            ),
            gap_detection=strict_bool(
                data.get("gap_detection", False),
                "gap_detection",
            ),
            max_recovery_window_seconds=strict_int(
                data.get("max_recovery_window_seconds", 0),
                "max_recovery_window_seconds",
                minimum=0,
            ),
            heartbeat_seconds=strict_int(
                data.get("heartbeat_seconds", 0),
                "heartbeat_seconds",
                minimum=0,
            ),
        )
