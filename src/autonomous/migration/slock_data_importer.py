"""Idempotent one-time migration of legacy Slock execution history and memory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..data.models import (
    DataKind,
    EmployeeDataDocumentV1,
    ExecutionAttemptContext,
    ExecutionHistoryPayloadV1,
    ExecutionHistoryRecordV1,
    SafeExecutionSummary,
)
from ..data.service import DataConflictError, EmployeeDataService


@dataclass(frozen=True)
class LegacySourceLocator:
    """Stable locator for one legacy file."""

    relative_path: str
    kind: str

    @property
    def locator_hash(self) -> str:
        return hashlib.sha256(
            f"{self.relative_path}|{self.kind}".encode()
        ).hexdigest()


@dataclass
class ImportManifest:
    """Per-file import tracking."""

    locator_hash: str
    content_hash: str
    state: str = "pending"
    imported_ids: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class SlockDataImportResult:
    """Result of one employee data migration run."""

    agent_id: str
    history_imported: int = 0
    history_skipped: int = 0
    documents_imported: int = 0
    documents_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)


class SlockDataImporter:
    """Migrates legacy execution_history.jsonl and memory files for one employee."""

    def __init__(
        self,
        *,
        service: EmployeeDataService,
        legacy_base: str | Path,
        tenant_key: str,
        owner_principal_id: str,
    ) -> None:
        self._service = service
        self._legacy_base = Path(legacy_base).expanduser()
        self._tenant_key = tenant_key
        self._owner_principal_id = owner_principal_id

    def import_employee(self, agent_id: str) -> SlockDataImportResult:
        """Run idempotent migration for one canonical employee."""
        result = SlockDataImportResult(agent_id=agent_id)
        agent_dir = self._legacy_base / "agents" / agent_id
        if not agent_dir.is_dir():
            return result
        self._import_history(agent_id, agent_dir, result)
        self._import_l1_memory(agent_id, agent_dir, result)
        self._import_skill_profile(agent_id, agent_dir, result)
        self._import_reasoning(agent_id, agent_dir, result)
        return result

    def _import_history(
        self, agent_id: str, agent_dir: Path, result: SlockDataImportResult
    ) -> None:
        history_file = agent_dir / "execution_history.jsonl"
        if not history_file.exists():
            return
        locator = LegacySourceLocator(
            relative_path=f"agents/{agent_id}/execution_history.jsonl",
            kind="execution_history",
        )
        state = self._service.state
        import_key = (self._tenant_key, agent_id, locator.locator_hash)
        if import_key in state.legacy_data_sources:
            result.history_skipped += 1
            return
        try:
            raw = history_file.read_bytes()
        except OSError as exc:
            result.errors.append(f"history read failed: {exc}")
            return
        content_hash = hashlib.sha256(raw).hexdigest()
        lines = raw.strip().split(b"\n") if raw.strip() else []
        imported_ids: list[str] = []
        for ordinal, line in enumerate(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                result.errors.append(f"history line {ordinal}: malformed JSON")
                continue
            record_id = self._import_history_row(agent_id, row, ordinal, locator, content_hash, result)
            if record_id:
                imported_ids.append(record_id)
        self._commit_import_event(agent_id, locator, content_hash, imported_ids)

    def _import_history_row(
        self,
        agent_id: str,
        row: dict[str, Any],
        ordinal: int,
        locator: LegacySourceLocator,
        file_hash: str,
        result: SlockDataImportResult,
    ) -> str | None:
        try:
            row_id = hashlib.sha256(
                f"{locator.locator_hash}|{file_hash}|{ordinal}".encode()
            ).hexdigest()[:16]
            ts = row.get("timestamp") or row.get("ended_at") or row.get("ts", "")
            if isinstance(ts, (int, float)):
                from datetime import UTC, datetime
                ended_at = datetime.fromtimestamp(ts, tz=UTC).isoformat()
            else:
                ended_at = str(ts) if ts else "2026-01-01T00:00:00+00:00"
            if not ended_at.endswith("+00:00") and not ended_at.endswith("Z"):
                ended_at = ended_at + "+00:00"
            ended_at = ended_at.replace("Z", "+00:00")
            duration = row.get("duration_ms") or row.get("duration", 0)
            if isinstance(duration, float):
                duration = int(duration)
            success = row.get("success", row.get("status") == "completed")
            status = "completed" if success else "failed"
            started_at_str = row.get("started_at", "")
            if not started_at_str:
                from datetime import UTC, datetime, timedelta
                ended_dt = datetime.fromisoformat(ended_at)
                started_dt = ended_dt - timedelta(milliseconds=max(duration, 0))
                started_at_str = started_dt.isoformat()
            if not started_at_str.endswith("+00:00"):
                started_at_str = started_at_str.replace("Z", "+00:00")
                if not started_at_str.endswith("+00:00"):
                    started_at_str += "+00:00"
            run_id = f"legacy_run_{row_id}"
            attempt_id = f"legacy_attempt_{row_id}"
            ctx = ExecutionAttemptContext(
                tenant_key=self._tenant_key,
                agent_id=agent_id,
                owner_principal_id=self._owner_principal_id,
                requester_principal_id="legacy_attribution_unknown",
                task_id=row.get("task_id", f"legacy_task_{row_id}"),
                run_id=run_id,
                attempt_id=attempt_id,
                message_id=row.get("message_id", f"legacy_msg_{row_id}"),
                thread_root_id=row.get("thread_root_id", ""),
                chat_id=row.get("chat_id", "legacy_unknown"),
                tool=row.get("tool", "unknown"),
                model=row.get("model", "unknown"),
                effort=row.get("effort", "unknown"),
                started_at=started_at_str,
                terminal_epoch=1,
            )
            record = ExecutionHistoryRecordV1.from_attempt(
                ctx,
                ended_at=ended_at,
                status=status,
                safe_summary=SafeExecutionSummary.build(
                    status=status, tool_count=0, attachment_count=0
                ),
                prompt_tokens=row.get("prompt_tokens", 0),
                completion_tokens=row.get("completion_tokens", 0),
                tool_usage=(),
                predecessor_sequence=0,
                predecessor_hash="",
                shard_timezone="UTC",
            )
            payload = ExecutionHistoryPayloadV1(
                record_id=record.record_id,
                occurrence_key=record.occurrence_key,
                request_text=str(row.get("prompt", ""))[:100_000],
                result_text=str(row.get("result", ""))[:100_000],
                error_detail=str(row.get("error", ""))[:10_000],
            )
            self._service.record_history(record, payload)
            result.history_imported += 1
            return record.record_id
        except DataConflictError:
            result.history_skipped += 1
            return None
        except (ValueError, TypeError, KeyError) as exc:
            result.errors.append(f"history row {ordinal}: {type(exc).__name__}")
            return None

    def _import_l1_memory(
        self, agent_id: str, agent_dir: Path, result: SlockDataImportResult
    ) -> None:
        memory_path = agent_dir / "memory" / "MEMORY.md"
        if not memory_path.exists():
            memory_path = agent_dir / "MEMORY.md"
        if not memory_path.exists():
            return
        self._import_document(
            agent_id, memory_path, DataKind.L1_MEMORY, "l1_memory",
            "text/markdown", result,
        )

    def _import_skill_profile(
        self, agent_id: str, agent_dir: Path, result: SlockDataImportResult
    ) -> None:
        skill_path = agent_dir / "skill_profile.json"
        if not skill_path.exists():
            return
        self._import_document(
            agent_id, skill_path, DataKind.SKILL_PROFILE, "skill_profile",
            "application/json", result,
        )

    def _import_reasoning(
        self, agent_id: str, agent_dir: Path, result: SlockDataImportResult
    ) -> None:
        reasoning_dir = agent_dir / "reasoning"
        if not reasoning_dir.is_dir():
            return
        for path in sorted(reasoning_dir.iterdir()):
            if not path.is_file() or not path.name.endswith(".json"):
                continue
            source_id = path.stem
            self._import_document(
                agent_id, path, DataKind.REASONING, source_id,
                "application/json", result,
            )

    def _import_document(
        self,
        agent_id: str,
        file_path: Path,
        kind: DataKind,
        source_id: str,
        content_type: str,
        result: SlockDataImportResult,
    ) -> None:
        try:
            content = file_path.read_bytes()
        except OSError as exc:
            result.errors.append(f"document read failed: {exc}")
            return
        content_hash = hashlib.sha256(content).hexdigest()
        doc_id = f"data_{hashlib.sha256(f'{agent_id}|{kind.value}|{source_id}'.encode()).hexdigest()[:16]}"
        from datetime import UTC, datetime
        doc = EmployeeDataDocumentV1(
            document_id=doc_id,
            tenant_key=self._tenant_key,
            agent_id=agent_id,
            owner_principal_id=self._owner_principal_id,
            kind=kind,
            version=1,
            source_id=source_id,
            created_at=datetime.now(UTC).isoformat(),
            predecessor_sequence=0,
            predecessor_hash="",
            content_type=content_type,
            content_hash=content_hash,
        )
        try:
            self._service.publish_document(doc, content)
            result.documents_imported += 1
        except DataConflictError:
            result.documents_skipped += 1
        except (ValueError, TypeError) as exc:
            result.errors.append(f"document {kind.value}/{source_id}: {type(exc).__name__}")

    def _commit_import_event(
        self,
        agent_id: str,
        locator: LegacySourceLocator,
        content_hash: str,
        imported_ids: list[str],
    ) -> None:
        from ..data.projection import is_data_event, reduce_data_event
        from ..journal.frame import JournalEvent
        event = JournalEvent(
            event_type="employee.legacy_data_imported",
            aggregate_id=f"legacy-data:{hashlib.sha256(self._tenant_key.encode()).hexdigest()[:16]}:{agent_id}:{locator.locator_hash[:16]}",
            payload={
                "tenant_key": self._tenant_key,
                "agent_id": agent_id,
                "source_locator_hash": locator.locator_hash,
                "content_hash": content_hash,
                "imported_ids": imported_ids[:100],
                "state": "imported",
            },
        )
        writer = self._service._writer
        versions = writer.get_aggregate_versions([event.aggregate_id])
        try:
            result = writer.commit(
                [event],
                versions,
                expected_head_sequence=self._service.state.cursor_sequence,
                expected_head_hash=self._service.state.cursor_hash or None,
            )
            frame = result.frame
            for ev in frame.events:
                if is_data_event(ev.event_type):
                    reduce_data_event(
                        self._service.state,
                        ev,
                        frame_sequence=frame.sequence,
                        frame_hash=frame.frame_hash,
                    )
            self._service.state.cursor_sequence = frame.sequence
            self._service.state.cursor_hash = frame.frame_hash
        except Exception:
            pass
