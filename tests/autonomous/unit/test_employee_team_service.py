from __future__ import annotations

import time

from src.autonomous.team import (
    EmployeeTeamService,
    TeamAttemptResult,
    TeamRunState,
    TeamTarget,
)
from tests.autonomous.workforce_helpers import make_writer


class _Backend:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str, str]] = []
        self.notifications: list[tuple[str, str, str]] = []

    def list_active(self, tenant_key: str, chat_id: str):
        assert (tenant_key, chat_id) == ("tenant_1", "oc_team")
        return (
            TeamTarget("agt_lead", "Lead", "coder"),
            TeamTarget("agt_review", "Review", "reviewer"),
        )

    def submit(self, *, run_id, step_id, target, instruction, **_kwargs):
        self.submissions.append((step_id, target.agent_id, instruction))
        return f"acc_{step_id}"

    def result(self, acceptance_id: str):
        step = acceptance_id.removeprefix("acc_")
        return TeamAttemptResult(
            "completed",
            output=f"output:{step}",
            history_record_id=f"hist_{step}",
        )

    def notify(self, message_id: str, chat_id: str, result: str) -> None:
        self.notifications.append((message_id, chat_id, result))


class _DeadlineBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.result_calls = 0

    def result(self, acceptance_id: str):
        self.result_calls += 1
        if self.result_calls == 1:
            return None
        return TeamAttemptResult(
            "timeout",
            history_record_id="hist_timeout",
            error_code="slock_session_timeout",
        )


def test_team_run_hands_off_reviews_and_synthesizes(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _Backend()
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=1,
        poll_seconds=0.001,
    )

    admitted = service.start_task(
        tenant_key="tenant_1",
        message_id="om_task",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="修复团队模式",
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = service.get_run(admitted.run_id)
        if state is not None and state.status == "completed":
            break
        time.sleep(0.01)

    assert state is not None and state.status == "completed"
    assert [item[:2] for item in backend.submissions] == [
        ("analysis", "agt_lead"),
        ("review", "agt_review"),
        ("synthesis", "agt_lead"),
    ]
    assert "output:analysis" in backend.submissions[1][2]
    assert "output:review" in backend.submissions[2][2]
    assert backend.notifications == [("om_task", "oc_team", "output:synthesis")]
    service.close()
    writer.close()


def test_restart_marks_unfinished_run_action_required(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _Backend()
    service = EmployeeTeamService(writer=writer, backend=backend)
    service._executor.shutdown(wait=True)
    service._closed = True

    # Anchor only the admission to model a crash before the first dispatch.
    from src.autonomous.journal.frame import JournalEvent

    service._commit(
        JournalEvent(
            event_type="team.run.created",
            aggregate_id="teamrun_crashed",
            payload={
                "tenant_key": "tenant_1",
                "message_id": "om_crashed",
                "chat_id": "oc_team",
                "requester_principal_id": "ou_user",
                "task_digest": "0" * 64,
                "max_handoffs": 8,
                "max_depth": 4,
                "max_fanout": 4,
            },
        )
    )

    assert service.recover() == 1
    assert service.get_run("teamrun_crashed").status == "action_required"
    writer.close()


def test_deadline_final_poll_observes_gateway_terminal_result(tmp_path, monkeypatch) -> None:
    writer = make_writer(tmp_path)
    backend = _DeadlineBackend()
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=1,
        poll_seconds=0,
    )
    state = TeamRunState(
        run_id="teamrun_deadline",
        tenant_key="tenant_1",
        message_id="om_deadline",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task_digest="0" * 64,
    )
    monotonic_values = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_values))

    result = service._run_step(
        state,
        step_id="analysis",
        depth=1,
        target=TeamTarget("agt_lead", "Lead", "coder"),
        instruction="deadline boundary",
    )

    assert backend.result_calls == 2
    assert result.status == "timeout"
    assert result.error_code == "slock_session_timeout"
    assert any(
        event.event_type == "team.step.failed"
        and event.payload["error_code"] == "slock_session_timeout"
        for frame in writer.replay()
        for event in frame.events
    )
    service.close()
    writer.close()
