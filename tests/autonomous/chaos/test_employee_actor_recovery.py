from __future__ import annotations

import hashlib
from pathlib import Path

from src.autonomous.journal.anchor import FileAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.writer import CommitState, JournalWriter
from src.autonomous.runtime.employee_supervisor import EmployeeRuntimeSupervisor


def _writer(tmp_path: Path) -> JournalWriter:
    return JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=b"employee-actor-recovery-key-32bytes",
    )


def _commit_queued(writer: JournalWriter, assignment_id: str) -> None:
    aggregate = f"employee-assignment:{assignment_id}"
    event = JournalEvent(
        event_type="employee.actor.assignment_queued",
        aggregate_id=aggregate,
        payload={
            "agent_id": "agt_1",
            "payload_ref": "",
            "prompt_digest": hashlib.sha256(b"secret prompt").hexdigest(),
            "timeout_seconds": 10.0,
            "session_key": {
                "tenant_key": "tenant_1",
                "agent_id": "agt_1",
                "project_root": "/project",
                "backend": "codex",
                "model": "m",
                "profile": "",
            },
        },
    )
    result = writer.commit((event,), {aggregate: 0})
    assert result.state is CommitState.ANCHORED


def test_recovery_terminalizes_unresolvable_anchored_mailbox_once(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    _commit_queued(writer, "asgn_1")
    supervisor = EmployeeRuntimeSupervisor(writer=writer)

    assert supervisor.recover() == 1
    assert supervisor.wait_terminal("asgn_1", timeout=0).error_code == (
        "employee_recovery_payload_unavailable"
    )
    assert supervisor.recover() == 0
    events = [
        event
        for frame in writer.replay()
        for event in frame.events
        if event.aggregate_id == "employee-assignment:asgn_1"
    ]
    assert [event.event_type for event in events] == [
        "employee.actor.assignment_queued",
        "employee.actor.assignment_terminal",
    ]
    assert "secret prompt" not in writer.journal_path.read_text(encoding="utf-8")
    supervisor.close()
    writer.close()


def test_backend_effect_is_anchored_before_session_factory(tmp_path: Path) -> None:
    from src.autonomous.runtime.employee_actor import EmployeeAssignment
    from tests.autonomous.unit.test_employee_actor import _bootstrap, _Session

    writer = _writer(tmp_path)
    seen: list[str] = []

    def factory(_bootstrap_value):
        seen.extend(
            event.event_type
            for frame in writer.replay()
            for event in frame.events
        )
        return _Session()

    supervisor = EmployeeRuntimeSupervisor(writer=writer, session_factory=factory)
    supervisor.submit(
        EmployeeAssignment("asgn_1", _bootstrap(tmp_path), "work", 1)
    )
    assert supervisor.wait_terminal("asgn_1", timeout=1).status == "completed"
    assert seen[-2:] == [
        "employee.actor.effect_prepared",
        "employee.actor.effect_executing",
    ]
    supervisor.close()
    writer.close()
