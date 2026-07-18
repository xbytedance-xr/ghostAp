from __future__ import annotations

from types import SimpleNamespace

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


def test_delivered_employee_contributions_anchor_collaboration_provenance(tmp_path) -> None:
    class _PublishingBackend(ImmediateTeamBackend):
        def __init__(self) -> None:
            super().__init__()
            self.publications = []

        def publish_collaboration(self, **coordinates):
            self.publications.append(coordinates)
            return f"causal_{len(self.publications)}"

    writer, blobs = make_team_storage(tmp_path)
    backend = _PublishingBackend()
    actor = TeamCoordinatorActor(
        writer=writer,
        blob_store=blobs,
        active_key_id="team-key",
        backend=backend,
        poll_seconds=0.001,
    )
    run = actor.start_task(
        tenant_key="tenant_1",
        message_id="om_provenance",
        chat_id="oc_team",
        requester_principal_id="ou_user",
        task="Implement, review, and revise",
    )
    actor.drain()

    projection = actor.projection()
    assert len(backend.publications) == 3
    assert set(projection.collaboration_events) == {
        "causal_1",
        "causal_2",
        "causal_3",
    }
    assert all(
        item["team_run_id"] == run.run_id for item in backend.publications
    )
    actor.close()
    blobs.close()
    writer.close()


def test_runtime_backend_binds_collaboration_to_delivered_employee_outbox() -> None:
    from src.autonomous.provisioning.composition import _RuntimeTeamBackend

    binding = SimpleNamespace(
        attempt_id="att_team",
        acceptance_id="acc_team",
        tenant_key="tenant_1",
        agent_id="agt_coder",
        chat_id="oc_team",
    )
    dispatch = SimpleNamespace(
        state=SimpleNamespace(
            attempt_by_acceptance_id={"acc_team": "att_team"},
            attempts={"att_team": SimpleNamespace(binding=binding)},
        )
    )
    delivered = SimpleNamespace(message_id="om_employee_contribution")
    delivery_calls = []
    publications = []
    outbox = SimpleNamespace(
        get_snapshot=lambda _outbox_id: SimpleNamespace(
            state=SimpleNamespace(terminal=True)
        ),
        record_collaboration_publication=lambda **values: publications.append(values)
        or True,
    )
    ingress = SimpleNamespace(
        get_payload=lambda _acceptance_id: SimpleNamespace(
            normalized_parts=(
                {
                    "team_run_id": "teamrun_1",
                    "team_step_id": "1",
                },
            )
        )
    )
    runtime = SimpleNamespace(
        _dispatch=dispatch,
        _outbox=outbox,
        _outbox_delivery=SimpleNamespace(
            deliver=lambda outbox_id: delivery_calls.append(outbox_id) or delivered
        ),
        _ingress=ingress,
    )
    backend = _RuntimeTeamBackend(runtime, lambda *_args: None)

    causal_event_id = backend.publish_collaboration(
        tenant_key="tenant_1",
        chat_id="oc_team",
        agent_id="agt_coder",
        team_run_id="teamrun_1",
        assignment_id="teamrun_1:assignment:1",
        acceptance_id="acc_team",
    )

    assert causal_event_id.startswith("collab_")
    assert len(delivery_calls) == 1
    assert publications == [
        {
            "outbox_id": delivery_calls[0],
            "team_run_id": "teamrun_1",
            "assignment_id": "teamrun_1:assignment:1",
            "causal_event_id": causal_event_id,
        }
    ]
