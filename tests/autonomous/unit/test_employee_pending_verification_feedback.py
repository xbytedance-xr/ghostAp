from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

from src.autonomous.ingress.models import EmployeeIngressMetadata
from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime
from src.autonomous.provisioning.hire_state import (
    DurableHireState,
    HireEffectState,
    HirePhase,
)
from src.autonomous.supervisor.employee_channels import (
    ChannelProcessState,
    ChannelSendReceipt,
)


def _pending_state() -> DurableHireState:
    return DurableHireState(
        intent_id="hire_pending_mention",
        tenant_key="tenant-a",
        message_id="om_hire",
        chat_id="oc_admin_dm",
        requester_principal_id="ou_admin",
        requester_union_id="on_admin",
        employee_name="Atlas",
        tool="codex",
        model="gpt-5.6-sol",
        effort="high",
        agent_id="agt_pending_mention",
        bot_principal_id="bot_pending_mention",
        app_id="cli_employee",
        credential_ref="cred_pending_mention",
        slash_spec_hash="a" * 64,
        slash_observed_hash="a" * 64,
        slash_verified_at=1.0,
        channel_generation=2,
        channel_identity_app_id="cli_employee",
        channel_connection_id="conn_pending_mention",
        channel_verified_at=2.0,
        verification_nonce="nonce_pending_mention",
        verification_issued_at=1.0,
        verification_expires_at=9_999_999_999.0,
        phase=HirePhase.READY_PENDING_VERIFICATION,
    )


class _Service:
    def __init__(self, state: DurableHireState) -> None:
        self.current = state
        self.transitions: list[HireEffectState] = []
        self.projection_state = SimpleNamespace(
            employees={
                state.agent_id: SimpleNamespace(
                    owner_principal_id=state.requester_principal_id,
                    member_groups=frozenset({"oc_employee_team"}),
                )
            }
        )

    def synchronize_projection(self):
        return self.projection_state

    def list_states(self):
        return (self.current,)

    def commit_effect_transition(
        self,
        intent_id,
        *,
        effect_id,
        effect_type,
        next_state,
        metadata=None,
    ):
        assert intent_id == self.current.intent_id
        assert effect_id.startswith("activation-required-reply:")
        assert effect_type == "employee_activation_required_reply"
        effects = dict(self.current.effects)
        effect_types = dict(self.current.effect_types)
        effect_metadata = {
            key: dict(value) for key, value in self.current.effect_metadata
        }
        effects[effect_id] = next_state
        effect_types[effect_id] = effect_type
        effect_metadata[effect_id] = {
            **effect_metadata.get(effect_id, {}),
            **(metadata or {}),
        }
        self.current = replace(
            self.current,
            effects=tuple(sorted(effects.items())),
            effect_types=tuple(sorted(effect_types.items())),
            effect_metadata=tuple(
                sorted(
                    (key, tuple(sorted(value.items())))
                    for key, value in effect_metadata.items()
                )
            ),
        )
        self.transitions.append(next_state)
        return self.current


class _Ingress:
    def __init__(self) -> None:
        self.dispositions: list[tuple[str, str, str]] = []

    def record_disposition(self, acceptance_id, *, state, reason_code):
        self.dispositions.append((acceptance_id, state, reason_code))


class _Channels:
    def __init__(self, state: DurableHireState) -> None:
        self.sent = []
        self._status = SimpleNamespace(
            agent_id=state.agent_id,
            app_id=state.app_id,
            tenant_key=state.tenant_key,
            bot_principal_id=state.bot_principal_id,
            state=ChannelProcessState.READY,
            generation=state.channel_generation,
            identity={"app_id": state.app_id, "open_id": "ou_employee"},
            ready_metadata={"connection_id": state.channel_connection_id},
        )

    def status(self, agent_id):
        return self._status if agent_id == self._status.agent_id else None

    def send(self, agent_id, *, generation, target, message, options=None):
        self.sent.append((agent_id, generation, target, message, options))
        return ChannelSendReceipt(
            request_id="send_pending_notice",
            success=True,
            app_id=self._status.app_id,
            generation=generation,
            connection_id=self._status.ready_metadata["connection_id"],
            message_id="om_pending_notice",
        )


def _metadata(state: DurableHireState) -> EmployeeIngressMetadata:
    raw_chat_id = "oc_employee_team"
    raw_message_id = "om_pending_group_mention"
    return EmployeeIngressMetadata(
        schema_version=1,
        envelope_id="ing_" + hashlib.sha256(b"pending_envelope").hexdigest(),
        tenant_key=state.tenant_key,
        agent_id=state.agent_id,
        bot_principal_id=state.bot_principal_id,
        app_id=state.app_id,
        channel_generation=state.channel_generation,
        connection_id=state.channel_connection_id,
        event_id="evt_" + hashlib.sha256(b"pending_group_mention").hexdigest(),
        message_id="om_" + hashlib.sha256(raw_message_id.encode()).hexdigest(),
        event_type="im.message.receive_v1",
        action_identity="",
        chat_id="oc_" + hashlib.sha256(raw_chat_id.encode()).hexdigest(),
        thread_root_message_id="",
        sender_principal_id="ou_employee_app_admin",
        received_at="2026-07-16T00:00:00.000Z",
        semantic_digest="b" * 64,
        payload_sha256="c" * 64,
        payload_size_bytes=1,
        attachment_count=0,
        attachment_total_bytes=0,
    )


def test_pending_group_direct_mention_durably_guides_status_once() -> None:
    state = _pending_state()
    service = _Service(state)
    ingress = _Ingress()
    channels = _Channels(state)
    runtime = EmployeeDepartmentRuntime()
    runtime._service = service
    runtime._ingress = ingress
    runtime._channels = channels
    metadata = _metadata(state)
    first = {
        "type": "message",
        "message_type": "text",
        "chat_type": "group",
        "content": {"text": "@_user_1 你好"},
        "mentions": (
            {
                "key": "@_user_1",
                "mentioned_type": "bot",
                "open_id": "ou_employee",
                "tenant_key": state.tenant_key,
            },
        ),
        "sender_id": metadata.sender_principal_id,
        "sender_id_type": "open_id",
        "sender_type": "user",
        "sender_tenant_key": state.tenant_key,
        "remote_chat_id": "oc_employee_team",
        "remote_message_id": "om_pending_group_mention",
        "remote_root_id": "",
    }
    acceptance_id = "acc_" + hashlib.sha256(b"pending_acceptance").hexdigest()
    record = SimpleNamespace(metadata=metadata)

    assert runtime._handle_pending_verification_group_mention(
        acceptance_id=acceptance_id,
        record=record,
        first=first,
    )

    assert service.transitions == [
        HireEffectState.PREPARED,
        HireEffectState.EXECUTING,
        HireEffectState.COMMITTED,
    ]
    assert ingress.dispositions == [
        (acceptance_id, "terminal", "activation_required")
    ]
    assert len(channels.sent) == 1
    agent_id, generation, target, message, options = channels.sent[0]
    assert (agent_id, generation, target) == (
        state.agent_id,
        state.channel_generation,
        "oc_employee_team",
    )
    assert state.employee_name in message["text"]
    assert "私聊" in message["text"] and "/status" in message["text"]
    assert options["reply_to"] == "om_pending_group_mention"
    assert options["uuid"] == hashlib.sha256(
        f"employee-activation-required:{state.agent_id}:{acceptance_id}".encode()
    ).hexdigest()[:50]
    assert service.current.phase is HirePhase.READY_PENDING_VERIFICATION
    assert service.current.effect_state(
        f"activation-required-reply:{metadata.event_id}"
    ) is HireEffectState.COMMITTED

    assert runtime._send_activation_required_notice(
        state=service.current,
        acceptance_id=acceptance_id,
        event_id=metadata.event_id,
        target_chat_id="oc_employee_team",
        reply_message_id="om_pending_group_mention",
    )
    assert len(channels.sent) == 1

    invalid_mentions = (
        (
            {
                "key": "@_user_1",
                "mentioned_type": "user",
                "open_id": "ou_employee",
                "tenant_key": state.tenant_key,
            },
        ),
        (
            {
                "key": "@_user_2",
                "mentioned_type": "bot",
                "open_id": "ou_other_bot",
                "tenant_key": state.tenant_key,
            },
        ),
        first["mentions"] + first["mentions"],
    )
    for mentions in invalid_mentions:
        assert not runtime._handle_pending_verification_group_mention(
            acceptance_id=acceptance_id,
            record=record,
            first={**first, "mentions": mentions},
        )
    assert len(channels.sent) == 1
    assert len(ingress.dispositions) == 1


def test_runtime_maps_only_active_hire_owner_union_to_main_bot_principal() -> None:
    active = replace(_pending_state(), phase=HirePhase.ACTIVE)
    runtime = EmployeeDepartmentRuntime()
    runtime._service = SimpleNamespace(
        list_states=lambda: (active,),
        synchronize_projection=lambda: None,
    )

    assert runtime._resolve_employee_requester_principal(
        tenant_key=active.tenant_key,
        agent_id=active.agent_id,
        owner_principal_id=active.requester_principal_id,
        sender_principal_id="ou_employee_app_admin",
        sender_union_id=active.requester_union_id,
    ) == active.requester_principal_id
    assert runtime._resolve_employee_requester_principal(
        tenant_key=active.tenant_key,
        agent_id=active.agent_id,
        owner_principal_id=active.requester_principal_id,
        sender_principal_id="ou_employee_app_attacker",
        sender_union_id="on_attacker",
    ) is None
    runtime._service = SimpleNamespace(
        list_states=lambda: (
            replace(active, phase=HirePhase.READY_PENDING_VERIFICATION),
        ),
        synchronize_projection=lambda: None,
    )
    assert runtime._resolve_employee_requester_principal(
        tenant_key=active.tenant_key,
        agent_id=active.agent_id,
        owner_principal_id=active.requester_principal_id,
        sender_principal_id="ou_employee_app_admin",
        sender_union_id=active.requester_union_id,
    ) is None
