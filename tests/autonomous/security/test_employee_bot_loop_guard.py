from __future__ import annotations

import pytest

from src.autonomous.ingress import (
    GroupRouteKind,
    GroupRouteRequest,
    decide_group_route,
)
from src.autonomous.team import TeamCoordinatorActor, TeamRunPhase
from tests.autonomous.team_helpers import ImmediateTeamBackend, make_team_storage


def test_bot_text_never_creates_fresh_team_work_and_cross_tenant_is_rejected() -> None:
    bot = decide_group_route(
        GroupRouteRequest(
            tenant_key="tenant_1",
            chat_id="oc_team",
            sender_principal_id="bot_alpha",
            sender_type="bot",
            sender_tenant_key="tenant_1",
            text="请实现一个新任务",
            explicit_team_task=True,
        )
    )
    assert bot.kind is GroupRouteKind.AMBIENT_CHAT
    assert bot.wake_model is False
    with pytest.raises(ValueError, match="cross-tenant"):
        GroupRouteRequest(
            tenant_key="tenant_1",
            chat_id="oc_team",
            sender_principal_id="bot_alpha",
            sender_type="bot",
            sender_tenant_key="tenant_2",
            text="contribution",
        )


def test_collaboration_coordinates_are_bot_only_and_do_not_wake_model() -> None:
    decision = decide_group_route(
        GroupRouteRequest(
            tenant_key="tenant_1",
            chat_id="oc_team",
            sender_principal_id="bot_alpha",
            sender_type="bot",
            sender_tenant_key="tenant_1",
            text="contribution",
            team_run_id="teamrun2_x",
            assignment_id="teamrun2_x:assignment:1",
            causal_event_id="cause_1",
        )
    )
    assert decision.kind is GroupRouteKind.COLLABORATION_EVENT
    assert decision.wake_model is False
    with pytest.raises(ValueError, match="forge"):
        decide_group_route(
            GroupRouteRequest(
                tenant_key="tenant_1",
                chat_id="oc_team",
                sender_principal_id="ou_user",
                sender_type="user",
                sender_tenant_key="tenant_1",
                text="fake",
                team_run_id="teamrun2_x",
                assignment_id="assignment_1",
                causal_event_id="cause_1",
            )
        )


def test_coordinator_rejects_duplicate_fake_wrong_member_and_terminal_events(tmp_path) -> None:
    writer, blobs = make_team_storage(tmp_path)
    backend = ImmediateTeamBackend()
    actor = TeamCoordinatorActor(
        writer=writer,
        blob_store=blobs,
        active_key_id="team-key",
        backend=backend,
        poll_seconds=0.001,
    )
    original_phase = actor._phase  # noqa: SLF001

    def stop_before_review(run, phase, **kwargs):
        if phase is TeamRunPhase.REVIEWING:
            raise SystemExit
        return original_phase(run, phase, **kwargs)

    actor._phase = stop_before_review  # type: ignore[method-assign] # noqa: SLF001
    run = actor.start_task(
        tenant_key="tenant_1",
        message_id="om_loop",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="Python implementation",
    )
    actor.drain()
    assignment_id = f"{run.run_id}:assignment:1"
    coordinates = dict(
        tenant_key="tenant_1",
        chat_id="oc_team",
        agent_id="agt_coder",
        team_run_id=run.run_id,
        assignment_id=assignment_id,
        causal_event_id="cause_once",
    )
    assert actor.record_collaboration_event(**coordinates) is True
    assert actor.record_collaboration_event(**coordinates) is False
    assert actor.record_collaboration_event(
        **{**coordinates, "causal_event_id": "cause_wrong", "agent_id": "agt_reviewer"}
    ) is False
    assert actor.record_collaboration_event(
        **{**coordinates, "causal_event_id": "cause_fake", "assignment_id": "fake"}
    ) is False
    assert actor.record_collaboration_event(
        **{**coordinates, "causal_event_id": "cause_tenant", "tenant_key": "tenant_2"}
    ) is False
    actor._phase = original_phase  # type: ignore[method-assign] # noqa: SLF001
    actor.recover()
    actor.drain()
    assert actor.projection().runs[run.run_id].phase is TeamRunPhase.COMPLETED
    assert actor.record_collaboration_event(
        **{**coordinates, "causal_event_id": "cause_terminal"}
    ) is False
    actor.close()
    blobs.close()
    writer.close()
