"""Strict, versioned IPC protocol for employee Channel workers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
_FRAME_KEYS = {"v", "type", "agent_id", "generation", "sequence", "payload"}
_BOOTSTRAP_KEYS = {"v", "type", "agent_id", "app_id", "generation", "app_secret"}
_FORBIDDEN_IPC_KEYS = {
    "app_secret",
    "credential_ref",
    "master_key",
    "vault_key",
    "access_token",
    "tenant_access_token",
    "refresh_token",
    "authorization",
}


class ProtocolError(ValueError):
    """The worker IPC peer sent a malformed or unsafe frame."""


class FrameType(str, Enum):
    READY = "READY"
    EVENT = "EVENT"
    HEALTH = "HEALTH"
    ERROR = "ERROR"
    STOP = "STOP"
    SEND = "SEND"


@dataclass(frozen=True, slots=True)
class ChannelFrame:
    frame_type: FrameType
    agent_id: str
    generation: int
    sequence: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChannelBootstrap:
    agent_id: str
    app_id: str
    generation: int
    app_secret: str


def encode_frame(frame: ChannelFrame) -> bytes:
    """Encode one ordinary IPC frame as canonical single-line NDJSON."""
    _validate_identity(frame.agent_id, frame.generation)
    if not isinstance(frame.sequence, int) or isinstance(frame.sequence, bool) or frame.sequence < 1:
        raise ProtocolError("invalid sequence")
    if not isinstance(frame.payload, dict):
        raise ProtocolError("payload must be an object")
    _reject_secret_fields(frame.payload)
    return _encode(
        {
            "v": PROTOCOL_VERSION,
            "type": frame.frame_type.value,
            "agent_id": frame.agent_id,
            "generation": frame.generation,
            "sequence": frame.sequence,
            "payload": frame.payload,
        }
    )


def decode_frame(raw: bytes) -> ChannelFrame:
    """Decode exactly one ordinary IPC frame and reject schema drift."""
    value = _decode(raw)
    if set(value) != _FRAME_KEYS:
        raise ProtocolError("invalid frame fields")
    if value["v"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    try:
        frame_type = FrameType(value["type"])
    except (TypeError, ValueError) as exc:
        raise ProtocolError("unknown frame type") from exc
    agent_id = value["agent_id"]
    generation = value["generation"]
    _validate_identity(agent_id, generation)
    sequence = value["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ProtocolError("invalid sequence")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")
    _reject_secret_fields(payload)
    return ChannelFrame(frame_type, agent_id, generation, sequence, payload)


def encode_bootstrap(bootstrap: ChannelBootstrap) -> bytes:
    """Encode the sole credential-bearing frame for the one-shot pipe."""
    _validate_identity(bootstrap.agent_id, bootstrap.generation)
    if not isinstance(bootstrap.app_id, str) or not bootstrap.app_id:
        raise ProtocolError("invalid app_id")
    if not isinstance(bootstrap.app_secret, str) or not bootstrap.app_secret:
        raise ProtocolError("invalid app_secret")
    return _encode(
        {
            "v": PROTOCOL_VERSION,
            "type": "BOOTSTRAP",
            "agent_id": bootstrap.agent_id,
            "app_id": bootstrap.app_id,
            "generation": bootstrap.generation,
            "app_secret": bootstrap.app_secret,
        }
    )


def decode_bootstrap(raw: bytes) -> ChannelBootstrap:
    """Decode the one allowed credential frame from the bootstrap pipe."""
    value = _decode(raw)
    if set(value) != _BOOTSTRAP_KEYS or value.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("invalid bootstrap frame")
    if value.get("type") != "BOOTSTRAP":
        raise ProtocolError("invalid bootstrap type")
    bootstrap = ChannelBootstrap(
        agent_id=value.get("agent_id"),
        app_id=value.get("app_id"),
        generation=value.get("generation"),
        app_secret=value.get("app_secret"),
    )
    _validate_identity(bootstrap.agent_id, bootstrap.generation)
    if not isinstance(bootstrap.app_id, str) or not bootstrap.app_id:
        raise ProtocolError("invalid app_id")
    if not isinstance(bootstrap.app_secret, str) or not bootstrap.app_secret:
        raise ProtocolError("invalid app_secret")
    return bootstrap


def _encode(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise ProtocolError("frame is not JSON serializable") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return encoded


def _decode(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("invalid frame size")
    if not raw.endswith(b"\n") or b"\n" in raw[:-1] or b"\r" in raw:
        raise ProtocolError("frame must be one NDJSON line")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON frame") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame must be an object")
    return value


def _validate_identity(agent_id: Any, generation: Any) -> None:
    if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 256:
        raise ProtocolError("invalid agent_id")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ProtocolError("invalid generation")


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_IPC_KEYS:
                raise ProtocolError("credential material is forbidden on ordinary IPC")
            _reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child)
