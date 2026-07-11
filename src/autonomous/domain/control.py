"""Principal, authorization, decision, and budget aggregates."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .ids import (
    freeze,
    new_id,
    strict_bool,
    strict_float,
    strict_int,
    strict_str,
    thaw,
)


@dataclass(frozen=True)
class Principal:
    principal_id: str = field(default_factory=lambda: new_id("principal"))
    tenant_key: str = ""
    user_id: str = ""
    union_id: str = ""
    app_open_ids: Any = field(default_factory=dict)
    roles: tuple[str, ...] = ()
    data_scopes: tuple[str, ...] = ()
    resource_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_open_ids", freeze(self.app_open_ids))
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "data_scopes", tuple(self.data_scopes))
        object.__setattr__(self, "resource_scopes", tuple(self.resource_scopes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "tenant_key": self.tenant_key,
            "user_id": self.user_id,
            "union_id": self.union_id,
            "app_open_ids": thaw(self.app_open_ids),
            "roles": list(self.roles),
            "data_scopes": list(self.data_scopes),
            "resource_scopes": list(self.resource_scopes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Principal:
        return cls(
            principal_id=strict_str(data["principal_id"], "principal_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            user_id=strict_str(data.get("user_id", ""), "user_id"),
            union_id=strict_str(data.get("union_id", ""), "union_id"),
            app_open_ids=data.get("app_open_ids", {}),
            roles=tuple(data.get("roles", ())),
            data_scopes=tuple(data.get("data_scopes", ())),
            resource_scopes=tuple(data.get("resource_scopes", ())),
        )


@dataclass(frozen=True)
class GoalActivationAuthorization:
    auth_id: str = field(default_factory=lambda: new_id("auth"))
    tenant_key: str = ""
    principal_id: str = ""
    goal_id: str = ""
    plan_hash: str = ""
    criteria_hash: str = ""
    budget_hash: str = ""
    canonical_payload_hash: str = ""
    canonical_render_hash: str = ""
    authorization_envelope: Any = field(default_factory=dict)
    epochs: Any = field(default_factory=dict)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    expires_at: float = 0.0
    consumed: bool = False
    consumed_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "authorization_envelope", freeze(self.authorization_envelope)
        )
        object.__setattr__(self, "epochs", freeze(self.epochs))

    def is_valid(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return not self.consumed and self.expires_at > current

    def consume(self, *, now: float | None = None) -> GoalActivationAuthorization:
        if not self.is_valid(now=now):
            raise ValueError("activation authorization is expired or consumed")
        current = time.time() if now is None else now
        return GoalActivationAuthorization(
            **{
                **self.to_dict(),
                "consumed": True,
                "consumed_at": current,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "auth_id": self.auth_id,
            "tenant_key": self.tenant_key,
            "principal_id": self.principal_id,
            "goal_id": self.goal_id,
            "plan_hash": self.plan_hash,
            "criteria_hash": self.criteria_hash,
            "budget_hash": self.budget_hash,
            "canonical_payload_hash": self.canonical_payload_hash,
            "canonical_render_hash": self.canonical_render_hash,
            "authorization_envelope": thaw(self.authorization_envelope),
            "epochs": thaw(self.epochs),
            "nonce": self.nonce,
            "expires_at": self.expires_at,
            "consumed": self.consumed,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalActivationAuthorization:
        return cls(
            auth_id=strict_str(data["auth_id"], "auth_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            principal_id=strict_str(
                data.get("principal_id", ""), "principal_id"
            ),
            goal_id=strict_str(data.get("goal_id", ""), "goal_id"),
            plan_hash=strict_str(data.get("plan_hash", ""), "plan_hash"),
            criteria_hash=strict_str(
                data.get("criteria_hash", ""), "criteria_hash"
            ),
            budget_hash=strict_str(data.get("budget_hash", ""), "budget_hash"),
            canonical_payload_hash=strict_str(
                data.get("canonical_payload_hash", ""),
                "canonical_payload_hash",
            ),
            canonical_render_hash=strict_str(
                data.get("canonical_render_hash", ""),
                "canonical_render_hash",
            ),
            authorization_envelope=data.get("authorization_envelope", {}),
            epochs=data.get("epochs", {}),
            nonce=strict_str(data.get("nonce", ""), "nonce"),
            expires_at=strict_float(data.get("expires_at", 0), "expires_at"),
            consumed=strict_bool(data.get("consumed", False), "consumed"),
            consumed_at=data.get("consumed_at"),
        )


class BudgetEntryState(str, Enum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"
    UNKNOWN_BILLING = "unknown_billing"
    CONSERVATIVE_SETTLED = "conservative_settled"


@dataclass(frozen=True)
class BudgetEntry:
    entry_id: str = field(default_factory=lambda: new_id("bud"))
    ledger_id: str = ""
    amount: float = 0.0
    state: BudgetEntryState = BudgetEntryState.RESERVED
    dimension: str = ""
    reserved_at: float = field(default_factory=time.time)
    settled_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "ledger_id": self.ledger_id,
            "amount": self.amount,
            "state": self.state.value,
            "dimension": self.dimension,
            "reserved_at": self.reserved_at,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetEntry:
        return cls(
            entry_id=strict_str(data["entry_id"], "entry_id"),
            ledger_id=strict_str(data.get("ledger_id", ""), "ledger_id"),
            amount=strict_float(data.get("amount", 0), "amount"),
            state=BudgetEntryState(
                data.get("state", BudgetEntryState.RESERVED.value)
            ),
            dimension=strict_str(data.get("dimension", ""), "dimension"),
            reserved_at=strict_float(
                data.get("reserved_at", 0), "reserved_at"
            ),
            settled_at=data.get("settled_at"),
        )


@dataclass(frozen=True)
class BudgetLedger:
    ledger_id: str = field(default_factory=lambda: new_id("ledger"))
    tenant_key: str = ""
    run_id: str = ""
    goal_id: str = ""
    employee_id: str = ""
    team_id: str = ""
    limits: Any = field(default_factory=dict)
    entries: tuple[BudgetEntry, ...] = ()
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "limits", freeze(self.limits))
        object.__setattr__(self, "entries", tuple(self.entries))

    def available(self, dimension: str) -> float:
        limit = float(self.limits.get(dimension, float("inf")))
        used = sum(
            entry.amount
            for entry in self.entries
            if entry.dimension == dimension
            and entry.state
            in {
                BudgetEntryState.RESERVED,
                BudgetEntryState.SETTLED,
                BudgetEntryState.UNKNOWN_BILLING,
                BudgetEntryState.CONSERVATIVE_SETTLED,
            }
        )
        return limit - used

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "tenant_key": self.tenant_key,
            "run_id": self.run_id,
            "goal_id": self.goal_id,
            "employee_id": self.employee_id,
            "team_id": self.team_id,
            "limits": thaw(self.limits),
            "entries": [entry.to_dict() for entry in self.entries],
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetLedger:
        return cls(
            ledger_id=strict_str(data["ledger_id"], "ledger_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            goal_id=strict_str(data.get("goal_id", ""), "goal_id"),
            employee_id=strict_str(
                data.get("employee_id", ""), "employee_id"
            ),
            team_id=strict_str(data.get("team_id", ""), "team_id"),
            limits=data.get("limits", {}),
            entries=tuple(
                BudgetEntry.from_dict(value)
                for value in data.get("entries", ())
            ),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )
