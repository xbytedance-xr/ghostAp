"""Strict immutable contracts for employee durable ingress.

This module deliberately contains schemas only. Journal persistence, encrypted
Blob ownership, replay, and routing belong to later Phase 3 tasks.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Mapping

MAX_INGRESS_PAYLOAD_BYTES = 256 * 1024
MAX_INGRESS_ATTACHMENT_COUNT = 10
MAX_INGRESS_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_INGRESS_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FORBIDDEN_KEYS = frozenset(
    {
        "app_secret",
        "access_token",
        "tenant_access_token",
        "refresh_token",
        "authorization",
        "credential_ref",
        "master_key",
        "vault_key",
    }
)
_ATTACHMENT_FIELDS = frozenset(
    {
        "resource_type",
        "resource_id",
        "mime_type",
        "size_bytes",
        "sha256",
    }
)
_DISPOSITION_STATES = frozenset(
    {
        "accepted",
        "authorized",
        "staging",
        "queued",
        "busy",
        "ignored",
        "rejected",
        "dispatching",
        "terminal",
    }
)
_ATTEMPT_STATES = frozenset(
    {
        "prepared",
        "dispatch_committed",
        "completed",
        "failed",
        "canceled",
        "timeout",
        "action_required",
    }
)
_TERMINAL_ATTEMPT_STATES = frozenset(
    {"completed", "failed", "canceled", "timeout", "action_required"}
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


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


def _strict_str(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
    maximum: int = 4096,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{name} must be non-empty")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds maximum length")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _sha256(value: Any, name: str) -> str:
    digest = _strict_str(value, name)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{name} must be lowercase sha256")
    return digest


def _identifier(value: Any, name: str, prefix: str) -> str:
    result = _strict_str(value, name, maximum=256)
    if not result.startswith(prefix) or _SAFE_ID_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must use {prefix} identifier space")
    return result


def _optional_identifier(value: Any, name: str, prefix: str) -> str:
    result = _strict_str(value, name, allow_empty=True, maximum=256)
    if result and (not result.startswith(prefix) or _SAFE_ID_RE.fullmatch(result) is None):
        raise ValueError(f"{name} must use {prefix} identifier space")
    return result


def _utc_timestamp(value: Any, name: str) -> str:
    text = _strict_str(value, name, maximum=64)
    if _RFC3339_UTC_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must be canonical RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} must be canonical RFC3339 UTC") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{name} must be canonical RFC3339 UTC")
    return text


def _validate_secret_key(key: Any, path: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError(f"{path} keys must be non-empty strings")
    normalized = key.casefold().replace("-", "_")
    if normalized == "token" or any(secret in normalized for secret in _FORBIDDEN_KEYS):
        raise ValueError(f"secret-bearing field is forbidden at {path}.{key}")
    return key


def _freeze_json(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = _validate_secret_key(raw_key, path)
            frozen[key] = _freeze_json(child, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"unsupported or non-finite JSON value at {path}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class EmployeeIngressPayload:
    """Normalized untrusted content intended for employee-scoped encryption."""

    schema_version: int
    envelope_id: str
    normalized_parts: tuple[Mapping[str, Any], ...]
    attachment_descriptors: tuple[Mapping[str, Any], ...]

    _FIELDS = frozenset(
        {
            "schema_version",
            "envelope_id",
            "normalized_parts",
            "attachment_descriptors",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported payload schema_version")
        object.__setattr__(
            self,
            "envelope_id",
            _identifier(self.envelope_id, "envelope_id", "ing_"),
        )
        parts = _freeze_json(tuple(self.normalized_parts), path="normalized_parts")
        attachments = _freeze_json(
            tuple(self.attachment_descriptors),
            path="attachment_descriptors",
        )
        if not isinstance(parts, tuple) or not all(isinstance(item, Mapping) for item in parts):
            raise ValueError("normalized_parts must contain objects")
        if not isinstance(attachments, tuple):
            raise ValueError("attachment_descriptors must be a collection")
        if len(attachments) > MAX_INGRESS_ATTACHMENT_COUNT:
            raise ValueError("attachment count exceeds maximum")
        total_size = 0
        for item in attachments:
            if not isinstance(item, Mapping) or set(item) != _ATTACHMENT_FIELDS:
                raise ValueError("attachment descriptor has invalid fields")
            for field_name in ("resource_type", "resource_id", "mime_type"):
                _strict_str(item[field_name], f"attachment {field_name}")
            size = _strict_int(item["size_bytes"], "attachment size_bytes")
            if size > MAX_INGRESS_ATTACHMENT_BYTES:
                raise ValueError("attachment exceeds maximum size")
            total_size += size
            _sha256(item["sha256"], "attachment sha256")
        if total_size > MAX_INGRESS_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("attachment total exceeds maximum size")
        object.__setattr__(self, "normalized_parts", parts)
        object.__setattr__(self, "attachment_descriptors", attachments)
        if self.canonical_size_bytes > MAX_INGRESS_PAYLOAD_BYTES:
            raise ValueError("payload size exceeds maximum")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @property
    def canonical_size_bytes(self) -> int:
        return len(self.canonical_bytes)

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def attachment_total_bytes(self) -> int:
        return sum(int(item["size_bytes"]) for item in self.attachment_descriptors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "envelope_id": self.envelope_id,
            "normalized_parts": _thaw_json(self.normalized_parts),
            "attachment_descriptors": _thaw_json(self.attachment_descriptors),
        }

    @classmethod
    def from_dict(cls, value: Any) -> EmployeeIngressPayload:
        return cls(**_exact_dict(value, cls._FIELDS, "employee ingress payload"))


@dataclass(frozen=True, slots=True)
class EmployeeIngressMetadata:
    """Secret-free safe indexes plus trusted worker/parent authority binding."""

    schema_version: int
    envelope_id: str
    # Trusted binding: these fields are supplied by worker ownership/projection.
    tenant_key: str
    agent_id: str
    bot_principal_id: str
    app_id: str
    channel_generation: int
    connection_id: str
    # Normalized untrusted provenance: safe indexes only, never authority.
    event_id: str
    message_id: str
    event_type: str
    action_identity: str
    chat_id: str
    thread_root_message_id: str
    sender_principal_id: str
    received_at: str
    semantic_digest: str
    payload_sha256: str
    payload_size_bytes: int
    attachment_count: int
    attachment_total_bytes: int

    _FIELDS = frozenset(
        {
            "schema_version",
            "envelope_id",
            "tenant_key",
            "agent_id",
            "bot_principal_id",
            "app_id",
            "channel_generation",
            "connection_id",
            "event_id",
            "message_id",
            "event_type",
            "action_identity",
            "chat_id",
            "thread_root_message_id",
            "sender_principal_id",
            "received_at",
            "semantic_digest",
            "payload_sha256",
            "payload_size_bytes",
            "attachment_count",
            "attachment_total_bytes",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported metadata schema_version")
        object.__setattr__(self, "envelope_id", _identifier(self.envelope_id, "envelope_id", "ing_"))
        object.__setattr__(self, "tenant_key", _strict_str(self.tenant_key, "tenant_key"))
        object.__setattr__(self, "agent_id", _identifier(self.agent_id, "agent_id", "agt_"))
        object.__setattr__(
            self,
            "bot_principal_id",
            _identifier(self.bot_principal_id, "bot_principal_id", "bot_"),
        )
        object.__setattr__(self, "app_id", _identifier(self.app_id, "app_id", "cli_"))
        object.__setattr__(
            self,
            "channel_generation",
            _strict_int(self.channel_generation, "channel_generation", minimum=1),
        )
        object.__setattr__(
            self,
            "connection_id",
            _identifier(self.connection_id, "connection_id", "conn_"),
        )
        object.__setattr__(self, "event_id", _optional_identifier(self.event_id, "event_id", "evt_"))
        object.__setattr__(self, "message_id", _identifier(self.message_id, "message_id", "om_"))
        object.__setattr__(self, "event_type", _strict_str(self.event_type, "event_type", maximum=256))
        object.__setattr__(
            self,
            "action_identity",
            _strict_str(self.action_identity, "action_identity", allow_empty=True, maximum=256),
        )
        if not self.event_id and not self.action_identity:
            raise ValueError("action_identity is required without event_id")
        object.__setattr__(self, "chat_id", _identifier(self.chat_id, "chat_id", "oc_"))
        object.__setattr__(
            self,
            "thread_root_message_id",
            _optional_identifier(
                self.thread_root_message_id,
                "thread_root_message_id",
                "om_",
            ),
        )
        object.__setattr__(
            self,
            "sender_principal_id",
            _strict_str(self.sender_principal_id, "sender_principal_id", maximum=256),
        )
        object.__setattr__(self, "received_at", _utc_timestamp(self.received_at, "received_at"))
        object.__setattr__(self, "semantic_digest", _sha256(self.semantic_digest, "semantic_digest"))
        object.__setattr__(self, "payload_sha256", _sha256(self.payload_sha256, "payload_sha256"))
        payload_size = _strict_int(self.payload_size_bytes, "payload_size_bytes")
        if payload_size > MAX_INGRESS_PAYLOAD_BYTES:
            raise ValueError("payload_size_bytes exceeds maximum")
        object.__setattr__(self, "payload_size_bytes", payload_size)
        count = _strict_int(self.attachment_count, "attachment_count")
        if count > MAX_INGRESS_ATTACHMENT_COUNT:
            raise ValueError("attachment_count exceeds maximum")
        object.__setattr__(self, "attachment_count", count)
        total = _strict_int(self.attachment_total_bytes, "attachment_total_bytes")
        if total > MAX_INGRESS_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("attachment_total_bytes exceeds maximum")
        object.__setattr__(self, "attachment_total_bytes", total)

    @property
    def canonical_dedup_material(self) -> tuple[str, ...]:
        if self.event_id:
            return (self.tenant_key, self.agent_id, "event", self.event_id)
        return (
            self.tenant_key,
            self.agent_id,
            "fallback",
            self.message_id,
            self.event_type,
            self.action_identity,
        )

    @property
    def dedup_key(self) -> str:
        return "dedup_" + hashlib.sha256(
            _canonical_json(list(self.canonical_dedup_material))
        ).hexdigest()

    @property
    def trusted_worker_binding(self) -> tuple[str, str, str, str, int, str]:
        return (
            self.tenant_key,
            self.agent_id,
            self.bot_principal_id,
            self.app_id,
            self.channel_generation,
            self.connection_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in sorted(self._FIELDS)}

    @classmethod
    def from_dict(cls, value: Any) -> EmployeeIngressMetadata:
        return cls(**_exact_dict(value, cls._FIELDS, "employee ingress metadata"))


@dataclass(frozen=True, slots=True)
class IngressAcceptance:
    """Canonical durable acceptance reused for every transport redelivery."""

    schema_version: int
    acceptance_id: str
    envelope_id: str
    dedup_key: str
    semantic_digest: str
    journal_sequence: int
    journal_frame_hash: str
    accepted_at: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "acceptance_id",
            "envelope_id",
            "dedup_key",
            "semantic_digest",
            "journal_sequence",
            "journal_frame_hash",
            "accepted_at",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported acceptance schema_version")
        object.__setattr__(self, "acceptance_id", _identifier(self.acceptance_id, "acceptance_id", "acc_"))
        object.__setattr__(self, "envelope_id", _identifier(self.envelope_id, "envelope_id", "ing_"))
        object.__setattr__(self, "dedup_key", _identifier(self.dedup_key, "dedup_key", "dedup_"))
        object.__setattr__(self, "semantic_digest", _sha256(self.semantic_digest, "semantic_digest"))
        object.__setattr__(
            self,
            "journal_sequence",
            _strict_int(self.journal_sequence, "journal_sequence", minimum=1),
        )
        object.__setattr__(self, "journal_frame_hash", _sha256(self.journal_frame_hash, "journal_frame_hash"))
        object.__setattr__(self, "accepted_at", _utc_timestamp(self.accepted_at, "accepted_at"))

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in sorted(self._FIELDS)}

    @classmethod
    def from_dict(cls, value: Any) -> IngressAcceptance:
        return cls(**_exact_dict(value, cls._FIELDS, "ingress acceptance"))


@dataclass(frozen=True, slots=True)
class EmployeeIngressAck:
    """One transport ACK bound to the current request and worker generation."""

    schema_version: int
    request_id: str
    acceptance: IngressAcceptance
    agent_id: str
    app_id: str
    channel_generation: int
    connection_id: str
    semantic_digest: str
    duplicate: bool
    acknowledged_at: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "request_id",
            "acceptance",
            "agent_id",
            "app_id",
            "channel_generation",
            "connection_id",
            "semantic_digest",
            "duplicate",
            "acknowledged_at",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported ACK schema_version")
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id", "req_"))
        if not isinstance(self.acceptance, IngressAcceptance):
            raise ValueError("acceptance must be an IngressAcceptance")
        object.__setattr__(self, "agent_id", _identifier(self.agent_id, "agent_id", "agt_"))
        object.__setattr__(self, "app_id", _identifier(self.app_id, "app_id", "cli_"))
        object.__setattr__(
            self,
            "channel_generation",
            _strict_int(self.channel_generation, "channel_generation", minimum=1),
        )
        object.__setattr__(
            self,
            "connection_id",
            _identifier(self.connection_id, "connection_id", "conn_"),
        )
        digest = _sha256(self.semantic_digest, "semantic_digest")
        if digest != self.acceptance.semantic_digest:
            raise ValueError("semantic_digest does not match acceptance")
        object.__setattr__(self, "semantic_digest", digest)
        if not isinstance(self.duplicate, bool):
            raise ValueError("duplicate must be boolean")
        object.__setattr__(self, "acknowledged_at", _utc_timestamp(self.acknowledged_at, "acknowledged_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "acceptance": self.acceptance.to_dict(),
            "agent_id": self.agent_id,
            "app_id": self.app_id,
            "channel_generation": self.channel_generation,
            "connection_id": self.connection_id,
            "semantic_digest": self.semantic_digest,
            "duplicate": self.duplicate,
            "acknowledged_at": self.acknowledged_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EmployeeIngressAck:
        data = _exact_dict(value, cls._FIELDS, "employee ingress ACK")
        return cls(
            **{
                **data,
                "acceptance": IngressAcceptance.from_dict(data["acceptance"]),
            }
        )


@dataclass(frozen=True, slots=True)
class IngressDisposition:
    """One durable Router disposition; reason_code is allowlisted metadata."""

    schema_version: int
    disposition_id: str
    acceptance_id: str
    state: str
    reason_code: str
    journal_sequence: int
    journal_frame_hash: str
    recorded_at: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "disposition_id",
            "acceptance_id",
            "state",
            "reason_code",
            "journal_sequence",
            "journal_frame_hash",
            "recorded_at",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported disposition schema_version")
        object.__setattr__(self, "disposition_id", _identifier(self.disposition_id, "disposition_id", "dsp_"))
        object.__setattr__(self, "acceptance_id", _identifier(self.acceptance_id, "acceptance_id", "acc_"))
        if self.state not in _DISPOSITION_STATES:
            raise ValueError("invalid disposition state")
        if not isinstance(self.reason_code, str) or _REASON_RE.fullmatch(self.reason_code) is None:
            raise ValueError("invalid disposition reason_code")
        object.__setattr__(
            self,
            "journal_sequence",
            _strict_int(self.journal_sequence, "journal_sequence", minimum=1),
        )
        object.__setattr__(self, "journal_frame_hash", _sha256(self.journal_frame_hash, "journal_frame_hash"))
        object.__setattr__(self, "recorded_at", _utc_timestamp(self.recorded_at, "recorded_at"))

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in sorted(self._FIELDS)}

    @classmethod
    def from_dict(cls, value: Any) -> IngressDisposition:
        return cls(**_exact_dict(value, cls._FIELDS, "ingress disposition"))


@dataclass(frozen=True, slots=True)
class EmployeeAttemptState:
    """Immutable employee attempt state bound to acceptance and generation."""

    schema_version: int
    attempt_id: str
    acceptance_id: str
    tenant_key: str
    agent_id: str
    app_id: str
    channel_generation: int
    state: str
    terminal_epoch: int
    journal_sequence: int
    journal_frame_hash: str
    updated_at: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "attempt_id",
            "acceptance_id",
            "tenant_key",
            "agent_id",
            "app_id",
            "channel_generation",
            "state",
            "terminal_epoch",
            "journal_sequence",
            "journal_frame_hash",
            "updated_at",
        }
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported attempt schema_version")
        object.__setattr__(self, "attempt_id", _identifier(self.attempt_id, "attempt_id", "atm_"))
        object.__setattr__(self, "acceptance_id", _identifier(self.acceptance_id, "acceptance_id", "acc_"))
        object.__setattr__(self, "tenant_key", _strict_str(self.tenant_key, "tenant_key"))
        object.__setattr__(self, "agent_id", _identifier(self.agent_id, "agent_id", "agt_"))
        object.__setattr__(self, "app_id", _identifier(self.app_id, "app_id", "cli_"))
        object.__setattr__(
            self,
            "channel_generation",
            _strict_int(self.channel_generation, "channel_generation", minimum=1),
        )
        if self.state not in _ATTEMPT_STATES:
            raise ValueError("invalid attempt state")
        epoch = _strict_int(self.terminal_epoch, "terminal_epoch")
        if self.state in _TERMINAL_ATTEMPT_STATES and epoch < 1:
            raise ValueError("terminal_epoch must be positive for terminal state")
        if self.state not in _TERMINAL_ATTEMPT_STATES and epoch != 0:
            raise ValueError("terminal_epoch must be zero for nonterminal state")
        object.__setattr__(self, "terminal_epoch", epoch)
        object.__setattr__(
            self,
            "journal_sequence",
            _strict_int(self.journal_sequence, "journal_sequence", minimum=1),
        )
        object.__setattr__(self, "journal_frame_hash", _sha256(self.journal_frame_hash, "journal_frame_hash"))
        object.__setattr__(self, "updated_at", _utc_timestamp(self.updated_at, "updated_at"))

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in sorted(self._FIELDS)}

    @classmethod
    def from_dict(cls, value: Any) -> EmployeeAttemptState:
        return cls(**_exact_dict(value, cls._FIELDS, "employee attempt state"))
