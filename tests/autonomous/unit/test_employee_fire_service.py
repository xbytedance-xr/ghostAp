from __future__ import annotations

from dataclasses import dataclass

from src.autonomous.journal.anchor import MemoryAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.provisioning.fire_service import (
    EmployeeFireRequest,
    EmployeeFireService,
    EmployeeFireTarget,
)
from src.autonomous.provisioning.fire_state import FIRE_EFFECT_ORDER, FirePhase


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
                },
            )
        )

    def mark_action_required(self, agent_id):
        self.action_required.append(agent_id)

    def mark_credential_destroyed(self, target):
        self.destroyed.append(target.credential_ref)

    def mark_archived(self, agent_id, intent_id):
        self._commit(
            JournalEvent(
                event_type="fire.completed",
                aggregate_id=intent_id,
                payload={"external_app_disposition": "manual_deletion_required"},
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

    def execute(self, _state):
        self.calls.append(self.name)

    def observe(self, _state):
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
