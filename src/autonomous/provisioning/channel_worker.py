"""Fresh-interpreter worker hosting exactly one employee FeishuChannel."""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import os
import resource
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

# ``python -I /absolute/worker.py`` intentionally excludes the repository from
# sys.path. Add only the immutable repository root derived from this file.
_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[3])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from src.autonomous.provisioning.channel_protocol import (  # noqa: E402
    MAX_FRAME_BYTES,
    ChannelFrame,
    FrameType,
    decode_bootstrap,
    decode_frame,
    encode_frame,
)


class WorkerSecurityError(RuntimeError):
    """Mandatory process hardening could not be applied."""


def apply_process_hardening() -> None:
    """Disable core dumps, dumpability, and privilege acquisition."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform != "linux":
        raise WorkerSecurityError("unsupported worker platform")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise WorkerSecurityError("dumpability hardening failed")
    if prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise WorkerSecurityError("privilege hardening failed")


class _FrameEmitter:
    def __init__(self, fd: int, agent_id: str, generation: int) -> None:
        self._fd = fd
        self._agent_id = agent_id
        self._generation = generation
        self._sequence = 0
        self._lock = threading.Lock()

    def emit(self, frame_type: FrameType, payload: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            raw = encode_frame(
                ChannelFrame(
                    frame_type=frame_type,
                    agent_id=self._agent_id,
                    generation=self._generation,
                    sequence=self._sequence,
                    payload=_json_safe(payload),
                )
            )
            _write_all(self._fd, raw)


def register_channel_handlers(channel: Any, emit: Callable[[str, Any], None]) -> None:
    """Register the exact lark-channel-sdk 1.1.0 event surface."""

    from lark_channel import Events

    async def on_message(event: Any) -> None:
        emit("message", event)

    async def on_card_action(event: Any) -> None:
        emit("cardAction", event)

    async def on_bot_added(event: Any) -> None:
        emit("botAdded", event)

    async def on_bot_leave(event: Any) -> None:
        emit("botLeave", event)

    async def on_raw(event: Any) -> None:
        metadata = extract_raw_message_metadata(event)
        if metadata is not None:
            emit("rawMessageMeta", metadata)

    def on_reconnecting() -> None:
        emit("reconnecting", {})

    def on_reconnected() -> None:
        emit("reconnected", {})

    def on_error(error: Any) -> None:
        emit("error", {"error_code": type(error).__name__})

    channel.on(Events.MESSAGE, on_message)
    channel.on(Events.CARD_ACTION, on_card_action)
    channel.on(Events.RECONNECTING, on_reconnecting)
    channel.on(Events.RECONNECTED, on_reconnected)
    channel.on(Events.ERROR, on_error)
    channel.on(Events.BOT_ADDED, on_bot_added)
    channel.on(Events.BOT_LEAVE, on_bot_leave)
    channel.on(Events.RAW, on_raw)


def extract_raw_message_metadata(event: Any) -> dict[str, str] | None:
    """Retain authoritative routing fields without forwarding raw tokens/body."""
    if not isinstance(event, dict):
        return None
    header = event.get("header")
    event_body = event.get("event")
    message = event_body.get("message") if isinstance(event_body, dict) else None
    if not isinstance(header, dict) or not isinstance(message, dict):
        return None
    values = {
        "event_id": header.get("event_id"),
        "tenant_key": header.get("tenant_key"),
        "message_id": message.get("message_id"),
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return values  # type: ignore[return-value]


async def _run(bootstrap_fd: int, control_fd: int, event_fd: int) -> None:
    apply_process_hardening()
    with os.fdopen(bootstrap_fd, "rb", buffering=0) as bootstrap_stream:
        raw = bootstrap_stream.readline(MAX_FRAME_BYTES + 1)
        bootstrap = decode_bootstrap(raw)

    emitter = _FrameEmitter(event_fd, bootstrap.agent_id, bootstrap.generation)

    from lark_channel import FeishuChannel, InboundConfig

    channel = FeishuChannel(
        app_id=bootstrap.app_id,
        app_secret=bootstrap.app_secret,
        inbound=InboundConfig(emit_raw_events=True, include_raw=True),
    )

    def emit_event(name: str, value: Any) -> None:
        if name == "error":
            emitter.emit(FrameType.ERROR, _json_safe(value))
        else:
            emitter.emit(FrameType.EVENT, {"event": name, "data": _json_safe(value)})

    register_channel_handlers(channel, emit_event)
    try:
        await channel.connect_until_ready(timeout=30.0)
        snapshot = channel.connection_snapshot()
        identity = channel.bot_identity
        if identity is None or not snapshot.ready:
            raise RuntimeError("employee Channel identity unavailable")
        connection_id = f"conn_{uuid.uuid4().hex}"
        emitter.emit(
            FrameType.READY,
            {
                "identity": _json_safe(identity),
                "connection": _json_safe(snapshot),
                "connection_id": connection_id,
            },
        )
        with os.fdopen(control_fd, "rb", buffering=0) as control:
            while True:
                raw = await asyncio.to_thread(control.readline, MAX_FRAME_BYTES + 1)
                if not raw:
                    break
                frame = decode_frame(raw)
                if frame.agent_id != bootstrap.agent_id or frame.generation != bootstrap.generation:
                    continue
                if frame.frame_type is FrameType.STOP:
                    break
                if frame.frame_type is FrameType.SEND:
                    await _handle_send(
                        channel,
                        frame.payload,
                        emitter,
                        app_id=bootstrap.app_id,
                        generation=bootstrap.generation,
                        connection_id=connection_id,
                    )
    finally:
        await channel.disconnect()


async def _handle_send(
    channel: Any,
    payload: dict[str, Any],
    emitter: _FrameEmitter,
    *,
    app_id: str,
    generation: int,
    connection_id: str,
) -> None:
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        emitter.emit(FrameType.ERROR, {"error_code": "invalid-send"})
        return
    target = payload.get("target")
    if not isinstance(target, str) or not target:
        emitter.emit(FrameType.ERROR, {"error_code": "invalid-send"})
        return
    try:
        result = await channel.send(target, payload.get("message"), payload.get("options"))
    except Exception as exc:
        emitter.emit(FrameType.ERROR, {"error_code": type(exc).__name__})
        emitter.emit(
            FrameType.HEALTH,
            {"operation": "send", "request_id": request_id, "success": False},
        )
        return
    emitter.emit(
        FrameType.HEALTH,
        {
            "operation": "send",
            "request_id": request_id,
            "success": bool(getattr(result, "success", False)),
            "app_id": app_id,
            "generation": generation,
            "connection_id": connection_id,
            "message_id": getattr(result, "message_id", "") or "",
        },
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _json_safe(child)
            for key, child in vars(value).items()
            if not str(key).startswith("_")
        }
    return {"type": type(value).__name__}


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BrokenPipeError("worker IPC closed")
        view = view[written:]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 64
    try:
        fds = [int(value) for value in args]
    except ValueError:
        return 64
    try:
        asyncio.run(_run(*fds))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
