from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.autonomous.journal.anchor import MemoryAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.provisioning.fire_service import (
    EmployeeFireRequest,
    EmployeeFireService,
    EmployeeFireTarget,
    FireServiceError,
)
from src.autonomous.provisioning.fire_state import (
    FIRE_EFFECT_ORDER,
    FireCleanupMode,
    FirePhase,
    FireProjectionError,
    rebuild_fire_projection,
)


class _Authority:
    def __init__(self, writer: JournalWriter) -> None:
        self.writer = writer
        self.target = EmployeeFireTarget(
            tenant_key="tenant_1",
            agent_id="agt_alpha",
            employee_name="Alpha",
            bot_principal_id="bot_alpha",
            app_id="cli_alpha",
            credential_ref="cred_alpha",
        )
        self.action_required: list[str] = []
        self.destroyed: list[str] = []

    def authorize_request(self, request):
        if request.requester_principal_id != "ou_admin":
            raise FireServiceError("fire is not authorized")

    def resolve(self, request):
        assert request.requester_principal_id == "ou_admin"
        return self.target

    def admit(self, request, target, intent_id):
        self._commit(
            JournalEvent(
                event_type="fire.requested",
                aggregate_id=intent_id,
                payload={
                    "intent_id": intent_id,
                    "tenant_key": request.tenant_key,
                    "message_id": request.message_id,
                    "chat_id": request.chat_id,
                    "requester_principal_id": request.requester_principal_id,
                    "agent_id": target.agent_id,
                    "employee_name": target.employee_name,
                    "bot_principal_id": target.bot_principal_id,
                    "app_id": target.app_id,
                    "credential_ref": target.credential_ref,
                    "drain": request.drain,
                    "cleanup_mode": target.cleanup_mode.value,
                },
            )
        )
        return target

    def mark_action_required(self, agent_id):
        self.action_required.append(agent_id)

    def confirm_external_disposition(self, request, state, disposition_ref):
        assert request.requester_principal_id == "ou_admin"
        self._commit(
            JournalEvent(
                event_type="fire.external_disposition_confirmed",
                aggregate_id=state.intent_id,
                payload={
                    "disposition_ref": disposition_ref,
                    "disposed_by": request.requester_principal_id,
                    "disposed_at": "2026-07-15T00:00:00+00:00",
                    "confirmation_message_id": request.message_id,
                },
            )
        )

    def mark_credential_destroyed(self, target):
        self.destroyed.append(target.credential_ref)

    def mark_archived(
        self,
        agent_id,
        intent_id,
        *,
        external_disposition_confirmed,
    ):
        self._commit(
            JournalEvent(
                event_type="fire.completed",
                aggregate_id=intent_id,
                payload={
                    "external_app_disposition": (
                        "manual_disposition_confirmed"
                        if external_disposition_confirmed
                        else "manual_deletion_required"
                    )
                },
            )
        )

    def _commit(self, event):
        last = self.writer.get_last_frame()
        self.writer.commit(
            (event,),
            self.writer.get_aggregate_versions((event.aggregate_id,)),
            expected_head_sequence=0 if last is None else last.sequence,
            expected_head_hash="" if last is None else last.frame_hash,
        )


@dataclass
class _Effect:
    name: str
    calls: list[str]
    observed: bool | None = True
    observations: list[str] | None = None

    def execute(self, _state):
        self.calls.append(self.name)

    def observe(self, _state):
        if self.observations is not None:
            self.observations.append(self.name)
        return self.observed


def _request() -> EmployeeFireRequest:
    return EmployeeFireRequest(
        employee="Alpha",
        tenant_key="tenant_1",
        message_id="om_fire_1",
        chat_id="oc_dm",
        requester_principal_id="ou_admin",
    )


def test_fire_anchors_each_effect_in_order_and_archives_only_after_cleanup(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )

    state = service.start_fire(_request())

    assert state.phase is FirePhase.ARCHIVED
    assert calls == list(FIRE_EFFECT_ORDER)
    assert authority.destroyed == ["cred_alpha"]
    assert service.start_fire(_request()) == state
    event_types = [event.event_type for frame in writer.replay() for event in frame.events]
    for effect in FIRE_EFFECT_ORDER:
        prepared = next(i for i, event in enumerate(event_types) if event == "fire.effect.prepared")
        executing = next(i for i, event in enumerate(event_types[prepared + 1 :], prepared + 1) if event == "fire.effect.executing")
        committed = next(i for i, event in enumerate(event_types[executing + 1 :], executing + 1) if event == "fire.effect.committed")
        assert prepared < executing < committed
        event_types = event_types[committed + 1 :]
    writer.close()


def test_unknown_external_outcome_is_action_required_and_recovery_never_replays(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    effects = {name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER}
    effects["slash_cleanup"].observed = None
    authority = _Authority(writer)
    service = EmployeeFireService(writer=writer, authority=authority, effects=effects)

    state = service.start_fire(_request())
    assert state.phase is FirePhase.ACTION_REQUIRED
    assert state.error_code == "outcome_unknown"
    assert calls == ["execution_quiesce", "slash_cleanup"]
    assert authority.destroyed == []

    recovered = service.recover()
    assert recovered == ()
    assert calls == ["execution_quiesce", "slash_cleanup"]
    writer.close()


def test_repeated_fire_reobserves_failed_effect_without_reexecuting_it(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    effects = {name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER}
    effects["slash_cleanup"].observed = None
    authority = _Authority(writer)
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects=effects,
    )
    pending = service.start_fire(_request())
    effects["slash_cleanup"].observed = True
    retry = EmployeeFireRequest(
        employee="Alpha",
        tenant_key="tenant_1",
        message_id="om_fire_retry_observe",
        chat_id="oc_dm",
        requester_principal_id="ou_admin",
    )

    completed = service.start_fire(retry)

    assert pending.phase is FirePhase.ACTION_REQUIRED
    assert completed.phase is FirePhase.ARCHIVED
    assert calls.count("slash_cleanup") == 1
    assert any(
        event.event_type == "fire.effect.reconciled"
        for frame in writer.replay()
        for event in frame.events
    )
    writer.close()


def test_pre_binding_fire_skips_unavailable_external_cleanup_and_archives(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        employee_name="Alpha",
        bot_principal_id="",
        app_id="",
        credential_ref="",
        cleanup_mode=FireCleanupMode.SAFE_ABORT,
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )

    state = service.start_fire(_request())

    assert state.phase is FirePhase.ARCHIVED
    assert state.pre_binding is True
    assert calls == ["archive_move"]
    assert authority.destroyed == []
    assert all(state.effect_state(name).value == "committed" for name in FIRE_EFFECT_ORDER)
    writer.close()


def test_pre_binding_unknown_external_resources_require_manual_cleanup(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        employee_name="Alpha",
        bot_principal_id="",
        app_id="cli_registered",
        credential_ref="",
        cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )

    state = service.start_fire(_request())

    assert state.phase is FirePhase.ACTION_REQUIRED
    assert state.error_code == "external_cleanup_authority_unavailable"
    assert calls == []
    assert authority.destroyed == []
    assert authority.action_required == ["agt_alpha"]
    writer.close()


def test_manual_external_disposition_confirmation_finishes_archive_idempotently(
    tmp_path,
):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        employee_name="Alpha",
        bot_principal_id="",
        app_id="cli_registered",
        credential_ref="",
        cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )
    pending = service.start_fire(_request())

    with pytest.raises(
        FireServiceError,
        match="external disposition reference mismatch",
    ):
        service.confirm_external_disposition(_request(), "cli_wrong")
    completed = service.confirm_external_disposition(
        _request(),
        "cli_registered",
    )
    repeated = service.confirm_external_disposition(
        _request(),
        "cli_registered",
    )

    assert pending.phase is FirePhase.ACTION_REQUIRED
    assert completed.phase is FirePhase.ARCHIVED
    assert completed.external_disposition_confirmed is True
    assert repeated == completed
    assert calls == ["archive_move"]
    writer.close()


@pytest.mark.parametrize(
    ("app_id", "disposition_ref"),
    (("cli_registered", "cli_registered"), ("", "NO_APP_FOUND")),
)
def test_repeated_fire_reuses_live_saga_then_accepts_manual_confirmation(
    tmp_path,
    app_id,
    disposition_ref,
):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        employee_name="Alpha",
        bot_principal_id="",
        app_id=app_id,
        credential_ref="",
        cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )
    first = service.start_fire(_request())
    retried_request = EmployeeFireRequest(
        employee="Alpha",
        tenant_key="tenant_1",
        message_id="om_fire_retry",
        chat_id="oc_dm",
        requester_principal_id="ou_admin",
    )

    retried = service.start_fire(retried_request)
    completed = service.confirm_external_disposition(
        retried_request,
        disposition_ref,
    )

    assert retried.intent_id == first.intent_id
    assert completed.phase is FirePhase.ARCHIVED
    assert sum(
        event.event_type == "fire.requested"
        for frame in writer.replay()
        for event in frame.events
    ) == 1
    writer.close()


def test_legacy_duplicate_external_fire_sagas_are_durably_coalesced(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    authority = _Authority(writer)
    for suffix in ("one", "two"):
        intent_id = f"fire_{suffix}"
        authority._commit(
            JournalEvent(
                event_type="fire.requested",
                aggregate_id=intent_id,
                payload={
                    "intent_id": intent_id,
                    "tenant_key": "tenant_1",
                    "message_id": f"om_{suffix}",
                    "chat_id": "oc_dm",
                    "requester_principal_id": "ou_admin",
                    "agent_id": "agt_alpha",
                    "employee_name": "Alpha",
                    "bot_principal_id": "",
                    "app_id": "cli_registered",
                    "credential_ref": "",
                    "drain": False,
                    "cleanup_mode": FireCleanupMode.EXTERNAL_UNKNOWN.value,
                },
            )
        )
        for effect_state in ("prepared", "executing", "action_required"):
            payload = {"effect_type": "credential_destroy"}
            if effect_state == "action_required":
                payload["error_code"] = (
                    "external_cleanup_authority_unavailable"
                )
            authority._commit(
                JournalEvent(
                    event_type=f"fire.effect.{effect_state}",
                    aggregate_id=intent_id,
                    payload=payload,
                )
            )
    calls: list[str] = []
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )
    frames_before_unauthorized = tuple(writer.replay())
    with pytest.raises(FireServiceError, match="not authorized"):
        service.start_fire(
            EmployeeFireRequest(
                employee="Alpha",
                tenant_key="tenant_1",
                message_id="om_attacker_retry",
                chat_id="oc_dm",
                requester_principal_id="ou_attacker",
            )
        )
    assert tuple(writer.replay()) == frames_before_unauthorized

    completed = service.confirm_external_disposition(
        _request(),
        "cli_registered",
    )

    states = service._states()
    assert completed.phase is FirePhase.ARCHIVED
    assert states["fire_two"].phase is FirePhase.SUPERSEDED
    assert sum(
        event.event_type == "fire.superseded"
        for frame in writer.replay()
        for event in frame.events
    ) == 1
    with pytest.raises(FireServiceError, match="request was superseded"):
        service.resume("fire_two")
    assert service._states()["fire_two"].phase is FirePhase.SUPERSEDED
    writer.close()


def test_mixed_archived_and_live_duplicates_keep_archived_canonical(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    authority = _Authority(writer)
    for suffix in ("live", "archived"):
        intent_id = f"fire_{suffix}"
        authority._commit(
            JournalEvent(
                event_type="fire.requested",
                aggregate_id=intent_id,
                payload={
                    "intent_id": intent_id,
                    "tenant_key": "tenant_1",
                    "message_id": f"om_{suffix}",
                    "chat_id": "oc_dm",
                    "requester_principal_id": "ou_admin",
                    "agent_id": "agt_alpha",
                    "employee_name": "Alpha",
                    "bot_principal_id": "",
                    "app_id": "cli_registered",
                    "credential_ref": "",
                    "drain": False,
                    "cleanup_mode": FireCleanupMode.EXTERNAL_UNKNOWN.value,
                },
            )
        )
        for effect_state in ("prepared", "executing", "action_required"):
            payload = {"effect_type": "credential_destroy"}
            if effect_state == "action_required":
                payload["error_code"] = (
                    "external_cleanup_authority_unavailable"
                )
            authority._commit(
                JournalEvent(
                    event_type=f"fire.effect.{effect_state}",
                    aggregate_id=intent_id,
                    payload=payload,
                )
            )
    authority._commit(
        JournalEvent(
            event_type="fire.external_disposition_confirmed",
            aggregate_id="fire_archived",
            payload={
                "disposition_ref": "cli_registered",
                "disposed_by": "ou_admin",
                "disposed_at": "2026-07-15T00:00:00+00:00",
                "confirmation_message_id": "om_archived",
            },
        )
    )
    for effect_type in FIRE_EFFECT_ORDER:
        if effect_type == "credential_destroy":
            continue
        for effect_state in ("prepared", "executing", "committed"):
            authority._commit(
                JournalEvent(
                    event_type=f"fire.effect.{effect_state}",
                    aggregate_id="fire_archived",
                    payload={"effect_type": effect_type},
                )
            )
    authority._commit(
        JournalEvent(
            event_type="fire.completed",
            aggregate_id="fire_archived",
            payload={
                "external_app_disposition": "manual_disposition_confirmed"
            },
        )
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, []) for name in FIRE_EFFECT_ORDER},
    )
    frames_before_unauthorized = tuple(writer.replay())
    unauthorized = EmployeeFireRequest(
        employee="Alpha",
        tenant_key="tenant_1",
        message_id="om_attacker",
        chat_id="oc_dm",
        requester_principal_id="ou_attacker",
    )
    with pytest.raises(FireServiceError, match="not authorized"):
        service.confirm_external_disposition(
            unauthorized,
            "cli_registered",
        )
    assert tuple(writer.replay()) == frames_before_unauthorized

    completed = service.confirm_external_disposition(
        _request(),
        "cli_registered",
    )

    states = service._states()
    assert completed.intent_id == "fire_archived"
    assert states["fire_archived"].phase is FirePhase.ARCHIVED
    assert states["fire_live"].phase is FirePhase.SUPERSEDED
    superseded_aggregates = [
        event.aggregate_id
        for frame in writer.replay()
        for event in frame.events
        if event.event_type == "fire.superseded"
    ]
    assert superseded_aggregates == ["fire_live"]
    writer.close()


def test_archived_fire_saga_supersedes_live_duplicates_before_retry_or_recovery(
    tmp_path,
):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    observations: list[str] = []
    authority = _Authority(writer)
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={
            name: _Effect(name, calls, observations=observations)
            for name in FIRE_EFFECT_ORDER
        },
    )
    archived = service.start_fire(_request())
    assert archived.phase is FirePhase.ARCHIVED
    calls.clear()
    observations.clear()

    def commit_legacy_duplicate(intent_id: str, message_id: str) -> None:
        authority._commit(
            JournalEvent(
                event_type="fire.requested",
                aggregate_id=intent_id,
                payload={
                    "intent_id": intent_id,
                    "tenant_key": "tenant_1",
                    "message_id": message_id,
                    "chat_id": "oc_dm",
                    "requester_principal_id": "ou_admin",
                    "agent_id": "agt_alpha",
                    "employee_name": "Alpha",
                    "bot_principal_id": "bot_alpha",
                    "app_id": "cli_alpha",
                    "credential_ref": "cred_alpha",
                    "drain": False,
                    "cleanup_mode": FireCleanupMode.BOUND.value,
                },
            )
        )

    commit_legacy_duplicate("fire_legacy_retry", "om_legacy_retry")
    with pytest.raises(FireServiceError, match="employee already archived"):
        service.start_fire(
            EmployeeFireRequest(
                employee="Alpha",
                tenant_key="tenant_1",
                message_id="om_retry_after_archive",
                chat_id="oc_dm",
                requester_principal_id="ou_admin",
            )
        )
    assert service._states()["fire_legacy_retry"].phase is FirePhase.SUPERSEDED
    assert calls == []
    assert observations == []
    event_types = [
        event.event_type for frame in writer.replay() for event in frame.events
    ]
    assert event_types.count("fire.requested") == 2
    assert event_types.count("fire.completed") == 1

    commit_legacy_duplicate("fire_legacy_recovery", "om_legacy_recovery")
    recovered = service.recover()

    assert recovered == ()
    states = service._states()
    assert states[archived.intent_id].phase is FirePhase.ARCHIVED
    assert states["fire_legacy_recovery"].phase is FirePhase.SUPERSEDED
    assert calls == []
    assert observations == []
    assert authority.action_required == []
    frames_after_recovery = tuple(writer.replay())
    assert service.recover() == ()
    assert tuple(writer.replay()) == frames_after_recovery
    writer.close()


def test_duplicate_fire_resource_coordinate_conflict_fails_closed(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    observations: list[str] = []
    authority = _Authority(writer)
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={
            name: _Effect(name, calls, observations=observations)
            for name in FIRE_EFFECT_ORDER
        },
    )
    service.start_fire(_request())
    calls.clear()
    observations.clear()
    authority._commit(
        JournalEvent(
            event_type="fire.requested",
            aggregate_id="fire_conflicting_live",
            payload={
                "intent_id": "fire_conflicting_live",
                "tenant_key": "tenant_1",
                "message_id": "om_conflicting_live",
                "chat_id": "oc_dm",
                "requester_principal_id": "ou_admin",
                "agent_id": "agt_alpha",
                "employee_name": "Alpha",
                "bot_principal_id": "bot_alpha",
                "app_id": "cli_different",
                "credential_ref": "cred_alpha",
                "drain": False,
                "cleanup_mode": FireCleanupMode.BOUND.value,
            },
        )
    )
    frames_before = tuple(writer.replay())

    with pytest.raises(
        FireServiceError,
        match="employee retirement authority is ambiguous",
    ):
        service.start_fire(
            EmployeeFireRequest(
                employee="Alpha",
                tenant_key="tenant_1",
                message_id="om_conflicting_retry",
                chat_id="oc_dm",
                requester_principal_id="ou_admin",
            )
        )
    with pytest.raises(
        FireServiceError,
        match="employee retirement authority is ambiguous",
    ):
        service.recover()

    assert tuple(writer.replay()) == frames_before
    assert service._states()["fire_conflicting_live"].phase is FirePhase.RETIRING
    assert calls == []
    assert observations == []
    writer.close()


def test_same_name_rehire_is_not_shadowed_by_archived_fire_history(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )
    archived_alpha = service.start_fire(_request())
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_beta",
        employee_name="Alpha",
        bot_principal_id="bot_beta",
        app_id="cli_beta",
        credential_ref="cred_beta",
    )
    calls.clear()

    archived_beta = service.start_fire(
        EmployeeFireRequest(
            employee="Alpha",
            tenant_key="tenant_1",
            message_id="om_fire_rehire",
            chat_id="oc_dm",
            requester_principal_id="ou_admin",
        )
    )

    assert archived_alpha.phase is FirePhase.ARCHIVED
    assert archived_beta.phase is FirePhase.ARCHIVED
    assert archived_beta.agent_id == "agt_beta"
    assert calls == list(FIRE_EFFECT_ORDER)
    writer.close()


def test_same_name_rehire_external_confirmation_targets_pending_agent(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    authority = _Authority(writer)
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_old",
        employee_name="Alpha",
        bot_principal_id="",
        app_id="cli_old",
        credential_ref="",
        cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, []) for name in FIRE_EFFECT_ORDER},
    )
    old_request = _request()
    old_pending = service.start_fire(old_request)
    old_archived = service.confirm_external_disposition(old_request, "cli_old")
    assert old_pending.phase is FirePhase.ACTION_REQUIRED
    assert old_archived.phase is FirePhase.ARCHIVED

    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_new",
        employee_name="Alpha",
        bot_principal_id="",
        app_id="cli_new",
        credential_ref="",
        cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
    )
    new_request = EmployeeFireRequest(
        employee="Alpha",
        tenant_key="tenant_1",
        message_id="om_fire_new",
        chat_id="oc_dm",
        requester_principal_id="ou_admin",
    )
    new_pending = service.start_fire(new_request)

    new_archived = service.confirm_external_disposition(new_request, "cli_new")

    assert new_pending.phase is FirePhase.ACTION_REQUIRED
    assert new_archived.phase is FirePhase.ARCHIVED
    assert new_archived.agent_id == "agt_new"
    states = service._states()
    assert states[old_archived.intent_id].phase is FirePhase.ARCHIVED
    assert states[new_archived.intent_id].phase is FirePhase.ARCHIVED
    writer.close()


def test_late_reconcile_cannot_resurrect_superseded_fire_saga(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    authority = _Authority(writer)
    authority._commit(
        JournalEvent(
            event_type="fire.requested",
            aggregate_id="fire_duplicate",
            payload={
                "intent_id": "fire_duplicate",
                "tenant_key": "tenant_1",
                "message_id": "om_duplicate",
                "chat_id": "oc_dm",
                "requester_principal_id": "ou_admin",
                "agent_id": "agt_alpha",
                "employee_name": "Alpha",
                "bot_principal_id": "",
                "app_id": "cli_registered",
                "credential_ref": "",
                "drain": False,
                "cleanup_mode": FireCleanupMode.EXTERNAL_UNKNOWN.value,
            },
        )
    )
    for effect_state in ("prepared", "executing", "action_required"):
        payload = {"effect_type": "credential_destroy"}
        if effect_state == "action_required":
            payload["error_code"] = "external_cleanup_authority_unavailable"
        authority._commit(
            JournalEvent(
                event_type=f"fire.effect.{effect_state}",
                aggregate_id="fire_duplicate",
                payload=payload,
            )
        )
    authority._commit(
        JournalEvent(
            event_type="fire.superseded",
            aggregate_id="fire_duplicate",
            payload={"canonical_intent_id": "fire_canonical"},
        )
    )
    authority._commit(
        JournalEvent(
            event_type="fire.effect.reconciled",
            aggregate_id="fire_duplicate",
            payload={
                "effect_type": "credential_destroy",
                "resolution_code": "observed_committed",
            },
        )
    )

    with pytest.raises(
        FireProjectionError,
        match="terminal fire request rejects later events",
    ):
        rebuild_fire_projection(tuple(writer.replay()))
    writer.close()


def test_duplicate_fire_completion_is_rejected_after_archive(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    authority = _Authority(writer)
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, []) for name in FIRE_EFFECT_ORDER},
    )
    archived = service.start_fire(_request())
    assert archived.phase is FirePhase.ARCHIVED
    authority._commit(
        JournalEvent(
            event_type="fire.completed",
            aggregate_id=archived.intent_id,
            payload={"external_app_disposition": "manual_deletion_required"},
        )
    )

    with pytest.raises(
        FireProjectionError,
        match="terminal fire request rejects later events",
    ):
        rebuild_fire_projection(tuple(writer.replay()))
    writer.close()


def test_recoverable_pre_binding_credentials_are_destroyed_before_archive(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    calls: list[str] = []
    authority = _Authority(writer)
    authority.target = EmployeeFireTarget(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        employee_name="Alpha",
        bot_principal_id="bot_planned",
        app_id="cli_registered",
        credential_ref="cred_live_secret",
        cleanup_mode=FireCleanupMode.RECOVERABLE,
    )
    service = EmployeeFireService(
        writer=writer,
        authority=authority,
        effects={name: _Effect(name, calls) for name in FIRE_EFFECT_ORDER},
    )

    state = service.start_fire(_request())

    assert state.phase is FirePhase.ARCHIVED
    assert calls == list(FIRE_EFFECT_ORDER)
    assert authority.destroyed == ["cred_live_secret"]
    writer.close()


def test_legacy_pre_binding_stream_cannot_replay_false_archive(tmp_path):
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=b"fire-service-hmac-key-at-least-32-bytes",
        writer_epoch=1,
    )
    authority = _Authority(writer)
    authority._commit(
        JournalEvent(
            event_type="fire.requested",
            aggregate_id="fire_legacy",
            payload={
                "intent_id": "fire_legacy",
                "tenant_key": "tenant_1",
                "message_id": "om_legacy",
                "chat_id": "oc_dm",
                "requester_principal_id": "ou_admin",
                "agent_id": "agt_alpha",
                "employee_name": "Alpha",
                "bot_principal_id": "",
                "app_id": "cli_live",
                "credential_ref": "",
                "drain": False,
                "pre_binding": True,
            },
        )
    )
    for effect_type in FIRE_EFFECT_ORDER:
        for effect_state in ("prepared", "executing", "committed"):
            authority._commit(
                JournalEvent(
                    event_type=f"fire.effect.{effect_state}",
                    aggregate_id="fire_legacy",
                    payload={"effect_type": effect_type},
                )
            )
    authority._commit(
        JournalEvent(
            event_type="fire.completed",
            aggregate_id="fire_legacy",
            payload={"external_app_disposition": "manual_deletion_required"},
        )
    )

    with pytest.raises(
        FireProjectionError,
        match="lacks external disposition evidence",
    ):
        rebuild_fire_projection(tuple(writer.replay()))
    writer.close()
