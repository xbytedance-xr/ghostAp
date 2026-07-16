from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

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
        self.deadlines: list[str] = []

    def list_active(self, tenant_key: str, chat_id: str):
        assert (tenant_key, chat_id) == ("tenant_1", "oc_team")
        return (
            TeamTarget("agt_lead", "Lead", "coder"),
            TeamTarget("agt_review", "Review", "reviewer"),
        )

    def submit(self, *, run_id, step_id, target, instruction, **kwargs):
        self.submissions.append((step_id, target.agent_id, instruction))
        self.deadlines.append(kwargs["deadline_at"])
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


class _TimeoutCancelBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.cancellations: list[tuple[str, str, str]] = []

    def result(self, acceptance_id: str):
        return None

    def cancel(self, acceptance_id: str, *, run_id: str, step_id: str):
        self.cancellations.append((acceptance_id, run_id, step_id))
        return TeamAttemptResult(
            "canceled",
            error_code="team_step_timeout",
            retry_allowed=False,
        )


class _CloseBarrierBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.result_entered = threading.Event()
        self.result_release = threading.Event()

    def result(self, acceptance_id: str):
        self.result_entered.set()
        assert self.result_release.wait(2)
        return None

    def cancel(self, acceptance_id: str, *, run_id: str, step_id: str):
        return TeamAttemptResult("canceled", error_code="team_service_stopping")


class _BlockingNotifyBackend(_Backend):
    def __init__(self, *, active: bool = True) -> None:
        super().__init__()
        self.active = active
        self.notify_entered = threading.Event()
        self.notify_release = threading.Event()

    def list_active(self, tenant_key: str, chat_id: str):
        return super().list_active(tenant_key, chat_id) if self.active else ()

    def notify(self, message_id: str, chat_id: str, result: str) -> None:
        self.notify_entered.set()
        assert self.notify_release.wait(2)
        super().notify(message_id, chat_id, result)


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


def test_deadline_final_poll_observes_gateway_terminal_result(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _DeadlineBackend()
    base = datetime(2026, 7, 14, tzinfo=UTC)
    clock_values = iter((base, base, base + timedelta(seconds=1)))
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=1,
        poll_seconds=0,
        clock=lambda: next(clock_values),
    )
    state = TeamRunState(
        run_id="teamrun_deadline",
        tenant_key="tenant_1",
        message_id="om_deadline",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task_digest="0" * 64,
    )
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


def test_team_step_deadline_preserves_microsecond_budget(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _Backend()
    base = datetime(2026, 7, 14, 0, 0, 0, 123456, tzinfo=UTC)
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=0.0005,
        poll_seconds=0,
        clock=lambda: base,
    )
    state = TeamRunState(
        run_id="teamrun_fractional",
        tenant_key="tenant_1",
        message_id="om_fractional",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task_digest="0" * 64,
    )

    result = service._run_step(
        state,
        step_id="analysis",
        depth=1,
        target=TeamTarget("agt_lead", "Lead"),
        instruction="fractional budget",
    )

    assert result.status == "completed"
    assert backend.deadlines == ["2026-07-14T00:00:00.123956Z"]
    service.close()
    writer.close()


def test_team_timeout_cancels_before_retry_admission(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _TimeoutCancelBackend()
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=0.001,
        poll_seconds=0.002,
    )
    state = TeamRunState(
        run_id="teamrun_timeout_cancel",
        tenant_key="tenant_1",
        message_id="om_timeout",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task_digest="0" * 64,
    )

    result = service._run_step(
        state,
        step_id="analysis",
        depth=1,
        target=TeamTarget("agt_lead", "Lead", "coder"),
        instruction="timeout and cancel",
    )

    assert backend.cancellations == [
        ("acc_analysis", "teamrun_timeout_cancel", "analysis")
    ]
    assert result.status == "canceled"
    service.close()
    writer.close()


def test_team_cancel_pending_does_not_admit_retry(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _TimeoutCancelBackend()
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=0.001,
        poll_seconds=0.002,
    )

    state = service.start_task(
        tenant_key="tenant_1",
        message_id="om_cancel_pending",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="do not overlap attempts",
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = service.get_run(state.run_id)
        if current is not None and current.status == "action_required":
            break
        time.sleep(0.01)

    assert len(backend.submissions) == 1
    service.close()
    writer.close()


def test_close_fence_prevents_retry_submission(tmp_path) -> None:
    writer = make_writer(tmp_path)
    backend = _CloseBarrierBackend()
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        attempt_timeout_seconds=30,
        poll_seconds=0,
    )
    state = service.start_task(
        tenant_key="tenant_1",
        message_id="om_close",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="close during result",
    )
    assert backend.result_entered.wait(1)

    closer = threading.Thread(target=service.close)
    closer.start()
    assert service._stop.wait(1)  # noqa: SLF001
    backend.result_release.set()
    closer.join(2)

    assert not closer.is_alive()
    assert len(backend.submissions) == 1
    assert service.get_run(state.run_id).status in {"stopping", "action_required"}
    events = [
        event.event_type
        for frame in writer.replay()
        for event in frame.events
        if event.aggregate_id.startswith(state.run_id)
    ]
    stopping_index = events.index("team.run.stopping")
    assert "team.step.prepared" not in events[stopping_index + 1 :]
    writer.close()


def test_team_run_terminal_cannot_regress_to_stopping(tmp_path) -> None:
    from src.autonomous.journal.frame import JournalEvent
    from src.autonomous.team.service import TeamServiceError

    writer = make_writer(tmp_path)
    service = EmployeeTeamService(writer=writer, backend=_Backend())
    service._executor.shutdown(wait=True)  # noqa: SLF001
    service._closed = True  # noqa: SLF001
    service._commit(  # noqa: SLF001
        JournalEvent(
            event_type="team.run.created",
            aggregate_id="teamrun_monotonic",
            payload={
                "tenant_key": "tenant_1",
                "message_id": "om_terminal",
                "chat_id": "oc_team",
                "requester_principal_id": "ou_user",
                "task_digest": "0" * 64,
                "max_handoffs": 8,
                "max_depth": 4,
                "max_fanout": 4,
            },
        )
    )
    service._commit(  # noqa: SLF001
        JournalEvent(
            event_type="team.run.completed",
            aggregate_id="teamrun_monotonic",
            payload={"result_digest": "1" * 64, "history_record_id": "hist_done"},
        )
    )

    with pytest.raises(TeamServiceError):
        service._commit(  # noqa: SLF001
            JournalEvent(
                event_type="team.run.stopping",
                aggregate_id="teamrun_monotonic",
                payload={"reason_code": "team_service_stopping"},
            )
        )
    assert service.get_run("teamrun_monotonic").status == "completed"
    writer.close()


def test_team_run_reducer_rejects_non_exact_stopping_payload(tmp_path) -> None:
    from src.autonomous.journal.frame import JournalEvent
    from src.autonomous.team.service import TeamServiceError

    writer = make_writer(tmp_path)
    service = EmployeeTeamService(writer=writer, backend=_Backend())
    service._executor.shutdown(wait=True)  # noqa: SLF001
    service._closed = True  # noqa: SLF001
    service._commit(  # noqa: SLF001
        JournalEvent(
            event_type="team.run.created",
            aggregate_id="teamrun_exact",
            payload={
                "tenant_key": "tenant_1",
                "message_id": "om_exact",
                "chat_id": "oc_team",
                "requester_principal_id": "ou_user",
                "task_digest": "0" * 64,
                "max_handoffs": 8,
                "max_depth": 4,
                "max_fanout": 4,
            },
        )
    )

    with pytest.raises(TeamServiceError):
        service._commit(  # noqa: SLF001
            JournalEvent(
                event_type="team.run.stopping",
                aggregate_id="teamrun_exact",
                payload={"reason_code": "team_service_stopping", "extra": True},
            )
        )
    writer.close()


def test_close_between_progress_check_and_initial_step_writes_nothing(tmp_path, monkeypatch) -> None:
    writer = make_writer(tmp_path)
    service = EmployeeTeamService(writer=writer, backend=_Backend())
    state = TeamRunState(
        run_id="teamrun_initial_fence",
        tenant_key="tenant_1",
        message_id="om_initial",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task_digest="0" * 64,
    )
    from src.autonomous.journal.frame import JournalEvent

    service._commit(  # noqa: SLF001
        JournalEvent(
            event_type="team.run.created",
            aggregate_id=state.run_id,
            payload={
                "tenant_key": state.tenant_key,
                "message_id": state.message_id,
                "chat_id": state.chat_id,
                "requester_principal_id": state.requester_principal_id,
                "task_digest": state.task_digest,
                "max_handoffs": 8,
                "max_depth": 4,
                "max_fanout": 4,
            },
        )
    )
    original = service._can_progress  # noqa: SLF001
    checked = False

    def close_after_check(current):
        nonlocal checked
        allowed = original(current)
        if not checked:
            checked = True
            service.close()
        return allowed

    monkeypatch.setattr(service, "_can_progress", close_after_check)
    service._run_step(  # noqa: SLF001
        state,
        step_id="analysis",
        depth=1,
        target=TeamTarget("agt_lead", "Lead"),
        instruction="fenced",
    )

    events = [
        event.event_type
        for frame in writer.replay()
        for event in frame.events
        if event.aggregate_id.startswith(state.run_id)
    ]
    stopping = events.index("team.run.stopping")
    assert not any(
        event.startswith(("team.step.", "team.effect."))
        for event in events[stopping + 1 :]
    )
    writer.close()


@pytest.mark.parametrize("active, expected", [(True, "completed"), (False, "action_required")])
def test_close_cannot_fence_after_notify_has_started(tmp_path, active, expected) -> None:
    writer = make_writer(tmp_path)
    backend = _BlockingNotifyBackend(active=active)
    service = EmployeeTeamService(writer=writer, backend=backend)
    state = service.start_task(
        tenant_key="tenant_1",
        message_id=f"om_notify_{active}",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="notify fence",
    )
    assert backend.notify_entered.wait(1)
    closer = threading.Thread(target=service.close)
    closer.start()
    time.sleep(0.05)
    backend.notify_release.set()
    closer.join(2)

    assert service.get_run(state.run_id).status == expected
    assert not any(
        event.event_type == "team.run.stopping"
        and event.aggregate_id == state.run_id
        for frame in writer.replay()
        for event in frame.events
    )
    writer.close()
