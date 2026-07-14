"""Integration tests for Slock data migration to canonical Journal storage."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from src.autonomous.data.projection import DataProjectionState
from src.autonomous.data.service import EmployeeDataService
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.migration.slock_data_importer import SlockDataImporter


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


def _setup(tmp_path: Path):
    key = secrets.token_bytes(32)
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
    return svc, state, writer, blob_store


def _legacy_dir(tmp_path: Path, agent_id: str) -> Path:
    agent_dir = tmp_path / "legacy" / "agents" / agent_id
    agent_dir.mkdir(parents=True)
    return agent_dir


class TestSlockDataImporter:
    def test_imports_history_jsonl(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        rows = [
            {"timestamp": 1720742400, "success": True, "prompt": "do task",
             "result": "done", "tool": "codex", "model": "gpt-test",
             "prompt_tokens": 10, "completion_tokens": 5, "duration_ms": 1000},
            {"timestamp": 1720742500, "success": False, "prompt": "fail task",
             "result": "", "error": "timeout", "tool": "codex", "model": "gpt-test",
             "duration_ms": 5000},
        ]
        history_file = agent_dir / "execution_history.jsonl"
        history_file.write_text("\n".join(json.dumps(r) for r in rows))

        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        result = importer.import_employee("agt_alpha")
        assert result.history_imported == 2
        assert result.errors == []
        assert len(state.history_records) == 2
        blob_store.close()
        writer.close()

    def test_imports_l1_memory(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        memory_dir = agent_dir / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("# Agent memory")

        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        result = importer.import_employee("agt_alpha")
        assert result.documents_imported >= 1
        blob_store.close()
        writer.close()

    def test_root_l1_is_retired_only_after_verified_import(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        root_memory = agent_dir / "MEMORY.md"
        root_memory.write_text("# root legacy memory")

        result = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        ).import_employee("agt_alpha")

        assert result.errors == []
        assert not root_memory.exists()
        retired = tuple((agent_dir / ".legacy-imported").glob("MEMORY.*.md"))
        assert len(retired) == 1
        assert retired[0].read_text() == "# root legacy memory"
        blob_store.close()
        writer.close()

    def test_multiple_legacy_l1_files_block_import(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        (agent_dir / "MEMORY.md").write_text("root")
        (agent_dir / "memory").mkdir()
        (agent_dir / "memory" / "MEMORY.md").write_text("nested")

        result = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        ).import_employee("agt_alpha")

        assert result.documents_imported == 0
        assert result.errors == ["multiple legacy L1 files exist"]
        blob_store.close()
        writer.close()

    def test_imports_skill_profile(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        (agent_dir / "skill_profile.json").write_text(json.dumps({"skills": ["python"]}))

        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        result = importer.import_employee("agt_alpha")
        assert result.documents_imported >= 1
        blob_store.close()
        writer.close()

    def test_imports_reasoning_files(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        reasoning_dir = agent_dir / "reasoning"
        reasoning_dir.mkdir()
        (reasoning_dir / "task_123.json").write_text(json.dumps({"steps": [1, 2, 3]}))

        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        result = importer.import_employee("agt_alpha")
        assert result.documents_imported >= 1
        blob_store.close()
        writer.close()

    def test_idempotent_second_run_skips(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        (agent_dir / "execution_history.jsonl").write_text(
            json.dumps({"timestamp": 1720742400, "success": True, "tool": "codex",
                        "model": "gpt", "prompt": "x", "result": "y", "duration_ms": 100})
        )

        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        first = importer.import_employee("agt_alpha")
        assert first.history_imported == 1
        second = importer.import_employee("agt_alpha")
        assert second.history_skipped == 1
        assert second.history_imported == 0
        blob_store.close()
        writer.close()

    def test_missing_agent_dir_returns_empty(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        (tmp_path / "legacy" / "agents").mkdir(parents=True)
        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        result = importer.import_employee("agt_nonexistent")
        assert result.history_imported == 0
        assert result.documents_imported == 0
        blob_store.close()
        writer.close()

    def test_malformed_history_row_logged_not_fatal(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        (agent_dir / "execution_history.jsonl").write_text(
            "not valid json\n" + json.dumps({
                "timestamp": 1720742400, "success": True, "tool": "codex",
                "model": "gpt", "prompt": "x", "result": "y", "duration_ms": 100
            })
        )
        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        result = importer.import_employee("agt_alpha")
        assert result.history_imported == 1
        assert len(result.errors) == 1
        assert "malformed JSON" in result.errors[0]
        assert state.legacy_data_sources == {}
        blob_store.close()
        writer.close()

    def test_symlinked_history_is_rejected(self, tmp_path: Path) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        outside = tmp_path / "outside-history.jsonl"
        outside.write_text(json.dumps({"timestamp": 1720742400, "success": True}))
        (agent_dir / "execution_history.jsonl").symlink_to(outside)

        result = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        ).import_employee("agt_alpha")

        assert result.history_imported == 0
        assert result.errors and result.errors[0].startswith("history read failed")
        assert state.history_records == {}
        blob_store.close()
        writer.close()

    def test_symlinked_agent_or_reasoning_directory_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        svc, state, writer, blob_store = _setup(tmp_path)
        outside_agent = tmp_path / "outside-agent"
        outside_agent.mkdir()
        agents = tmp_path / "legacy" / "agents"
        agents.mkdir(parents=True)
        (agents / "agt_symlink").symlink_to(outside_agent, target_is_directory=True)

        importer = SlockDataImporter(
            service=svc,
            legacy_base=tmp_path / "legacy",
            tenant_key="tenant_1",
            owner_principal_id="principal_owner",
        )
        agent_result = importer.import_employee("agt_symlink")
        assert agent_result.errors == [
            "legacy agent directory is not a regular directory"
        ]

        agent_dir = _legacy_dir(tmp_path, "agt_alpha")
        outside_reasoning = tmp_path / "outside-reasoning"
        outside_reasoning.mkdir()
        (agent_dir / "reasoning").symlink_to(
            outside_reasoning,
            target_is_directory=True,
        )
        reasoning_result = importer.import_employee("agt_alpha")
        assert reasoning_result.errors == [
            "reasoning source is not a regular directory"
        ]
        assert state.employee_documents == {}
        blob_store.close()
        writer.close()
