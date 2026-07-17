from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from src.autonomous.team import (
    CoordinatorAction,
    CoordinatorDecision,
    EmployeeTeamService,
    SessionCoordinatorDecisionProvider,
    TeamCoordinatorActor,
    TeamRunPhase,
)
from tests.autonomous.team_helpers import ImmediateTeamBackend, make_team_storage


def _actor(tmp_path, backend=None):
    writer, blobs = make_team_storage(tmp_path)
    actor = TeamCoordinatorActor(
        writer=writer,
        blob_store=blobs,
        active_key_id="team-key",
        backend=backend or ImmediateTeamBackend(),
        poll_seconds=0.001,
    )
    return writer, blobs, actor


def test_coordinator_persists_encrypted_task_and_completes_dynamic_run(tmp_path) -> None:
    backend = ImmediateTeamBackend()
    writer, blobs, actor = _actor(tmp_path, backend)
    run = actor.start_task(
        tenant_key="tenant_1",
        message_id="om_team",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="请实现 Python 功能并完成安全评审",
    )
    actor.drain()
    final = actor.projection().runs[run.run_id]

    assert final.phase is TeamRunPhase.COMPLETED
    assert [item[1] for item in backend.submissions] == [
        "agt_coder",
        "agt_reviewer",
        "agt_coder",
    ]
    assert len(backend.notifications) == 1
    assert "请实现 Python 功能" not in str(
        [event.payload for frame in writer.replay() for event in frame.events]
    )
    actor.close()
    blobs.close()
    writer.close()


def test_explicit_mention_wins_and_assignment_claim_is_single_winner(tmp_path) -> None:
    backend = ImmediateTeamBackend()
    writer, blobs, actor = _actor(tmp_path, backend)
    run = actor.start_task(
        tenant_key="tenant_1",
        message_id="om_mention",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="@agt_reviewer 请先负责这个任务",
    )
    actor.drain()
    first = actor.projection().assignments[f"{run.run_id}:assignment:1"]
    assert first.agent_id == "agt_reviewer"

    winners = []
    # Completed assignments are fenced just as strictly as concurrent claims.
    threads = [
        threading.Thread(
            target=lambda: winners.append(actor.claim(first.assignment_id, first.agent_id))
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert winners == [False, False]
    actor.close()
    blobs.close()
    writer.close()


def test_coordinator_decision_rejects_bounds_and_forged_completion() -> None:
    with pytest.raises(ValueError, match="fanout"):
        CoordinatorDecision(
            CoordinatorAction.ASSIGN,
            tuple(f"agt_{index}" for index in range(5)),
            role="execute",
            instruction="work",
        )
    with pytest.raises(ValueError, match="forge"):
        CoordinatorDecision(
            CoordinatorAction.COMPLETE,
            done_checks={"review": False},
        )
    with pytest.raises(ValueError, match="agent ID"):
        CoordinatorDecision(
            CoordinatorAction.ASSIGN,
            ("not-an-agent",),
            role="execute",
            instruction="work",
        )


def test_configured_coordinator_session_is_reused_and_strictly_parsed(
    monkeypatch,
) -> None:
    calls = []

    class _Session:
        def send_prompt(self, prompt, timeout):
            calls.append((prompt, timeout))
            return SimpleNamespace(
                text=(
                    '{"action":"assign","agent_ids":["agt_coder"],'
                    '"role":"execute","instruction":"do it",'
                    '"depends_on":[],"done_checks":{},"reason_code":""}'
                )
            )

    created = []
    monkeypatch.setattr(
        "src.agent_session.create_engine_session",
        lambda **kwargs: created.append(kwargs) or _Session(),
    )
    monkeypatch.setattr("src.agent_session.close_session_safely", lambda session: None)
    provider = SessionCoordinatorDecisionProvider(
        tool="codex",
        model="gpt-test",
        cwd_resolver=lambda _run: "/project",
    )
    run = SimpleNamespace(coordinator_session_key="session-key")
    targets = (
        SimpleNamespace(
            agent_id="agt_coder",
            role="coder",
            capabilities=("python",),
            runtime_status="ready_warm",
            mailbox_load=0,
        ),
    )
    first = provider(run, targets, "task")
    second = provider(run, targets, "task 2")
    assert first.agent_ids == second.agent_ids == ("agt_coder",)
    assert len(created) == 1
    assert created[0]["agent_type"] == "codex"
    assert created[0]["model_name"] == "gpt-test"
    assert len(calls) == 2
    provider.close()


def test_team_service_coordinator_mode_does_not_enter_legacy_pipeline(tmp_path) -> None:
    backend = ImmediateTeamBackend()
    writer, blobs = make_team_storage(tmp_path)
    service = EmployeeTeamService(
        writer=writer,
        backend=backend,
        runtime_mode="coordinator",
        blob_store=blobs,
        active_key_id="team-key",
        poll_seconds=0.001,
    )
    accepted = service.start_task(
        tenant_key="tenant_1",
        message_id="om_facade",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="实现并评审",
    )
    service._coordinator.drain()  # noqa: SLF001
    final = service.get_run(accepted.run_id)
    assert final is not None and final.status == "completed"
    assert all(step_id.isdigit() for step_id, *_rest in backend.submissions)
    service.close()
    blobs.close()
    writer.close()


def test_coordinator_accepts_bounded_parallel_fanout(tmp_path) -> None:
    backend = ImmediateTeamBackend()
    writer, blobs = make_team_storage(tmp_path)
    actor = TeamCoordinatorActor(
        writer=writer,
        blob_store=blobs,
        active_key_id="team-key",
        backend=backend,
        poll_seconds=0.001,
        decision_provider=lambda _run, _targets, task: CoordinatorDecision(
            CoordinatorAction.ASSIGN,
            ("agt_coder", "agt_reviewer"),
            role="execute",
            instruction=f"并行处理：{task}",
        ),
    )
    actor.start_task(
        tenant_key="tenant_1",
        message_id="om_fanout",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="并行分析",
    )
    actor.drain()
    assert [item[1] for item in backend.submissions[:2]] == [
        "agt_coder",
        "agt_reviewer",
    ]
    actor.close()
    blobs.close()
    writer.close()


def test_selector_excludes_degraded_busy_and_roleless_targets() -> None:
    targets = (
        SimpleNamespace(
            agent_id="agt_none",
            role="",
            capabilities=(),
            runtime_status="ready",
            mailbox_load=0,
        ),
        SimpleNamespace(
            agent_id="agt_busy",
            role="coder",
            capabilities=("python",),
            runtime_status="busy",
            mailbox_load=0,
        ),
        SimpleNamespace(
            agent_id="agt_ready",
            role="coder",
            capabilities=("python",),
            runtime_status="ready_cold",
            mailbox_load=1,
        ),
    )
    selected = TeamCoordinatorActor._select_target(  # noqa: SLF001
        None, targets, "Python", role="execute"
    )
    assert selected.agent_id == "agt_ready"
