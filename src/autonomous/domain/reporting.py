"Progress, reports, and reliable-delivery value objects."

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .enums import RunState
from .ids import (
    freeze,
    new_id,
    strict_float,
    strict_int,
    strict_str,
    thaw,
)


@dataclass(frozen=True)
class ProgressSnapshot:
    run_id: str = ""
    tenant_key: str = ""
    run_state: RunState = RunState.QUEUED
    plan_version: int = 0
    completed_steps: int = 0
    total_steps: int = 0
    current_step: str | None = None
    next_steps: tuple[str, ...] = ()
    current_attempt: str | None = None
    last_heartbeat: float = 0.0
    eta: float | None = None
    deadline: float | None = None
    budget_used: Any = field(default_factory=dict)
    budget_remaining: Any = field(default_factory=dict)
    blockers: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    unresolved_effects: tuple[str, ...] = ()
    updated_at: float = field(default_factory=time.time)
    source_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "next_steps", tuple(self.next_steps))
        object.__setattr__(self, "budget_used", freeze(self.budget_used))
        object.__setattr__(
            self, "budget_remaining", freeze(self.budget_remaining)
        )
        object.__setattr__(self, "blockers", tuple(self.blockers))
        object.__setattr__(self, "approvals", tuple(self.approvals))
        object.__setattr__(
            self, "unresolved_effects", tuple(self.unresolved_effects)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_key": self.tenant_key,
            "run_state": self.run_state.value,
            "plan_version": self.plan_version,
            "completed_steps": self.completed_steps,
            "total_steps": self.total_steps,
            "current_step": self.current_step,
            "next_steps": list(self.next_steps),
            "current_attempt": self.current_attempt,
            "last_heartbeat": self.last_heartbeat,
            "eta": self.eta,
            "deadline": self.deadline,
            "budget_used": thaw(self.budget_used),
            "budget_remaining": thaw(self.budget_remaining),
            "blockers": list(self.blockers),
            "approvals": list(self.approvals),
            "unresolved_effects": list(self.unresolved_effects),
            "updated_at": self.updated_at,
            "source_sequence": self.source_sequence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressSnapshot:
        return cls(
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            run_state=RunState(data.get("run_state", RunState.QUEUED.value)),
            plan_version=strict_int(
                data.get("plan_version", 0), "plan_version", minimum=0
            ),
            completed_steps=strict_int(
                data.get("completed_steps", 0), "completed_steps", minimum=0
            ),
            total_steps=strict_int(
                data.get("total_steps", 0), "total_steps", minimum=0
            ),
            current_step=data.get("current_step"),
            next_steps=tuple(data.get("next_steps", ())),
            current_attempt=data.get("current_attempt"),
            last_heartbeat=strict_float(
                data.get("last_heartbeat", 0), "last_heartbeat"
            ),
            eta=data.get("eta"),
            deadline=data.get("deadline"),
            budget_used=data.get("budget_used", {}),
            budget_remaining=data.get("budget_remaining", {}),
            blockers=tuple(data.get("blockers", ())),
            approvals=tuple(data.get("approvals", ())),
            unresolved_effects=tuple(data.get("unresolved_effects", ())),
            updated_at=strict_float(data.get("updated_at", 0), "updated_at"),
            source_sequence=strict_int(
                data.get("source_sequence", 0), "source_sequence", minimum=0
            ),
        )


@dataclass(frozen=True)
class Report:
    report_id: str = field(default_factory=lambda: new_id("report"))
    tenant_key: str = ""
    run_id: str = ""
    report_type: str = ""
    payload_blob_ref: Any = None
    payload_hash: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "payload_blob_ref",
            (
                None
                if self.payload_blob_ref is None
                else freeze(self.payload_blob_ref)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "tenant_key": self.tenant_key,
            "run_id": self.run_id,
            "report_type": self.report_type,
            "payload_blob_ref": (
                None
                if self.payload_blob_ref is None
                else thaw(self.payload_blob_ref)
            ),
            "payload_hash": self.payload_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Report:
        return cls(
            report_id=strict_str(data["report_id"], "report_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            report_type=strict_str(
                data.get("report_type", ""),
                "report_type",
            ),
            payload_blob_ref=data.get("payload_blob_ref"),
            payload_hash=strict_str(
                data.get("payload_hash", ""),
                "payload_hash",
            ),
            created_at=strict_float(data.get("created_at", 0), "created_at"),
        )


@dataclass(frozen=True)
class DecisionRequest:
    decision_id: str = field(default_factory=lambda: new_id("decision"))
    tenant_key: str = ""
    run_id: str = ""
    plan_epoch: int = 0
    requester_principal_id: str = ""
    allowed_decider_principals: tuple[str, ...] = ()
    required_role: str = ""
    action_scope: str = ""
    question: str = ""
    options: tuple[str, ...] = ()
    default_behavior: str = ""
    nonce: str = ""
    expires_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_decider_principals",
            tuple(self.allowed_decider_principals),
        )
        object.__setattr__(self, "options", tuple(self.options))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "tenant_key": self.tenant_key,
            "run_id": self.run_id,
            "plan_epoch": self.plan_epoch,
            "requester_principal_id": self.requester_principal_id,
            "allowed_decider_principals": list(
                self.allowed_decider_principals
            ),
            "required_role": self.required_role,
            "action_scope": self.action_scope,
            "question": self.question,
            "options": list(self.options),
            "default_behavior": self.default_behavior,
            "nonce": self.nonce,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionRequest:
        return cls(
            decision_id=strict_str(data["decision_id"], "decision_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            plan_epoch=strict_int(
                data.get("plan_epoch", 0),
                "plan_epoch",
                minimum=0,
            ),
            requester_principal_id=strict_str(
                data.get("requester_principal_id", ""),
                "requester_principal_id",
            ),
            allowed_decider_principals=tuple(
                data.get("allowed_decider_principals", ())
            ),
            required_role=strict_str(
                data.get("required_role", ""),
                "required_role",
            ),
            action_scope=strict_str(
                data.get("action_scope", ""),
                "action_scope",
            ),
            question=strict_str(data.get("question", ""), "question"),
            options=tuple(data.get("options", ())),
            default_behavior=strict_str(
                data.get("default_behavior", ""),
                "default_behavior",
            ),
            nonce=strict_str(data.get("nonce", ""), "nonce"),
            expires_at=strict_float(
                data.get("expires_at", 0),
                "expires_at",
            ),
        )
