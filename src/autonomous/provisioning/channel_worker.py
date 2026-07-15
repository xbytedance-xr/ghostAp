"""Fresh-interpreter worker hosting exactly one employee FeishuChannel."""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import json
import os
import queue
import resource
import select
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

# ``python -I /absolute/worker.py`` intentionally excludes the repository from
# sys.path. Add only the immutable repository root derived from this file.
_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[3])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from src.autonomous.ingress.models import (  # noqa: E402
    EmployeeIngressAck,
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
)
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


@dataclass(slots=True)
class _PendingIngressAck:
    request_id: str
    agent_id: str
    app_id: str
    generation: int
    connection_id: str
    semantic_digest: str
    envelope_id: str
    dedup_key: str
    completed: threading.Event = field(default_factory=threading.Event)
    ack: EmployeeIngressAck | None = None
    closed: bool = False


class IngressAckMailbox:
    """Contextual owner for bounded, current-generation durable ACK waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingIngressAck] = {}
        self._closed = False

    def register(
        self,
        *,
        request_id: str,
        agent_id: str,
        app_id: str,
        generation: int,
        connection_id: str,
        semantic_digest: str,
        envelope_id: str,
        dedup_key: str,
    ) -> _PendingIngressAck:
        pending = _PendingIngressAck(
            request_id=request_id,
            agent_id=agent_id,
            app_id=app_id,
            generation=generation,
            connection_id=connection_id,
            semantic_digest=semantic_digest,
            envelope_id=envelope_id,
            dedup_key=dedup_key,
        )
        with self._lock:
            if self._closed:
                raise EOFError("employee ingress parent unavailable")
            if request_id in self._pending:
                raise ValueError("duplicate ingress request")
            self._pending[request_id] = pending
        return pending

    def deliver(self, ack: EmployeeIngressAck) -> bool:
        if not isinstance(ack, EmployeeIngressAck):
            return False
        with self._lock:
            pending = self._pending.get(ack.request_id)
            if pending is None or pending.closed:
                return False
            if (
                ack.agent_id != pending.agent_id
                or ack.app_id != pending.app_id
                or ack.channel_generation != pending.generation
                or ack.connection_id != pending.connection_id
                or ack.semantic_digest != pending.semantic_digest
                or ack.acceptance.envelope_id != pending.envelope_id
                or ack.acceptance.dedup_key != pending.dedup_key
            ):
                return False
            pending.ack = ack
            pending.completed.set()
            return True

    def wait(
        self,
        pending: _PendingIngressAck,
        *,
        timeout: float,
    ) -> EmployeeIngressAck:
        if not pending.completed.wait(timeout):
            with self._lock:
                if self._pending.get(pending.request_id) is pending:
                    self._pending.pop(pending.request_id, None)
                    pending.closed = True
            raise TimeoutError("employee ingress durable ACK timed out")
        with self._lock:
            if self._pending.get(pending.request_id) is pending:
                self._pending.pop(pending.request_id, None)
            pending.closed = True
            ack = pending.ack
        if ack is None:
            raise EOFError("employee ingress parent unavailable")
        return ack

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending.values())
            self._pending.clear()
            for item in pending:
                item.closed = True
                item.completed.set()

    def cancel_pending(self) -> None:
        """Fail current waits while keeping the mailbox open for reconnect."""
        with self._lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
            for item in pending:
                item.closed = True
                item.completed.set()

    def cancel(self, pending: _PendingIngressAck) -> None:
        """Remove one request that could not be emitted to the parent."""
        with self._lock:
            if self._pending.get(pending.request_id) is pending:
                self._pending.pop(pending.request_id, None)
            pending.closed = True
            pending.completed.set()


class _ConnectionAdmission:
    """Atomically fence callback ownership to one observed WS connection."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._epoch = 0
        self._connection_id = ""
        self._ready = False

    @property
    def epoch(self) -> int:
        with self._condition:
            return self._epoch

    def begin_reconnect(self, mailbox: IngressAckMailbox) -> int:
        with self._condition:
            self._epoch += 1
            self._connection_id = ""
            self._ready = False
            mailbox.cancel_pending()
            return self._epoch

    def publish_observed(
        self,
        connection_id: str,
        *,
        expected_epoch: int,
        emit_ready: Callable[[], None],
    ) -> bool:
        with self._condition:
            if self._epoch != expected_epoch or self._ready:
                return False
            self._connection_id = connection_id
            self._ready = True
            try:
                emit_ready()
            except BaseException:
                self._connection_id = ""
                self._ready = False
                raise
            self._condition.notify_all()
            return True

    def wait_snapshot(self, *, deadline: float) -> tuple[int, str]:
        with self._condition:
            while not self._ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(timeout=remaining):
                    raise TimeoutError(
                        "employee ingress connection readiness timed out"
                    )
            return self._epoch, self._connection_id

    def register(
        self,
        mailbox: IngressAckMailbox,
        *,
        expected_epoch: int,
        request_id: str,
        agent_id: str,
        app_id: str,
        generation: int,
        connection_id: str,
        semantic_digest: str,
        envelope_id: str,
        dedup_key: str,
    ) -> _PendingIngressAck:
        with self._condition:
            if (
                not self._ready
                or self._epoch != expected_epoch
                or self._connection_id != connection_id
            ):
                raise EOFError("employee ingress connection ownership changed")
            return mailbox.register(
                request_id=request_id,
                agent_id=agent_id,
                app_id=app_id,
                generation=generation,
                connection_id=connection_id,
                semantic_digest=semantic_digest,
                envelope_id=envelope_id,
                dedup_key=dedup_key,
            )


def run_low_level_employee_channel(
    bootstrap_fd: int,
    control_fd: int,
    event_fd: int,
    *,
    domain: str = "https://open.feishu.cn",
    proxy_url: str | None = None,
    proxy_allowlist: tuple[str, ...] = (),
    allow_test_local_endpoint_discovery: bool = False,
) -> None:
    """Run the pinned synchronous SDK dispatcher with parent-durable ACKs."""

    apply_process_hardening()
    domain_url = urlparse(domain)
    local_test_domain = (
        allow_test_local_endpoint_discovery
        and domain_url.scheme == "http"
        and domain_url.hostname in {"127.0.0.1", "localhost"}
    )
    if (
        not domain_url.hostname
        or (domain_url.scheme != "https" and not local_test_domain)
    ):
        raise WorkerSecurityError("employee Channel requires HTTPS endpoint discovery")
    if proxy_url is not None:
        proxy_host = urlparse(proxy_url).hostname
        if not proxy_host or proxy_host not in set(proxy_allowlist):
            raise WorkerSecurityError("employee Channel proxy is not allowlisted")

    from src.autonomous.ingress.sdk_capability import (
        collect_sdk_distribution_identity,
        prepare_controlled_sdk_import_cache,
    )

    prepare_controlled_sdk_import_cache(
        Path("/tmp") / f"ghostap-employee-channel-sdk-{os.getpid()}"
    )
    collect_sdk_distribution_identity(require_controlled_import_cache=True)

    with os.fdopen(bootstrap_fd, "rb", buffering=0) as bootstrap_stream:
        bootstrap = decode_bootstrap(bootstrap_stream.readline(MAX_FRAME_BYTES + 1))
    tenant_key = bootstrap.tenant_key
    bot_principal_id = bootstrap.bot_principal_id
    ack_timeout = bootstrap.ack_timeout_seconds

    emitter = _FrameEmitter(event_fd, bootstrap.agent_id, bootstrap.generation)
    mailbox = IngressAckMailbox()
    stop_requested = threading.Event()
    admission = _ConnectionAdmission()

    import lark_oapi as lark
    from lark_channel.core.enum import LogLevel
    from lark_channel.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
    from lark_channel.event.dispatcher_handler import EventDispatcherHandler
    from lark_channel.ws import Client as WSClient

    from src.autonomous.provisioning.lark_outbound import LarkEmployeeOutbound

    security = _strict_sdk_security_config()
    outbound = LarkEmployeeOutbound(
        lark.Client.builder()
        .app_id(bootstrap.app_id)
        .app_secret(bootstrap.app_secret)
        .build()
    )
    control_reader = threading.Thread(
        target=_read_low_level_control,
        args=(
            control_fd,
            bootstrap,
            mailbox,
            emitter,
            stop_requested,
            outbound,
            admission,
        ),
        name=f"employee-channel-control-{bootstrap.agent_id}-{bootstrap.generation}",
        daemon=True,
    )
    control_reader.start()

    def wait_for_parent(event: Any, *, kind: str) -> EmployeeIngressAck:
        deadline = time.monotonic() + float(ack_timeout)
        if stop_requested.is_set():
            raise EOFError("employee ingress admission is closed")
        connection_epoch, connection_id = admission.wait_snapshot(
            deadline=deadline
        )
        if stop_requested.is_set():
            raise EOFError("employee ingress admission is closed")
        metadata, payload, correlation = _normalize_sdk_ingress(
            event,
            kind=kind,
            agent_id=bootstrap.agent_id,
            app_id=bootstrap.app_id,
            generation=bootstrap.generation,
            connection_id=connection_id,
            tenant_key=tenant_key,
            bot_principal_id=bot_principal_id,
        )
        request_id = f"req_{uuid.uuid4().hex}"
        pending = admission.register(
            mailbox,
            expected_epoch=connection_epoch,
            request_id=request_id,
            agent_id=bootstrap.agent_id,
            app_id=bootstrap.app_id,
            generation=bootstrap.generation,
            connection_id=connection_id,
            semantic_digest=metadata.semantic_digest,
            envelope_id=metadata.envelope_id,
            dedup_key=metadata.dedup_key,
        )
        try:
            emitter.emit(
                FrameType.INGRESS,
                {
                    "request_id": request_id,
                    "app_id": bootstrap.app_id,
                    "connection_id": connection_id,
                    "metadata": metadata.to_dict(),
                    "payload": payload.to_dict(),
                    "action_correlation": correlation,
                },
                deadline=deadline,
            )
        except BaseException:
            mailbox.cancel(pending)
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            mailbox.wait(pending, timeout=0.0)
        return mailbox.wait(pending, timeout=remaining)

    def on_message(event: Any) -> None:
        wait_for_parent(event, kind="message")

    def on_card_action(event: Any) -> Any:
        wait_for_parent(event, kind="card")
        return P2CardActionTriggerResponse({})

    def on_bot_added(event: Any) -> None:
        wait_for_parent(event, kind="membership_added")

    def on_bot_deleted(event: Any) -> None:
        wait_for_parent(event, kind="membership_deleted")

    dispatcher = (
        EventDispatcherHandler.builder("", "", security=security)
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_chat_member_bot_added_v1(on_bot_added)
        .register_p2_im_chat_member_bot_deleted_v1(on_bot_deleted)
        .build()
    )
    client = WSClient(
        app_id=bootstrap.app_id,
        app_secret=bootstrap.app_secret,
        log_level=LogLevel.ERROR,
        event_handler=dispatcher,
        domain=domain,
        auto_reconnect=True,
        proxy_url=proxy_url,
        trust_env_proxy=False,
        handshake_timeout=10.0,
        security=security,
    )

    def on_reconnecting() -> None:
        admission.begin_reconnect(mailbox)
        _emit_nonblocking(emitter, "reconnecting", {})

    def on_reconnected() -> None:
        connection_epoch = admission.epoch
        connection_id = f"conn_{uuid.uuid4().hex}"
        _emit_nonblocking(emitter, "reconnected", {"connection_id": connection_id})
        _publish_observed_low_level_connection(
            client,
            bootstrap,
            connection_id,
            emitter,
            admission,
            expected_epoch=connection_epoch,
        )

    client.on_reconnecting = on_reconnecting
    client.on_reconnected = on_reconnected
    readiness = threading.Thread(
        target=_observe_low_level_connection,
        args=(client, bootstrap, emitter, stop_requested, admission),
        name=f"employee-channel-ready-{bootstrap.agent_id}-{bootstrap.generation}",
        daemon=True,
    )
    readiness.start()
    try:
        client.start()
    finally:
        stop_requested.set()
        mailbox.close()
        emitter.close()


def _strict_sdk_security_config() -> Any:
    from lark_channel.channel.config import SecurityConfig

    return SecurityConfig(
        mode="strict",
        allow_insecure_ws=False,
        allow_local_insecure_ws=False,
        max_ws_fragment_parts=8,
        max_ws_fragment_bytes=256 * 1024,
        max_concurrent_ws_handlers=1,
        resource_overflow_policy="drop",
    )


def _read_low_level_control(
    control_fd: int,
    bootstrap: Any,
    mailbox: IngressAckMailbox,
    emitter: _FrameEmitter,
    stop_requested: threading.Event,
    outbound: Any,
    admission: _ConnectionAdmission,
) -> None:
    inbound_sequence = 0
    try:
        with os.fdopen(control_fd, "rb", buffering=0) as control:
            while raw := control.readline(MAX_FRAME_BYTES + 1):
                try:
                    frame = decode_frame(raw)
                except Exception:
                    continue
                if (
                    frame.agent_id != bootstrap.agent_id
                    or frame.generation != bootstrap.generation
                    or frame.sequence <= inbound_sequence
                ):
                    continue
                inbound_sequence = frame.sequence
                if frame.frame_type is FrameType.INGRESS_ACK:
                    try:
                        ack = EmployeeIngressAck.from_dict(frame.payload["ack"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    mailbox.deliver(ack)
                elif frame.frame_type is FrameType.STOP:
                    stop_requested.set()
                    mailbox.close()
                    return
                elif frame.frame_type in {FrameType.SEND, FrameType.UPDATE_CARD}:
                    _handle_low_level_outbound(
                        frame,
                        bootstrap,
                        outbound,
                        admission,
                        emitter,
                    )
    finally:
        stop_requested.set()
        mailbox.close()


def _handle_low_level_outbound(
    frame: ChannelFrame,
    bootstrap: Any,
    outbound: Any,
    admission: _ConnectionAdmission,
    emitter: _FrameEmitter,
) -> None:
    """Execute one parent-authorized send with current Channel authority."""

    operation = "send" if frame.frame_type is FrameType.SEND else "update_card"
    request_id = frame.payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        emitter.emit(
            FrameType.HEALTH,
            {
                "operation": operation,
                "request_id": "",
                "success": False,
                "error_code": "invalid-outbound-request",
            },
        )
        return
    try:
        _epoch, connection_id = admission.wait_snapshot(
            deadline=time.monotonic() + 10.0
        )
        if frame.frame_type is FrameType.SEND:
            result = outbound.send(
                frame.payload.get("target"),
                frame.payload.get("message"),
                frame.payload.get("options"),
            )
        else:
            result = outbound.update_card(
                frame.payload.get("message_id"),
                frame.payload.get("card"),
            )
        if getattr(result, "success", None) is not True:
            raise RuntimeError("employee outbound operation was not acknowledged")
        message_id = getattr(result, "message_id", "")
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("employee outbound receipt is invalid")
    except Exception as exc:
        emitter.emit(
            FrameType.HEALTH,
            {
                "operation": operation,
                "request_id": request_id,
                "success": False,
                "error_code": type(exc).__name__,
            },
        )
        return
    emitter.emit(
        FrameType.HEALTH,
        {
            "operation": operation,
            "request_id": request_id,
            "success": True,
            "app_id": bootstrap.app_id,
            "generation": bootstrap.generation,
            "connection_id": connection_id,
            "message_id": message_id,
        },
    )


def _observe_low_level_connection(
    client: Any,
    bootstrap: Any,
    emitter: _FrameEmitter,
    stop_requested: threading.Event,
    admission: _ConnectionAdmission,
) -> None:
    connection_epoch = admission.epoch
    connection_id = f"conn_{uuid.uuid4().hex}"
    while not stop_requested.is_set():
        if _publish_observed_low_level_connection(
            client,
            bootstrap,
            connection_id,
            emitter,
            admission,
            expected_epoch=connection_epoch,
        ):
            return
        if admission.epoch != connection_epoch:
            return
        stop_requested.wait(0.01)


def _publish_observed_low_level_connection(
    client: Any,
    bootstrap: Any,
    connection_id: str,
    emitter: _FrameEmitter,
    admission: _ConnectionAdmission,
    *,
    expected_epoch: int,
) -> bool:
    connection = getattr(client, "_conn", None)
    sdk_connection_id = getattr(client, "_conn_id", "")
    service_id = getattr(client, "_service_id", "")
    connection_url = getattr(client, "_conn_url", "")
    if (
        connection is None
        or not isinstance(sdk_connection_id, str)
        or not sdk_connection_id
        or not isinstance(service_id, str)
        or not service_id
        or not isinstance(connection_url, str)
        or not connection_url.startswith("wss://")
    ):
        return False
    ready_payload = {
        "identity": {"app_id": bootstrap.app_id},
        "connection_id": connection_id,
        "connection": {
            "observed": True,
            "sdk_connection_id": sdk_connection_id,
            "service_id": service_id,
            "secure": True,
        },
    }
    return admission.publish_observed(
        connection_id,
        expected_epoch=expected_epoch,
        emit_ready=lambda: emitter.emit(
            FrameType.READY,
            ready_payload,
        ),
    )


def _emit_nonblocking(
    emitter: _FrameEmitter,
    event_name: str,
    data: dict[str, Any],
) -> None:
    emitter.try_emit(
        FrameType.EVENT,
        {"event": event_name, "data": _json_safe(data)},
    )


def _normalize_sdk_ingress(
    event: Any,
    *,
    kind: str,
    agent_id: str,
    app_id: str,
    generation: int,
    connection_id: str,
    tenant_key: str,
    bot_principal_id: str,
) -> tuple[EmployeeIngressMetadata, EmployeeIngressPayload, str | None]:
    header = getattr(event, "header", None)
    body = getattr(event, "event", None)
    raw_event_id = getattr(header, "event_id", "")
    event_type = getattr(header, "event_type", "")
    header_tenant = getattr(header, "tenant_key", "")
    if header_tenant != tenant_key or getattr(header, "app_id", "") != app_id:
        raise ValueError("employee ingress header authority mismatch")
    received_at = _sdk_timestamp(getattr(header, "create_time", ""))
    correlation: str | None = None
    if kind == "message":
        message = getattr(body, "message", None)
        sender = getattr(body, "sender", None)
        sender_id = getattr(getattr(sender, "sender_id", None), "open_id", "")
        message_id = getattr(message, "message_id", "")
        chat_id = getattr(message, "chat_id", "")
        root_id = getattr(message, "root_id", "") or getattr(message, "parent_id", "") or ""
        content_raw = getattr(message, "content", "")
        try:
            content = json.loads(content_raw)
        except (TypeError, json.JSONDecodeError):
            content = content_raw
        parts = (
            {
                "type": "message",
                "message_type": getattr(message, "message_type", ""),
                "chat_type": getattr(message, "chat_type", ""),
                "content": content,
                "sender_id": sender_id,
                "sender_id_type": "open_id",
                "sender_type": getattr(sender, "sender_type", ""),
                "sender_tenant_key": getattr(sender, "tenant_key", ""),
                "feishu_thread_id": getattr(message, "thread_id", "") or "",
            },
        )
        action_identity = ""
    elif kind == "card":
        context = getattr(body, "context", None)
        operator = getattr(body, "operator", None)
        sender_id = getattr(operator, "open_id", "")
        message_id = getattr(context, "open_message_id", "")
        chat_id = getattr(context, "open_chat_id", "")
        root_id = ""
        action_identity = ""
        parts = (
            {
                "type": "card_action",
                "sender_id": sender_id,
                "sender_id_type": "open_id",
                "sender_type": getattr(operator, "sender_type", "") or "",
                "sender_tenant_key": getattr(operator, "tenant_key", "") or "",
            },
        )
    elif kind in {"membership_added", "membership_deleted"}:
        sender_id = getattr(getattr(body, "operator_id", None), "open_id", "")
        message_id = raw_event_id
        chat_id = getattr(body, "chat_id", "")
        root_id = ""
        action_identity = kind
        parts = (
            {
                "type": "membership_event",
                "operation": (
                    "added" if kind == "membership_added" else "deleted"
                ),
                "operator_tenant_key": getattr(body, "operator_tenant_key", ""),
                "external": getattr(body, "external", False),
            },
        )
    else:
        raise ValueError("unsupported employee ingress event kind")
    if not raw_event_id:
        raise ValueError("employee ingress requires trusted event identity")
    event_id = _canonical_ingress_id(raw_event_id, "evt_")
    message_id = _canonical_ingress_id(message_id, "om_")
    chat_id = _canonical_ingress_id(chat_id, "oc_")
    if root_id:
        root_id = _canonical_ingress_id(root_id, "om_")
    stable = json.dumps(
        [tenant_key, agent_id, event_id, message_id, event_type, action_identity],
        separators=(",", ":"),
    ).encode()
    import hashlib

    payload = EmployeeIngressPayload(
        schema_version=1,
        envelope_id="ing_" + hashlib.sha256(stable).hexdigest(),
        normalized_parts=parts,
        attachment_descriptors=(),
    )
    metadata = EmployeeIngressMetadata(
        schema_version=1,
        envelope_id=payload.envelope_id,
        tenant_key=tenant_key,
        agent_id=agent_id,
        bot_principal_id=bot_principal_id,
        app_id=app_id,
        channel_generation=generation,
        connection_id=connection_id,
        event_id=event_id,
        message_id=message_id,
        event_type=event_type,
        action_identity=action_identity,
        chat_id=chat_id,
        thread_root_message_id=root_id,
        sender_principal_id=sender_id,
        received_at=received_at,
        semantic_digest=payload.payload_sha256,
        payload_sha256=payload.payload_sha256,
        payload_size_bytes=payload.canonical_size_bytes,
        attachment_count=0,
        attachment_total_bytes=0,
    )
    return metadata, payload, correlation


def _sdk_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("invalid employee ingress timestamp")
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _canonical_ingress_id(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("missing employee ingress identifier")
    import hashlib

    return prefix + hashlib.sha256(value.encode()).hexdigest()


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


@dataclass(slots=True)
class _EmitRequest:
    frame_type: FrameType
    payload: dict[str, Any]
    deadline: float
    required: bool
    completed: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class _FrameEmitter:
    """Single-owner, deadline-bounded NDJSON writer for the child event pipe."""

    _DEFAULT_TIMEOUT_SECONDS = 10.0
    _QUEUE_CAPACITY = 128

    def __init__(self, fd: int, agent_id: str, generation: int) -> None:
        self._fd = fd
        self._agent_id = agent_id
        self._generation = generation
        self._sequence = 0
        self._requests: queue.Queue[_EmitRequest | None] = queue.Queue(
            maxsize=self._QUEUE_CAPACITY
        )
        self._state_lock = threading.Lock()
        self._failed: BaseException | None = None
        self._closed = False
        os.set_blocking(fd, False)
        self._writer = threading.Thread(
            target=self._write_loop,
            name=f"employee-channel-emitter-{agent_id}-{generation}",
            daemon=True,
        )
        self._writer.start()

    def emit(
        self,
        frame_type: FrameType,
        payload: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> None:
        if deadline is None:
            deadline = time.monotonic() + self._DEFAULT_TIMEOUT_SECONDS
        request = _EmitRequest(frame_type, payload, deadline, True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = TimeoutError("employee Channel IPC emit timed out")
            self._fail(error)
            raise error
        self._raise_if_unavailable()
        try:
            self._requests.put(request, timeout=remaining)
        except queue.Full as exc:
            error = TimeoutError("employee Channel IPC emitter queue timed out")
            self._fail(error)
            raise error from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not request.completed.wait(remaining):
            error = TimeoutError("employee Channel IPC emit timed out")
            self._fail(error)
            raise error
        if request.error is not None:
            raise request.error

    def try_emit(self, frame_type: FrameType, payload: dict[str, Any]) -> bool:
        """Queue a best-effort notification without blocking its SDK callback."""
        with self._state_lock:
            if self._closed or self._failed is not None:
                return False
        request = _EmitRequest(
            frame_type,
            payload,
            time.monotonic(),
            False,
        )
        try:
            self._requests.put_nowait(request)
        except queue.Full:
            self._fail(BufferError("employee Channel IPC emitter queue full"))
            return False
        return True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass

    def _raise_if_unavailable(self) -> None:
        with self._state_lock:
            if self._failed is not None:
                raise EOFError("employee Channel IPC emitter failed") from self._failed
            if self._closed:
                raise EOFError("employee Channel IPC emitter closed")

    def _write_loop(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            try:
                self._write_request(request)
            except BaseException as exc:
                request.error = exc
                self._fail(exc)
                return
            finally:
                request.completed.set()

    def _write_request(self, request: _EmitRequest) -> None:
        self._raise_if_unavailable()
        self._sequence += 1
        raw = encode_frame(
            ChannelFrame(
                frame_type=request.frame_type,
                agent_id=self._agent_id,
                generation=self._generation,
                sequence=self._sequence,
                payload=_json_safe(request.payload),
            )
        )
        view = memoryview(raw)
        bytes_written = 0
        while view:
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                written = 0
            if written > 0:
                bytes_written += written
                view = view[written:]
                continue
            if not request.required and bytes_written == 0:
                raise BlockingIOError(
                    "employee Channel IPC notification could not be emitted"
                )
            remaining = request.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("employee Channel IPC emit timed out")
            _, writable, _ = select.select([], [self._fd], [], remaining)
            if not writable:
                raise TimeoutError("employee Channel IPC emit timed out")

    def _fail(self, error: BaseException) -> None:
        with self._state_lock:
            if self._failed is None:
                self._failed = error
            already_closed = self._closed
            self._closed = True
        if not already_closed:
            try:
                os.close(self._fd)
            except OSError:
                pass
        while True:
            try:
                queued = self._requests.get_nowait()
            except queue.Empty:
                return
            if queued is None:
                continue
            queued.error = EOFError("employee Channel IPC emitter failed")
            queued.completed.set()


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
    sender = event_body.get("sender") if isinstance(event_body, dict) else None
    sender_id = sender.get("sender_id") if isinstance(sender, dict) else None
    if (
        not isinstance(header, dict)
        or not isinstance(message, dict)
        or not isinstance(sender_id, dict)
    ):
        return None
    values = {
        "event_id": header.get("event_id"),
        "tenant_key": header.get("tenant_key"),
        "message_id": message.get("message_id"),
        "sender_union_id": sender_id.get("union_id"),
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return values  # type: ignore[return-value]


def create_employee_channel(app_id: str, app_secret: str) -> Any:
    """Build the employee SDK channel with fail-closed transport and logs."""
    from lark_channel import (
        FeishuChannel,
        InboundConfig,
        LogLevel,
        SecurityConfig,
        TransportConfig,
    )

    return FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        log_level=LogLevel.ERROR,
        transport=TransportConfig(
            auto_reconnect=True,
            proxy_url=None,
            trust_env_proxy=False,
            handshake_timeout_seconds=10.0,
        ),
        security=SecurityConfig(
            mode="strict",
            allow_insecure_ws=False,
            allow_local_insecure_ws=False,
            max_ws_fragment_parts=8,
            max_ws_fragment_bytes=256 * 1024,
            max_concurrent_ws_handlers=1,
            resource_overflow_policy="drop",
        ),
        inbound=InboundConfig(emit_raw_events=True, include_raw=True),
    )


async def _run(bootstrap_fd: int, control_fd: int, event_fd: int) -> None:
    apply_process_hardening()
    with os.fdopen(bootstrap_fd, "rb", buffering=0) as bootstrap_stream:
        raw = bootstrap_stream.readline(MAX_FRAME_BYTES + 1)
        bootstrap = decode_bootstrap(raw)

    emitter = _FrameEmitter(event_fd, bootstrap.agent_id, bootstrap.generation)

    channel = create_employee_channel(bootstrap.app_id, bootstrap.app_secret)

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
                if frame.frame_type is FrameType.UPDATE_CARD:
                    await _handle_update_card(
                        channel,
                        frame.payload,
                        emitter,
                        app_id=bootstrap.app_id,
                        generation=bootstrap.generation,
                        connection_id=connection_id,
                    )
    finally:
        try:
            await channel.disconnect()
        finally:
            emitter.close()


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


async def _handle_update_card(
    channel: Any,
    payload: dict[str, Any],
    emitter: _FrameEmitter,
    *,
    app_id: str,
    generation: int,
    connection_id: str,
) -> None:
    request_id = payload.get("request_id")
    message_id = payload.get("message_id")
    card = payload.get("card")
    if (
        not isinstance(request_id, str)
        or not request_id
        or not isinstance(message_id, str)
        or not message_id
        or not isinstance(card, dict)
    ):
        emitter.emit(FrameType.ERROR, {"error_code": "invalid-update-card"})
        return
    try:
        result = await channel.update_card(message_id, card)
    except Exception as exc:
        emitter.emit(FrameType.ERROR, {"error_code": type(exc).__name__})
        emitter.emit(
            FrameType.HEALTH,
            {
                "operation": "update_card",
                "request_id": request_id,
                "success": False,
            },
        )
        return
    emitter.emit(
        FrameType.HEALTH,
        {
            "operation": "update_card",
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 64
    try:
        fds = [int(value) for value in args]
    except ValueError:
        return 64
    try:
        run_low_level_employee_channel(*fds)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
