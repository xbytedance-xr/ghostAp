"Employee, bot-principal, and worker-runtime aggregates."

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .enums import EmployeeIdOrigin, EmployeeState, WorkerType
from .ids import freeze, new_id, strict_float, strict_int, strict_str, thaw


@dataclass(frozen=True)
class EmployeeDefinition:
    agent_id: str = field(default_factory=lambda: new_id("agt"))
    tenant_key: str = ""
    owner_principal_id: str = ""
    name: str = ""
    emoji: str = "🤖"
    tool: str = ""
    model: str = ""
    profile: str = "standard"
    effort: str = "default"
    role: str = ""
    persona: str = ""
    personality_traits: tuple[str, ...] = ()
    worker_type: WorkerType = WorkerType.LOGICAL
    state: EmployeeState = EmployeeState.DRAFT
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    budget_template: Any = field(default_factory=dict)
    bot_principal_id: str | None = None
    member_groups: tuple[str, ...] = ()
    id_origin: EmployeeIdOrigin = EmployeeIdOrigin.NATIVE
    legacy_id_alias: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        if self.worker_type is WorkerType.VISIBLE:
            if not self.tenant_key:
                raise ValueError("visible employee requires tenant_key")
            if not self.owner_principal_id:
                raise ValueError("visible employee requires owner_principal_id")
        object.__setattr__(
            self,
            "personality_traits",
            tuple(self.personality_traits),
        )
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "permissions", tuple(self.permissions))
        object.__setattr__(self, "budget_template", freeze(self.budget_template))
        object.__setattr__(self, "member_groups", tuple(self.member_groups))

    @property
    def employee_id(self) -> str:
        return self.agent_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "tenant_key": self.tenant_key,
            "owner_principal_id": self.owner_principal_id,
            "name": self.name,
            "emoji": self.emoji,
            "tool": self.tool,
            "model": self.model,
            "profile": self.profile,
            "effort": self.effort,
            "role": self.role,
            "persona": self.persona,
            "personality_traits": list(self.personality_traits),
            "worker_type": self.worker_type.value,
            "state": self.state.value,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "budget_template": thaw(self.budget_template),
            "bot_principal_id": self.bot_principal_id,
            "member_groups": list(self.member_groups),
            "id_origin": self.id_origin.value,
            "legacy_id_alias": self.legacy_id_alias,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmployeeDefinition:
        raw_agent_id = data.get("agent_id", data.get("employee_id"))
        if raw_agent_id is None:
            raise ValueError("agent_id is required")
        return cls(
            agent_id=strict_str(raw_agent_id, "agent_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            owner_principal_id=strict_str(
                data.get("owner_principal_id", ""), "owner_principal_id"
            ),
            name=strict_str(data.get("name", ""), "name"),
            emoji=strict_str(data.get("emoji", "🤖"), "emoji"),
            tool=strict_str(data.get("tool", ""), "tool"),
            model=strict_str(data.get("model", ""), "model"),
            profile=strict_str(data.get("profile", "standard"), "profile"),
            effort=strict_str(data.get("effort", "default"), "effort"),
            role=strict_str(data.get("role", ""), "role"),
            persona=strict_str(data.get("persona", ""), "persona"),
            personality_traits=tuple(data.get("personality_traits", ())),
            worker_type=WorkerType(
                data.get("worker_type", WorkerType.LOGICAL.value)
            ),
            state=EmployeeState(data.get("state", EmployeeState.DRAFT.value)),
            capabilities=tuple(data.get("capabilities", ())),
            permissions=tuple(data.get("permissions", ())),
            budget_template=data.get("budget_template", {}),
            bot_principal_id=data.get("bot_principal_id"),
            member_groups=tuple(data.get("member_groups", ())),
            id_origin=EmployeeIdOrigin(
                data.get("id_origin", EmployeeIdOrigin.NATIVE.value)
            ),
            legacy_id_alias=strict_str(
                data.get("legacy_id_alias", ""),
                "legacy_id_alias",
            ),
            created_at=strict_float(data.get("created_at", 0), "created_at"),
            updated_at=strict_float(data.get("updated_at", 0), "updated_at"),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class BotPrincipal:
    bot_principal_id: str = field(default_factory=lambda: new_id("bot"))
    tenant_key: str = ""
    agent_id: str = ""
    app_id: str = ""
    credential_ref: str = ""
    scopes: tuple[str, ...] = ()
    desired_manifest_hash: str = ""
    observed_manifest_hash: str = ""
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", tuple(self.scopes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_principal_id": self.bot_principal_id,
            "tenant_key": self.tenant_key,
            "agent_id": self.agent_id,
            "app_id": self.app_id,
            "credential_ref": self.credential_ref,
            "scopes": list(self.scopes),
            "desired_manifest_hash": self.desired_manifest_hash,
            "observed_manifest_hash": self.observed_manifest_hash,
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BotPrincipal:
        raw_agent_id = data.get("agent_id", data.get("employee_id"))
        if raw_agent_id is None:
            raise ValueError("agent_id is required")
        return cls(
            bot_principal_id=strict_str(
                data["bot_principal_id"],
                "bot_principal_id",
            ),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            agent_id=strict_str(raw_agent_id, "agent_id"),
            app_id=strict_str(data.get("app_id", ""), "app_id"),
            credential_ref=strict_str(
                data.get("credential_ref", ""),
                "credential_ref",
            ),
            scopes=tuple(data.get("scopes", ())),
            desired_manifest_hash=strict_str(
                data.get("desired_manifest_hash", ""),
                "desired_manifest_hash",
            ),
            observed_manifest_hash=strict_str(
                data.get("observed_manifest_hash", ""),
                "observed_manifest_hash",
            ),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class WorkerRuntime:
    worker_runtime_id: str = field(default_factory=lambda: new_id("worker"))
    tenant_key: str = ""
    employee_id: str = ""
    run_id: str = ""
    step_id: str = ""
    attempt_id: str = ""
    pid: int | None = None
    os_uid: int | None = None
    lease_id: str = ""
    fencing_token: int = 0
    checkpoint_blob_ref: Any = None

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
            "worker_runtime_id": self.worker_runtime_id,
            "tenant_key": self.tenant_key,
            "employee_id": self.employee_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt_id": self.attempt_id,
            "pid": self.pid,
            "os_uid": self.os_uid,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "checkpoint_blob_ref": (
                None
                if self.checkpoint_blob_ref is None
                else thaw(self.checkpoint_blob_ref)
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerRuntime:
        pid = data.get("pid")
        os_uid = data.get("os_uid")
        return cls(
            worker_runtime_id=strict_str(
                data["worker_runtime_id"],
                "worker_runtime_id",
            ),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            employee_id=strict_str(
                data.get("employee_id", ""),
                "employee_id",
            ),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            step_id=strict_str(data.get("step_id", ""), "step_id"),
            attempt_id=strict_str(
                data.get("attempt_id", ""),
                "attempt_id",
            ),
            pid=None if pid is None else strict_int(pid, "pid", minimum=1),
            os_uid=(
                None
                if os_uid is None
                else strict_int(os_uid, "os_uid", minimum=0)
            ),
            lease_id=strict_str(data.get("lease_id", ""), "lease_id"),
            fencing_token=strict_int(
                data.get("fencing_token", 0),
                "fencing_token",
                minimum=0,
            ),
            checkpoint_blob_ref=data.get("checkpoint_blob_ref"),
        )
