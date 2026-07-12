"""Chaos tests: blob publish failure, anchor failure, and head race boundaries."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest

from src.autonomous.data.models import (
    DataKind,
    EmployeeDataDocumentV1,
    ExecutionAttemptContext,
    ExecutionHistoryPayloadV1,
    ExecutionHistoryRecordV1,
    SafeExecutionSummary,
    ToolUsageV1,
)
from src.autonomous.data.projection import DataProjectionState
from src.autonomous.data.service import (
    DataBlobError,
    DataWriteDisabledError,
    EmployeeDataService,
)
from src.autonomous.journal.blob_store import (
    AesGcmEncryptionProvider,
    BlobPublishError,
    BlobStore,
)
from src.autonomous.journal.writer import CommitState, JournalWriter


class _InMemoryAnchor:
    def __init__(self, *, fail_after: int = -1) -> None:
        self._sequence = 0
        self._hash = "0" * 64
        self._call_count = 0
        self._fail_after = fail_after

    def read(self):
        from src.autonomous.journal.anchor import AnchorState
        return AnchorState(self._sequence, self._hash)

    def compare_and_swap(
        self,
        expected_sequence: int,
        expected_hash: str,
        new_sequence: int,
        new_hash: str,
    ) -> bool:
        self._call_count += 1
        if self._fail_after >= 0 and self._call_count > self._fail_after:
            return False
        if self._sequence == expected_sequence and self._hash == expected_hash:
            self._sequence = new_sequence
            self._hash = new_hash
            return True
        return False


def _key() -> bytes:
    return secrets.token_bytes(32)


def _context() -> ExecutionAttemptContext:
    return ExecutionAttemptContext(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        owner_principal_id="principal_owner",
        requester_principal_id="principal_requester",
        task_id="task_1",
        run_id="run_1",
        attempt_id="attempt_1",
        message_id="message_1",
        thread_root_id="thread_1",
        chat_id="chat_1",
        tool="codex",
        model="gpt-test",
        effort="high",
        started_at="2026-07-12T00:30:00+00:00",
        terminal_epoch=1,
    )


def _record() -> ExecutionHistoryRecordV1:
    return ExecutionHistoryRecordV1.from_attempt(
        _context(),
        ended_at="2026-07-12T01:30:00+00:00",
        status="completed",
        safe_summary=SafeExecutionSummary.build(status="completed", tool_count=1, attachment_count=0),
        prompt_tokens=10,
        completion_tokens=5,
        tool_usage=(),
        predecessor_sequence=0,
        predecessor_hash="",
        shard_timezone="UTC",
    )


def _payload(record: ExecutionHistoryRecordV1) -> ExecutionHistoryPayloadV1:
    return ExecutionHistoryPayloadV1(
        record_id=record.record_id,
        occurrence_key=record.occurrence_key,
        request_text="test",
        result_text="ok",
        error_detail="",
    )


class TestAnchorFailureDisablesWrites:
    def test_anchor_failure_disables_subsequent_writes(self, tmp_path: Path) -> None:
        anchor = _InMemoryAnchor(fail_after=0)
        key = _key()
        provider = AesGcmEncryptionProvider(lambda _ref: key)
        blob_store = BlobStore(tmp_path / "blobs", provider)
        writer = JournalWriter.open(
            tmp_path / "journal",
            anchor=anchor,
            hmac_key=secrets.token_bytes(32),
        )
        state = DataProjectionState()
        svc = EmployeeDataService(
            writer=writer,
            blob_store=blob_store,
            data_state=state,
            active_key_id="k1",
        )
        with pytest.raises(DataWriteDisabledError):
            svc.start_attempt(_context())
        blob_store.close()
        writer.close()


class TestBlobPublishFailure:
    def test_blob_failure_does_not_commit_event(self, tmp_path: Path) -> None:
        key = _key()
        provider = AesGcmEncryptionProvider(lambda _ref: key)
        blob_store = BlobStore(tmp_path / "blobs", provider)
        writer = JournalWriter.open(
            tmp_path / "journal",
            anchor=_InMemoryAnchor(),
            hmac_key=secrets.token_bytes(32),
        )
        state = DataProjectionState()
        svc = EmployeeDataService(
            writer=writer,
            blob_store=blob_store,
            data_state=state,
            active_key_id="k1",
        )
        record = _record()
        payload = _payload(record)
        with patch.object(blob_store, "stage_and_publish", side_effect=BlobPublishError("disk full")):
            with pytest.raises(BlobPublishError):
                svc.record_history(record, payload)
        assert record.record_id not in state.history_records
        assert state.cursor_sequence == 0
        blob_store.close()
        writer.close()


class TestHeadRaceRetry:
    def test_stale_head_causes_integrity_error(self, tmp_path: Path) -> None:
        key = _key()
        provider = AesGcmEncryptionProvider(lambda _ref: key)
        blob_store = BlobStore(tmp_path / "blobs", provider)
        writer = JournalWriter.open(
            tmp_path / "journal",
            anchor=_InMemoryAnchor(),
            hmac_key=secrets.token_bytes(32),
        )
        state = DataProjectionState()
        svc = EmployeeDataService(
            writer=writer,
            blob_store=blob_store,
            data_state=state,
            active_key_id="k1",
        )
        svc.start_attempt(_context())
        state.cursor_sequence = 0
        state.cursor_hash = ""
        ctx2 = ExecutionAttemptContext(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            owner_principal_id="principal_owner",
            requester_principal_id="principal_requester",
            task_id="task_2",
            run_id="run_2",
            attempt_id="attempt_2",
            message_id="message_2",
            thread_root_id="thread_2",
            chat_id="chat_2",
            tool="codex",
            model="gpt-test",
            effort="high",
            started_at="2026-07-12T00:30:00+00:00",
            terminal_epoch=1,
        )
        from src.autonomous.journal.frame import JournalIntegrityError
        with pytest.raises(JournalIntegrityError, match="head mismatch"):
            svc.start_attempt(ctx2)
        blob_store.close()
        writer.close()


class TestMultipleTerminalStatuses:
    @pytest.mark.parametrize("status", ["completed", "failed", "canceled", "timeout", "action_required"])
    def test_all_terminal_statuses_commit(self, tmp_path: Path, status: str) -> None:
        key = _key()
        provider = AesGcmEncryptionProvider(lambda _ref: key)
        blob_store = BlobStore(tmp_path / "blobs", provider)
        writer = JournalWriter.open(
            tmp_path / "journal",
            anchor=_InMemoryAnchor(),
            hmac_key=secrets.token_bytes(32),
        )
        state = DataProjectionState()
        svc = EmployeeDataService(
            writer=writer,
            blob_store=blob_store,
            data_state=state,
            active_key_id="k1",
        )
        ctx = ExecutionAttemptContext(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            owner_principal_id="principal_owner",
            requester_principal_id="principal_requester",
            task_id="task_1",
            run_id="run_1",
            attempt_id=f"attempt_{status}",
            message_id="message_1",
            thread_root_id="",
            chat_id="chat_1",
            tool="codex",
            model="gpt-test",
            effort="high",
            started_at="2026-07-12T00:30:00+00:00",
            terminal_epoch=1,
        )
        record = ExecutionHistoryRecordV1.from_attempt(
            ctx,
            ended_at="2026-07-12T01:30:00+00:00",
            status=status,
            safe_summary=SafeExecutionSummary.build(status=status, tool_count=0, attachment_count=0),
            prompt_tokens=0,
            completion_tokens=0,
            tool_usage=(),
            predecessor_sequence=0,
            predecessor_hash="",
            shard_timezone="UTC",
        )
        payload = ExecutionHistoryPayloadV1(
            record_id=record.record_id,
            occurrence_key=record.occurrence_key,
            request_text="",
            result_text="",
            error_detail="" if status == "completed" else "timed out",
        )
        result = svc.record_history(record, payload)
        assert result.commit_result.state == CommitState.ANCHORED
        metadata = state.history_records[record.record_id]
        assert metadata.status == status
        blob_store.close()
        writer.close()
