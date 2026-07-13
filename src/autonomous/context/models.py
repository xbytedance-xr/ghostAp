"""Frozen contracts for employee-scoped context snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ContextUnavailableReason(str, Enum):
    """Stable, non-secret reasons that prohibit employee task execution."""

    SCOPE = "scope"
    CREDENTIALS = "credentials"
    PERMISSION = "permission"
    VISIBILITY = "visibility"
    ROOT_THREAD_BINDING = "root_thread_binding"
    PAGINATION = "pagination"
    ORDERING = "ordering"
    REVISION = "revision"
    CURRENT_MESSAGE = "current_message"
    CONTENT = "content"
    DEADLINE = "deadline"
    BUDGET = "budget"
    MEMORY = "memory"
    SOURCE = "source"


class ContextUnavailableError(RuntimeError):
    """Context cannot be trusted; execution must not proceed."""

    def __init__(
        self,
        reason: ContextUnavailableReason | str,
        *,
        internal_detail: str = "",
    ) -> None:
        # ``internal_detail`` is accepted at the boundary for callers that need
        # local classification, but is intentionally neither stored nor rendered.
        del internal_detail
        if isinstance(reason, ContextUnavailableReason):
            self.reason = reason
            message = f"CONTEXT_UNAVAILABLE:{reason.value}"
        else:
            # Compatibility for the pre-production assembler. Task 3 replaces
            # these free-text construction sites with stable reason values.
            self.reason = ContextUnavailableReason.SOURCE
            message = "CONTEXT_UNAVAILABLE:source"
        super().__init__(message)


class MessageSourceError(RuntimeError):
    """Employee-scoped Feishu message source failed."""


class ContextLayer(str, Enum):
    THREAD_FULL = "thread_full"
    GROUP_RECENT = "group_recent"
    L1_MEMORY = "l1_memory"
    L2_GROUP = "l2_group"


def _require_prefix(value: str, field_name: str, prefix: str) -> None:
    suffix = value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else ""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", suffix) is None:
        raise ValueError(f"{field_name} must use {prefix} identifier space")


@dataclass(frozen=True)
class EmployeeMessageScope:
    """Non-secret employee and Feishu coordinates for one context read."""

    tenant_key: str
    agent_id: str
    bot_principal_id: str
    app_id: str
    chat_id: str
    thread_root_message_id: str
    current_message_id: str
    feishu_thread_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_key, str) or not self.tenant_key.strip():
            raise ValueError("tenant_key is required")
        _require_prefix(self.agent_id, "agent_id", "agt_")
        _require_prefix(self.bot_principal_id, "bot_principal_id", "bot_")
        _require_prefix(self.app_id, "app_id", "cli_")
        _require_prefix(self.chat_id, "chat_id", "oc_")
        _require_prefix(
            self.thread_root_message_id,
            "thread_root_message_id",
            "om_",
        )
        _require_prefix(self.current_message_id, "current_message_id", "om_")
        if self.feishu_thread_id:
            _require_prefix(self.feishu_thread_id, "feishu_thread_id", "omt_")


@dataclass(frozen=True)
class ContextMessage:
    """One normalized message and its Feishu revision coordinates."""

    # Content fields remain first for migration readability; production scope
    # fields below are mandatory and intentionally tighten the old scaffold API.
    message_id: str
    sender_id: str
    sender_type: str
    text: str
    timestamp: float
    is_system: bool = False
    is_current: bool = False
    edited: bool = False
    deleted: bool = False
    chat_id: str = ""
    thread_id: str = ""
    root_id: str = ""
    parent_id: str = ""
    sender_id_type: str = ""
    sender_tenant_key: str = ""
    msg_type: str = "text"
    create_time_ms: int = 0
    update_time_ms: int = 0
    message_position: int | None = None
    thread_message_position: int | None = None

    def __post_init__(self) -> None:
        _require_prefix(self.message_id, "message_id", "om_")
        if not isinstance(self.sender_id, str) or not self.sender_id:
            raise ValueError("sender_id is required")
        if not isinstance(self.sender_type, str) or not self.sender_type:
            raise ValueError("sender_type is required")
        _require_prefix(self.chat_id, "chat_id", "oc_")
        if self.root_id and not self.thread_id:
            raise ValueError("root_id requires thread_id")
        if self.thread_id:
            _require_prefix(self.thread_id, "thread_id", "omt_")
            if self.root_id:
                _require_prefix(self.root_id, "root_id", "om_")
        if self.parent_id:
            _require_prefix(self.parent_id, "parent_id", "om_")
        if not isinstance(self.sender_id_type, str) or not self.sender_id_type:
            raise ValueError("sender_id_type is required")
        if (
            not isinstance(self.sender_tenant_key, str)
            or not self.sender_tenant_key.strip()
            or self.sender_tenant_key != self.sender_tenant_key.strip()
        ):
            raise ValueError("sender_tenant_key is required")
        if not isinstance(self.msg_type, str) or not self.msg_type:
            raise ValueError("msg_type is required")
        if not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("timestamp must be non-negative and finite")
        create_ms = self.create_time_ms or int(self.timestamp * 1000)
        update_ms = self.update_time_ms or create_ms
        if isinstance(create_ms, bool) or not isinstance(create_ms, int) or create_ms < 0:
            raise ValueError("create_time_ms must be a non-negative integer")
        if isinstance(update_ms, bool) or not isinstance(update_ms, int):
            raise ValueError("update_time_ms must be an integer")
        if update_ms < create_ms:
            raise ValueError("update_time_ms must not precede create_time_ms")
        for field_name, position in (
            ("message_position", self.message_position),
            ("thread_message_position", self.thread_message_position),
        ):
            if position is not None and (
                isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        object.__setattr__(self, "create_time_ms", create_ms)
        object.__setattr__(self, "update_time_ms", update_ms)
        if self.deleted:
            object.__setattr__(self, "text", "")

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def order_key(self) -> tuple[int, int, int, str]:
        return (
            self.thread_message_position if self.thread_message_position is not None else -1,
            self.message_position if self.message_position is not None else -1,
            self.create_time_ms,
            self.message_id,
        )


@dataclass(frozen=True)
class MessageRevision:
    """Deterministic digest of fields that may change context meaning."""

    message_id: str
    digest: str
    update_time_ms: int
    deleted: bool

    @classmethod
    def from_message(cls, message: ContextMessage) -> MessageRevision:
        payload = {
            "message_id": message.message_id,
            "chat_id": message.chat_id,
            "thread_id": message.thread_id,
            "root_id": message.root_id,
            "parent_id": message.parent_id,
            "sender_id": message.sender_id,
            "sender_id_type": message.sender_id_type,
            "sender_type": message.sender_type,
            "sender_tenant_key": message.sender_tenant_key,
            "msg_type": message.msg_type,
            "create_time_ms": message.create_time_ms,
            "update_time_ms": message.update_time_ms,
            "edited": message.edited,
            "deleted": message.deleted,
            "message_position": message.message_position,
            "thread_message_position": message.thread_message_position,
            "content_digest": message.content_digest,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            message_id=message.message_id,
            digest=hashlib.sha256(encoded).hexdigest(),
            update_time_ms=message.update_time_ms,
            deleted=message.deleted,
        )


@dataclass(frozen=True)
class ThreadWatermark:
    """Source watermark captured before context budget trimming."""

    # Original cursor fields remain first for readable migration diffs; the
    # authority fields below are mandatory and intentionally strict.
    thread_root_id: str
    last_message_id: str
    last_timestamp: float
    message_count: int
    revision: int = 0
    tenant_key: str = ""
    chat_id: str = ""
    feishu_thread_id: str = ""
    revision_digest: str = ""

    def __post_init__(self) -> None:
        _require_prefix(self.thread_root_id, "thread_root_id", "om_")
        _require_prefix(self.last_message_id, "last_message_id", "om_")
        if not math.isfinite(self.last_timestamp) or self.last_timestamp < 0:
            raise ValueError("last_timestamp must be non-negative and finite")
        if (
            isinstance(self.message_count, bool)
            or not isinstance(self.message_count, int)
            or self.message_count <= 0
        ):
            raise ValueError("message_count must be a positive integer")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if not isinstance(self.tenant_key, str) or not self.tenant_key.strip():
            raise ValueError("tenant_key is required")
        _require_prefix(self.chat_id, "chat_id", "oc_")
        _require_prefix(self.feishu_thread_id, "feishu_thread_id", "omt_")
        if re.fullmatch(r"[0-9a-f]{64}", self.revision_digest) is None:
            raise ValueError("revision_digest must be a SHA-256 hex digest")


@dataclass(frozen=True)
class ContextLayerMetrics:
    layer: ContextLayer
    source_messages: int
    retained_messages: int
    source_chars: int
    retained_chars: int
    omission_reason: str = ""

    def __post_init__(self) -> None:
        values = (
            self.source_messages,
            self.retained_messages,
            self.source_chars,
            self.retained_chars,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("layer metrics must be non-negative integers")
        if self.retained_messages > self.source_messages:
            raise ValueError("retained_messages exceeds source_messages")
        if self.retained_chars > self.source_chars:
            raise ValueError("retained_chars exceeds source_chars")


@dataclass(frozen=True)
class TrimmingRecord:
    layer: ContextLayer
    removed_messages: int
    removed_chars: int
    reason: str = "budget"

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.removed_messages, self.removed_chars)
        ):
            raise ValueError("trimming counts must be non-negative")


@dataclass(frozen=True)
class AssembledContext:
    """Immutable context snapshot ready for an authority-bound executor."""

    thread_messages: tuple[ContextMessage, ...]
    group_messages: tuple[ContextMessage, ...]
    l1_summary: str
    l2_summary: str
    total_tokens_estimate: int
    watermark: ThreadWatermark | None
    layers_used: tuple[ContextLayer, ...]
    truncated: bool = False
    total_chars: int = 0
    layer_metrics: tuple[ContextLayerMetrics, ...] = ()
    trimming_trace: tuple[TrimmingRecord, ...] = ()
    snapshot_hash: str = ""
    group_layer_unavailable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_messages", tuple(self.thread_messages))
        object.__setattr__(self, "group_messages", tuple(self.group_messages))
        object.__setattr__(self, "layers_used", tuple(self.layers_used))
        object.__setattr__(self, "layer_metrics", tuple(self.layer_metrics))
        object.__setattr__(self, "trimming_trace", tuple(self.trimming_trace))
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.total_tokens_estimate, self.total_chars)
        ):
            raise ValueError("context totals must be non-negative")

    def diagnostics(self) -> dict[str, Any]:
        """Return structural diagnostics without message or memory plaintext."""
        return {
            "thread_messages": len(self.thread_messages),
            "group_messages": len(self.group_messages),
            "l1_chars": len(self.l1_summary),
            "l2_chars": len(self.l2_summary),
            "total_chars": self.total_chars,
            "total_tokens_estimate": self.total_tokens_estimate,
            "layers_used": tuple(layer.value for layer in self.layers_used),
            "truncated": self.truncated,
            "group_layer_unavailable": self.group_layer_unavailable,
            "snapshot_hash": self.snapshot_hash,
        }


@dataclass(frozen=True)
class ThreadContextConfig:
    """Validated context budgets and official API pagination limits."""

    max_thread_messages: int = 200
    max_group_messages: int = 50
    max_context_tokens: int = 128_000
    tokens_per_char: float = 0.3
    thread_page_size: int = 50
    group_page_size: int = 20
    fetch_timeout_seconds: float = 30.0
    max_context_chars: int = 400_000
    max_pages: int = 200

    def __post_init__(self) -> None:
        for field_name in (
            "max_thread_messages",
            "max_group_messages",
            "max_context_tokens",
            "max_context_chars",
            "max_pages",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        for field_name in ("thread_page_size", "group_page_size"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
                raise ValueError(f"{field_name} must be between 1 and 50")
        if (
            isinstance(self.tokens_per_char, bool)
            or not isinstance(self.tokens_per_char, (int, float))
            or not math.isfinite(self.tokens_per_char)
            or self.tokens_per_char <= 0
        ):
            raise ValueError("tokens_per_char must be positive and finite")
        if (
            isinstance(self.fetch_timeout_seconds, bool)
            or not isinstance(self.fetch_timeout_seconds, (int, float))
            or not math.isfinite(self.fetch_timeout_seconds)
            or self.fetch_timeout_seconds <= 0
        ):
            raise ValueError("fetch_timeout_seconds must be positive and finite")

    @classmethod
    def from_settings(cls, settings: Any) -> ThreadContextConfig:
        return cls(
            max_thread_messages=settings.autonomous_thread_context_max_messages,
            max_group_messages=settings.autonomous_group_context_max_messages,
            max_context_tokens=settings.autonomous_context_max_tokens,
            max_context_chars=settings.autonomous_thread_context_max_chars,
            thread_page_size=settings.autonomous_thread_context_page_size,
            group_page_size=settings.autonomous_group_context_page_size,
            fetch_timeout_seconds=(
                settings.autonomous_context_fetch_timeout_seconds
            ),
            max_pages=settings.autonomous_context_max_pages,
        )


__all__ = [
    "AssembledContext",
    "ContextLayer",
    "ContextLayerMetrics",
    "ContextMessage",
    "ContextUnavailableError",
    "ContextUnavailableReason",
    "EmployeeMessageScope",
    "MessageRevision",
    "MessageSourceError",
    "ThreadContextConfig",
    "ThreadWatermark",
    "TrimmingRecord",
]
