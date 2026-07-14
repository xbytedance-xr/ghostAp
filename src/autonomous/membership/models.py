"""Immutable exact-schema membership contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_EFFECT_RE = re.compile(r"membfx_[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_ERROR_RE = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")


class MembershipState(StrEnum):
    ABSENT = "absent"
    ADDING = "adding"
    ACTIVE = "active"
    REMOVING = "removing"
    DEGRADED = "degraded"


class MembershipOperation(StrEnum):
    ADD = "add"
    REMOVE = "remove"


class MembershipEffectState(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    ACTION_REQUIRED = "action_required"

    @property
    def terminal(self) -> bool:
        return self in {
            MembershipEffectState.COMMITTED,
            MembershipEffectState.ACTION_REQUIRED,
        }


def _required_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _SAFE_ID_RE.fullmatch(value) is None
    ):
        raise ValueError(f"invalid {name}")
    return value


def membership_effect_id(
    tenant_key: str,
    chat_id: str,
    agent_id: str,
    operation: MembershipOperation | str,
    membership_epoch: int,
) -> str:
    coordinates = tuple(
        _required_text(value, name)
        for value, name in (
            (tenant_key, "tenant_key"),
            (chat_id, "chat_id"),
            (agent_id, "agent_id"),
        )
    )
    try:
        operation_value = MembershipOperation(operation)
    except (TypeError, ValueError):
        raise ValueError("invalid membership operation") from None
    if type(membership_epoch) is not int or membership_epoch < 1:
        raise ValueError("invalid membership_epoch")
    raw = "\x00".join((*coordinates, operation_value.value, str(membership_epoch)))
    return f"membfx_{hashlib.sha256(raw.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class MembershipEffect:
    schema_version: int
    effect_id: str
    tenant_key: str
    chat_id: str
    agent_id: str
    app_id: str
    requester_principal_id: str
    operation: MembershipOperation
    state: MembershipEffectState
    membership_epoch: int
    error_code: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "effect_id",
            "tenant_key",
            "chat_id",
            "agent_id",
            "app_id",
            "requester_principal_id",
            "operation",
            "state",
            "membership_epoch",
            "error_code",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported membership effect schema")
        for name in (
            "tenant_key",
            "chat_id",
            "agent_id",
            "app_id",
            "requester_principal_id",
        ):
            _required_text(getattr(self, name), name)
        try:
            operation = MembershipOperation(self.operation)
            state = MembershipEffectState(self.state)
        except (TypeError, ValueError):
            raise ValueError("invalid membership effect enum") from None
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "state", state)
        expected = membership_effect_id(
            self.tenant_key,
            self.chat_id,
            self.agent_id,
            operation,
            self.membership_epoch,
        )
        if self.effect_id != expected or _EFFECT_RE.fullmatch(self.effect_id) is None:
            raise ValueError("membership effect_id does not match coordinates")
        if state is MembershipEffectState.ACTION_REQUIRED:
            if not isinstance(self.error_code, str) or _ERROR_RE.fullmatch(self.error_code) is None:
                raise ValueError("action_required membership effect requires error_code")
        elif self.error_code != "":
            raise ValueError("membership effect error_code requires action_required")

    @property
    def desired_state(self) -> MembershipState:
        return (
            MembershipState.ACTIVE
            if self.operation is MembershipOperation.ADD
            else MembershipState.ABSENT
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "effect_id": self.effect_id,
            "tenant_key": self.tenant_key,
            "chat_id": self.chat_id,
            "agent_id": self.agent_id,
            "app_id": self.app_id,
            "requester_principal_id": self.requester_principal_id,
            "operation": self.operation.value,
            "state": self.state.value,
            "membership_epoch": self.membership_epoch,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: object) -> MembershipEffect:
        if not isinstance(value, dict) or set(value) != cls._FIELDS:
            raise ValueError("membership effect must use exact schema")
        return cls(**value)


__all__ = [
    "MembershipEffect",
    "MembershipEffectState",
    "MembershipOperation",
    "MembershipState",
    "membership_effect_id",
]
