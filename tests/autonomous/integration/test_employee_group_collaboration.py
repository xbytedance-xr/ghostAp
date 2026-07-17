from __future__ import annotations

from src.autonomous.ingress import (
    GroupRouteKind,
    GroupRouteRequest,
    decide_group_route,
)
from src.autonomous.team import TeamCoordinatorActor, TeamRunPhase
from tests.autonomous.team_helpers import ImmediateTeamBackend, make_team_storage


def test_ambient_group_chat_is_context_only_and_explicit_task_wakes_coordinator() -> None:
    common = dict(
        tenant_key="tenant_1",
        chat_id="oc_team",
        sender_principal_id="ou_user",
        sender_type="user",
        sender_tenant_key="tenant_1",
        text="大家早上好",
    )
    ambient = decide_group_route(GroupRouteRequest(**common))
    task = decide_group_route(GroupRouteRequest(**common, explicit_team_task=True))
    assert ambient == ambient.__class__(GroupRouteKind.AMBIENT_CHAT)
    assert ambient.wake_model is False
    assert task.kind is GroupRouteKind.TEAM_TASK and task.wake_model is True


def test_employee_contribution_flows_into_review_and_main_final_transport(tmp_path) -> None:
    writer, blobs = make_team_storage(tmp_path)
    backend = ImmediateTeamBackend()
    actor = TeamCoordinatorActor(
        writer=writer,
        blob_store=blobs,
        active_key_id="team-key",
        backend=backend,
        poll_seconds=0.001,
    )
    run = actor.start_task(
        tenant_key="tenant_1",
        message_id="om_collaboration",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="Python implementation then review",
    )
    actor.drain()
    assert actor.projection().runs[run.run_id].phase is TeamRunPhase.COMPLETED
    execute = backend.submissions[0]
    review = backend.submissions[1]
    assert execute[1] == "agt_coder"
    assert review[1] == "agt_reviewer"
    assert "deliverable by agt_coder" in review[2]
    assert len(backend.notifications) == 1
    actor.close()
    blobs.close()
    writer.close()
