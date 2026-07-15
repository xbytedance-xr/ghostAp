"""Fail-closed authority tests for the durable employee ingress Router."""

from __future__ import annotations

import hashlib
import importlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from src.autonomous.context.runtime import RuntimeRequesterChatAcl
from src.autonomous.domain import BotPrincipal, EmployeeDefinition, EmployeeState, WorkerType
from src.autonomous.ingress.attachments import (
    AttachmentStagingService,
    AttachmentStateError,
    DownloadedAttachment,
)
from src.autonomous.ingress.models import EmployeeIngressMetadata, EmployeeIngressPayload
from src.autonomous.ingress.projection import IngressProjectionState
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.journal.anchor import MemoryAnchor
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.frame import GENESIS_HASH, JournalEvent
from src.autonomous.journal.projections import ProjectionState
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.supervisor.employee_channels import (
    ChannelProcessState,
    ChannelProcessStatus,
)
from src.autonomous.workforce.registry import ProjectedAgentRegistry

HMAC_KEY = b"employee-router-security-hmac-key!"
DATA_KEY = b"s" * 32


def _router_module():
    return importlib.import_module("src.autonomous.ingress.router")


def test_employee_channel_state_reexports_shared_enum_identity() -> None:
    shared = importlib.import_module(
        "src.autonomous.supervisor.channel_models"
    ).ChannelProcessState

    assert ChannelProcessState is shared


def _payload(
    *,
    text: str = "inspect this change",
    sender_id: str = "ou_requester",
    sender_id_type: str = "open_id",
    sender_type: str = "user",
    sender_tenant_key: str = "tenant_1",
    part_type: str = "message",
    value: dict[str, object] | None = None,
    feishu_thread_id: str = "omt_1",
    attachment_descriptors: tuple[dict[str, object], ...] = (),
) -> EmployeeIngressPayload:
    if part_type == "card_action":
        part = {
            "type": "card_action",
            "tag": "button",
            "value": value or {},
            "sender_id": sender_id,
            "sender_id_type": sender_id_type,
            "sender_type": sender_type,
            "sender_tenant_key": sender_tenant_key,
        }
    else:
        part = {
            "type": "message",
            "message_type": "text",
            "chat_type": "group",
            "content": {"text": text},
            "sender_id": sender_id,
            "sender_id_type": sender_id_type,
            "sender_type": sender_type,
            "sender_tenant_key": sender_tenant_key,
            "feishu_thread_id": feishu_thread_id,
        }
    digest = hashlib.sha256(repr(sorted(part.items())).encode()).hexdigest()
    return EmployeeIngressPayload(
        schema_version=1,
        envelope_id="ing_" + digest,
        normalized_parts=(part,),
        attachment_descriptors=attachment_descriptors,
    )


def _metadata(payload: EmployeeIngressPayload, **changes: object) -> EmployeeIngressMetadata:
    values: dict[str, object] = {
        "schema_version": 1,
        "envelope_id": payload.envelope_id,
        "tenant_key": "tenant_1",
        "agent_id": "agt_alpha",
        "bot_principal_id": "bot_alpha",
        "app_id": "cli_alpha",
        "channel_generation": 3,
        "connection_id": "conn_3",
        "event_id": "evt_" + payload.envelope_id.removeprefix("ing_")[:24],
        "message_id": "om_" + payload.envelope_id.removeprefix("ing_")[:24],
        "event_type": (
            "card.action.trigger"
            if payload.normalized_parts[0]["type"] == "card_action"
            else "im.message.receive_v1"
        ),
        "action_identity": "",
        "chat_id": "oc_team",
        "thread_root_message_id": "om_root",
        "sender_principal_id": "ou_requester",
        "received_at": "2026-07-13T00:00:00Z",
        "semantic_digest": payload.payload_sha256,
        "payload_sha256": payload.payload_sha256,
        "payload_size_bytes": payload.canonical_size_bytes,
        "attachment_count": len(payload.attachment_descriptors),
        "attachment_total_bytes": payload.attachment_total_bytes,
    }
    values.update(changes)
    return EmployeeIngressMetadata(**values)


def _workforce() -> ProjectionState:
    state = ProjectionState()
    state.cursor_sequence = 0
    state.cursor_hash = GENESIS_HASH
    state.employees["agt_alpha"] = EmployeeDefinition(
        agent_id="agt_alpha",
        tenant_key="tenant_1",
        owner_principal_id="ou_owner",
        name="Alpha",
        tool="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        worker_type=WorkerType.VISIBLE,
        state=EmployeeState.ACTIVE,
        bot_principal_id="bot_alpha",
        member_groups=("oc_team",),
        aggregate_version=4,
    )
    state.bot_principals["bot_alpha"] = BotPrincipal(
        bot_principal_id="bot_alpha",
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        app_id="cli_alpha",
        credential_ref="cred_alpha",
    )
    return state


class _Channels:
    def __init__(self) -> None:
        self.statuses = {
            "agt_alpha": ChannelProcessStatus(
                agent_id="agt_alpha",
                app_id="cli_alpha",
                generation=3,
                pid=123,
                state=ChannelProcessState.READY,
                tenant_key="tenant_1",
                bot_principal_id="bot_alpha",
                identity={"app_id": "cli_alpha"},
                ready_metadata={"connection_id": "conn_3"},
            )
        }

    def status(self, agent_id: str):
        return self.statuses.get(agent_id)


class _MembershipHealth:
    def __init__(self, degraded: object) -> None:
        self.degraded = degraded

    def is_degraded(self, agent_id: str, team_id: str):
        assert agent_id == "agt_alpha"
        assert team_id == "oc_team"
        if isinstance(self.degraded, BaseException):
            raise self.degraded
        return self.degraded


def _stack(
    tmp_path: Path,
    *,
    allowed: bool = True,
    membership_degraded: bool = False,
    attachment_staging: object | None = None,
    fault_hook=None,
    provider_failure: str = "",
):
    module = _router_module()
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    blob_store = BlobStore(
        tmp_path / "blobs",
        AesGcmEncryptionProvider(lambda _key_ref: DATA_KEY),
    )
    ingress = EmployeeIngressService(
        writer=writer,
        blob_store=blob_store,
        ingress_state=IngressProjectionState(),
        active_key_id="k1",
    )
    workforce = _workforce()
    channels = _Channels()
    acl = RuntimeRequesterChatAcl(
        allowed_requesters=("ou_requester",) if allowed else (),
        allowed_chats=("oc_team",),
    )
    def registry_provider():
        return ProjectedAgentRegistry(workforce)

    if provider_failure == "registry":
        def registry_provider():
            raise RuntimeError("registry down")

    if provider_failure == "channel":
        def channel_failure(_agent_id: str):
            raise RuntimeError("channel down")

        channels.status = channel_failure  # type: ignore[method-assign]
    router = module.DurableEmployeeIngressRouter(
        writer=writer,
        ingress_service=ingress,
        registry_provider=registry_provider,
        channel_status_provider=channels,
        requester_acl=acl,
        queue_limits=module.RouterQueueLimits(per_employee=4, per_team=8, global_limit=16),
        membership_health=_MembershipHealth(membership_degraded),
        attachment_staging=attachment_staging,
        fault_hook=fault_hook,
        constraints_digest="a" * 64,
        system_prompt_token_reserve=512,
    )
    return module, writer, ingress, workforce, channels, router


def _accept(ingress: EmployeeIngressService, payload: EmployeeIngressPayload, **changes: object) -> str:
    metadata = _metadata(payload, **changes)
    ack = ingress.accept(metadata, payload, request_id="req_" + metadata.event_id.removeprefix("evt_"))
    return ack.acceptance.acceptance_id


def _commit_workforce_change(writer: JournalWriter, marker: str) -> None:
    aggregate_id = f"workforce_race_{marker}"
    writer.commit(
        [
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id=aggregate_id,
                payload={"marker": marker},
            )
        ],
        writer.get_aggregate_versions([aggregate_id]),
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("inactive", "authority_denied"),
        ("principal", "authority_denied"),
        ("app", "authority_denied"),
        ("generation", "authority_denied"),
        ("membership", "authority_denied"),
        ("acl", "requester_denied"),
    ),
)
def test_router_grants_only_projected_active_binding_and_live_channel(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    _, writer, ingress, workforce, channels, router = _stack(
        tmp_path,
        allowed=mutation != "acl",
    )
    if mutation == "inactive":
        workforce.employees["agt_alpha"] = replace(
            workforce.employees["agt_alpha"], state=EmployeeState.DRAFT
        )
    elif mutation == "principal":
        workforce.employees["agt_alpha"] = replace(
            workforce.employees["agt_alpha"], bot_principal_id="bot_other"
        )
    elif mutation == "app":
        channels.statuses["agt_alpha"] = replace(
            channels.statuses["agt_alpha"], app_id="cli_other"
        )
    elif mutation == "generation":
        channels.statuses["agt_alpha"] = replace(
            channels.statuses["agt_alpha"], generation=4
        )
    elif mutation == "membership":
        workforce.employees["agt_alpha"] = replace(
            workforce.employees["agt_alpha"], member_groups=()
        )
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == reason
    assert router.dequeue() is None
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    "payload",
    (
        _payload(sender_id_type="user_id"),
        _payload(sender_type="bot"),
        _payload(sender_tenant_key="tenant_other"),
        _payload(sender_id="bot_alpha"),
    ),
)
def test_untrusted_sender_fields_only_reduce_authority(
    tmp_path: Path,
    payload: EmployeeIngressPayload,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, payload)

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == (
        "bot_loop"
        if payload.normalized_parts[0]["sender_type"] in {"bot", "app"}
        else "sender_invalid"
    )
    ingress.close()
    writer.close()


def test_stale_projection_or_channel_is_rejected_again_at_dequeue(tmp_path: Path) -> None:
    _, writer, ingress, workforce, channels, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload())
    assert router.route(acceptance_id).state == "queued"
    workforce.employees["agt_alpha"] = replace(
        workforce.employees["agt_alpha"], aggregate_version=5
    )
    channels.statuses["agt_alpha"] = replace(
        channels.statuses["agt_alpha"], generation=4
    )

    assert router.dequeue() is None
    record = router.state.by_acceptance_id[acceptance_id]
    assert record.state == "terminal"
    assert record.reason_code == "authority_stale"
    ingress.close()
    writer.close()


def test_degraded_membership_is_a_deny_only_signal(tmp_path: Path) -> None:
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        membership_degraded=True,
    )
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "membership_degraded"
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    "health_result",
    (None, 1, "healthy", RuntimeError("membership unavailable")),
)
def test_only_exact_false_membership_health_is_accepted(
    tmp_path: Path,
    health_result: object,
) -> None:
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        membership_degraded=health_result,
    )
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "membership_degraded"
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    "acl_result",
    (False, 1, "allowed", object(), None, RuntimeError("acl unavailable")),
)
def test_requester_acl_requires_exact_true_and_maps_errors_to_requester_denied(
    tmp_path: Path,
    acl_result: object,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)

    class _Acl:
        def is_authorized(self, _request):
            if isinstance(acl_result, BaseException):
                raise acl_result
            return acl_result

    router._requester_acl = _Acl()
    acceptance_id = _accept(ingress, _payload())

    terminal = router.route(acceptance_id)

    assert terminal.state == "terminal"
    assert terminal.reason_code == "requester_denied"
    ingress.close()
    writer.close()


def test_membership_health_port_is_required_and_structural(tmp_path: Path) -> None:
    module, writer, ingress, workforce, channels, _ = _stack(tmp_path)
    common = {
        "writer": writer,
        "ingress_service": ingress,
        "registry_provider": lambda: ProjectedAgentRegistry(workforce),
        "channel_status_provider": channels,
        "requester_acl": RuntimeRequesterChatAcl(
            allowed_requesters=("ou_requester",),
            allowed_chats=("oc_team",),
        ),
        "queue_limits": module.RouterQueueLimits(4, 8, 16),
    }

    with pytest.raises(TypeError):
        module.DurableEmployeeIngressRouter(**common)
    with pytest.raises(TypeError, match="membership_health"):
        module.DurableEmployeeIngressRouter(
            **common,
            membership_health=object(),
        )
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    ("status_field", "wrong_value"),
    (("tenant_key", "tenant_other"), ("bot_principal_id", "bot_other")),
)
def test_live_channel_status_binds_tenant_and_bot_principal(
    tmp_path: Path,
    status_field: str,
    wrong_value: str,
) -> None:
    _, writer, ingress, _, channels, router = _stack(tmp_path)
    status = channels.statuses["agt_alpha"]
    values = {
        "agent_id": status.agent_id,
        "app_id": status.app_id,
        "generation": status.generation,
        "pid": status.pid,
        "state": status.state,
        "identity": status.identity,
        "ready_metadata": status.ready_metadata,
        "tenant_key": "tenant_1",
        "bot_principal_id": "bot_alpha",
    }
    values[status_field] = wrong_value
    channels.statuses["agt_alpha"] = SimpleNamespace(**values)
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "authority_denied"
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("generation", 3.0),
        ("generation", True),
        ("generation", "3"),
        ("generation", 0),
        ("agent_id", ""),
        ("app_id", ""),
        ("tenant_key", ""),
        ("bot_principal_id", ""),
        ("connection_id", ""),
        ("connection_id", 3),
        ("identity", []),
        ("ready_metadata", []),
        ("state", "ready"),
        ("state", SimpleNamespace(value="ready")),
    ),
)
def test_live_channel_status_requires_strict_parent_trusted_shape(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
) -> None:
    _, writer, ingress, _, channels, router = _stack(tmp_path)
    status = channels.statuses["agt_alpha"]
    values: dict[str, object] = {
        "agent_id": status.agent_id,
        "app_id": status.app_id,
        "generation": status.generation,
        "pid": status.pid,
        "state": status.state,
        "identity": status.identity,
        "ready_metadata": dict(status.ready_metadata),
        "tenant_key": status.tenant_key,
        "bot_principal_id": status.bot_principal_id,
    }
    if field_name == "connection_id":
        values["ready_metadata"]["connection_id"] = invalid_value
    else:
        values[field_name] = invalid_value
    channels.statuses["agt_alpha"] = SimpleNamespace(**values)
    acceptance_id = _accept(ingress, _payload())

    terminal = router.route(acceptance_id)

    assert terminal.state == "terminal"
    assert terminal.reason_code == "authority_denied"
    ingress.close()
    writer.close()


def test_unrelated_projection_head_advancement_does_not_stale_same_employee_version(
    tmp_path: Path,
) -> None:
    _, writer, ingress, workforce, channels, router = _stack(tmp_path)
    original_status = channels.status

    def advancing_status(agent_id: str):
        result = original_status(agent_id)
        committed = writer.commit(
            [
                JournalEvent(
                    event_type="test.unrelated",
                    aggregate_id="unrelated",
                    payload={"value": workforce.cursor_sequence + 1},
                )
            ],
            writer.get_aggregate_versions(["unrelated"]),
        )
        workforce.cursor_sequence = committed.frame.sequence
        workforce.cursor_hash = committed.frame.frame_hash
        return result

    channels.status = advancing_status  # type: ignore[method-assign]
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "queued"
    assert record.authority is not None
    assert record.authority.employee_version == 4
    ingress.close()
    writer.close()


def test_future_or_foreign_projection_coordinates_are_durably_denied(
    tmp_path: Path,
) -> None:
    _, writer, ingress, workforce, _, router = _stack(tmp_path)
    workforce.cursor_sequence = writer.anchor.read().sequence + 1
    workforce.cursor_hash = "f" * 64
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "authority_denied"
    ingress.close()
    writer.close()


def test_sequence_zero_projection_requires_genesis_hash_semantics(
    tmp_path: Path,
) -> None:
    _, writer, ingress, workforce, _, router = _stack(tmp_path)
    workforce.cursor_sequence = 0
    workforce.cursor_hash = "f" * 64
    acceptance_id = _accept(ingress, _payload())

    assert router.route(acceptance_id).reason_code == "authority_denied"
    ingress.close()
    writer.close()


@pytest.mark.parametrize("invalid_field", ("identity", "ready_metadata"))
def test_malformed_channel_status_is_a_durable_authority_deny(
    tmp_path: Path,
    invalid_field: str,
) -> None:
    _, writer, ingress, _, channels, router = _stack(tmp_path)
    channels.statuses["agt_alpha"] = replace(
        channels.statuses["agt_alpha"],
        **{invalid_field: None},
    )
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "authority_denied"
    ingress.close()
    writer.close()


def test_invalid_execution_shape_and_acl_exception_use_specific_durable_denials(
    tmp_path: Path,
) -> None:
    _, writer, ingress, workforce, _, router = _stack(tmp_path)
    workforce.employees["agt_alpha"] = replace(
        workforce.employees["agt_alpha"],
        tool="",
    )
    invalid_shape = _accept(ingress, _payload(text="invalid shape"))
    assert router.route(invalid_shape).reason_code == "authority_denied"

    workforce.employees["agt_alpha"] = replace(
        workforce.employees["agt_alpha"],
        tool="codex",
    )

    class RaisingAcl:
        def is_authorized(self, _request) -> bool:
            raise RuntimeError("ACL unavailable")

    router._requester_acl = RaisingAcl()
    acl_failure = _accept(ingress, _payload(text="acl failure"))
    assert router.route(acl_failure).reason_code == "requester_denied"
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    "value",
    (
        {"correlation_id": "already-consumed"},
        {"correlation_id": "expired", "expires_at": 1},
        {"correlation_id": "other-employee", "agent_id": "agt_other"},
    ),
)
def test_card_actions_are_durably_unsupported_before_trusted_issuance(
    tmp_path: Path,
    value: dict[str, object],
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    payload = _payload(part_type="card_action", value=value)
    acceptance_id = _accept(ingress, payload)

    first = router.route(acceptance_id)
    replay = router.route(acceptance_id)

    assert first == replay
    assert first.state == "terminal"
    assert first.reason_code == "card_action_unsupported"
    assert router.queue_depth() == 0
    assert router.dequeue() is None
    ingress.close()
    writer.close()


def test_exact_status_control_cannot_enter_general_router_queue(tmp_path: Path) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload(text="/status"))

    routed = router.route(acceptance_id)

    assert routed.state == "terminal"
    assert routed.reason_code == "control_consumed"
    assert router.claim_control(acceptance_id, command="/status") is True
    assert router.dequeue() is None
    ingress.close()
    writer.close()


def test_status_prefix_remains_a_normal_employee_task(tmp_path: Path) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload(text="/status please inspect"))

    routed = router.route(acceptance_id)

    assert routed.state == "queued"
    assert router.claim_control(acceptance_id, command="/status") is False
    ingress.close()
    writer.close()


def test_status_control_claim_holds_shared_journal_guard(tmp_path: Path) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload(text="/status"))
    terminal_entered = Event()
    release_terminal = Event()
    unrelated_committed = Event()
    original_terminal = router._terminal_unlocked

    def blocked_terminal(record, reason_code):
        terminal_entered.set()
        assert release_terminal.wait(2)
        return original_terminal(record, reason_code)

    router._terminal_unlocked = blocked_terminal  # type: ignore[method-assign]

    def unrelated_commit() -> None:
        with writer.transaction_guard():
            _commit_workforce_change(writer, "during_status_control_claim")
        unrelated_committed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim = executor.submit(
            router.claim_control,
            acceptance_id,
            command="/status",
        )
        assert terminal_entered.wait(2)
        unrelated = executor.submit(unrelated_commit)
        assert not unrelated_committed.wait(0.1)
        release_terminal.set()
        assert claim.result(timeout=2) is True
        unrelated.result(timeout=2)

    assert unrelated_committed.is_set()
    assert router.state.by_acceptance_id[acceptance_id].reason_code == "control_consumed"
    ingress.close()
    writer.close()


def test_authorized_context_uses_frozen_snapshot_and_trusted_constraints_only(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    payload = _payload(
        value={
            "agent_id": "agt_attacker",
            "system_prompt_token_reserve": 999999,
            "constraints_digest": "f" * 64,
        }
    )
    acceptance_id = _accept(ingress, payload)
    queued = router.route(acceptance_id)

    grant = router.dequeue()

    assert grant is not None
    assert grant.record.acceptance_id == acceptance_id
    assert grant.request.tenant_key == queued.authority.tenant_key == "tenant_1"
    assert grant.request.agent_id == queued.authority.agent_id == "agt_alpha"
    assert grant.request.app_id == queued.authority.app_id == "cli_alpha"
    assert grant.request.chat_id == queued.authority.team_id == "oc_team"
    assert grant.request.current_message_id == queued.message_id
    assert grant.request.requester_principal_id == queued.authority.requester_principal_id
    assert grant.request.system_prompt_token_reserve == 512
    assert grant.request.constraints_digest == "a" * 64
    ingress.close()
    writer.close()


def test_accepted_router_projection_retains_requester_principal(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.requester_principal_id == "ou_requester"
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("tenant_key", "tenant_other"),
        ("agent_id", "agt_other"),
        ("bot_principal_id", "bot_other"),
        ("app_id", "cli_other"),
        ("channel_generation", 4),
        ("connection_id", "conn_other"),
        ("team_id", "oc_other"),
        ("requester_principal_id", "ou_other"),
    ),
)
def test_authorized_replay_rejects_every_forged_acceptance_coordinate(
    tmp_path: Path,
    field_name: str,
    forged_value: object,
) -> None:
    module, writer, ingress, workforce, channels, _ = _stack(tmp_path)
    payload = _payload()
    acceptance_id = _accept(ingress, payload)
    ingress_record = ingress.state.by_acceptance_id[acceptance_id]
    authority = module.RouterAuthoritySnapshot(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        bot_principal_id="bot_alpha",
        app_id="cli_alpha",
        channel_generation=3,
        connection_id="conn_3",
        team_id="oc_team",
        requester_principal_id="ou_requester",
        projection_sequence=0,
        projection_hash=GENESIS_HASH,
        employee_version=4,
        tool="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        constraints_digest="a" * 64,
        system_prompt_token_reserve=512,
    ).to_dict()
    authority[field_name] = forged_value
    writer.commit(
        [
            JournalEvent(
                event_type="employee.ingress.router_authorized",
                aggregate_id=ingress_record.aggregate_id,
                payload={
                    "acceptance_id": acceptance_id,
                    "authority": authority,
                },
            )
        ],
        writer.get_aggregate_versions([ingress_record.aggregate_id]),
    )

    with pytest.raises(module.RouterProjectionError):
        module.DurableEmployeeIngressRouter(
            writer=writer,
            ingress_service=ingress,
            registry_provider=lambda: ProjectedAgentRegistry(workforce),
            channel_status_provider=channels,
            requester_acl=RuntimeRequesterChatAcl(
                allowed_requesters=("ou_requester",),
                allowed_chats=("oc_team",),
            ),
            queue_limits=module.RouterQueueLimits(4, 8, 16),
            membership_health=_MembershipHealth(False),
            constraints_digest="a" * 64,
            system_prompt_token_reserve=512,
        )
    ingress.close()
    writer.close()


def test_root_message_without_thread_id_keeps_resolvable_current_coordinates(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    payload = _payload(feishu_thread_id="")
    acceptance_id = _accept(ingress, payload, thread_root_message_id="")

    assert router.route(acceptance_id).state == "queued"
    grant = router.dequeue()

    assert grant is not None
    assert grant.request.thread_root_message_id == grant.request.current_message_id
    assert grant.request.feishu_thread_id == ""
    assert grant.request.to_message_scope().feishu_thread_id == ""
    ingress.close()
    writer.close()


def test_worker_normalizer_encrypts_sender_and_optional_thread_provenance() -> None:
    from src.autonomous.provisioning.channel_worker import _normalize_sdk_ingress

    event = SimpleNamespace(
        header=SimpleNamespace(
            event_id="event-1",
            event_type="im.message.receive_v1",
            create_time="1783900800000",
            tenant_key="tenant_1",
            app_id="cli_alpha",
        ),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(
                    open_id="ou_requester",
                    union_id="on_requester",
                ),
                sender_type="user",
                tenant_key="tenant_1",
            ),
            message=SimpleNamespace(
                message_id="om_current",
                root_id="",
                parent_id="",
                thread_id="",
                chat_id="oc_team",
                chat_type="p2p",
                message_type="text",
                content='{"text":"hello"}',
            ),
        ),
    )

    metadata, payload, _ = _normalize_sdk_ingress(
        event,
        kind="message",
        agent_id="agt_alpha",
        app_id="cli_alpha",
        generation=3,
        connection_id="conn_3",
        tenant_key="tenant_1",
        bot_principal_id="bot_alpha",
    )

    part = payload.normalized_parts[0]
    assert metadata.thread_root_message_id == ""
    assert part["sender_id"] == "ou_requester"
    assert part["sender_union_id"] == "on_requester"
    assert part["sender_id_type"] == "open_id"
    assert part["sender_type"] == "user"
    assert part["sender_tenant_key"] == "tenant_1"
    assert part["feishu_thread_id"] == ""


def test_non_message_event_type_cannot_grant_message_authority(tmp_path: Path) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload(), event_type="evil.event")

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "unsupported_event"
    assert router.dequeue() is None
    ingress.close()
    writer.close()


@pytest.mark.parametrize("provider_failure", ("registry", "channel"))
def test_authority_provider_failure_is_a_durable_deny(
    tmp_path: Path,
    provider_failure: str,
) -> None:
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        provider_failure=provider_failure,
    )
    acceptance_id = _accept(ingress, _payload())

    record = router.route(acceptance_id)

    assert record.state == "terminal"
    assert record.reason_code == "authority_denied"
    ingress.close()
    writer.close()


def test_authority_callbacks_run_outside_the_journal_transaction_guard(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, channels, router = _stack(tmp_path)
    original_registry = router._registry_provider
    original_status = channels.status

    def assert_guard_is_available() -> None:
        acquired = Event()

        def probe() -> None:
            with writer.transaction_guard():
                acquired.set()

        thread = Thread(target=probe)
        thread.start()
        assert acquired.wait(1), "authority callback ran under Journal transaction guard"
        thread.join(1)

    def registry_provider():
        assert_guard_is_available()
        return original_registry()

    def channel_status(agent_id: str):
        assert_guard_is_available()
        return original_status(agent_id)

    router._registry_provider = registry_provider
    channels.status = channel_status  # type: ignore[method-assign]
    acceptance_id = _accept(ingress, _payload())

    assert router.route(acceptance_id).state == "queued"
    assert router.dequeue() is not None
    ingress.close()
    writer.close()


class _BlockingStaging:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0
        self.state = SimpleNamespace(by_acceptance_id={}, by_staging_id={})

    def stage(self, request) -> None:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        staging_id = "stg_done"
        self.state.by_acceptance_id[request.acceptance_id] = staging_id
        self.state.by_staging_id[staging_id] = SimpleNamespace(
            staging_id=staging_id,
            status="completed",
            cleanup_state="none",
        )

    def trusted_paths(self, staging_id: str):
        assert staging_id == "stg_done"
        return (Path("/trusted/attachment"),)

    def completed_for_acceptance(self, acceptance_id: str):
        staging_id = self.state.by_acceptance_id.get(acceptance_id)
        return None if staging_id is None else self.state.by_staging_id[staging_id]


class _RecordingStaging:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[object] = []
        self.cleanup_calls: list[str] = []
        self.recover_calls = 0
        self.state = SimpleNamespace(by_acceptance_id={}, by_staging_id={})

    def stage(self, request) -> None:
        self.calls += 1
        self.requests.append(request)
        staging_id = f"stg_recorded_{self.calls}"
        self.state.by_acceptance_id[request.acceptance_id] = staging_id
        self.state.by_staging_id[staging_id] = SimpleNamespace(
            staging_id=staging_id,
            status="completed",
            cleanup_state="none",
        )

    def completed_for_acceptance(self, acceptance_id: str):
        staging_id = self.state.by_acceptance_id.get(acceptance_id)
        if staging_id is None:
            return None
        record = self.state.by_staging_id[staging_id]
        if record.cleanup_state == "completed":
            return None
        if record.cleanup_state != "none":
            raise AttachmentStateError("attachment staging is not reusable")
        return record

    def cleanup(self, staging_id: str) -> None:
        self.cleanup_calls.append(staging_id)
        self.state.by_staging_id[staging_id].cleanup_state = "completed"

    def recover(self) -> int:
        self.recover_calls += 1
        return 0


class _CrashCleanupStaging(_RecordingStaging):
    def cleanup(self, staging_id: str) -> None:
        self.cleanup_calls.append(staging_id)
        record = self.state.by_staging_id[staging_id]
        record.cleanup_state = "started"
        raise RuntimeError("cleanup interrupted")

    def recover(self) -> int:
        self.recover_calls += 1
        recovered = 0
        for record in self.state.by_staging_id.values():
            if record.cleanup_state == "started":
                record.cleanup_state = "completed"
                recovered += 1
        return recovered


class _BrokenRecoveryStaging:
    def recover(self) -> int:
        raise RuntimeError("recovery unavailable")

    @property
    def state(self):
        raise RuntimeError("staging state unavailable")


class _ApiOnlyCleanupStaging:
    def __init__(self) -> None:
        self.calls = 0
        self.cleanup_calls: list[str] = []
        self.records: dict[str, object] = {}

    def stage(self, request) -> None:
        self.calls += 1
        staging_id = f"stg_api_{self.calls}"
        self.records[request.acceptance_id] = SimpleNamespace(
            staging_id=staging_id,
            status="completed",
            cleanup_state="none",
        )

    def completed_for_acceptance(self, acceptance_id: str):
        record = self.records.get(acceptance_id)
        if record is None or record.cleanup_state == "completed":
            return None
        return record

    def cleanup(self, staging_id: str) -> None:
        self.cleanup_calls.append(staging_id)
        for record in self.records.values():
            if record.staging_id == staging_id:
                record.cleanup_state = "completed"


class _MalformedCompletionStaging(_RecordingStaging):
    def __init__(self, behavior: str) -> None:
        super().__init__()
        self.behavior = behavior

    def completed_for_acceptance(self, acceptance_id: str):
        if acceptance_id not in self.state.by_acceptance_id:
            return None
        if self.behavior == "raise":
            raise RuntimeError("completion lookup failed")
        if self.behavior == "state_error":
            raise AttachmentStateError("attachment staging is not reusable")
        if self.behavior == "invalid_cleanup":
            return SimpleNamespace(
                staging_id="stg_invalid",
                status="completed",
                cleanup_state="unexpected",
            )
        return SimpleNamespace(status="completed", cleanup_state="none")


class _StateOnlyStaging(_RecordingStaging):
    completed_for_acceptance = None


class _RealCredentialResolver:
    def resolve(self, credential_ref: str, agent_id: str, app_id: str) -> str:
        assert (credential_ref, agent_id, app_id) == (
            "cred_alpha",
            "agt_alpha",
            "cli_alpha",
        )
        return "employee-secret"


class _RealDownloader:
    def download(self, _descriptor) -> DownloadedAttachment:
        return DownloadedAttachment(content=b"", file_name="note.txt")


def _attachment_payload() -> EmployeeIngressPayload:
    return _payload(
        attachment_descriptors=(
            {
                "resource_type": "file",
                "resource_id": "file_1",
                "mime_type": "text/plain",
                "size_bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            },
        )
    )


@pytest.mark.parametrize("second_provider_kind", ("forged", "wrong_credential"))
def test_attachment_stage_requires_second_full_authority_resolution(
    tmp_path: Path,
    second_provider_kind: str,
) -> None:
    staging = _RecordingStaging()
    _, writer, ingress, workforce, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    valid_registry = ProjectedAgentRegistry(workforce)
    valid_binding = valid_registry.context_binding(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        bot_principal_id="bot_alpha",
        app_id="cli_alpha",
        chat_id="oc_team",
    )
    assert valid_binding is not None
    if second_provider_kind == "forged":
        class _ForgedRegistry:
            def context_binding(self, **_coordinates):
                return valid_binding

        second_provider = _ForgedRegistry()
    else:
        wrong_workforce = _workforce()
        wrong_workforce.bot_principals["bot_alpha"] = replace(
            wrong_workforce.bot_principals["bot_alpha"],
            credential_ref="cred_wrong",
        )
        second_provider = ProjectedAgentRegistry(wrong_workforce)
    provider_calls = 0

    def registry_provider():
        nonlocal provider_calls
        provider_calls += 1
        return second_provider if provider_calls == 2 else valid_registry

    router._registry_provider = registry_provider
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)

    assert provider_calls == 2
    assert terminal.state == "terminal"
    assert staging.calls == 0
    assert staging.requests == []
    ingress.close()
    writer.close()


def test_attachment_stage_uses_only_validated_employee_credential_ephemerally(
    tmp_path: Path,
) -> None:
    staging = _RecordingStaging()
    _, writer, ingress, workforce, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    registry = ProjectedAgentRegistry(workforce)
    provider_calls = 0

    def registry_provider():
        nonlocal provider_calls
        provider_calls += 1
        return registry

    router._registry_provider = registry_provider
    acceptance_id = _accept(ingress, _attachment_payload())

    queued = router.route(acceptance_id)

    assert queued.state == "queued"
    assert provider_calls == 3
    assert staging.calls == 1
    assert staging.requests[0].credential_ref == "cred_alpha"
    assert queued.authority is not None
    assert "credential" not in repr(queued.authority.to_dict()).lower()
    journal_text = repr(tuple(writer.replay())).lower()
    assert "credential_ref" not in journal_text
    assert "cred_alpha" not in journal_text
    ingress.close()
    writer.close()


def test_attachment_final_resolution_rejects_post_stage_credential_change(
    tmp_path: Path,
) -> None:
    staging = _RecordingStaging()
    _, writer, ingress, workforce, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    valid_registry = ProjectedAgentRegistry(workforce)
    wrong_workforce = _workforce()
    wrong_workforce.bot_principals["bot_alpha"] = replace(
        wrong_workforce.bot_principals["bot_alpha"],
        credential_ref="cred_rotated",
    )
    wrong_registry = ProjectedAgentRegistry(wrong_workforce)
    provider_calls = 0

    def registry_provider():
        nonlocal provider_calls
        provider_calls += 1
        return wrong_registry if provider_calls == 3 else valid_registry

    router._registry_provider = registry_provider
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)

    assert provider_calls == 3
    assert staging.calls == 1
    assert terminal.state == "terminal"
    assert terminal.reason_code == "authority_stale"
    assert staging.cleanup_calls == ["stg_recorded_1"]
    assert not any(
        event.event_type == "employee.ingress.router_queued"
        and event.payload["acceptance_id"] == acceptance_id
        for frame in writer.replay()
        for event in frame.events
    )
    ingress.close()
    writer.close()


def test_queue_full_terminal_cleans_completed_attachment_stage(tmp_path: Path) -> None:
    staging = _RecordingStaging()
    module, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    router._limits = module.RouterQueueLimits(1, 2, 2)
    first = _accept(ingress, _payload(text="occupy employee queue"))
    assert router.route(first).state == "queued"
    attachment = _accept(ingress, _attachment_payload())

    terminal = router.route(attachment)

    assert terminal.state == "terminal"
    assert terminal.reason_code == "queue_full"
    assert staging.cleanup_calls == ["stg_recorded_1"]
    assert staging.state.by_staging_id["stg_recorded_1"].cleanup_state == "completed"
    ingress.close()
    writer.close()


def test_attachment_stage_survives_queued_and_current_dispatch_grant(
    tmp_path: Path,
) -> None:
    staging = _RecordingStaging()
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    acceptance_id = _accept(ingress, _attachment_payload())

    assert router.route(acceptance_id).state == "queued"
    assert staging.cleanup_calls == []
    from tests.autonomous.integration.test_employee_router_queues import (
        _commit_dispatch,
    )

    grant = _commit_dispatch(router, writer, acceptance_id)
    assert grant is not None and grant.record.state == "dispatching"
    assert staging.cleanup_calls == []

    terminal = router.finish(acceptance_id, reason_code="completed")

    assert terminal.state == "terminal"
    assert staging.cleanup_calls == ["stg_recorded_1"]
    ingress.close()
    writer.close()


def test_dequeue_authority_terminal_cleans_completed_attachment_stage(
    tmp_path: Path,
) -> None:
    staging = _RecordingStaging()
    _, writer, ingress, _, channels, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    acceptance_id = _accept(ingress, _attachment_payload())
    assert router.route(acceptance_id).state == "queued"
    channels.statuses["agt_alpha"] = replace(
        channels.statuses["agt_alpha"],
        generation=4,
    )

    assert router.dequeue() is None

    assert router.state.by_acceptance_id[acceptance_id].reason_code == "authority_stale"
    assert staging.cleanup_calls == ["stg_recorded_1"]
    ingress.close()
    writer.close()


def test_terminal_attachment_cleanup_crash_is_reported_and_recovery_is_idempotent(
    tmp_path: Path,
) -> None:
    staging = _CrashCleanupStaging()
    module, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    router._limits = module.RouterQueueLimits(1, 2, 2)
    assert router.route(_accept(ingress, _payload(text="occupy"))).state == "queued"
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)

    assert terminal.state == "terminal"
    assert terminal.reason_code == "queue_full"
    assert router.last_attachment_cleanup_report.failed_acceptance_ids == (
        acceptance_id,
    )
    assert staging.state.by_staging_id["stg_recorded_1"].cleanup_state == "started"

    first = router.recover_terminal_attachments()
    second = router.recover_terminal_attachments()

    assert first.recovered_staging == 1
    assert first.failed_acceptance_ids == ()
    assert second.recovered_staging == 0
    assert second.cleaned_acceptance_ids == ()
    assert second.failed_acceptance_ids == ()
    assert staging.recover_calls == 2
    ingress.close()
    writer.close()


def test_terminal_attachment_cleanup_runs_outside_router_and_journal_guards(
    tmp_path: Path,
) -> None:
    staging = _RecordingStaging()
    module, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    router._limits = module.RouterQueueLimits(1, 2, 2)
    assert router.route(_accept(ingress, _payload(text="occupy"))).state == "queued"
    original_completed = staging.completed_for_acceptance
    original_cleanup = staging.cleanup
    original_recover = staging.recover

    def assert_guards_are_available() -> None:
        acquired = Event()

        def probe() -> None:
            with writer.transaction_guard(), router._mutex:
                acquired.set()

        thread = Thread(target=probe)
        thread.start()
        assert acquired.wait(1), "Task 4 port ran under Router/Journal guard"
        thread.join(1)

    def completed_for_acceptance(acceptance_id: str):
        assert_guards_are_available()
        return original_completed(acceptance_id)

    def cleanup(staging_id: str) -> None:
        assert_guards_are_available()
        original_cleanup(staging_id)

    def recover() -> int:
        assert_guards_are_available()
        return original_recover()

    staging.completed_for_acceptance = completed_for_acceptance
    staging.cleanup = cleanup
    staging.recover = recover

    terminal = router.route(_accept(ingress, _attachment_payload()))
    recovery = router.recover_terminal_attachments()

    assert terminal.reason_code == "queue_full"
    assert staging.cleanup_calls == ["stg_recorded_1"]
    assert recovery.failed_acceptance_ids == ()
    ingress.close()
    writer.close()


def test_attachment_recovery_reports_provider_and_sweep_errors_without_raising(
    tmp_path: Path,
) -> None:
    staging = _BrokenRecoveryStaging()
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    acceptance_id = _accept(ingress, _payload(), event_type="evil.event")
    terminal = router.route(acceptance_id)
    assert terminal.state == "terminal"

    report = router.recover_terminal_attachments()

    assert report.recovery_error_code == "attachment_recover_failed"
    assert report.sweep_error_code == "attachment_completion_port_invalid"
    assert router.state.by_acceptance_id[acceptance_id] == terminal
    ingress.close()
    writer.close()


def test_terminal_attachment_sweeper_uses_verified_task4_completion_port(
    tmp_path: Path,
) -> None:
    staging = _ApiOnlyCleanupStaging()
    module, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    router._limits = module.RouterQueueLimits(1, 2, 2)
    assert router.route(_accept(ingress, _payload(text="occupy"))).state == "queued"
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)

    assert terminal.reason_code == "queue_full"
    assert staging.cleanup_calls == ["stg_api_1"]
    assert router.last_attachment_cleanup_report.cleaned_acceptance_ids == (
        acceptance_id,
    )
    ingress.close()
    writer.close()


def test_attachment_staging_requires_verified_task4_completion_port(
    tmp_path: Path,
) -> None:
    staging = _StateOnlyStaging()
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)

    assert terminal.state == "terminal"
    assert terminal.reason_code == "attachment_staging_failed"
    assert staging.calls == 0
    ingress.close()
    writer.close()


def test_real_task4_stage_terminal_cleanup_and_recovery_are_idempotent(
    tmp_path: Path,
) -> None:
    module, writer, ingress, _, _, router = _stack(tmp_path)
    staging = AttachmentStagingService(
        writer=writer,
        root=tmp_path / "real-staging",
        credential_resolver=_RealCredentialResolver(),
        downloader_builder=lambda **_kwargs: _RealDownloader(),
    )
    router._attachment_staging = staging
    router._limits = module.RouterQueueLimits(1, 2, 2)
    assert router.route(_accept(ingress, _payload(text="occupy"))).state == "queued"
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)
    first = router.recover_terminal_attachments()
    second = router.recover_terminal_attachments()

    assert terminal.reason_code == "queue_full"
    assert staging.completed_for_acceptance(acceptance_id) is None
    assert first.recovered_staging == 0
    assert first.failed_acceptance_ids == ()
    assert first.cleaned_acceptance_ids == ()
    assert second.recovered_staging == 0
    assert second.failed_acceptance_ids == ()
    assert second.cleaned_acceptance_ids == ()
    assert sum(
        event.event_type == "employee.ingress.attachment_cleanup_completed"
        for frame in writer.replay()
        for event in frame.events
    ) == 1
    staging.close()
    ingress.close()
    writer.close()


@pytest.mark.parametrize(
    "behavior",
    ("malformed", "raise", "state_error", "invalid_cleanup"),
)
def test_terminal_attachment_sweeper_reports_invalid_task4_completion_result(
    tmp_path: Path,
    behavior: str,
) -> None:
    staging = _MalformedCompletionStaging(behavior)
    module, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    router._limits = module.RouterQueueLimits(1, 2, 2)
    assert router.route(_accept(ingress, _payload(text="occupy"))).state == "queued"
    acceptance_id = _accept(ingress, _attachment_payload())

    terminal = router.route(acceptance_id)

    assert terminal.reason_code == "queue_full"
    assert staging.cleanup_calls == []
    assert router.last_attachment_cleanup_report.failed_acceptance_ids == (
        acceptance_id,
    )
    ingress.close()
    writer.close()


def test_attachment_stage_does_not_hold_global_journal_admission_lock(
    tmp_path: Path,
) -> None:
    staging = _BlockingStaging()
    _, writer, ingress, _, _, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    first = _accept(ingress, _attachment_payload())
    route_done = Event()

    def route_attachment() -> None:
        router.route(first)
        route_done.set()

    route_thread = Thread(target=route_attachment)
    route_thread.start()
    assert staging.entered.wait(2)
    admitted = Event()

    def admit_other() -> None:
        _accept(ingress, _payload(text="independent admission"))
        admitted.set()

    admission_thread = Thread(target=admit_other)
    admission_thread.start()
    try:
        assert admitted.wait(1), "attachment download held global Journal admission"
    finally:
        staging.release.set()
        route_thread.join(5)
        admission_thread.join(5)
    assert route_done.is_set()
    ingress.close()
    writer.close()


def test_router_rechecks_inbox_tombstone_before_queue_commit(tmp_path: Path) -> None:
    authorized = Event()
    release = Event()

    def fault(point: str, _record) -> None:
        if point == "after_authorized":
            authorized.set()
            assert release.wait(5)

    _, writer, ingress, _, _, router = _stack(tmp_path, fault_hook=fault)
    acceptance_id = _accept(ingress, _payload())
    result: list[object] = []

    thread = Thread(target=lambda: result.append(router.route(acceptance_id)))
    thread.start()
    assert authorized.wait(2)
    ingress.record_disposition(
        acceptance_id,
        state="terminal",
        reason_code="superseded",
    )
    assert ingress.gc_terminal_payloads() == 1
    release.set()
    thread.join(5)

    assert result and result[0].state == "terminal"
    assert result[0].reason_code == "inbox_not_dispatchable"
    assert not any(
        event.event_type == "employee.ingress.router_queued"
        and event.payload["acceptance_id"] == acceptance_id
        for frame in writer.replay()
        for event in frame.events
    )
    ingress.close()
    writer.close()


class _CrashAfterDurableStage(_BlockingStaging):
    def stage(self, request) -> None:
        self.calls += 1
        staging_id = "stg_done"
        self.state.by_acceptance_id[request.acceptance_id] = staging_id
        self.state.by_staging_id[staging_id] = SimpleNamespace(
            status="completed",
            cleanup_state="none",
        )
        raise KeyboardInterrupt("simulated process crash")


class _ConcurrentStaging(_BlockingStaging):
    def __init__(self) -> None:
        super().__init__()
        self.lock = Lock()

    def completed_for_acceptance(self, acceptance_id: str):
        with self.lock:
            return super().completed_for_acceptance(acceptance_id)

    def stage(self, request) -> None:
        with self.lock:
            if request.acceptance_id in self.state.by_acceptance_id:
                raise AttachmentStateError("attachment staging already exists")
            self.calls += 1
            time.sleep(0.1)
            staging_id = "stg_done"
            self.state.by_acceptance_id[request.acceptance_id] = staging_id
            self.state.by_staging_id[staging_id] = SimpleNamespace(
                status="completed",
                cleanup_state="none",
            )


class _WinningAndLosingStaging(_BlockingStaging):
    def __init__(self) -> None:
        super().__init__()
        self.lock = Lock()

    def completed_for_acceptance(self, acceptance_id: str):
        with self.lock:
            return super().completed_for_acceptance(acceptance_id)

    def stage(self, request) -> None:
        with self.lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.entered.set()
            assert self.release.wait(5)
            raise RuntimeError("stale losing callback")
        with self.lock:
            staging_id = "stg_done"
            self.state.by_acceptance_id[request.acceptance_id] = staging_id
            self.state.by_staging_id[staging_id] = SimpleNamespace(
                status="completed",
                cleanup_state="none",
            )


def test_attachment_stage_crash_retry_reuses_durable_completed_stage(
    tmp_path: Path,
) -> None:
    staging = _CrashAfterDurableStage()
    module, writer, ingress, workforce, channels, router = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    acceptance_id = _accept(ingress, _attachment_payload())

    with pytest.raises(KeyboardInterrupt, match="simulated"):
        router.route(acceptance_id)
    assert router.state.by_acceptance_id[acceptance_id].state == "authorized"

    restarted = module.DurableEmployeeIngressRouter(
        writer=writer,
        ingress_service=ingress,
        registry_provider=lambda: ProjectedAgentRegistry(workforce),
        channel_status_provider=channels,
        requester_acl=RuntimeRequesterChatAcl(
            allowed_requesters=("ou_requester",),
            allowed_chats=("oc_team",),
        ),
        queue_limits=module.RouterQueueLimits(
            per_employee=4,
            per_team=8,
            global_limit=16,
        ),
        membership_health=_MembershipHealth(False),
        attachment_staging=staging,
        constraints_digest="a" * 64,
        system_prompt_token_reserve=512,
    )
    assert restarted.route(acceptance_id).state == "queued"
    assert staging.calls == 1
    ingress.close()
    writer.close()


def test_two_router_instances_reuse_one_concurrent_attachment_stage(
    tmp_path: Path,
) -> None:
    staging = _ConcurrentStaging()
    module, writer, ingress, workforce, channels, first = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    second = module.DurableEmployeeIngressRouter(
        writer=writer,
        ingress_service=ingress,
        registry_provider=lambda: ProjectedAgentRegistry(workforce),
        channel_status_provider=channels,
        requester_acl=RuntimeRequesterChatAcl(
            allowed_requesters=("ou_requester",),
            allowed_chats=("oc_team",),
        ),
        queue_limits=module.RouterQueueLimits(4, 8, 16),
        membership_health=_MembershipHealth(False),
        attachment_staging=staging,
        constraints_digest="a" * 64,
        system_prompt_token_reserve=512,
    )
    acceptance_id = _accept(ingress, _attachment_payload())
    barrier = Barrier(2)

    def route(router):
        barrier.wait()
        return router.route(acceptance_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(route, (first, second)))

    assert all(record.state == "queued" for record in records)
    assert staging.calls == 1
    ingress.close()
    writer.close()


def test_losing_attachment_callback_cannot_revoke_concurrent_dispatch(
    tmp_path: Path,
) -> None:
    staging = _WinningAndLosingStaging()
    module, writer, ingress, workforce, channels, first = _stack(
        tmp_path,
        attachment_staging=staging,
    )
    second = module.DurableEmployeeIngressRouter(
        writer=writer,
        ingress_service=ingress,
        registry_provider=lambda: ProjectedAgentRegistry(workforce),
        channel_status_provider=channels,
        requester_acl=RuntimeRequesterChatAcl(
            allowed_requesters=("ou_requester",),
            allowed_chats=("oc_team",),
        ),
        queue_limits=module.RouterQueueLimits(4, 8, 16),
        membership_health=_MembershipHealth(False),
        attachment_staging=staging,
        constraints_digest="a" * 64,
        system_prompt_token_reserve=512,
    )
    acceptance_id = _accept(ingress, _attachment_payload())
    results: list[object] = []
    losing = Thread(target=lambda: results.append(first.route(acceptance_id)))
    losing.start()
    assert staging.entered.wait(2)
    assert second.route(acceptance_id).state == "queued"
    from tests.autonomous.integration.test_employee_router_queues import (
        _commit_dispatch,
    )

    grant = _commit_dispatch(second, writer, acceptance_id)
    assert grant is not None and grant.record.state == "dispatching"
    staging.release.set()
    losing.join(5)

    assert results and results[0].state == "dispatching"
    assert second.state.by_acceptance_id[acceptance_id].state == "dispatching"
    ingress.close()
    writer.close()


def test_dequeue_rechecks_inbox_tombstone_in_dispatch_commit_guard(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload())
    assert router.route(acceptance_id).state == "queued"
    ingress.record_disposition(
        acceptance_id,
        state="terminal",
        reason_code="superseded",
    )
    assert ingress.gc_terminal_payloads() == 1

    assert router.dequeue() is None
    terminal = router.state.by_acceptance_id[acceptance_id]
    assert terminal.state == "terminal"
    assert terminal.reason_code == "inbox_not_dispatchable"
    ingress.close()
    writer.close()


def test_dequeue_rejects_registry_projection_that_missed_workforce_change(
    tmp_path: Path,
) -> None:
    _, writer, ingress, workforce, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload())
    assert router.route(acceptance_id).state == "queued"
    head = writer.anchor.read()
    workforce.cursor_sequence = head.sequence
    workforce.cursor_hash = head.frame_hash
    writer.commit(
        [
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id="agt_alpha",
                payload={"state": "draft"},
            )
        ],
        writer.get_aggregate_versions(["agt_alpha"]),
    )

    assert router.dequeue() is None
    terminal = router.state.by_acceptance_id[acceptance_id]
    assert terminal.state == "terminal"
    assert terminal.reason_code == "authority_stale"
    ingress.close()
    writer.close()


def test_route_fences_initial_authority_sample_before_authorized_commit(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    original_resolve = router._resolve_authority
    injected = False

    def resolve_then_revoke(metadata, payload):
        nonlocal injected
        result = original_resolve(metadata, payload)
        if result[0] is not None and not injected:
            injected = True
            _commit_workforce_change(writer, "route_initial")
        return result

    router._resolve_authority = resolve_then_revoke
    acceptance_id = _accept(ingress, _payload())

    terminal = router.route(acceptance_id)

    assert terminal.state == "terminal"
    assert terminal.reason_code == "authority_stale"
    assert not any(
        event.event_type == "employee.ingress.router_authorized"
        and event.payload["acceptance_id"] == acceptance_id
        for frame in writer.replay()
        for event in frame.events
    )
    ingress.close()
    writer.close()


def test_dequeue_fences_sampled_authority_against_late_workforce_change(
    tmp_path: Path,
) -> None:
    _, writer, ingress, _, _, router = _stack(tmp_path)
    acceptance_id = _accept(ingress, _payload())
    assert router.route(acceptance_id).state == "queued"
    original_resolve = router._resolve_authority
    injected = False

    def resolve_then_revoke(metadata, payload):
        nonlocal injected
        result = original_resolve(metadata, payload)
        if result[0] is not None and not injected:
            injected = True
            _commit_workforce_change(writer, "dequeue")
        return result

    router._resolve_authority = resolve_then_revoke

    assert router.dequeue() is None

    terminal = router.state.by_acceptance_id[acceptance_id]
    assert terminal.state == "terminal"
    assert terminal.reason_code == "authority_stale"
    assert not any(
        event.event_type == "employee.ingress.router_dispatching"
        and event.payload["acceptance_id"] == acceptance_id
        for frame in writer.replay()
        for event in frame.events
    )
    ingress.close()
    writer.close()
