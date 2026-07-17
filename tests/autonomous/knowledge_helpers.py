from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from src.autonomous.data.composition import build_employee_data_composition
from src.autonomous.data.models import ExecutionAttemptContext
from src.autonomous.data.ports import AuthenticatedExecutionTerminal
from src.autonomous.journal.anchor import FileAnchor
from src.autonomous.journal.writer import JournalWriter


def build_knowledge_composition(tmp_path: Path):
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    settings = SimpleNamespace(
        autonomous_data_keys=SecretStr(json.dumps({"version": 1, "keys": {"k1": key}})),
        autonomous_data_active_key_id="k1",
        autonomous_data_blob_dir=str(tmp_path / "data-blobs"),
        autonomous_history_timezone="UTC",
        autonomous_history_max_range_days=31,
        autonomous_history_page_size=50,
    )
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=secrets.token_bytes(32),
    )
    composition = build_employee_data_composition(
        settings=settings,
        writer=writer,
        admin_principal_ids=frozenset({"ou_admin"}),
        main_bot_app_id="main_bot",
        agents_root=tmp_path / "agents",
    )
    return writer, composition


def seed_terminal(composition, suffix: str, result_text: str):
    attempt_id = f"attempt_{suffix}"
    composition.service.start_attempt(
        ExecutionAttemptContext(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            owner_principal_id="ou_owner",
            requester_principal_id="ou_requester",
            task_id=f"task_{suffix}",
            run_id=f"run_{suffix}",
            attempt_id=attempt_id,
            message_id=f"om_{suffix}",
            thread_root_id="",
            chat_id="oc_team",
            tool="codex",
            model="gpt-test",
            effort="high",
            started_at="2026-07-17T00:00:00+00:00",
        )
    )
    terminal = AuthenticatedExecutionTerminal(
        attempt_id=attempt_id,
        status="completed",
        request_text="task",
        result_text=result_text,
        error_detail="",
    )
    composition.record_terminal(terminal)
    return terminal


def close_knowledge_composition(writer, composition) -> None:
    composition.close()
    writer.close()
