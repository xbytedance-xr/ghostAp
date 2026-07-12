from src.autonomous.journal import JournalWriter, MemoryAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.projections import ProjectionRepository, ProjectionState

HMAC_KEY = b"test-workforce-key-at-least-32-bytes!"


def make_writer(tmp_path):
    return JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )


def commit_events(writer, state: ProjectionState, *events: JournalEvent):
    from src.autonomous.workforce.projection import commit_workforce_events

    return commit_workforce_events(writer, state, events)


def employee_created(agent_id: str = "agt_1", name: str = "Atlas") -> JournalEvent:
    return JournalEvent(
        event_type="employee.created",
        aggregate_id=agent_id,
        payload={
            "agent_id": agent_id,
            "tenant_key": "tenant_1",
            "owner_principal_id": "ou_admin",
            "name": name,
            "tool": "codex",
            "model": "gpt-5.6-sol",
            "worker_type": "visible",
            "state": "draft",
            "member_groups": ["oc_team"],
        },
    )


def bot_binding_events(
    agent_id: str = "agt_1",
    bot_principal_id: str = "bot_1",
    app_id: str = "cli_1",
    credential_ref: str = "cred_1",
) -> tuple[JournalEvent, JournalEvent]:
    return (
        JournalEvent(
            event_type="employee.bot_principal_bound",
            aggregate_id=agent_id,
            payload={
                "agent_id": agent_id,
                "bot_principal_id": bot_principal_id,
            },
        ),
        JournalEvent(
            event_type="bot_principal.bound",
            aggregate_id=bot_principal_id,
            payload={
                "bot_principal_id": bot_principal_id,
                "tenant_key": "tenant_1",
                "agent_id": agent_id,
                "app_id": app_id,
                "credential_ref": credential_ref,
                "scopes": [],
            },
        ),
    )


def seed_workforce_state(tmp_path) -> tuple[JournalWriter, ProjectionState]:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(writer, state, *bot_binding_events())
    return writer, state


def replay_state(writer) -> ProjectionState:
    return ProjectionRepository().rebuild(writer.replay())
