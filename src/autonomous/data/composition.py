"""Production composition: wires data keyring, BlobStore, service, and materializers."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..journal.writer import JournalWriter
from .facades import EmployeeDocumentMaterializer, EmployeeMemoryFacade
from .keyring import build_employee_data_storage
from .materializer import DailyHistoryMaterializer
from .models import (
    DataKind,
    EmployeeDataDocumentV1,
    ExecutionAttemptContext,
    ExecutionHistoryPayloadV1,
    ExecutionHistoryRecordV1,
    SafeExecutionSummary,
    ToolUsageV1,
)
from .ports import AuthenticatedExecutionTerminal, PublishEmployeeDocumentCommand
from .projection import DataProjectionState
from .query import (
    EmployeeDataRequestContextFactory,
    HistoryRangeQuery,
)
from .service import EmployeeDataService


@dataclass
class EmployeeDataComposition:
    """Complete wired data-plane ready for production use."""

    service: EmployeeDataService
    state: DataProjectionState
    history_materializer: DailyHistoryMaterializer
    document_materializer: EmployeeDocumentMaterializer
    memory_facade: EmployeeMemoryFacade
    query: HistoryRangeQuery
    context_factory: EmployeeDataRequestContextFactory

    def record_terminal(
        self,
        terminal: AuthenticatedExecutionTerminal,
    ) -> None:
        """Record one terminal execution outcome from trusted orchestration."""
        binding = self.state.execution_attempts.get(terminal.attempt_id)
        if binding is None:
            raise ValueError(f"no anchored attempt: {terminal.attempt_id}")
        ctx = ExecutionAttemptContext(
            tenant_key=binding.tenant_key,
            agent_id=binding.agent_id,
            owner_principal_id=binding.owner_principal_id,
            requester_principal_id=binding.requester_principal_id,
            task_id=binding.task_id,
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            message_id=binding.message_id,
            thread_root_id=binding.thread_root_id,
            chat_id=binding.chat_id,
            tool=binding.tool,
            model=binding.model,
            effort=binding.effort,
            started_at=binding.started_at,
            terminal_epoch=binding.terminal_epoch,
        )
        ended_at = datetime.now(UTC).isoformat()
        record = ExecutionHistoryRecordV1.from_attempt(
            ctx,
            ended_at=ended_at,
            status=terminal.status,
            safe_summary=SafeExecutionSummary.build(
                status=terminal.status,
                tool_count=len(terminal.tool_usage),
                attachment_count=len(terminal.attachments),
            ),
            prompt_tokens=terminal.prompt_tokens,
            completion_tokens=terminal.completion_tokens,
            tool_usage=tuple(
                ToolUsageV1(t["name"], t["count"], t["duration_ms"], t["status"])
                for t in terminal.tool_usage
            ) if terminal.tool_usage else (),
            predecessor_sequence=0,
            predecessor_hash="",
            shard_timezone="UTC",
        )
        payload = ExecutionHistoryPayloadV1(
            record_id=record.record_id,
            occurrence_key=record.occurrence_key,
            request_text=terminal.request_text[:1_000_000],
            result_text=terminal.result_text[:1_000_000],
            error_detail=terminal.error_detail[:100_000],
            attachments=terminal.attachments,
            tool_calls=(),
        )
        self.service.record_history(record, payload)

    def publish_document(
        self,
        command: PublishEmployeeDocumentCommand,
    ) -> None:
        """Publish one employee document from trusted orchestration."""
        content_hash = hashlib.sha256(command.content).hexdigest()
        doc_id = f"data_{secrets.token_hex(8)}"
        doc = EmployeeDataDocumentV1(
            document_id=doc_id,
            tenant_key=command.tenant_key,
            agent_id=command.agent_id,
            owner_principal_id=command.owner_principal_id,
            kind=command.kind,
            version=1,
            source_id=command.kind.value if command.kind != DataKind.MEMORY_SUMMARY else
                EmployeeDataDocumentV1.memory_summary_source_id(
                    chat_id=command.chat_id, thread_root_id=command.thread_root_id
                ),
            created_at=datetime.now(UTC).isoformat(),
            predecessor_sequence=0,
            predecessor_hash="",
            content_type=command.content_type,
            content_hash=content_hash,
            chat_id=command.chat_id if command.kind == DataKind.MEMORY_SUMMARY else "",
            thread_root_id=command.thread_root_id if command.kind == DataKind.MEMORY_SUMMARY else "",
        )
        self.service.publish_document(doc, command.content)
        self.document_materializer.materialize(
            agent_id=command.agent_id,
            kind=command.kind,
            source_id=doc.source_id,
            content=command.content,
            content_hash=content_hash,
        )

    def rebuild_all(self) -> None:
        """Full projection rebuild from Journal replay."""
        fresh = DataProjectionState()
        self.service.replay_into(fresh)
        self.state.history_records = fresh.history_records
        self.state.history_by_employee_day = fresh.history_by_employee_day
        self.state.history_by_task = fresh.history_by_task
        self.state.history_by_occurrence = fresh.history_by_occurrence
        self.state.execution_attempts = fresh.execution_attempts
        self.state.employee_documents = fresh.employee_documents
        self.state.latest_employee_document = fresh.latest_employee_document
        self.state.legacy_data_sources = fresh.legacy_data_sources
        self.state.data_authority = fresh.data_authority
        self.state.data_read_audits = fresh.data_read_audits
        self.state.cursor_sequence = fresh.cursor_sequence
        self.state.cursor_hash = fresh.cursor_hash
        self.history_materializer.materialize_all(self.state)


def build_employee_data_composition(
    *,
    settings: Any,
    writer: JournalWriter,
    admin_principal_ids: frozenset[str],
    main_bot_app_id: str,
    agents_root: str | Path,
    legacy_base: str | Path | None = None,
) -> EmployeeDataComposition:
    """Factory that constructs the full wired data-plane from settings."""
    storage = build_employee_data_storage(settings)
    state = DataProjectionState()
    service = EmployeeDataService(
        writer=writer,
        blob_store=storage.blob_store,
        data_state=state,
        active_key_id=storage.active_key_id,
        shard_timezone=getattr(settings, "autonomous_history_timezone", "UTC"),
    )
    service.replay_into(state)
    history_mat = DailyHistoryMaterializer(agents_root)
    doc_mat = EmployeeDocumentMaterializer(agents_root)
    memory_facade = EmployeeMemoryFacade(
        materializer=doc_mat,
        state=state,
        legacy_base_path=legacy_base,
    )
    context_factory = EmployeeDataRequestContextFactory(
        admin_principal_ids=admin_principal_ids,
        main_bot_app_id=main_bot_app_id,
    )
    query = HistoryRangeQuery(
        state=state,
        context_factory=context_factory,
        max_range_days=getattr(settings, "autonomous_history_max_range_days", 31),
        page_size=getattr(settings, "autonomous_history_page_size", 50),
    )
    return EmployeeDataComposition(
        service=service,
        state=state,
        history_materializer=history_mat,
        document_materializer=doc_mat,
        memory_facade=memory_facade,
        query=query,
        context_factory=context_factory,
    )
