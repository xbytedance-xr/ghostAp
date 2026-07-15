"""Focused RED/GREEN contract for durable visible-employee admission."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autonomous.domain import EmployeeState, WorkerType
from src.autonomous.journal.anchor import FileAnchor, MemoryAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.projections import ProjectionState
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.provisioning.hire_port import EmployeeHireRequest
from src.autonomous.provisioning.hire_service import (
    HireAdmissionError,
    ProductionEmployeeHireService,
)
from src.autonomous.provisioning.hire_state import (
    DurableHireState,
    HireEffectState,
    HirePhase,
    HireProjection,
    HireProjectionError,
)
from src.autonomous.workforce.projection import commit_workforce_events
from src.config.settings import Settings

HMAC_KEY = b"employee-hire-test-hmac-key-32-bytes"


def _request(
    *,
    message_id: str = "om_hire_1",
    employee_name: str = "Atlas",
    existing_app_id: str = "",
) -> EmployeeHireRequest:
    return EmployeeHireRequest(
        employee_name=employee_name,
        tool="codex",
        model="gpt-5.6-sol",
        effort="high",
        chat_id="oc_admin_dm",
        message_id=message_id,
        requester_principal_id="ou_admin",
        requester_union_id="on_admin",
        tenant_key="tenant-a",
        profile="standard",
        role="software engineer",
        persona="careful reviewer",
        existing_app_id=existing_app_id,
    )


def _service(
    tmp_path: Path,
    *,
    visible_employee_limit: int = 1,
    release_evidence_ready: bool = True,
    credential_keyring_ready: bool = True,
    memory_anchor: bool = False,
    runtime_recovery_ready: bool = True,
    provisioning_submitter=None,
) -> tuple[ProductionEmployeeHireService, JournalWriter, ProjectionState]:
    anchor = MemoryAnchor() if memory_anchor else FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    projection = ProjectionState()
    service = ProductionEmployeeHireService(
        writer,
        projection,
        visible_employee_limit=visible_employee_limit,
        release_evidence_ready=release_evidence_ready,
        credential_keyring_ready=credential_keyring_ready,
        runtime_recovery_ready=runtime_recovery_ready,
        provisioning_submitter=provisioning_submitter,
    )
    return service, writer, projection


def test_new_and_duplicate_hire_submit_only_after_all_commit_locks_release(
    tmp_path: Path,
) -> None:
    import src.autonomous.workforce.projection as workforce_projection

    observations = []
    service = None
    writer = None

    def submit(intent_id):
        assert service is not None and writer is not None
        observations.append(intent_id)
        assert not workforce_projection._WORKFORCE_COMMIT_LOCK._is_owned()
        assert not service._mutex._is_owned()
        assert writer._mutex.acquire(blocking=False)
        writer._mutex.release()

    service, writer, _projection = _service(
        tmp_path,
        provisioning_submitter=submit,
    )
    first = service.start_hire(_request())
    duplicate = service.start_hire(_request())

    assert duplicate == first
    assert observations == [first.intent_id, first.intent_id]


def test_admission_is_anchored_single_frame_and_updates_projection_cursor(
    tmp_path: Path,
) -> None:
    service, writer, projection = _service(tmp_path)

    state = service.start_hire(_request())

    frames = tuple(writer.replay())
    assert len(frames) == 1
    assert len(frames[0].events) == 1
    event = frames[0].events[0]
    assert event.event_type == "employee.created"
    assert event.aggregate_id == state.agent_id
    assert event.payload["hire_intent_id"] == state.intent_id
    assert event.payload["provisioning_attempt_id"] == state.attempt_id
    assert event.payload["planned_bot_principal_id"] == state.bot_principal_id
    assert event.payload["state"] == EmployeeState.PROVISIONING_APP.value
    assert event.payload["worker_type"] == WorkerType.VISIBLE.value
    assert event.payload["requester_union_id"] == "on_admin"
    assert projection.cursor_sequence == frames[0].sequence
    assert projection.cursor_hash == frames[0].frame_hash
    assert projection.employees[state.agent_id].state is EmployeeState.PROVISIONING_APP
    assert projection.employee_name_keys[("tenant-a", "atlas")] == state.agent_id
    assert writer.anchor.read().sequence == frames[0].sequence


def test_same_tenant_message_is_stable_and_idempotent(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path)
    request = _request()

    first = service.start_hire(request)
    second = service.start_hire(request)

    assert second == first
    assert first.intent_id.startswith("hire_")
    assert first.agent_id.startswith("agt_")
    assert first.bot_principal_id.startswith("bot_")
    assert first.attempt_id.startswith("attempt_")
    assert len(tuple(writer.replay())) == 1


def test_same_message_with_changed_request_fails_closed(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path)
    service.start_hire(_request())

    with pytest.raises(HireAdmissionError, match="idempotency"):
        service.start_hire(_request(employee_name="Changed"))

    assert len(tuple(writer.replay())) == 1


def test_same_message_with_changed_existing_app_fails_closed(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path)
    service.start_hire(_request(existing_app_id="cli_first_123"))

    with pytest.raises(HireAdmissionError, match="idempotency"):
        service.start_hire(_request(existing_app_id="cli_second_456"))

    assert len(tuple(writer.replay())) == 1


def test_existing_app_cannot_be_claimed_by_two_live_hires(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path, visible_employee_limit=2)
    service.start_hire(
        _request(
            message_id="om_first",
            employee_name="Atlas",
            existing_app_id="cli_shared_123",
        )
    )

    with pytest.raises(HireAdmissionError, match="existing app already assigned"):
        service.start_hire(
            _request(
                message_id="om_second",
                employee_name="Beacon",
                existing_app_id="cli_shared_123",
            )
        )

    assert len(tuple(writer.replay())) == 1


def test_hire_replay_rejects_duplicate_live_existing_app_claim(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path, visible_employee_limit=2)
    service.start_hire(
        _request(existing_app_id="cli_shared_123")
    )
    first_frame = tuple(writer.replay())[0]
    first_event = first_frame.events[0]
    second_event = JournalEvent(
        event_type="employee.created",
        aggregate_id="agt_second",
        payload={
            **first_event.payload,
            "agent_id": "agt_second",
            "name": "Beacon",
            "hire_intent_id": "hire_second",
            "hire_message_id": "om_second",
            "planned_bot_principal_id": "bot_second",
            "provisioning_attempt_id": "attempt_second",
        },
    )

    with pytest.raises(HireProjectionError, match="duplicate existing_app_id admission"):
        HireProjection.rebuild(
            (
                first_frame,
                SimpleNamespace(sequence=2, events=(second_event,)),
            )
        )


def test_concurrent_existing_app_claim_submits_only_one_hire(tmp_path: Path) -> None:
    submitted: list[str] = []
    service, writer, _projection = _service(
        tmp_path,
        visible_employee_limit=2,
        provisioning_submitter=submitted.append,
    )
    requests = (
        _request(
            message_id="om_first",
            employee_name="Atlas",
            existing_app_id="cli_shared_123",
        ),
        _request(
            message_id="om_second",
            employee_name="Beacon",
            existing_app_id="cli_shared_123",
        ),
    )

    def admit(request):
        try:
            return service.start_hire(request)
        except HireAdmissionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, requests))

    assert sum(isinstance(result, DurableHireState) for result in results) == 1
    assert sum(isinstance(result, HireAdmissionError) for result in results) == 1
    assert len(submitted) == 1
    assert len(tuple(writer.replay())) == 1


def test_archived_hire_releases_existing_app_for_rehire(tmp_path: Path) -> None:
    service, writer, projection = _service(tmp_path, visible_employee_limit=2)
    previous = service.start_hire(
        _request(existing_app_id="cli_shared_123")
    )
    commit_workforce_events(
        writer,
        projection,
        (
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id=previous.agent_id,
                payload={"state": "archived"},
            ),
        ),
    )

    current = service.start_hire(
        _request(
            message_id="om_second",
            employee_name="Beacon",
            existing_app_id="cli_shared_123",
        )
    )

    assert current.agent_id != previous.agent_id
    assert current.existing_app_id == "cli_shared_123"
    assert len(HireProjection.rebuild(writer.replay()).states) == 2


def test_existing_app_claim_rejects_authoritative_principal_binding(
    tmp_path: Path,
) -> None:
    service, writer, _projection = _service(tmp_path, visible_employee_limit=2)
    first = service.start_hire(_request())
    service._bind_principal(first, "cli_shared_123", "cred_first")

    with pytest.raises(HireAdmissionError, match="existing app already assigned"):
        service.start_hire(
            _request(
                message_id="om_second",
                employee_name="Beacon",
                existing_app_id="cli_shared_123",
            )
        )

    assert len(tuple(writer.replay())) == 2


def test_archived_principal_binding_releases_app_for_existing_app_hire(
    tmp_path: Path,
) -> None:
    service, writer, projection = _service(tmp_path, visible_employee_limit=2)
    first = service.start_hire(_request())
    service._bind_principal(first, "cli_shared_123", "cred_first")
    commit_workforce_events(
        writer,
        projection,
        (
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id=first.agent_id,
                payload={"state": "archived"},
            ),
        ),
    )

    second = service.start_hire(
        _request(
            message_id="om_second",
            employee_name="Beacon",
            existing_app_id="cli_shared_123",
        )
    )

    assert second.existing_app_id == "cli_shared_123"


def test_default_registration_cannot_select_app_owned_by_live_principal(
    tmp_path: Path,
) -> None:
    service, _writer, _projection = _service(tmp_path, visible_employee_limit=2)
    first = service.start_hire(_request())
    service._bind_principal(first, "cli_shared_123", "cred_first")
    second = service.start_hire(
        _request(message_id="om_second", employee_name="Beacon")
    )
    for next_state in (HireEffectState.PREPARED, HireEffectState.EXECUTING):
        second = service.commit_effect_transition(
            second.intent_id,
            effect_id="register-app",
            effect_type="app_registration",
            next_state=next_state,
        )

    with pytest.raises(HireAdmissionError, match="registered app already assigned"):
        service._commit_registered_app(second.intent_id, "cli_shared_123")

    current = service._hire_projection.get(second.intent_id)
    assert current is not None
    assert current.effect_state("register-app") is HireEffectState.EXECUTING


def test_default_registration_result_reserves_app_for_later_admission(
    tmp_path: Path,
) -> None:
    service, writer, _projection = _service(tmp_path, visible_employee_limit=2)
    first = service.start_hire(_request())
    for next_state in (HireEffectState.PREPARED, HireEffectState.EXECUTING):
        first = service.commit_effect_transition(
            first.intent_id,
            effect_id="register-app",
            effect_type="app_registration",
            next_state=next_state,
        )
    first = service._commit_registered_app(first.intent_id, "cli_shared_123")

    with pytest.raises(HireAdmissionError, match="existing app already assigned"):
        service.start_hire(
            _request(
                message_id="om_second",
                employee_name="Beacon",
                existing_app_id="cli_shared_123",
            )
        )

    assert dict(first.metadata_for("register-app"))["app_id"] == "cli_shared_123"
    assert len(tuple(writer.replay())) == 4


def test_concurrent_default_registration_results_reserve_app_once(
    tmp_path: Path,
) -> None:
    service, _writer, _projection = _service(tmp_path, visible_employee_limit=2)
    states = [
        service.start_hire(
            _request(message_id="om_first", employee_name="Atlas")
        ),
        service.start_hire(
            _request(message_id="om_second", employee_name="Beacon")
        ),
    ]
    for index, state in enumerate(states):
        for next_state in (HireEffectState.PREPARED, HireEffectState.EXECUTING):
            state = service.commit_effect_transition(
                state.intent_id,
                effect_id="register-app",
                effect_type="app_registration",
                next_state=next_state,
            )
        states[index] = state

    def reserve(state):
        try:
            return service._commit_registered_app(
                state.intent_id,
                "cli_shared_123",
            )
        except HireAdmissionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, states))

    assert sum(isinstance(result, DurableHireState) for result in results) == 1
    assert sum(isinstance(result, HireAdmissionError) for result in results) == 1


@pytest.mark.parametrize(
    "existing_app_id",
    ("cli_", "not_cli_existing", "cli_bad/app"),
)
def test_hire_service_rejects_invalid_existing_app_before_journal(
    tmp_path: Path,
    existing_app_id: str,
) -> None:
    service, writer, _projection = _service(tmp_path)

    with pytest.raises(HireAdmissionError, match="existing_app_id"):
        service.start_hire(_request(existing_app_id=existing_app_id))

    assert tuple(writer.replay()) == ()


def test_hire_replay_rejects_invalid_existing_app_identity(tmp_path: Path) -> None:
    from src.autonomous.provisioning.hire_state import HireProjectionError

    service, writer, _projection = _service(tmp_path)
    service.start_hire(_request(existing_app_id="cli_existing_123"))
    frame = tuple(writer.replay())[0]
    event = frame.events[0]
    invalid_event = JournalEvent(
        event_type=event.event_type,
        aggregate_id=event.aggregate_id,
        payload={**event.payload, "existing_app_id": "cli_bad/app"},
    )

    with pytest.raises(HireProjectionError, match="existing_app_id"):
        HireProjection.rebuild((replace(frame, events=(invalid_event,)),))


def test_duplicate_tenant_name_is_rejected_without_second_frame(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path, visible_employee_limit=2)
    service.start_hire(_request())

    with pytest.raises(HireAdmissionError, match="name"):
        service.start_hire(_request(message_id="om_hire_2", employee_name="atlas"))

    assert len(tuple(writer.replay())) == 1


def test_same_name_rehire_atomically_releases_archived_tombstone(tmp_path: Path) -> None:
    service, writer, projection = _service(tmp_path, visible_employee_limit=2)
    previous = service.start_hire(_request())
    commit_workforce_events(
        writer,
        projection,
        (
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id=previous.agent_id,
                payload={"state": "archived"},
            ),
        ),
    )

    current = service.start_hire(
        _request(message_id="om_hire_2", employee_name="ATLAS")
    )

    assert current.agent_id != previous.agent_id
    assert projection.employees[previous.agent_id].state is EmployeeState.ARCHIVED
    assert projection.employee_name_keys[("tenant-a", "atlas")] == current.agent_id
    assert [event.event_type for event in tuple(writer.replay())[-1].events] == [
        "employee.name_released",
        "employee.created",
    ]


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"visible_employee_limit": 0}, "visible_employee_limit"),
        ({"release_evidence_ready": False}, "release_evidence"),
        ({"credential_keyring_ready": False}, "credential_keyring"),
        ({"memory_anchor": True}, "production_anchor"),
        ({"runtime_recovery_ready": False}, "runtime_recovery"),
    ],
)
def test_readiness_gates_reject_before_journal_write(
    tmp_path: Path,
    overrides: dict[str, object],
    blocker: str,
) -> None:
    service, writer, projection = _service(tmp_path, **overrides)

    readiness = service.readiness()
    assert readiness.ready is False
    assert blocker in readiness.blockers
    with pytest.raises(HireAdmissionError, match=blocker):
        service.start_hire(_request())

    assert tuple(writer.replay()) == ()
    assert projection.cursor_sequence == 0


def test_admission_opens_only_after_runtime_recovery_completes(
    tmp_path: Path,
) -> None:
    service, writer, _projection = _service(
        tmp_path,
        runtime_recovery_ready=False,
    )
    with pytest.raises(HireAdmissionError, match="runtime_recovery"):
        service.start_hire(_request())

    service.mark_runtime_recovered()
    admitted = service.start_hire(_request())

    assert admitted.intent_id
    assert len(tuple(writer.replay())) == 1


def test_recover_rebuilds_hire_and_canonical_projection(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path)
    admitted = service.start_hire(_request())
    service.close()

    reopened_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered_service = ProductionEmployeeHireService(
        reopened_writer,
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
    )

    recovered = recovered_service.recover()

    assert recovered.get(admitted.intent_id) == admitted
    assert recovered_service.projection_state.cursor_sequence == 1
    assert recovered_service.start_hire(_request()) == admitted
    assert len(tuple(reopened_writer.replay())) == 1
    recovered_service.close()


def test_hire_state_and_replay_projection_are_frozen(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path)
    state = service.start_hire(_request())
    rebuilt = HireProjection.rebuild(writer.replay())

    assert rebuilt.get(state.intent_id) == state
    assert state.phase is HirePhase.PROVISIONING_APP
    assert state.effects == ()
    assert HireEffectState.PREPARED.value == "prepared"
    with pytest.raises(FrozenInstanceError):
        state.phase = HirePhase.ACTIVE  # type: ignore[misc]
    with pytest.raises(TypeError):
        rebuilt.states[state.intent_id] = DurableHireState()  # type: ignore[index]

    service.close()


def test_effect_transition_commit_is_anchored_and_keeps_projection_cursor_current(
    tmp_path: Path,
) -> None:
    service, writer, projection = _service(tmp_path)
    admitted = service.start_hire(_request())

    prepared = service.commit_effect_transition(
        admitted.intent_id,
        effect_id="register-app",
        effect_type="app_registration",
        next_state=HireEffectState.PREPARED,
    )
    executing = service.commit_effect_transition(
        admitted.intent_id,
        effect_id="register-app",
        effect_type="app_registration",
        next_state=HireEffectState.EXECUTING,
    )

    frames = tuple(writer.replay())
    assert [frame.events[0].event_type for frame in frames] == [
        "employee.created",
        "hire.effect.prepared",
        "hire.effect.executing",
    ]
    assert prepared.effect_state("register-app") is HireEffectState.PREPARED
    assert executing.effect_state("register-app") is HireEffectState.EXECUTING
    assert projection.cursor_sequence == 3
    assert projection.cursor_hash == frames[-1].frame_hash
    assert writer.anchor.read().sequence == 3


def test_invalid_effect_transition_rejects_before_journal_write(tmp_path: Path) -> None:
    service, writer, _projection = _service(tmp_path)
    admitted = service.start_hire(_request())

    with pytest.raises(HireAdmissionError, match="effect transition"):
        service.commit_effect_transition(
            admitted.intent_id,
            effect_id="register-app",
            effect_type="app_registration",
            next_state=HireEffectState.EXECUTING,
        )

    assert len(tuple(writer.replay())) == 1


def test_closed_service_rejects_even_an_idempotent_request(tmp_path: Path) -> None:
    service, _writer, _projection = _service(tmp_path)
    request = _request()
    service.start_hire(request)
    service.close()

    with pytest.raises(HireAdmissionError, match="closed"):
        service.start_hire(request)


def test_hire_request_keeps_old_callers_compatible_but_service_requires_tenant(
    tmp_path: Path,
) -> None:
    legacy = EmployeeHireRequest(
        employee_name="Atlas",
        tool="codex",
        model="model",
        effort="high",
        chat_id="oc_dm",
        message_id="om_legacy",
        requester_principal_id="ou_admin",
    )
    assert legacy.tenant_key == ""
    assert legacy.profile == "standard"
    assert legacy.role == ""
    assert legacy.persona == ""
    assert legacy.requester_union_id == ""
    service, writer, _projection = _service(tmp_path)

    with pytest.raises(HireAdmissionError, match="tenant_key"):
        service.start_hire(legacy)

    assert tuple(writer.replay()) == ()


def test_legacy_pending_hire_can_durably_bind_cross_app_identity(
    tmp_path: Path,
) -> None:
    service, writer, _projection = _service(tmp_path)
    pending = service.start_hire(replace(_request(), requester_union_id=""))

    bound = service.bind_requester_union_id(pending.intent_id, "on_admin")
    replayed = service.recover().get(pending.intent_id)

    assert bound.requester_union_id == "on_admin"
    assert replayed is not None and replayed.requester_union_id == "on_admin"
    assert [
        event.event_type for frame in writer.replay() for event in frame.events
    ][-1] == "hire.requester_identity_bound"


@pytest.mark.parametrize(
    "hire_request",
    [
        replace(_request(), effort="potato"),
        replace(_request(), model="gpt-5.6-sol/xhigh", effort="xhigh"),
        replace(_request(), profile="max"),
    ],
)
def test_hire_rejects_noncanonical_or_unsupported_model_components(
    tmp_path: Path,
    hire_request: EmployeeHireRequest,
) -> None:
    service, writer, _projection = _service(tmp_path)

    with pytest.raises(HireAdmissionError, match="invalid employee model selection"):
        service.start_hire(hire_request)

    assert tuple(writer.replay()) == ()


def test_journal_hmac_and_anchor_settings_are_strict_redacted_and_fail_closed() -> None:
    defaults = Settings(_env_file=None)
    assert defaults.autonomous_journal_hmac_key.get_secret_value() == ""
    assert defaults.autonomous_anchor_path == "~/.ghostap/autonomy/journal.anchor"
    assert defaults.autonomous_anchor_provider == ""
    assert defaults.autonomous_visible_employee_limit == 8
    assert defaults.autonomous_employee_release_trust_socket == ""
    assert defaults.autonomous_employee_release_trust_timeout_seconds == 2.0
    assert defaults.autonomous_main_bot_audit_dir.endswith("main-bot-send-audit")
    assert defaults.autonomous_main_bot_audit_anchor_path.endswith(
        "main-bot-send-audit.anchor"
    )

    encoded = base64.urlsafe_b64encode(b"k" * 32).decode()
    configured = Settings(
        _env_file=None,
        autonomous_journal_hmac_key=encoded,
        autonomous_anchor_provider="file",
        autonomous_anchor_path="/var/lib/ghostap/journal.anchor",
    )
    assert encoded not in repr(configured)

    with pytest.raises(ValueError, match="autonomous_journal_hmac_key"):
        Settings(_env_file=None, autonomous_journal_hmac_key="too-short")
    with pytest.raises(ValueError, match="autonomous_anchor_provider"):
        Settings(_env_file=None, autonomous_anchor_provider="file anchor")
    with pytest.raises(ValueError, match="rejects booleans"):
        Settings(
            _env_file=None,
            autonomous_employee_release_trust_timeout_seconds=True,
        )
