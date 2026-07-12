import json
import os
from dataclasses import replace

import pytest

from src.autonomous.domain import EmployeeState
from src.autonomous.journal.frame import JournalEvent, JournalIntegrityError
from src.autonomous.journal.projections import (
    ProjectionError,
    ProjectionState,
    apply_event,
)
from src.autonomous.workforce.authority import AuthorityMode, AuthoritySnapshot
from src.autonomous.workforce.projection import (
    EmployeeIdentityMaterializer,
    validate_workforce_events,
)
from tests.autonomous.workforce_helpers import (
    bot_binding_events,
    commit_events,
    employee_created,
    make_writer,
    replay_state,
    seed_workforce_state,
)


def test_employee_events_replay_into_projection(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(writer, state, *bot_binding_events())

    replayed = replay_state(writer)
    assert state.employees["agt_1"].bot_principal_id == "bot_1"
    assert replayed.bot_principals["bot_1"].credential_ref == "cred_1"


def test_duplicate_active_name_is_rejected_casefolded() -> None:
    state = ProjectionState()
    apply_event(state, employee_created("agt_1", "Atlas"))
    with pytest.raises(ProjectionError, match="duplicate active employee name"):
        apply_event(state, employee_created("agt_2", "ATLAS"))


def test_rejected_employee_command_does_not_advance_journal(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created("agt_1", "Atlas"))
    sequence = writer.get_last_frame().sequence

    with pytest.raises(ProjectionError, match="duplicate active employee name"):
        commit_events(writer, state, employee_created("agt_2", "ATLAS"))

    assert writer.get_last_frame().sequence == sequence
    assert "agt_2" not in state.employees


def test_stale_projection_cannot_poison_journal(tmp_path) -> None:
    writer = make_writer(tmp_path)
    live = ProjectionState()
    stale = ProjectionState()
    commit_events(writer, live, employee_created("agt_1", "Atlas"))
    sequence = writer.get_last_frame().sequence

    with pytest.raises(ProjectionError, match="projection is stale"):
        commit_events(writer, stale, employee_created("agt_2", "ATLAS"))

    assert writer.get_last_frame().sequence == sequence


def test_conditional_head_commit_rejects_intervening_append(
    tmp_path, monkeypatch
) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    original_commit = writer.commit

    def racing_commit(events, expected_versions, **kwargs):
        original_commit(
            (JournalEvent(event_type="other.event", aggregate_id="other_1", payload={}),),
            {"other_1": 0},
        )
        return original_commit(events, expected_versions, **kwargs)

    monkeypatch.setattr(writer, "commit", racing_commit)
    with pytest.raises(JournalIntegrityError, match="head mismatch"):
        commit_events(writer, state, employee_created())

    assert writer.get_last_frame().sequence == 1
    assert state.employees == {}


@pytest.mark.parametrize("agent_id", ["../escape", "bot_1", "employee_1"])
def test_employee_created_requires_canonical_agent_id(tmp_path, agent_id) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()

    with pytest.raises(ProjectionError, match="canonical agent_id"):
        commit_events(writer, state, employee_created(agent_id, "Atlas"))

    assert writer.get_last_frame() is None


def test_validation_is_isolated_from_live_state() -> None:
    state = ProjectionState()
    validate_workforce_events(state, (employee_created(),))
    assert state.employees == {}


def test_bot_binding_requires_both_events_in_one_transaction(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())

    with pytest.raises(ProjectionError, match="same transaction"):
        commit_events(writer, state, bot_binding_events()[1])


def test_bot_binding_rejects_mismatched_pair(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    employee_event, principal_event = bot_binding_events()
    principal_event = JournalEvent(
        event_type=principal_event.event_type,
        aggregate_id=principal_event.aggregate_id,
        payload={**principal_event.payload, "agent_id": "agt_other"},
    )

    with pytest.raises(ProjectionError, match="binding events do not match"):
        commit_events(writer, state, employee_event, principal_event)


def test_bot_binding_requires_disjoint_canonical_principal_id(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    employee_event, principal_event = bot_binding_events(
        bot_principal_id="agt_1"
    )

    with pytest.raises(ProjectionError, match="canonical bot_principal_id"):
        commit_events(writer, state, employee_event, principal_event)


def test_replay_rejects_unpaired_bot_binding_frame(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    event = bot_binding_events()[0]
    writer.commit(
        (event,),
        {event.aggregate_id: writer._aggregate_versions[event.aggregate_id]},
    )

    with pytest.raises(ProjectionError, match="same transaction"):
        replay_state(writer)


def test_duplicate_bot_binding_event_is_rejected(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    employee_event, principal_event = bot_binding_events()

    with pytest.raises(ProjectionError, match="duplicate event type"):
        commit_events(writer, state, employee_event, employee_event, principal_event)


def test_duplicate_profile_event_is_rejected_without_commit(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    sequence = writer.get_last_frame().sequence
    event = JournalEvent(
        event_type="employee.profile_changed",
        aggregate_id="agt_1",
        payload={"model": "opus"},
    )

    with pytest.raises(ProjectionError, match="duplicate event type"):
        commit_events(writer, state, event, event)

    assert writer.get_last_frame().sequence == sequence


def test_distinct_same_aggregate_events_use_frame_version(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    result = commit_events(
        writer,
        state,
        employee_created(),
        JournalEvent(
            event_type="employee.legacy_alias_bound",
            aggregate_id="agt_1",
            payload={
                "legacy_id_alias": "codex:default:Atlas",
                "source_hash": "sha256:legacy-source",
            },
        ),
    )

    expected_version = result.frame.aggregate_versions["agt_1"]
    assert state.employees["agt_1"].aggregate_version == expected_version
    assert replay_state(writer).employees["agt_1"].aggregate_version == expected_version


def test_app_id_cannot_bind_two_non_archived_employees(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created("agt_1", "Atlas"))
    commit_events(writer, state, *bot_binding_events())
    commit_events(writer, state, employee_created("agt_2", "Borealis"))

    with pytest.raises(ProjectionError, match="app_id already bound"):
        commit_events(
            writer,
            state,
            *bot_binding_events("agt_2", "bot_2", "cli_1", "cred_2"),
        )


def test_archived_employee_cannot_reactivate_after_app_id_reuse(tmp_path) -> None:
    writer, state = seed_workforce_state(tmp_path)
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.state_changed",
            aggregate_id="agt_1",
            payload={"state": "archived"},
        ),
    )
    commit_events(writer, state, employee_created("agt_2", "Borealis"))
    commit_events(
        writer,
        state,
        *bot_binding_events("agt_2", "bot_2", "cli_1", "cred_2"),
    )
    sequence = writer.get_last_frame().sequence

    with pytest.raises(ProjectionError, match="archived employee is terminal"):
        commit_events(
            writer,
            state,
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id="agt_1",
                payload={"state": "active"},
            ),
        )

    assert writer.get_last_frame().sequence == sequence


def test_bot_principal_must_match_employee_tenant(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    employee_event, principal_event = bot_binding_events()
    principal_event = JournalEvent(
        event_type=principal_event.event_type,
        aggregate_id=principal_event.aggregate_id,
        payload={**principal_event.payload, "tenant_key": "tenant_2"},
    )

    with pytest.raises(ProjectionError, match="tenant"):
        commit_events(writer, state, employee_event, principal_event)


@pytest.mark.parametrize("field", ["app_id", "credential_ref"])
def test_bot_principal_requires_nonempty_binding_identity(tmp_path, field) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    employee_event, principal_event = bot_binding_events()
    principal_event = JournalEvent(
        event_type=principal_event.event_type,
        aggregate_id=principal_event.aggregate_id,
        payload={**principal_event.payload, field: ""},
    )

    with pytest.raises(ProjectionError, match=field):
        commit_events(writer, state, employee_event, principal_event)


def test_profile_membership_manifest_and_credential_events_replay(tmp_path) -> None:
    writer, state = seed_workforce_state(tmp_path)
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.profile_changed",
            aggregate_id="agt_1",
            payload={"tool": "claude", "model": "opus", "effort": "high"},
        ),
    )
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.membership_changed",
            aggregate_id="agt_1",
            payload={"member_groups": ["oc_team", "oc_review"]},
        ),
    )
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="bot_principal.manifest_observed",
            aggregate_id="bot_1",
            payload={"observed_manifest_hash": "sha256:observed"},
        ),
    )
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="credential.destroyed",
            aggregate_id="bot_1",
            payload={"credential_ref": "cred_1"},
        ),
    )

    replayed = replay_state(writer)
    assert replayed.employees["agt_1"].tool == "claude"
    assert replayed.employees["agt_1"].member_groups == ("oc_team", "oc_review")
    assert replayed.bot_principals["bot_1"].observed_manifest_hash == "sha256:observed"
    assert replayed.bot_principals["bot_1"].credential_ref == ""


def test_legacy_alias_and_authority_snapshot_replay_exactly(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.legacy_alias_bound",
            aggregate_id="agt_1",
            payload={
                "legacy_id_alias": "codex:default:Atlas",
                "source_hash": "sha256:legacy-source",
            },
        ),
    )
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="authority.cutover",
            aggregate_id="workforce_authority",
            payload={
                "authority_epoch": 7,
                "authority_mode": "v5_write",
                "cutover_sequence": 91,
            },
        ),
    )

    assert state.legacy_agent_aliases["codex:default:Atlas"] == "agt_1"
    assert state.legacy_source_hashes["sha256:legacy-source"] == "agt_1"
    expected = AuthoritySnapshot(7, AuthorityMode.V5_WRITE, 91)
    assert state.authority_snapshot() == expected
    assert replay_state(writer).authority_snapshot() == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"authority_epoch": 1, "authority_mode": "v5_write"},
        {
            "authority_epoch": True,
            "authority_mode": "v5_write",
            "cutover_sequence": 1,
        },
        {
            "authority_epoch": 1,
            "authority_mode": "unknown",
            "cutover_sequence": 1,
        },
        {
            "authority_epoch": 1,
            "authority_mode": "v5_write",
            "cutover_sequence": -1,
        },
        {
            "authority_epoch": 1,
            "authority_mode": "v5_write",
            "cutover_sequence": 1,
            "extra": "rejected",
        },
    ],
)
def test_authority_cutover_payload_is_strict_and_does_not_advance(
    tmp_path,
    payload: dict,
) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()

    with pytest.raises(ProjectionError):
        commit_events(
            writer,
            state,
            JournalEvent(
                event_type="authority.cutover",
                aggregate_id="workforce_authority",
                payload=payload,
            ),
        )

    assert state.authority_snapshot() == AuthoritySnapshot(
        0,
        AuthorityMode.LEGACY_WRITE,
        0,
    )
    assert writer.get_last_frame() is None


def test_archived_employee_keeps_name_tombstone(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.state_changed",
            aggregate_id="agt_1",
            payload={"state": "archived"},
        ),
    )

    assert state.employees["agt_1"].state is EmployeeState.ARCHIVED
    with pytest.raises(ProjectionError, match="duplicate active employee name"):
        commit_events(writer, state, employee_created("agt_2", "ATLAS"))


def test_archived_employee_profile_cannot_free_name_tombstone(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created())
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.state_changed",
            aggregate_id="agt_1",
            payload={"state": "archived"},
        ),
    )
    sequence = writer.get_last_frame().sequence

    with pytest.raises(ProjectionError, match="archived employee is terminal"):
        commit_events(
            writer,
            state,
            JournalEvent(
                event_type="employee.profile_changed",
                aggregate_id="agt_1",
                payload={"name": "Borealis"},
            ),
        )

    assert writer.get_last_frame().sequence == sequence
    assert state.employee_name_keys[("tenant_1", "atlas")] == "agt_1"


def test_materializer_writes_non_secret_identity_with_mode_0600(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    materializer = EmployeeIdentityMaterializer(tmp_path / "agents")
    path = materializer.materialize(state, "agt_1")
    payload = json.loads(path.read_text())

    assert payload["app_id"] == "cli_1"
    assert payload["credential_ref"] == "cred_1"
    assert "app_secret" not in payload
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_materializer_allowlist_excludes_nested_app_secret(tmp_path) -> None:
    state = ProjectionState()
    apply_event(state, employee_created())
    state.employees["agt_1"] = replace(
        state.employees["agt_1"],
        budget_template={"provider": {"app_secret": "plaintext"}},
    )

    path = EmployeeIdentityMaterializer(tmp_path / "agents").materialize(
        state, "agt_1"
    )

    content = path.read_text()
    assert "budget_template" not in content
    assert "app_secret" not in content
    assert "plaintext" not in content


def test_materializer_rejects_agent_id_path_escape(tmp_path) -> None:
    state = ProjectionState()
    apply_event(state, employee_created())
    unsafe = state.employees.pop("agt_1")
    object.__setattr__(unsafe, "agent_id", "../escape")
    state.employees["../escape"] = unsafe

    with pytest.raises(ProjectionError, match="safe path component"):
        EmployeeIdentityMaterializer(tmp_path / "agents").materialize(
            state, "../escape"
        )
    assert not (tmp_path / "escape" / "identity.json").exists()


def test_materializer_rejects_symlink_employee_directory(tmp_path) -> None:
    state = ProjectionState()
    apply_event(state, employee_created())
    agents_root = tmp_path / "agents"
    target = tmp_path / "target"
    agents_root.mkdir()
    target.mkdir()
    (agents_root / "agt_1").symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        EmployeeIdentityMaterializer(agents_root).materialize(state, "agt_1")
    assert not (target / "identity.json").exists()


def test_materializer_rejects_symlink_root(tmp_path) -> None:
    state = ProjectionState()
    apply_event(state, employee_created())
    target = tmp_path / "target"
    target.mkdir()
    agents_root = tmp_path / "agents"
    agents_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        EmployeeIdentityMaterializer(agents_root).materialize(state, "agt_1")
    assert not (target / "agt_1" / "identity.json").exists()


def test_materializer_rejects_symlink_root_ancestor(tmp_path) -> None:
    state = ProjectionState()
    apply_event(state, employee_created())
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    agents_root = link / "agents"

    with pytest.raises(OSError):
        EmployeeIdentityMaterializer(agents_root).materialize(state, "agt_1")
    assert not (outside / "agents" / "agt_1" / "identity.json").exists()


def test_missing_projection_file_is_rebuilt_from_state(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    materializer = EmployeeIdentityMaterializer(tmp_path / "agents")
    first = materializer.materialize(state, "agt_1")
    first.unlink()

    rebuilt = materializer.materialize(state, "agt_1")
    assert rebuilt.exists()


def test_materialize_all_is_deterministic_and_skips_no_employee(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = ProjectionState()
    commit_events(writer, state, employee_created("agt_2", "Borealis"))
    commit_events(writer, state, employee_created("agt_1", "Atlas"))

    paths = EmployeeIdentityMaterializer(tmp_path / "agents").materialize_all(state)
    assert [path.parent.name for path in paths] == ["agt_1", "agt_2"]
