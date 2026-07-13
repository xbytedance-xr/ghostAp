from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

from src.autonomous.provisioning.channel_protocol import (
    MAX_FRAME_BYTES,
    ChannelFrame,
    FrameType,
    ProtocolError,
    decode_frame,
    encode_frame,
)
from src.autonomous.provisioning.channel_worker import (
    extract_raw_message_metadata,
    register_channel_handlers,
)
from src.autonomous.supervisor.employee_channels import EmployeeChannelSupervisor


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
