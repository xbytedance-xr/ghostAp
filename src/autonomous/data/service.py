"""Employee data publish service backed by Journal + encrypted BlobStore."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from ..journal.blob_store import BlobStore
from ..journal.frame import JournalEvent
from ..journal.writer import CommitResult, CommitState, JournalWriter
from .models import (
    EmployeeDataDocumentV1,
    ExecutionAttemptContext,
    ExecutionHistoryPayloadV1,
    ExecutionHistoryRecordV1,
)
from .policy import build_document_labels, build_history_labels, validate_blob_ref_labels
from .projection import (
    DataProjectionState,
    JournalHead,
    is_data_event,
    reduce_data_event,
)


class DataServiceError(RuntimeError):
    """Base error for employee data service failures."""


class DataConflictError(DataServiceError):
    """An occurrence with different content already exists."""


class DataHeadRaceError(DataServiceError):
    """The journal head advanced between read and commit."""


class DataBlobError(DataServiceError):
    """Blob publish or validation failed."""


class DataWriteDisabledError(DataServiceError):
    """Data writes are disabled due to authority or anchor failure."""


class AuditPort(Protocol):
    """Injected port for non-sensitive durable audit events."""

    def emit_read_audit(
        self,
        *,
        principal_id: str,
        operation: str,
        resource_id: str,
        outcome: str,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True)
class RecordResult:
    """Outcome of a successful history record publish."""

    record: ExecutionHistoryRecordV1
    commit_result: CommitResult


@dataclass(frozen=True)
class DocumentResult:
    """Outcome of a successful document publish."""

    document: EmployeeDataDocumentV1
    commit_result: CommitResult


@dataclass(frozen=True)
class AttemptResult:
    """Outcome of starting an execution attempt binding."""

    context: ExecutionAttemptContext
    commit_result: CommitResult


class EmployeeDataService:
    """Transactional publish of employee data records into Journal+BlobStore."""

    def __init__(
        self,
        *,
        writer: JournalWriter,
        blob_store: BlobStore,
        data_state: DataProjectionState,
        active_key_id: str,
        shard_timezone: str = "UTC",
    ) -> None:
        self._writer = writer
        self._blob_store = blob_store
        self._state = data_state
        self._active_key_id = active_key_id
        self._shard_timezone = shard_timezone
        self._mutex = threading.RLock()
        self._known_cursor = (
            data_state.cursor_sequence,
            data_state.cursor_hash,
        )

    @property
    def state(self) -> DataProjectionState:
        return self._state

    def get_head(self) -> JournalHead:
        with self._mutex:
            return JournalHead(
                sequence=self._state.cursor_sequence,
                logical_hash=self._state.cursor_hash,
            )

    def close(self) -> None:
        self._blob_store.close()

    @contextmanager
    def read_guard(self) -> Iterator[None]:
        """Hold a consistent data projection view during an authorized read."""
        with self._mutex:
            yield

    def rebuild_projection(self) -> DataProjectionState:
        """Replay and atomically replace the live projection under one owner."""
        with self._mutex:
            fresh = self._replay_into_unlocked(DataProjectionState())
            self._replace_state_unlocked(fresh)
            return fresh

    def quarantine_unreferenced_blobs(self) -> int:
        """Quarantine orphan blobs against one locked projection snapshot."""
        with self._mutex:
            live_ids = {
                blob_id
                for record in self._state.history_records.values()
                if (blob_id := record.blob_ref.get("blob_id", ""))
            }
            live_ids.update(
                blob_id
                for document in self._state.employee_documents.values()
                if (blob_id := document.blob_ref.get("blob_id", ""))
            )
            orphan_ids = set(self._blob_store.iter_blob_ids()) - live_ids
            quarantined = 0
            for blob_id in orphan_ids:
                try:
                    self._blob_store.quarantine_blob(blob_id)
                    quarantined += 1
                except Exception:
                    continue
            return quarantined

    def start_attempt(self, context: ExecutionAttemptContext) -> AttemptResult:
        """Anchor an immutable attempt binding before ACP dispatch."""
        if not isinstance(context, ExecutionAttemptContext):
            raise TypeError("context must be ExecutionAttemptContext")
        with self._mutex, self._writer.transaction_guard():
            self._synchronize_projection_unlocked()
            if context.attempt_id in self._state.execution_attempts:
                raise DataConflictError(f"attempt already started: {context.attempt_id}")
            event = JournalEvent(
                event_type="employee.execution_attempt.started",
                aggregate_id=context.attempt_id,
                payload=context.to_dict(),
            )
            versions = self._writer.get_aggregate_versions([context.attempt_id])
            result = self._writer.commit(
                [event],
                versions,
                expected_head_sequence=self._state.cursor_sequence,
                expected_head_hash=self._state.cursor_hash or None,
            )
            if result.state != CommitState.ANCHORED:
                raise DataWriteDisabledError("attempt commit not anchored")
            self._apply_frame(result)
            return AttemptResult(context=context, commit_result=result)

    def record_history(
        self,
        record: ExecutionHistoryRecordV1,
        sensitive_payload: ExecutionHistoryPayloadV1,
    ) -> RecordResult:
        """Publish one terminal history record with encrypted sensitive payload."""
        if not isinstance(record, ExecutionHistoryRecordV1):
            raise TypeError("record must be ExecutionHistoryRecordV1")
        if not isinstance(sensitive_payload, ExecutionHistoryPayloadV1):
            raise TypeError("sensitive_payload must be ExecutionHistoryPayloadV1")
        if record.record_id != sensitive_payload.record_id:
            raise ValueError("record/payload ID mismatch")
        if record.occurrence_key != sensitive_payload.occurrence_key:
            raise ValueError("record/payload occurrence mismatch")
        with self._mutex, self._writer.transaction_guard():
            self._synchronize_projection_unlocked()
            occ_key = (record.tenant_key, record.agent_id, record.occurrence_key)
            existing_id = self._state.history_by_occurrence.get(occ_key)
            if existing_id is not None:
                existing = self._state.history_records[existing_id]
                payload_bytes = _canonical_payload(sensitive_payload)
                payload_hash = hashlib.sha256(payload_bytes).hexdigest()
                existing_blob_hash = existing.blob_ref.get("content_hash", "")
                if existing_blob_hash == payload_hash:
                    return RecordResult(
                        record=record,
                        commit_result=CommitResult(
                            frame=None,  # type: ignore[arg-type]
                            state=CommitState.ANCHORED,
                        ),
                    )
                raise DataConflictError(f"occurrence conflict: {record.occurrence_key}")
            labels = build_history_labels(
                record.tenant_key,
                record.owner_principal_id,
                record.record_id,
            )
            payload_bytes = _canonical_payload(sensitive_payload)
            blob_ref = self._blob_store.stage_and_publish(
                payload_bytes,
                labels,
                self._active_key_id,
            )
            validate_blob_ref_labels(blob_ref, labels)
            readback = self._blob_store.read(blob_ref)
            if readback != payload_bytes:
                raise DataBlobError("blob readback verification failed")
            event_payload: dict[str, Any] = {
                **record.to_dict(),
                "safe_summary": record.safe_summary.text,
                "blob_ref": blob_ref.to_dict(),
            }
            del event_payload["tool_usage"]
            event = JournalEvent(
                event_type="employee.history.recorded",
                aggregate_id=record.record_id,
                payload=event_payload,
            )
            versions = self._writer.get_aggregate_versions([record.record_id])
            result = self._writer.commit(
                [event],
                versions,
                expected_head_sequence=self._state.cursor_sequence,
                expected_head_hash=self._state.cursor_hash or None,
            )
            if result.state != CommitState.ANCHORED:
                raise DataWriteDisabledError("history commit not anchored")
            self._apply_frame(result)
            return RecordResult(record=record, commit_result=result)

    def publish_document(
        self,
        document: EmployeeDataDocumentV1,
        content: bytes,
    ) -> DocumentResult:
        """Publish one employee data document with encrypted content."""
        if not isinstance(document, EmployeeDataDocumentV1):
            raise TypeError("document must be EmployeeDataDocumentV1")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash != document.content_hash:
            raise ValueError("content hash does not match document")
        with self._mutex, self._writer.transaction_guard():
            self._synchronize_projection_unlocked()
            if document.document_id in self._state.employee_documents:
                raise DataConflictError(f"document already exists: {document.document_id}")
            labels = build_document_labels(
                tenant_key=document.tenant_key,
                owner_principal_id=document.owner_principal_id,
                document_id=document.document_id,
                kind=document.kind.value,
            )
            blob_ref = self._blob_store.stage_and_publish(
                content,
                labels,
                self._active_key_id,
            )
            validate_blob_ref_labels(blob_ref, labels)
            readback = self._blob_store.read(blob_ref)
            if readback != content:
                raise DataBlobError("blob readback verification failed")
            aggregate_id = (
                f"data-chain:{_hash(document.tenant_key)}:"
                f"{document.agent_id}:{document.kind.value}:"
                f"{_hash(document.source_id)}"
            )
            event_payload: dict[str, Any] = {
                **document.to_dict(),
                "kind": document.kind.value,
                "blob_ref": blob_ref.to_dict(),
            }
            event = JournalEvent(
                event_type="employee.data.published",
                aggregate_id=aggregate_id,
                payload=event_payload,
            )
            versions = self._writer.get_aggregate_versions([aggregate_id])
            result = self._writer.commit(
                [event],
                versions,
                expected_head_sequence=self._state.cursor_sequence,
                expected_head_hash=self._state.cursor_hash or None,
            )
            if result.state != CommitState.ANCHORED:
                raise DataWriteDisabledError("document commit not anchored")
            self._apply_frame(result)
            return DocumentResult(document=document, commit_result=result)

    def replay_into(self, state: DataProjectionState) -> DataProjectionState:
        """Full Journal replay populating a fresh state."""
        with self._mutex:
            replayed = self._replay_into_unlocked(state)
            if state is self._state:
                self._known_cursor = (
                    state.cursor_sequence,
                    state.cursor_hash,
                )
            return replayed

    def _replay_into_unlocked(
        self,
        state: DataProjectionState,
    ) -> DataProjectionState:
        for frame in self._writer.replay():
            for event in frame.events:
                if is_data_event(event.event_type):
                    reduce_data_event(
                        state,
                        event,
                        frame_sequence=frame.sequence,
                        frame_hash=frame.frame_hash,
                    )
            state.cursor_sequence = frame.sequence
            state.cursor_hash = frame.frame_hash
        return state

    def _synchronize_projection_unlocked(self) -> None:
        if (
            self._state.cursor_sequence,
            self._state.cursor_hash,
        ) != self._known_cursor:
            return
        last = self._writer.get_last_frame()
        sequence = 0 if last is None else last.sequence
        logical_hash = "" if last is None else last.frame_hash
        if self._state.cursor_sequence != sequence or self._state.cursor_hash != logical_hash:
            self._replace_state_unlocked(self._replay_into_unlocked(DataProjectionState()))

    def _replace_state_unlocked(self, fresh: DataProjectionState) -> None:
        self._state.history_records = fresh.history_records
        self._state.history_by_employee_day = fresh.history_by_employee_day
        self._state.history_by_task = fresh.history_by_task
        self._state.history_by_occurrence = fresh.history_by_occurrence
        self._state.execution_attempts = fresh.execution_attempts
        self._state.employee_documents = fresh.employee_documents
        self._state.latest_employee_document = fresh.latest_employee_document
        self._state.legacy_data_sources = fresh.legacy_data_sources
        self._state.data_authority = fresh.data_authority
        self._state.data_read_audits = fresh.data_read_audits
        self._state.cursor_sequence = fresh.cursor_sequence
        self._state.cursor_hash = fresh.cursor_hash
        self._known_cursor = (fresh.cursor_sequence, fresh.cursor_hash)

    def _apply_frame(self, result: CommitResult) -> None:
        frame = result.frame
        for event in frame.events:
            if is_data_event(event.event_type):
                reduce_data_event(
                    self._state,
                    event,
                    frame_sequence=frame.sequence,
                    frame_hash=frame.frame_hash,
                )
        self._state.cursor_sequence = frame.sequence
        self._state.cursor_hash = frame.frame_hash
        self._known_cursor = (frame.sequence, frame.frame_hash)


def _canonical_payload(payload: ExecutionHistoryPayloadV1) -> bytes:
    return json.dumps(
        payload.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
