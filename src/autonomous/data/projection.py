"""Journal-backed data projections: history records, documents, and attempt bindings."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..journal.frame import JournalEvent
from .models import DataKind


class DataProjectionError(RuntimeError):
    """An unrecoverable inconsistency in data replay."""


@dataclass(frozen=True)
class JournalHead:
    """Immutable snapshot of journal writer position."""

    sequence: int = 0
    logical_hash: str = ""

    def __post_init__(self) -> None:
        if (self.sequence == 0) != (self.logical_hash == ""):
            raise ValueError("genesis head must be (0, '')")


GENESIS_HEAD = JournalHead(0, "")


@dataclass(frozen=True)
class HistoryMetadataRecord:
    """Non-secret indexed metadata from a committed history event."""

    record_id: str
    occurrence_key: str
    tenant_key: str
    agent_id: str
    owner_principal_id: str
    requester_principal_id: str
    task_id: str
    run_id: str
    attempt_id: str
    chat_id: str
    thread_root_id: str
    shard_day: str
    status: str
    started_at: str
    ended_at: str
    duration_ms: int
    tool: str
    model: str
    effort: str
    safe_summary_text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    blob_ref: dict[str, Any]
    publish_sequence: int = 0
    publish_frame_hash: str = ""
    tombstoned: bool = False


@dataclass(frozen=True)
class DocumentMetadataRecord:
    """Non-secret indexed metadata from a committed document event."""

    document_id: str
    tenant_key: str
    agent_id: str
    owner_principal_id: str
    kind: DataKind
    version: int
    source_id: str
    content_hash: str
    content_type: str
    blob_ref: dict[str, Any]
    publish_sequence: int = 0
    publish_frame_hash: str = ""
    tombstoned: bool = False
    chat_id: str = ""
    thread_root_id: str = ""
    previous_document_id: str = ""
    predecessor_sequence: int = 0
    predecessor_hash: str = ""


@dataclass(frozen=True)
class AttemptBinding:
    """Immutable attempt context anchored before ACP dispatch."""

    attempt_id: str
    tenant_key: str
    agent_id: str
    owner_principal_id: str
    requester_principal_id: str
    task_id: str
    run_id: str
    message_id: str
    thread_root_id: str
    chat_id: str
    tool: str
    model: str
    effort: str
    started_at: str
    terminal_epoch: int
    publish_sequence: int = 0


@dataclass(frozen=True)
class DataAuthority:
    """Independent epoch/mode state for data plane cutover."""

    epoch: int = 0
    mode: str = "legacy"
    cutover_sequence: int = 0


@dataclass(frozen=True)
class LegacyImportManifest:
    """Metadata from a legacy data import."""

    source_locator_hash: str
    content_hash: str
    imported_ids: tuple[str, ...] = ()
    state: str = "imported"


@dataclass(frozen=True)
class ReadAuditRecord:
    """Non-secret durable read audit entry."""

    audit_id: str
    principal_id: str
    operation: str
    resource_id: str
    outcome: str
    reason: str = ""
    timestamp: float = 0.0


@dataclass
class DataProjectionState:
    """Data-plane projection materialized from Journal replay."""

    history_records: dict[str, HistoryMetadataRecord] = field(default_factory=dict)
    history_by_employee_day: dict[tuple[str, str, str], list[str]] = field(
        default_factory=dict
    )
    history_by_task: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    history_by_occurrence: dict[tuple[str, str, str], str] = field(
        default_factory=dict
    )
    execution_attempts: dict[str, AttemptBinding] = field(default_factory=dict)
    employee_documents: dict[str, DocumentMetadataRecord] = field(default_factory=dict)
    latest_employee_document: dict[tuple[str, str, str, str], str] = field(
        default_factory=dict
    )
    legacy_data_sources: dict[tuple[str, str, str], LegacyImportManifest] = field(
        default_factory=dict
    )
    data_authority: DataAuthority = field(default_factory=DataAuthority)
    data_read_audits: dict[str, ReadAuditRecord] = field(default_factory=dict)
    cursor_sequence: int = 0
    cursor_hash: str = ""

    def clone(self) -> DataProjectionState:
        return copy.deepcopy(self)


_DATA_EVENT_TYPES = frozenset(
    {
        "employee.history.recorded",
        "employee.history.tombstoned",
        "employee.data.published",
        "employee.data.tombstoned",
        "employee.execution_attempt.started",
        "employee.data.authority_cutover",
        "employee.data.read_audited",
        "employee.legacy_data_imported",
        "employee.data.projection_failed",
        "employee.data.projection_recovered",
    }
)


def is_data_event(event_type: str) -> bool:
    return event_type in _DATA_EVENT_TYPES


def reduce_data_event(
    state: DataProjectionState,
    event: JournalEvent,
    *,
    frame_sequence: int = 0,
    frame_hash: str = "",
) -> None:
    """Apply one data event to projection state. Raises DataProjectionError on inconsistency."""
    handler = _DATA_REDUCERS.get(event.event_type)
    if handler is None:
        raise DataProjectionError(f"unknown data event: {event.event_type}")
    handler(state, event, frame_sequence, frame_hash)


_DATA_REDUCERS: dict[str, Any] = {}


def _data_reducer(event_type: str):
    def decorator(func):
        _DATA_REDUCERS[event_type] = func
        return func
    return decorator


@_data_reducer("employee.history.recorded")
def _reduce_history_recorded(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    payload = event.payload
    record_id = payload["record_id"]
    if record_id in state.history_records:
        raise DataProjectionError(f"duplicate history record: {record_id}")
    tenant = payload["tenant_key"]
    agent_id = payload["agent_id"]
    occurrence_key = payload["occurrence_key"]
    occ_key = (tenant, agent_id, occurrence_key)
    if occ_key in state.history_by_occurrence:
        raise DataProjectionError(f"duplicate occurrence: {occurrence_key}")
    metadata = HistoryMetadataRecord(
        record_id=record_id,
        occurrence_key=occurrence_key,
        tenant_key=tenant,
        agent_id=agent_id,
        owner_principal_id=payload["owner_principal_id"],
        requester_principal_id=payload["requester_principal_id"],
        task_id=payload["task_id"],
        run_id=payload["run_id"],
        attempt_id=payload["attempt_id"],
        chat_id=payload["chat_id"],
        thread_root_id=payload.get("thread_root_id", ""),
        shard_day=payload["shard_day"],
        status=payload["status"],
        started_at=payload["started_at"],
        ended_at=payload["ended_at"],
        duration_ms=payload["duration_ms"],
        tool=payload["tool"],
        model=payload["model"],
        effort=payload["effort"],
        safe_summary_text=payload["safe_summary"],
        prompt_tokens=payload["prompt_tokens"],
        completion_tokens=payload["completion_tokens"],
        total_tokens=payload["total_tokens"],
        blob_ref=dict(payload["blob_ref"]),
        publish_sequence=frame_sequence,
        publish_frame_hash=frame_hash,
    )
    state.history_records[record_id] = metadata
    state.history_by_occurrence[occ_key] = record_id
    day_key = (tenant, agent_id, payload["shard_day"])
    state.history_by_employee_day.setdefault(day_key, []).append(record_id)
    task_key = (tenant, payload["task_id"])
    state.history_by_task.setdefault(task_key, []).append(record_id)


@_data_reducer("employee.history.tombstoned")
def _reduce_history_tombstoned(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    record_id = event.aggregate_id
    existing = state.history_records.get(record_id)
    if existing is None:
        raise DataProjectionError(f"tombstone for unknown record: {record_id}")
    if existing.tombstoned:
        raise DataProjectionError(f"already tombstoned: {record_id}")
    from dataclasses import replace
    state.history_records[record_id] = replace(existing, tombstoned=True)


@_data_reducer("employee.data.published")
def _reduce_data_published(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    payload = event.payload
    document_id = payload["document_id"]
    if document_id in state.employee_documents:
        raise DataProjectionError(f"duplicate document: {document_id}")
    kind = DataKind(payload["kind"])
    latest_key = (
        payload["tenant_key"],
        payload["agent_id"],
        kind.value,
        payload["source_id"],
    )
    previous_id = state.latest_employee_document.get(latest_key, "")
    if payload["version"] == 1:
        if previous_id or payload.get("previous_document_id", ""):
            raise DataProjectionError("document chain cannot restart at version 1")
    else:
        previous = state.employee_documents.get(previous_id)
        if (
            previous is None
            or payload.get("previous_document_id", "") != previous_id
            or payload["version"] != previous.version + 1
            or payload.get("predecessor_sequence", 0) != previous.publish_sequence
            or payload.get("predecessor_hash", "") != previous.publish_frame_hash
        ):
            raise DataProjectionError("document predecessor mismatch")
    metadata = DocumentMetadataRecord(
        document_id=document_id,
        tenant_key=payload["tenant_key"],
        agent_id=payload["agent_id"],
        owner_principal_id=payload["owner_principal_id"],
        kind=kind,
        version=payload["version"],
        source_id=payload["source_id"],
        content_hash=payload["content_hash"],
        content_type=payload["content_type"],
        blob_ref=dict(payload["blob_ref"]),
        publish_sequence=frame_sequence,
        publish_frame_hash=frame_hash,
        chat_id=payload.get("chat_id", ""),
        thread_root_id=payload.get("thread_root_id", ""),
        previous_document_id=payload.get("previous_document_id", ""),
        predecessor_sequence=payload.get("predecessor_sequence", 0),
        predecessor_hash=payload.get("predecessor_hash", ""),
    )
    state.employee_documents[document_id] = metadata
    state.latest_employee_document[latest_key] = document_id


@_data_reducer("employee.data.tombstoned")
def _reduce_data_tombstoned(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    document_id = event.payload.get("document_id", "")
    existing = state.employee_documents.get(document_id)
    if existing is None:
        raise DataProjectionError(f"tombstone for unknown document: {document_id}")
    if existing.tombstoned:
        raise DataProjectionError(f"already tombstoned: {document_id}")
    from dataclasses import replace
    state.employee_documents[document_id] = replace(existing, tombstoned=True)
    latest_key = (
        existing.tenant_key,
        existing.agent_id,
        existing.kind.value,
        existing.source_id,
    )
    if state.latest_employee_document.get(latest_key) == document_id:
        del state.latest_employee_document[latest_key]


@_data_reducer("employee.execution_attempt.started")
def _reduce_attempt_started(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    payload = event.payload
    attempt_id = payload["attempt_id"]
    if attempt_id in state.execution_attempts:
        raise DataProjectionError(f"duplicate attempt: {attempt_id}")
    state.execution_attempts[attempt_id] = AttemptBinding(
        attempt_id=attempt_id,
        tenant_key=payload["tenant_key"],
        agent_id=payload["agent_id"],
        owner_principal_id=payload["owner_principal_id"],
        requester_principal_id=payload["requester_principal_id"],
        task_id=payload["task_id"],
        run_id=payload["run_id"],
        message_id=payload["message_id"],
        thread_root_id=payload.get("thread_root_id", ""),
        chat_id=payload["chat_id"],
        tool=payload["tool"],
        model=payload["model"],
        effort=payload["effort"],
        started_at=payload["started_at"],
        terminal_epoch=payload["terminal_epoch"],
        publish_sequence=frame_sequence,
    )


@_data_reducer("employee.data.authority_cutover")
def _reduce_authority_cutover(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    payload = event.payload
    if event.aggregate_id != "employee-data-authority":
        raise DataProjectionError("invalid data authority aggregate")
    if set(payload) != {"epoch", "mode", "cutover_sequence"}:
        raise DataProjectionError("invalid data authority payload")
    new_epoch = payload["epoch"]
    if type(new_epoch) is not int or new_epoch <= state.data_authority.epoch:
        raise DataProjectionError("authority epoch must advance monotonically")
    if payload["mode"] != "canonical":
        raise DataProjectionError("invalid data authority mode")
    if payload["cutover_sequence"] != frame_sequence:
        raise DataProjectionError("invalid data authority cutover sequence")
    state.data_authority = DataAuthority(
        epoch=new_epoch,
        mode="canonical",
        cutover_sequence=frame_sequence,
    )


@_data_reducer("employee.data.read_audited")
def _reduce_read_audited(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    payload = event.payload
    audit_id = event.aggregate_id
    if audit_id in state.data_read_audits:
        return
    state.data_read_audits[audit_id] = ReadAuditRecord(
        audit_id=audit_id,
        principal_id=payload["principal_id"],
        operation=payload["operation"],
        resource_id=payload["resource_id"],
        outcome=payload["outcome"],
        reason=payload.get("reason", ""),
        timestamp=event.timestamp,
    )


@_data_reducer("employee.legacy_data_imported")
def _reduce_legacy_imported(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    payload = event.payload
    tenant_key = payload["tenant_key"]
    agent_id = payload["agent_id"]
    source_locator_hash = payload["source_locator_hash"]
    key = (tenant_key, agent_id, source_locator_hash)
    if key in state.legacy_data_sources:
        raise DataProjectionError(f"duplicate legacy import: {source_locator_hash}")
    state.legacy_data_sources[key] = LegacyImportManifest(
        source_locator_hash=source_locator_hash,
        content_hash=payload["content_hash"],
        imported_ids=tuple(payload.get("imported_ids", ())),
        state=payload.get("state", "imported"),
    )


@_data_reducer("employee.data.projection_failed")
def _reduce_projection_failed(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    pass


@_data_reducer("employee.data.projection_recovered")
def _reduce_projection_recovered(
    state: DataProjectionState,
    event: JournalEvent,
    frame_sequence: int,
    frame_hash: str,
) -> None:
    pass
