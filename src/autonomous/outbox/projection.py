"""Journal-backed safe metadata projection for the employee Durable Outbox."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping

from ..journal.blob_store import BlobRef
from ..journal.frame import JournalEvent
from .models import (
    DeliveryEffectKind,
    DeliveryEffectState,
    EmployeeCardState,
    EmployeeOutboxBinding,
    OutboxDeliveryEffect,
    employee_outbox_id,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class OutboxProjectionError(RuntimeError):
    """The authenticated Outbox history is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class OutboxSnapshotRecord:
    version: int
    state: EmployeeCardState
    progress_percent: int
    created_at: str
    terminal_version: int
    payload_sha256: str
    blob_ref: BlobRef


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    tenant_key: str
    agent_id: str
    attempt_id: str
    chat_id: str
    thread_root_message_id: str
    snapshots: Mapping[int, OutboxSnapshotRecord]
    latest_version: int
    tombstoned_versions: frozenset[int] = frozenset()
    binding: EmployeeOutboxBinding | None = None
    effects: Mapping[str, OutboxDeliveryEffect] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def employee_key(self) -> tuple[str, str]:
        return (self.tenant_key, self.agent_id)

    @property
    def latest(self) -> OutboxSnapshotRecord:
        return self.snapshots[self.latest_version]


@dataclass
class OutboxProjectionState:
    by_outbox_id: dict[str, OutboxRecord] = field(default_factory=dict)
    closed_employees: set[tuple[str, str]] = field(default_factory=set)
    cursor_sequence: int = 0
    cursor_hash: str = ""

    def clone(self) -> OutboxProjectionState:
        return OutboxProjectionState(
            by_outbox_id=dict(self.by_outbox_id),
            closed_employees=set(self.closed_employees),
            cursor_sequence=self.cursor_sequence,
            cursor_hash=self.cursor_hash,
        )


_OUTBOX_EVENTS = frozenset(
    {
        "employee.outbox.snapshot_appended",
        "employee.outbox.snapshot_tombstoned",
        "employee.outbox.effect_prepared",
        "employee.outbox.effect_executing",
        "employee.outbox.delivery_committed",
    }
)


def is_outbox_event(event_type: str) -> bool:
    return event_type in _OUTBOX_EVENTS


def reduce_outbox_event(state: OutboxProjectionState, event: JournalEvent) -> None:
    if event.event_type == "employee.outbox.snapshot_appended":
        _reduce_snapshot_appended(state, event)
    elif event.event_type == "employee.outbox.snapshot_tombstoned":
        _reduce_snapshot_tombstoned(state, event)
    elif event.event_type == "employee.outbox.effect_prepared":
        _reduce_effect_prepared(state, event)
    elif event.event_type == "employee.outbox.effect_executing":
        _reduce_effect_executing(state, event)
    elif event.event_type == "employee.outbox.delivery_committed":
        _reduce_delivery_committed(state, event)
    else:
        raise OutboxProjectionError(f"unknown Outbox event: {event.event_type}")


def _reduce_snapshot_appended(
    state: OutboxProjectionState,
    event: JournalEvent,
) -> None:
    required = {
        "tenant_key",
        "agent_id",
        "attempt_id",
        "chat_id",
        "thread_root_message_id",
        "version",
        "state",
        "progress_percent",
        "created_at",
        "terminal_version",
        "payload_sha256",
        "blob_ref",
    }
    payload = event.payload
    if set(payload) != required:
        raise OutboxProjectionError("invalid employee.outbox.snapshot_appended payload")
    try:
        outbox_id = employee_outbox_id(
            payload["tenant_key"],
            payload["agent_id"],
            payload["attempt_id"],
        )
        version = payload["version"]
        state_value = EmployeeCardState(payload["state"])
        progress = payload["progress_percent"]
        terminal_version = payload["terminal_version"]
        digest = payload["payload_sha256"]
        blob_ref = BlobRef.from_dict(payload["blob_ref"])
        if event.aggregate_id != outbox_id:
            raise ValueError("aggregate mismatch")
        if type(version) is not int or version < 1:
            raise ValueError("invalid version")
        if type(progress) is not int or not 0 <= progress <= 100:
            raise ValueError("invalid progress")
        if type(terminal_version) is not int or terminal_version < 0:
            raise ValueError("invalid terminal version")
        if state_value.terminal != (terminal_version == version):
            raise ValueError("terminal fence mismatch")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("invalid snapshot digest")
        for name in ("chat_id", "created_at"):
            if not isinstance(payload[name], str) or not payload[name]:
                raise ValueError(f"invalid {name}")
        if not isinstance(payload["thread_root_message_id"], str):
            raise ValueError("invalid thread root")
    except (TypeError, ValueError) as exc:
        raise OutboxProjectionError("invalid employee Outbox snapshot metadata") from exc

    snapshot = OutboxSnapshotRecord(
        version=version,
        state=state_value,
        progress_percent=progress,
        created_at=payload["created_at"],
        terminal_version=terminal_version,
        payload_sha256=digest,
        blob_ref=blob_ref,
    )
    current = state.by_outbox_id.get(outbox_id)
    if current is None:
        if version != 1 or state_value is not EmployeeCardState.QUEUED:
            raise OutboxProjectionError("Outbox must start with queued version 1")
        state.by_outbox_id[outbox_id] = OutboxRecord(
            outbox_id=outbox_id,
            tenant_key=payload["tenant_key"],
            agent_id=payload["agent_id"],
            attempt_id=payload["attempt_id"],
            chat_id=payload["chat_id"],
            thread_root_message_id=payload["thread_root_message_id"],
            snapshots=MappingProxyType({1: snapshot}),
            latest_version=1,
        )
        return

    coordinates = (
        (current.tenant_key, payload["tenant_key"]),
        (current.agent_id, payload["agent_id"]),
        (current.attempt_id, payload["attempt_id"]),
        (current.chat_id, payload["chat_id"]),
        (current.thread_root_message_id, payload["thread_root_message_id"]),
    )
    if any(left != right for left, right in coordinates):
        raise OutboxProjectionError("Outbox coordinates changed")
    previous = current.latest
    if version != current.latest_version + 1:
        raise OutboxProjectionError("Outbox snapshot version is not continuous")
    if previous.state.terminal:
        raise OutboxProjectionError("terminal Outbox snapshot is fenced")
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
    if state_value not in allowed[previous.state]:
        raise OutboxProjectionError("invalid Outbox state transition")
    if progress < previous.progress_percent:
        raise OutboxProjectionError("Outbox progress decreased")
    if snapshot.created_at != previous.created_at:
        raise OutboxProjectionError("Outbox created_at changed")
    snapshots = dict(current.snapshots)
    snapshots[version] = snapshot
    state.by_outbox_id[outbox_id] = replace(
        current,
        snapshots=MappingProxyType(snapshots),
        latest_version=version,
    )


def _reduce_snapshot_tombstoned(
    state: OutboxProjectionState,
    event: JournalEvent,
) -> None:
    if set(event.payload) != {"version", "tombstoned_at"}:
        raise OutboxProjectionError("invalid Outbox tombstone payload")
    record = state.by_outbox_id.get(event.aggregate_id)
    version = event.payload.get("version")
    if record is None or type(version) is not int or version not in record.snapshots:
        raise OutboxProjectionError("Outbox tombstone references unknown snapshot")
    if version == record.latest_version or not record.latest.state.terminal:
        raise OutboxProjectionError("Outbox latest or nonterminal snapshot cannot tombstone")
    if version in record.tombstoned_versions:
        raise OutboxProjectionError("Outbox snapshot already tombstoned")
    state.by_outbox_id[event.aggregate_id] = replace(
        record,
        tombstoned_versions=record.tombstoned_versions | {version},
    )


def _reduce_effect_prepared(
    state: OutboxProjectionState,
    event: JournalEvent,
) -> None:
    if set(event.payload) != {"effect"}:
        raise OutboxProjectionError("invalid Outbox effect prepared payload")
    record = state.by_outbox_id.get(event.aggregate_id)
    if record is None:
        raise OutboxProjectionError("Outbox effect references unknown record")
    try:
        effect = OutboxDeliveryEffect.from_dict(event.payload["effect"])
    except (TypeError, ValueError) as exc:
        raise OutboxProjectionError("invalid Outbox delivery effect") from exc
    if effect.outbox_id != record.outbox_id:
        raise OutboxProjectionError("Outbox effect aggregate mismatch")
    if effect.state is not DeliveryEffectState.PREPARED:
        raise OutboxProjectionError("new Outbox effect must be PREPARED")
    if effect.snapshot_version not in record.snapshots:
        raise OutboxProjectionError("Outbox effect references unknown snapshot")
    expected_kind = DeliveryEffectKind.CREATE if record.binding is None else DeliveryEffectKind.PATCH
    if effect.kind is not expected_kind:
        raise OutboxProjectionError("Outbox effect kind does not match binding")
    if effect.effect_id in record.effects:
        raise OutboxProjectionError("duplicate Outbox effect")
    if any(
        existing.state in {DeliveryEffectState.PREPARED, DeliveryEffectState.EXECUTING}
        for existing in record.effects.values()
    ):
        raise OutboxProjectionError("Outbox already has an active delivery effect")
    effects = dict(record.effects)
    effects[effect.effect_id] = effect
    state.by_outbox_id[event.aggregate_id] = replace(
        record,
        effects=MappingProxyType(effects),
    )


def _reduce_effect_executing(
    state: OutboxProjectionState,
    event: JournalEvent,
) -> None:
    if set(event.payload) != {"effect_id"}:
        raise OutboxProjectionError("invalid Outbox effect executing payload")
    record = state.by_outbox_id.get(event.aggregate_id)
    if record is None:
        raise OutboxProjectionError("Outbox effect references unknown record")
    effect_id = event.payload.get("effect_id")
    effect = record.effects.get(effect_id)
    if effect is None or effect.state is not DeliveryEffectState.PREPARED:
        raise OutboxProjectionError("Outbox effect cannot enter EXECUTING")
    effects = dict(record.effects)
    effects[effect.effect_id] = replace(
        effect,
        state=DeliveryEffectState.EXECUTING,
    )
    state.by_outbox_id[event.aggregate_id] = replace(
        record,
        effects=MappingProxyType(effects),
    )


def _reduce_delivery_committed(
    state: OutboxProjectionState,
    event: JournalEvent,
) -> None:
    if set(event.payload) != {"effect_id", "binding"}:
        raise OutboxProjectionError("invalid Outbox delivery committed payload")
    record = state.by_outbox_id.get(event.aggregate_id)
    if record is None:
        raise OutboxProjectionError("Outbox delivery references unknown record")
    effect = record.effects.get(event.payload.get("effect_id"))
    if effect is None or effect.state is not DeliveryEffectState.EXECUTING:
        raise OutboxProjectionError("Outbox delivery effect is not EXECUTING")
    try:
        binding = EmployeeOutboxBinding.from_dict(event.payload["binding"])
    except (TypeError, ValueError) as exc:
        raise OutboxProjectionError("invalid Outbox delivery binding") from exc
    if binding.outbox_id != record.outbox_id or binding.bound_snapshot_version != effect.snapshot_version:
        raise OutboxProjectionError("Outbox delivery binding mismatch")
    if effect.kind is DeliveryEffectKind.CREATE:
        if record.binding is not None:
            raise OutboxProjectionError("Outbox create already has a binding")
    else:
        current = record.binding
        if current is None:
            raise OutboxProjectionError("Outbox patch has no binding")
        if (
            binding.app_id != current.app_id
            or binding.message_id != current.message_id
            or binding.stable_uuid != current.stable_uuid
            or binding.bound_snapshot_version <= current.bound_snapshot_version
        ):
            raise OutboxProjectionError("Outbox patch changed stable binding")
    effects = dict(record.effects)
    effects[effect.effect_id] = replace(
        effect,
        state=DeliveryEffectState.COMMITTED,
    )
    state.by_outbox_id[event.aggregate_id] = replace(
        record,
        binding=binding,
        effects=MappingProxyType(effects),
    )


__all__ = [
    "OutboxProjectionError",
    "OutboxProjectionState",
    "OutboxRecord",
    "OutboxSnapshotRecord",
    "is_outbox_event",
    "reduce_outbox_event",
]
