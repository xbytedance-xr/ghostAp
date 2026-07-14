"""Frozen exact-schema contracts for employee-owned response delivery."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

_OUTBOX_NAMESPACE = uuid.UUID("28ba4d58-d227-5c8c-9583-e4b64f9749ec")
_OUTBOX_RE = re.compile(r"out_[0-9a-f]{64}\Z")
_EFFECT_RE = re.compile(r"outeff_[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_FORBIDDEN_COLLAPSED_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", key)
    for key in {
        "app_secret",
        "access_token",
        "tenant_access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "client_secret",
        "private_key",
        "password",
        "token",
        "credential_ref",
        "master_key",
        "vault_key",
    }
)


class EmployeeCardState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    ACTION_REQUIRED = "action_required"

    @property
    def terminal(self) -> bool:
        return self in {
            EmployeeCardState.COMPLETED,
            EmployeeCardState.FAILED,
            EmployeeCardState.CANCELED,
            EmployeeCardState.ACTION_REQUIRED,
        }


class DeliveryEffectKind(str, Enum):
    CREATE = "create"
    PATCH = "patch"


class DeliveryEffectState(str, Enum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    ACTION_REQUIRED = "action_required"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_text(
    value: Any,
    name: str,
    *,
    maximum: int = 4096,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"invalid {name}")
    if len(value) > maximum or any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"invalid {name}")
    return value


def _exact_dict(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    missing = fields - set(value)
    extra = set(value) - fields
    if missing:
        raise ValueError(f"{name} missing fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"{name} has unknown fields: {sorted(extra)}")
    return value


def _freeze_json(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError(f"invalid JSON key at {path}")
            collapsed = re.sub(r"[^a-z0-9]", "", raw_key.casefold())
            if collapsed in _FORBIDDEN_COLLAPSED_KEYS:
                raise ValueError(f"secret-bearing field is forbidden at {path}.{raw_key}")
            frozen[raw_key] = _freeze_json(child, path=f"{path}.{raw_key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child, path=f"{path}[{index}]") for index, child in enumerate(value))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and value == value and value not in {float("inf"), float("-inf")}:
        return value
    raise ValueError(f"unsupported JSON value at {path}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def employee_outbox_id(tenant_key: str, agent_id: str, attempt_id: str) -> str:
    coordinates = tuple(
        _strict_text(value, name, maximum=256)
        for value, name in (
            (tenant_key, "tenant_key"),
            (agent_id, "agent_id"),
            (attempt_id, "attempt_id"),
        )
    )
    digest = hashlib.sha256("\x00".join(coordinates).encode()).hexdigest()
    return f"out_{digest}"


def employee_outbox_uuid(outbox_id: str) -> str:
    if not isinstance(outbox_id, str) or _OUTBOX_RE.fullmatch(outbox_id) is None:
        raise ValueError("invalid outbox_id")
    return str(uuid.uuid5(_OUTBOX_NAMESPACE, outbox_id))


def employee_outbox_effect_id(
    outbox_id: str,
    kind: DeliveryEffectKind | str,
    snapshot_version: int,
    attempt: int,
) -> str:
    if not isinstance(outbox_id, str) or _OUTBOX_RE.fullmatch(outbox_id) is None:
        raise ValueError("invalid outbox_id")
    try:
        effect_kind = DeliveryEffectKind(kind)
    except ValueError:
        raise ValueError("invalid Outbox effect kind") from None
    for value, name in (
        (snapshot_version, "snapshot_version"),
        (attempt, "attempt"),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"invalid {name}")
    coordinates = f"{outbox_id}\x00{effect_kind.value}\x00{snapshot_version}\x00{attempt}"
    return f"outeff_{hashlib.sha256(coordinates.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class EmployeeOutboxSnapshot:
    schema_version: int
    outbox_id: str
    tenant_key: str
    agent_id: str
    attempt_id: str
    chat_id: str
    thread_root_message_id: str
    version: int
    state: EmployeeCardState
    title: str
    summary: str
    progress_percent: int
    card_json: Mapping[str, Any]
    created_at: str
    terminal_version: int

    _FIELDS = frozenset(
        {
            "schema_version",
            "outbox_id",
            "tenant_key",
            "agent_id",
            "attempt_id",
            "chat_id",
            "thread_root_message_id",
            "version",
            "state",
            "title",
            "summary",
            "progress_percent",
            "card_json",
            "created_at",
            "terminal_version",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported snapshot schema_version")
        for name in (
            "tenant_key",
            "agent_id",
            "attempt_id",
            "chat_id",
        ):
            _strict_text(getattr(self, name), name, maximum=256)
        _strict_text(
            self.thread_root_message_id,
            "thread_root_message_id",
            maximum=256,
            allow_empty=True,
        )
        expected_id = employee_outbox_id(
            self.tenant_key,
            self.agent_id,
            self.attempt_id,
        )
        if self.outbox_id != expected_id:
            raise ValueError("outbox_id does not match employee coordinates")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("invalid snapshot version")
        try:
            state = EmployeeCardState(self.state)
        except ValueError:
            raise ValueError("invalid employee card state") from None
        object.__setattr__(self, "state", state)
        _strict_text(self.title, "title", maximum=512)
        _strict_text(self.summary, "summary", maximum=100_000, allow_empty=True)
        if type(self.progress_percent) is not int or not 0 <= self.progress_percent <= 100:
            raise ValueError("invalid progress_percent")
        if not isinstance(self.card_json, Mapping):
            raise ValueError("card_json must be an object")
        object.__setattr__(self, "card_json", _freeze_json(self.card_json, path="card_json"))
        if not isinstance(self.created_at, str) or _RFC3339_UTC_RE.fullmatch(self.created_at) is None:
            raise ValueError("invalid created_at")
        if type(self.terminal_version) is not int or self.terminal_version < 0:
            raise ValueError("invalid terminal_version")
        if state.terminal:
            if self.terminal_version != self.version:
                raise ValueError("terminal_version must equal terminal snapshot version")
        elif self.terminal_version != 0:
            raise ValueError("nonterminal snapshot cannot set terminal_version")

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outbox_id": self.outbox_id,
            "tenant_key": self.tenant_key,
            "agent_id": self.agent_id,
            "attempt_id": self.attempt_id,
            "chat_id": self.chat_id,
            "thread_root_message_id": self.thread_root_message_id,
            "version": self.version,
            "state": self.state.value,
            "title": self.title,
            "summary": self.summary,
            "progress_percent": self.progress_percent,
            "card_json": _thaw_json(self.card_json),
            "created_at": self.created_at,
            "terminal_version": self.terminal_version,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EmployeeOutboxSnapshot:
        return cls(**_exact_dict(value, cls._FIELDS, "employee Outbox snapshot"))


@dataclass(frozen=True, slots=True)
class EmployeeOutboxBinding:
    schema_version: int
    outbox_id: str
    stable_uuid: str
    app_id: str
    generation: int
    connection_id: str
    message_id: str
    bound_snapshot_version: int

    _FIELDS = frozenset(
        {
            "schema_version",
            "outbox_id",
            "stable_uuid",
            "app_id",
            "generation",
            "connection_id",
            "message_id",
            "bound_snapshot_version",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported Outbox binding schema_version")
        if not isinstance(self.outbox_id, str) or _OUTBOX_RE.fullmatch(self.outbox_id) is None:
            raise ValueError("invalid outbox_id")
        if self.stable_uuid != employee_outbox_uuid(self.outbox_id):
            raise ValueError("stable_uuid does not match outbox_id")
        for name in ("app_id", "connection_id", "message_id"):
            _strict_text(getattr(self, name), name, maximum=256)
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("invalid generation")
        if type(self.bound_snapshot_version) is not int or self.bound_snapshot_version < 1:
            raise ValueError("invalid bound_snapshot_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outbox_id": self.outbox_id,
            "stable_uuid": self.stable_uuid,
            "app_id": self.app_id,
            "generation": self.generation,
            "connection_id": self.connection_id,
            "message_id": self.message_id,
            "bound_snapshot_version": self.bound_snapshot_version,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EmployeeOutboxBinding:
        return cls(**_exact_dict(value, cls._FIELDS, "employee Outbox binding"))


@dataclass(frozen=True, slots=True)
class OutboxDeliveryEffect:
    schema_version: int
    effect_id: str
    outbox_id: str
    kind: DeliveryEffectKind
    state: DeliveryEffectState
    snapshot_version: int
    snapshot_sha256: str
    attempt: int
    error_code: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "effect_id",
            "outbox_id",
            "kind",
            "state",
            "snapshot_version",
            "snapshot_sha256",
            "attempt",
            "error_code",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported Outbox effect schema_version")
        if not isinstance(self.effect_id, str) or _EFFECT_RE.fullmatch(self.effect_id) is None:
            raise ValueError("invalid effect_id")
        if not isinstance(self.outbox_id, str) or _OUTBOX_RE.fullmatch(self.outbox_id) is None:
            raise ValueError("invalid outbox_id")
        try:
            object.__setattr__(self, "kind", DeliveryEffectKind(self.kind))
            object.__setattr__(self, "state", DeliveryEffectState(self.state))
        except ValueError:
            raise ValueError("invalid Outbox effect enum") from None
        if type(self.snapshot_version) is not int or self.snapshot_version < 1:
            raise ValueError("invalid snapshot_version")
        if not isinstance(self.snapshot_sha256, str) or _SHA256_RE.fullmatch(self.snapshot_sha256) is None:
            raise ValueError("invalid snapshot_sha256")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("invalid delivery attempt")
        expected_effect_id = employee_outbox_effect_id(
            self.outbox_id,
            self.kind,
            self.snapshot_version,
            self.attempt,
        )
        if self.effect_id != expected_effect_id:
            raise ValueError("effect_id does not match delivery coordinates")
        _strict_text(self.error_code, "error_code", maximum=128, allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "effect_id": self.effect_id,
            "outbox_id": self.outbox_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "snapshot_version": self.snapshot_version,
            "snapshot_sha256": self.snapshot_sha256,
            "attempt": self.attempt,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: Any) -> OutboxDeliveryEffect:
        return cls(**_exact_dict(value, cls._FIELDS, "employee Outbox effect"))


def advance_snapshot(
    previous: EmployeeOutboxSnapshot,
    candidate: EmployeeOutboxSnapshot,
) -> EmployeeOutboxSnapshot:
    """Validate one immutable, monotonic status-card transition."""

    if not isinstance(previous, EmployeeOutboxSnapshot) or not isinstance(
        candidate,
        EmployeeOutboxSnapshot,
    ):
        raise TypeError("snapshot transition requires EmployeeOutboxSnapshot")
    coordinates = (
        "outbox_id",
        "tenant_key",
        "agent_id",
        "attempt_id",
        "chat_id",
        "thread_root_message_id",
    )
    if any(getattr(previous, name) != getattr(candidate, name) for name in coordinates):
        raise ValueError("employee Outbox coordinates changed")
    if candidate.version != previous.version + 1:
        raise ValueError("snapshot version must increase by exactly one")
    if candidate.created_at != previous.created_at:
        raise ValueError("snapshot created_at is immutable")
    if previous.state.terminal:
        raise ValueError("terminal employee Outbox snapshot is fenced")
    allowed = {
        EmployeeCardState.QUEUED: {
            EmployeeCardState.QUEUED,
            EmployeeCardState.RUNNING,
            EmployeeCardState.FAILED,
            EmployeeCardState.CANCELED,
            EmployeeCardState.ACTION_REQUIRED,
        },
        EmployeeCardState.RUNNING: {
            EmployeeCardState.RUNNING,
            EmployeeCardState.COMPLETED,
            EmployeeCardState.FAILED,
            EmployeeCardState.CANCELED,
            EmployeeCardState.ACTION_REQUIRED,
        },
    }
    if candidate.state not in allowed[previous.state]:
        raise ValueError("invalid employee card state transition")
    if candidate.progress_percent < previous.progress_percent:
        raise ValueError("snapshot progress cannot decrease")
    return candidate


__all__ = [
    "DeliveryEffectKind",
    "DeliveryEffectState",
    "EmployeeCardState",
    "EmployeeOutboxBinding",
    "EmployeeOutboxSnapshot",
    "OutboxDeliveryEffect",
    "advance_snapshot",
    "employee_outbox_effect_id",
    "employee_outbox_id",
    "employee_outbox_uuid",
]
