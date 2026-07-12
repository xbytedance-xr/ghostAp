"""Integration test: full employee data composition end-to-end."""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr

from src.autonomous.data.composition import (
    EmployeeDataComposition,
    build_employee_data_composition,
)
from src.autonomous.data.models import DataKind, ExecutionAttemptContext
from src.autonomous.data.ports import (
    AuthenticatedExecutionTerminal,
    PublishEmployeeDocumentCommand,
)
from src.autonomous.data.projection import DataProjectionState
from src.autonomous.data.query import AuthenticatedDataRequest, HistoryQuerySpec
from src.autonomous.journal.writer import CommitState, JournalWriter


class _InMemoryAnchor:
    def __init__(self) -> None:
        self._sequence = 0
        self._hash = "0" * 64

    def read(self):
        from src.autonomous.journal.anchor import AnchorState
        return AnchorState(self._sequence, self._hash)

    def compare_and_swap(self, es, eh, ns, nh) -> bool:
        if self._sequence == es and self._hash == eh:
            self._sequence = ns
            self._hash = nh
            return True
        return False


class _FakeSettings:
    def __init__(self, tmp_path: Path) -> None:
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.autonomous_data_keys = SecretStr(
            json.dumps({"version": 1, "keys": {"k1": key}})
        )
        self.autonomous_data_active_key_id = "k1"
        self.autonomous_data_blob_dir = str(tmp_path / "data-blobs")
        self.autonomous_history_timezone = "UTC"
        self.autonomous_history_max_range_days = 31
        self.autonomous_history_page_size = 50


@pytest.fixture
def composition(tmp_path: Path) -> EmployeeDataComposition:
    settings = _FakeSettings(tmp_path)
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=_InMemoryAnchor(),
        hmac_key=secrets.token_bytes(32),
    )
    comp = build_employee_data_composition(
        settings=settings,
        writer=writer,
        admin_principal_ids=frozenset({"admin_1"}),
        main_bot_app_id="main_bot",
        agents_root=tmp_path / "agents",
        legacy_base=tmp_path / "legacy",
    )
    yield comp
    comp.service._blob_store.close()
    writer.close()


class TestFullComposition:
    def test_start_attempt_record_terminal_and_query(
        self, composition: EmployeeDataComposition
    ) -> None:
        ctx = ExecutionAttemptContext(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            owner_principal_id="principal_owner",
            requester_principal_id="principal_requester",
            task_id="task_1",
            run_id="run_1",
            attempt_id="attempt_1",
            message_id="msg_1",
            thread_root_id="",
            chat_id="chat_1",
            tool="codex",
            model="gpt-test",
            effort="high",
            started_at="2026-07-12T00:30:00+00:00",
            terminal_epoch=1,
        )
        composition.service.start_attempt(ctx)
        terminal = AuthenticatedExecutionTerminal(
            attempt_id="attempt_1",
            status="completed",
            request_text="do something",
            result_text="done",
            error_detail="",
            prompt_tokens=100,
            completion_tokens=50,
        )
        composition.record_terminal(terminal)
        assert len(composition.state.history_records) == 1
        request = AuthenticatedDataRequest(
            principal_id="admin_1",
            tenant_key="tenant_1",
            receiving_bot_app_id="main_bot",
            chat_id="dm",
            chat_type="p2p",
            thread_root_id="",
            requested_agent_id="agt_alpha",
        )
        from datetime import date, timedelta
        today = date.today().isoformat()
        result = composition.query.query(
            request,
            HistoryQuerySpec(start_day=today, end_day=today),
        )
        assert result.total_available == 1
        assert result.records[0].status == "completed"

    def test_publish_document_materializes_file(
        self, composition: EmployeeDataComposition
    ) -> None:
        command = PublishEmployeeDocumentCommand(
            agent_id="agt_alpha",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
            kind=DataKind.L1_MEMORY,
            source_id="l1_memory",
            content=b"# Memory content",
            content_type="text/markdown",
        )
        composition.publish_document(command)
        assert len(composition.state.employee_documents) == 1
        l1 = composition.memory_facade.read_l1("agt_alpha", "tenant_1")
        assert l1 == "# Memory content"

    def test_rebuild_recovers_state(
        self, composition: EmployeeDataComposition
    ) -> None:
        ctx = ExecutionAttemptContext(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            owner_principal_id="principal_owner",
            requester_principal_id="principal_requester",
            task_id="task_2",
            run_id="run_2",
            attempt_id="attempt_rebuild",
            message_id="msg_2",
            thread_root_id="",
            chat_id="chat_1",
            tool="codex",
            model="gpt-test",
            effort="high",
            started_at="2026-07-12T02:00:00+00:00",
            terminal_epoch=1,
        )
        composition.service.start_attempt(ctx)
        composition.state.execution_attempts.clear()
        composition.state.cursor_sequence = 0
        composition.state.cursor_hash = ""
        composition.rebuild_all()
        assert "attempt_rebuild" in composition.state.execution_attempts

    def test_unanchored_attempt_raises(
        self, composition: EmployeeDataComposition
    ) -> None:
        terminal = AuthenticatedExecutionTerminal(
            attempt_id="nonexistent_attempt",
            status="failed",
            request_text="",
            result_text="",
            error_detail="no such attempt",
        )
        with pytest.raises(ValueError, match="no anchored attempt"):
            composition.record_terminal(terminal)

    def test_memory_summary_publishes(
        self, composition: EmployeeDataComposition
    ) -> None:
        command = PublishEmployeeDocumentCommand(
            agent_id="agt_alpha",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
            kind=DataKind.MEMORY_SUMMARY,
            source_id="",
            content=b"# Chat summary",
            content_type="text/markdown",
            chat_id="chat_1",
            thread_root_id="",
        )
        composition.publish_document(command)
        summary = composition.memory_facade.read_memory_summary(
            "agt_alpha", "tenant_1", "chat_1"
        )
        assert summary == "# Chat summary"
