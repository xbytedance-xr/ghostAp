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
from .query import EmployeeDataRequestContextFactory, EmployeeMemoryQuery, HistoryRangeQuery
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
    memory_query: EmployeeMemoryQuery
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
            shard_timezone=self.service.shard_timezone,
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
        source_id = (
            EmployeeDataDocumentV1.memory_summary_source_id(
                chat_id=command.chat_id,
                thread_root_id=command.thread_root_id,
            )
            if command.kind is DataKind.MEMORY_SUMMARY
            else command.source_id
            if command.kind is DataKind.REASONING
            else command.kind.value
        )
        identity = command.idempotency_key or secrets.token_hex(16)
        doc_id = "data_" + hashlib.sha256(
            "\x00".join(
                (
                    command.tenant_key,
                    command.agent_id,
                    command.kind.value,
                    source_id,
                    identity,
                )
            ).encode()
        ).hexdigest()[:16]
        with self.service.employee_dispatch_guard():
            self.service.synchronize_projection_unlocked()
            duplicate = self.state.employee_documents.get(doc_id)
            if duplicate is not None:
                if duplicate.content_hash != content_hash:
                    raise ValueError("document idempotency conflict")
                self.document_materializer.materialize(
                    agent_id=command.agent_id,
                    kind=command.kind,
                    source_id=source_id,
                    content=command.content,
                    content_hash=content_hash,
                )
                return
            latest_key = (
                command.tenant_key,
                command.agent_id,
                command.kind.value,
                source_id,
            )
            previous_id = self.state.latest_employee_document.get(latest_key, "")
            previous = self.state.employee_documents.get(previous_id) if previous_id else None
            doc = EmployeeDataDocumentV1(
                document_id=doc_id,
                tenant_key=command.tenant_key,
                agent_id=command.agent_id,
                owner_principal_id=command.owner_principal_id,
                kind=command.kind,
                version=1 if previous is None else previous.version + 1,
                source_id=source_id,
                created_at=datetime.now(UTC).isoformat(),
                predecessor_sequence=(0 if previous is None else previous.publish_sequence),
                predecessor_hash=("" if previous is None else previous.publish_frame_hash),
                previous_document_id=previous_id,
                content_type=command.content_type,
                content_hash=content_hash,
                chat_id=(command.chat_id if command.kind == DataKind.MEMORY_SUMMARY else ""),
                thread_root_id=(
                    command.thread_root_id
                    if command.kind == DataKind.MEMORY_SUMMARY
                    else ""
                ),
            )
            self.service.publish_document(doc, command.content)
        self.document_materializer.materialize(
            agent_id=command.agent_id,
            kind=command.kind,
            source_id=source_id,
            content=command.content,
            content_hash=content_hash,
        )

    def rebuild_all(self) -> None:
        """Full projection rebuild from Journal replay."""
        snapshot = self.service.rebuild_projection()
        self.service.verify_live_blobs()
        self.history_materializer.materialize_all(snapshot)
        agent_ids = {
            metadata.agent_id
            for metadata in snapshot.employee_documents.values()
            if not metadata.tombstoned
        }
        for agent_id in sorted(agent_ids):
            self.document_materializer.materialize_from_state(
                snapshot,
                agent_id,
                self.service.read_blob,
            )

    def gc_unreferenced_blobs(self) -> int:
        """Quarantine blobs not referenced by any projected record or document."""
        return self.service.quarantine_unreferenced_blobs()

    def close(self) -> None:
        """Release the owned encrypted BlobStore; the shared Writer is external."""
        self.service.close()


def build_employee_data_composition(
    *,
    settings: Any,
    writer: JournalWriter,
    admin_principal_ids: frozenset[str],
    main_bot_app_id: str,
    agents_root: str | Path,
    legacy_base: str | Path | None = None,
    subject_resolver: Any = None,
    auto_cutover: bool = True,
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
        authority_required=True,
    )
    service.replay_into(state)
    if auto_cutover:
        service.cutover_to_canonical()
    history_mat = DailyHistoryMaterializer(agents_root)
    doc_mat = EmployeeDocumentMaterializer(agents_root)
    memory_facade = EmployeeMemoryFacade(
        materializer=doc_mat,
        state=state,
        legacy_base_path=legacy_base,
        projection_guard=service.read_guard,
    )
    context_factory = EmployeeDataRequestContextFactory(
        admin_principal_ids=admin_principal_ids,
        main_bot_app_id=main_bot_app_id,
        subject_resolver=subject_resolver,
    )
    query = HistoryRangeQuery(
        state=state,
        context_factory=context_factory,
        max_range_days=getattr(settings, "autonomous_history_max_range_days", 31),
        page_size=getattr(settings, "autonomous_history_page_size", 50),
        audit_port=service,
    )
    memory_query = EmployeeMemoryQuery(
        memory_facade=memory_facade,
        state=state,
        context_factory=context_factory,
        audit_port=service,
    )
    return EmployeeDataComposition(
        service=service,
        state=state,
        history_materializer=history_mat,
        document_materializer=doc_mat,
        memory_facade=memory_facade,
        query=query,
        memory_query=memory_query,
        context_factory=context_factory,
    )
