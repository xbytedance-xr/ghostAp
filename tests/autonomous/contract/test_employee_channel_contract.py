from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.autonomous.ingress.models import (
    EmployeeIngressAck,
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
    IngressAcceptance,
)
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.provisioning.channel_protocol import (
    MAX_FRAME_BYTES,
    ChannelFrame,
    FrameType,
    ProtocolError,
    decode_frame,
    encode_frame,
)
from src.autonomous.provisioning.channel_worker import (
    _normalize_sdk_ingress,
    create_employee_channel,
    extract_raw_message_metadata,
    register_channel_handlers,
    run_low_level_employee_channel,
)
from src.autonomous.provisioning.channel_worker import (
    main as channel_worker_main,
)
from src.autonomous.supervisor.employee_channels import EmployeeChannelSupervisor


def test_parent_durable_ingress_call_graph_excludes_router_and_acp_execution() -> None:
    source = "\n".join(
        textwrap.dedent(inspect.getsource(method))
        for method in (
            EmployeeChannelSupervisor._accept_ingress,
            EmployeeIngressService.accept,
        )
    )
    tree = ast.parse(source)
    invoked = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }

    assert invoked.isdisjoint(
        {"route", "execute", "start_session", "ensure_session", "_run_acp_session"}
    )
    assert "src.acp" not in source
    assert "provisioning.router" not in source


def test_protocol_round_trips_a_strict_versioned_ndjson_frame() -> None:
    frame = ChannelFrame(
        frame_type=FrameType.EVENT,
        agent_id="agt_1",
        generation=7,
        sequence=3,
        payload={"event": "message", "data": {"text": "hello"}},
    )

    encoded = encode_frame(frame)

    assert encoded.endswith(b"\n")
    assert decode_frame(encoded) == frame


@pytest.mark.parametrize(
    "secret_key",
    ["AccessToken", "APIKey", "ClientSecret", "private-key", "PASSWORD"],
)
def test_ordinary_ipc_recursively_rejects_collapsed_secret_aliases(
    secret_key: str,
) -> None:
    frame = ChannelFrame(
        FrameType.EVENT,
        "agt_1",
        1,
        1,
        {"event": "fixture", "data": {"nested": {secret_key: "sentinel"}}},
    )

    with pytest.raises(ProtocolError, match="credential material"):
        encode_frame(frame)


@pytest.mark.parametrize(
    "ordinary_key",
    ["authorization_type", "access_token_expires_at", "password_policy"],
)
def test_ordinary_ipc_allows_non_secret_metadata_with_secret_words(
    ordinary_key: str,
) -> None:
    frame = ChannelFrame(
        FrameType.EVENT,
        "agt_1",
        1,
        1,
        {"event": "fixture", "data": {"nested": {ordinary_key: "safe"}}},
    )

    assert decode_frame(encode_frame(frame)) == frame


def _transport_contract() -> tuple[
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
    EmployeeIngressAck,
]:
    payload = EmployeeIngressPayload(
        schema_version=1,
        envelope_id="ing_channel_contract",
        normalized_parts=({"kind": "text", "text": "hello"},),
        attachment_descriptors=(),
    )
    digest = hashlib.sha256(payload.canonical_bytes).hexdigest()
    metadata = EmployeeIngressMetadata(
        schema_version=1,
        envelope_id=payload.envelope_id,
        tenant_key="tenant_contract",
        agent_id="agt_channel_contract",
        bot_principal_id="bot_channel_contract",
        app_id="cli_channel_contract",
        channel_generation=7,
        connection_id="conn_channel_contract",
        event_id="evt_channel_contract",
        message_id="om_channel_contract",
        event_type="im.message.receive_v1",
        action_identity="",
        chat_id="oc_channel_contract",
        thread_root_message_id="",
        sender_principal_id="ou_sender",
        received_at="2026-07-13T00:00:00Z",
        semantic_digest=digest,
        payload_sha256=payload.payload_sha256,
        payload_size_bytes=payload.canonical_size_bytes,
        attachment_count=0,
        attachment_total_bytes=0,
    )
    acceptance = IngressAcceptance(
        schema_version=1,
        acceptance_id="acc_channel_contract",
        envelope_id=payload.envelope_id,
        dedup_key=metadata.dedup_key,
        semantic_digest=metadata.semantic_digest,
        journal_sequence=9,
        journal_frame_hash="a" * 64,
        accepted_at="2026-07-13T00:00:00Z",
    )
    ack = EmployeeIngressAck(
        schema_version=1,
        request_id="req_channel_contract",
        acceptance=acceptance,
        agent_id=metadata.agent_id,
        app_id=metadata.app_id,
        channel_generation=metadata.channel_generation,
        connection_id=metadata.connection_id,
        semantic_digest=metadata.semantic_digest,
        duplicate=False,
        acknowledged_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    return metadata, payload, ack


def test_protocol_round_trips_strict_ingress_and_canonical_ack_frames() -> None:
    metadata, payload, ack = _transport_contract()
    ingress = ChannelFrame(
        frame_type=FrameType.INGRESS,
        agent_id=metadata.agent_id,
        generation=metadata.channel_generation,
        sequence=4,
        payload={
            "request_id": ack.request_id,
            "app_id": metadata.app_id,
            "connection_id": metadata.connection_id,
            "metadata": metadata.to_dict(),
            "payload": payload.to_dict(),
            "action_correlation": None,
        },
    )
    ingress_ack = ChannelFrame(
        frame_type=FrameType.INGRESS_ACK,
        agent_id=metadata.agent_id,
        generation=metadata.channel_generation,
        sequence=5,
        payload={
            "request_id": ack.request_id,
            "app_id": ack.app_id,
            "connection_id": ack.connection_id,
            "ack": ack.to_dict(),
        },
    )

    assert decode_frame(encode_frame(ingress)) == ingress
    assert decode_frame(encode_frame(ingress_ack)) == ingress_ack


@pytest.mark.parametrize(
    ("frame_kind", "mutation"),
    [
        ("ingress", lambda value: value["payload"].update({"unknown": True})),
        ("ingress", lambda value: value["payload"]["metadata"].update({"app_secret": "x"})),
        ("ingress", lambda value: value.update({"generation": 8})),
        ("ingress", lambda value: value["payload"].update({"connection_id": "conn_other"})),
        ("ack", lambda value: value["payload"]["ack"].update({"request_id": "req_other"})),
        ("ack", lambda value: value.update({"agent_id": "agt_other"})),
        ("ack", lambda value: value.update({"generation": 8})),
        ("ack", lambda value: value["payload"]["ack"].update({"connection_id": "conn_other"})),
    ],
)
def test_protocol_rejects_malformed_stale_or_cross_owner_ingress_frames(
    frame_kind: str,
    mutation,
) -> None:
    metadata, payload, ack = _transport_contract()
    value = {
        "v": 1,
        "type": "INGRESS" if frame_kind == "ingress" else "INGRESS_ACK",
        "agent_id": metadata.agent_id,
        "generation": metadata.channel_generation,
        "sequence": 1,
        "payload": (
            {
                "request_id": ack.request_id,
                "app_id": metadata.app_id,
                "connection_id": metadata.connection_id,
                "metadata": metadata.to_dict(),
                "payload": payload.to_dict(),
                "action_correlation": None,
            }
            if frame_kind == "ingress"
            else {
                "request_id": ack.request_id,
                "app_id": ack.app_id,
                "connection_id": ack.connection_id,
                "ack": ack.to_dict(),
            }
        ),
    }
    mutation(value)

    with pytest.raises(ProtocolError):
        decode_frame((json.dumps(value, separators=(",", ":")) + "\n").encode())


@pytest.mark.parametrize(
    "raw",
    [
        {"v": 2, "type": "READY", "agent_id": "agt_1", "generation": 1, "sequence": 1, "payload": {}},
        {"v": 1, "type": "UNKNOWN", "agent_id": "agt_1", "generation": 1, "sequence": 1, "payload": {}},
        {"v": 1, "type": "READY", "agent_id": "agt_1", "generation": 1, "sequence": 1, "payload": {}, "extra": True},
    ],
)
def test_protocol_rejects_wrong_version_type_and_unknown_fields(raw: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        decode_frame((json.dumps(raw) + "\n").encode())


def test_protocol_rejects_oversized_and_multiline_frames() -> None:
    with pytest.raises(ProtocolError):
        decode_frame(b"{}\n{}\n")
    with pytest.raises(ProtocolError):
        decode_frame(b"x" * (MAX_FRAME_BYTES + 1))


class _FakeChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler


@pytest.mark.filterwarnings(
    "ignore:datetime.datetime.utcfromtimestamp.*:DeprecationWarning:lark_channel.*"
)
def test_worker_registers_exact_sdk_events_with_sync_reconnect_shims() -> None:
    """The pinned SDK imports cleanly when invoked in its production loop."""
    channel = _FakeChannel()

    async def register() -> None:
        register_channel_handlers(channel, lambda *_: None)

    asyncio.run(register())

    assert set(channel.handlers) == {
        "message",
        "cardAction",
        "reconnecting",
        "reconnected",
        "error",
        "botAdded",
        "botLeave",
        "raw",
    }
    assert inspect.iscoroutinefunction(channel.handlers["message"])
    assert inspect.iscoroutinefunction(channel.handlers["cardAction"])
    assert not inspect.iscoroutinefunction(channel.handlers["reconnecting"])
    assert not inspect.iscoroutinefunction(channel.handlers["reconnected"])


def test_worker_extracts_only_authoritative_non_secret_raw_message_metadata() -> None:
    raw = {
        "header": {
            "event_id": "evt_1",
            "tenant_key": "tenant_a",
            "token": "must-not-cross-ipc",
            "app_id": "cli_employee",
        },
        "event": {
            "message": {"message_id": "om_1", "content": "secret user text"},
            "sender": {"sender_id": {"open_id": "ou_admin"}},
        },
    }

    assert extract_raw_message_metadata(raw) == {
        "event_id": "evt_1",
        "tenant_key": "tenant_a",
        "message_id": "om_1",
    }


def test_worker_channel_forces_strict_direct_wss_and_error_only_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lark_channel

    captured: dict[str, object] = {}

    def fake_channel(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(lark_channel, "FeishuChannel", fake_channel)

    create_employee_channel("cli_employee", "secret-only-in-memory")

    assert captured["log_level"] is lark_channel.LogLevel.ERROR
    transport = captured["transport"]
    assert isinstance(transport, lark_channel.TransportConfig)
    assert transport.proxy_url is None
    assert transport.trust_env_proxy is False
    assert transport.handshake_timeout_seconds == 10.0
    security = captured["security"]
    assert isinstance(security, lark_channel.SecurityConfig)
    assert security.mode == "strict"
    assert security.allow_insecure_ws is False
    assert security.allow_local_insecure_ws is False
    assert security.max_ws_fragment_parts == 8
    assert security.max_ws_fragment_bytes == 256 * 1024
    assert security.max_concurrent_ws_handlers == 1
    assert security.resource_overflow_policy == "drop"


def test_production_launch_contract_is_fixed_fresh_interpreter() -> None:
    supervisor = EmployeeChannelSupervisor(secret_resolver=lambda *_: "unused")

    contract = supervisor.launch_contract(bootstrap_fd=41, control_fd=42, event_fd=43)

    expected_worker = Path(
        inspect.getfile(sys.modules["src.autonomous.provisioning.channel_worker"])
    ).resolve()
    assert contract.argv[0] == "/usr/bin/bwrap"
    assert "--unshare-user" in contract.argv
    assert "--unshare-pid" in contract.argv
    assert "--ro-bind" in contract.argv
    assert str(expected_worker.parents[3] / ".env") not in contract.argv
    assert contract.argv[-7:] == (
        "--",
        sys.executable,
        "-I",
        str(expected_worker),
        "41",
        "42",
        "43",
    )
    assert contract.close_fds is True
    assert contract.pass_fds == (41, 42, 43)
    assert "credential" not in " ".join(contract.argv).lower()
    assert contract.env == {"PYTHONUTF8": "1"}


def test_production_worker_main_reaches_only_the_low_level_durable_bridge() -> None:
    source = inspect.getsource(channel_worker_main)

    assert "run_low_level_employee_channel" in source
    assert "asyncio.run" not in source
    assert "create_employee_channel" not in source


def test_low_level_entry_hardens_before_credentials_or_sdk_import() -> None:
    source = inspect.getsource(run_low_level_employee_channel)

    assert source.index("apply_process_hardening()") < source.index(
        "decode_bootstrap"
    )
    assert source.index("apply_process_hardening()") < source.index(
        "from lark_channel"
    )


def test_card_action_never_self_attests_user_value_as_trusted_correlation() -> None:
    from lark_channel.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    event = P2CardActionTrigger(
        {
            "schema": "2.0",
            "header": {
                "event_id": "external-event-id",
                "event_type": "card.action.trigger",
                "create_time": "1783900800000",
                "app_id": "cli_contract",
                "tenant_key": "tenant-contract",
            },
            "event": {
                "operator": {"open_id": "ou_sender"},
                "action": {
                    "tag": "button",
                    "value": {"correlation_id": "user-controlled"},
                },
                "context": {
                    "open_message_id": "om_external",
                    "open_chat_id": "oc_external",
                },
            },
        }
    )

    metadata, _payload, correlation = _normalize_sdk_ingress(
        event,
        kind="card",
        agent_id="agt_contract",
        app_id="cli_contract",
        generation=2,
        connection_id="conn_contract",
        tenant_key="tenant-contract",
        bot_principal_id="bot_contract",
    )

    assert metadata.action_identity == ""
    assert correlation is None

    event.header.event_id = ""
    with pytest.raises(ValueError, match="trusted event identity"):
        _normalize_sdk_ingress(
            event,
            kind="card",
            agent_id="agt_contract",
            app_id="cli_contract",
            generation=2,
            connection_id="conn_contract",
            tenant_key="tenant-contract",
            bot_principal_id="bot_contract",
        )
