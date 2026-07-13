from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path

import pytest

from src.autonomous.ingress.models import (
    EmployeeIngressAck,
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
)
from src.autonomous.ingress.projection import IngressProjectionState
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.journal.anchor import FileAnchor
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.provisioning.channel_protocol import (
    MAX_FRAME_BYTES,
    ChannelBootstrap,
    ChannelFrame,
    FrameType,
    decode_frame,
    encode_bootstrap,
    encode_frame,
)
from tests.autonomous.contract.test_employee_channel_sdk_capability import (
    _card_payload,
    _event_frame,
    _message_payload,
    _WireHarness,
)

_HMAC_KEY = b"employee-bridge-wire-hmac-key-32b"
_DATA_KEY = b"employee-bridge-wire-data-key-32"
_WAIT_SECONDS = 10.0


class _DisconnectBeforeAckHarness(_WireHarness):
    def __init__(self, tmp_path: Path, payload: dict[str, object]) -> None:
        super().__init__(tmp_path, payload)
        self.close_requested = threading.Event()
        self.connection_closed = threading.Event()

    def _handle_ws(self, connection: object) -> None:
        with self._connection_lock:
            self.connection_count += 1
            connection_number = self.connection_count
        if connection_number > 2:
            return
        if connection_number == 2:
            self.second_connection.set()
            connection.send(  # type: ignore[attr-defined]
                _event_frame(
                    self._payload,
                    wire_type=self._wire_type,
                    message_id="msg-capability-2",
                )
            )
            from lark_channel.ws.enum import FrameType as SDKFrameType
            from lark_channel.ws.pb.pbbp2_pb2 import Frame

            while True:
                raw = connection.recv(timeout=_WAIT_SECONDS)  # type: ignore[attr-defined]
                if not isinstance(raw, bytes):
                    continue
                response = Frame()
                response.ParseFromString(raw)
                if response.method != SDKFrameType.DATA.value:
                    continue
                headers = {header.key: header.value for header in response.headers}
                if headers.get("message_id") == "msg-capability-2":
                    self.responses.put(raw)
                    self.second_response_received.set()
                    return
        self.first_connection_ready.set()
        connection.send(  # type: ignore[attr-defined]
            _event_frame(
                self._payload,
                wire_type=self._wire_type,
                message_id="msg-capability",
            )
        )
        self.read_waiting.set()
        if self.close_requested.wait(_WAIT_SECONDS):
            connection.close()  # type: ignore[attr-defined]
            self.connection_closed.set()


class _FailAfterPublishAnchor(FileAnchor):
    def compare_and_swap(
        self,
        expected_sequence: int,
        expected_hash: str,
        new_sequence: int,
        new_hash: str,
    ) -> bool:
        raise OSError("injected anchor failure")


def _service(
    tmp_path: Path,
    *,
    fail_anchor: bool = False,
) -> tuple[EmployeeIngressService, JournalWriter]:
    anchor_type = _FailAfterPublishAnchor if fail_anchor else FileAnchor
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor_type(tmp_path / "anchor.json"),
        hmac_key=_HMAC_KEY,
        writer_epoch=1,
    )
    service = EmployeeIngressService(
        writer=writer,
        blob_store=BlobStore(
            tmp_path / "ingress-blobs",
            AesGcmEncryptionProvider(lambda _ref: _DATA_KEY),
        ),
        ingress_state=IngressProjectionState(),
        active_key_id="k1",
    )
    return service, writer


class _ParentDurableAck:
    def __init__(
        self,
        *,
        event_fd: int,
        control_fd: int,
        service: EmployeeIngressService,
        write_ack: bool = True,
        fail_ack_encode: bool = False,
        fail_ack_write: bool = False,
    ) -> None:
        self._event_fd = event_fd
        self._control_fd = control_fd
        self._service = service
        self._write_ack = write_ack
        self._fail_ack_encode = fail_ack_encode
        self._fail_ack_write = fail_ack_write
        self.ingress_received = threading.Event()
        self.release_commit = threading.Event()
        self.cancel_commit = threading.Event()
        self.anchored = threading.Event()
        self.ready = threading.Event()
        self.ack_written = threading.Event()
        self.accepted_ack = None
        self.accepted_acks: list[EmployeeIngressAck] = []
        self.failure: BaseException | None = None
        self.failed = threading.Event()
        self.accept_invocations = 0
        self.execution_calls: list[str] = []
        self.frame_types: list[FrameType] = []
        self.ingress_frame: ChannelFrame | None = None
        self.ingress_frames: list[ChannelFrame] = []
        self.ingress_before_ready = False
        self._sequence = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.release_commit.set()
        for fd in (self._event_fd, self._control_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        self._thread.join(timeout=1)

    def close_control(self) -> None:
        fd, self._control_fd = self._control_fd, -1
        if fd >= 0:
            os.close(fd)

    def abort(self) -> None:
        self.cancel_commit.set()
        self.release_commit.set()
        self.close_control()

    def stop_pending(self) -> None:
        frame = self.ingress_frame
        if frame is None:
            raise RuntimeError("no pending ingress frame")
        self.cancel_commit.set()
        self._sequence += 1
        os.write(
            self._control_fd,
            encode_frame(
                ChannelFrame(
                    FrameType.STOP,
                    frame.agent_id,
                    frame.generation,
                    self._sequence,
                    {},
                )
            ),
        )
        self.release_commit.set()

    def _profile_execution_call(self, frame: object, event: str, _arg: object) -> None:
        if event != "call":
            return
        module = getattr(frame, "f_globals", {}).get("__name__", "")
        if module == "src.autonomous.provisioning.router" or module.startswith(
            "src.acp"
        ):
            code = getattr(frame, "f_code")
            self.execution_calls.append(f"{module}.{code.co_name}")

    def _run(self) -> None:
        with os.fdopen(self._event_fd, "rb", buffering=0) as stream:
            self._event_fd = -1
            while raw := stream.readline(MAX_FRAME_BYTES + 1):
                frame = decode_frame(raw)
                self.frame_types.append(frame.frame_type)
                if frame.frame_type is FrameType.READY:
                    self.ready.set()
                    continue
                if frame.frame_type is not FrameType.INGRESS:
                    continue
                if not self.ready.is_set():
                    self.ingress_before_ready = True
                self.ingress_frame = frame
                self.ingress_frames.append(frame)
                self.ingress_received.set()
                if not self.release_commit.wait(_WAIT_SECONDS):
                    continue
                if self.cancel_commit.is_set():
                    return
                try:
                    metadata = EmployeeIngressMetadata.from_dict(
                        frame.payload["metadata"]
                    )
                    payload = EmployeeIngressPayload.from_dict(
                        frame.payload["payload"]
                    )
                    self.accept_invocations += 1
                    sys.setprofile(self._profile_execution_call)
                    try:
                        ack = self._service.accept(
                            metadata,
                            payload,
                            request_id=frame.payload["request_id"],
                            action_correlation=frame.payload["action_correlation"],
                        )
                    finally:
                        sys.setprofile(None)
                    self.accepted_ack = ack
                    self.accepted_acks.append(ack)
                    self.anchored.set()
                    if not self._write_ack:
                        continue
                    self._sequence += 1
                    if self._fail_ack_encode:
                        raise ValueError("injected post-anchor ACK encode failure")
                    response = encode_frame(
                        ChannelFrame(
                            FrameType.INGRESS_ACK,
                            frame.agent_id,
                            frame.generation,
                            self._sequence,
                            {
                                "request_id": ack.request_id,
                                "app_id": ack.app_id,
                                "connection_id": ack.connection_id,
                                "ack": ack.to_dict(),
                            },
                        )
                    )
                    if self._fail_ack_write:
                        fd, self._control_fd = self._control_fd, -1
                        os.close(fd)
                        os.write(fd, response)
                    os.write(self._control_fd, response)
                    self.ack_written.set()
                except BaseException as exc:
                    self.failure = exc
                    self.failed.set()


class _ReadyOnlyParent:
    """Read READY, then retain the event pipe without draining INGRESS."""

    def __init__(self, *, event_fd: int, control_fd: int) -> None:
        self._event_fd = event_fd
        self._control_fd = control_fd
        self.ready = threading.Event()
        self.release = threading.Event()
        self.frame_types: list[FrameType] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.release.set()
        for fd in (self._event_fd, self._control_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        self._thread.join(timeout=1)

    def _run(self) -> None:
        with os.fdopen(self._event_fd, "rb", buffering=0) as stream:
            self._event_fd = -1
            raw = stream.readline(MAX_FRAME_BYTES + 1)
            if raw:
                frame = decode_frame(raw)
                self.frame_types.append(frame.frame_type)
                if frame.frame_type is FrameType.READY:
                    self.ready.set()
            self.release.wait(_WAIT_SECONDS)


def _start_production_bridge(
    harness: _WireHarness,
    parent: _ParentDurableAck,
    *,
    bootstrap_w: int,
    bootstrap_r: int,
    control_r: int,
    event_w: int,
    generation: int = 3,
    post_ack_notify_fd: int | None = None,
    post_ack_release_fd: int | None = None,
    fail_sdk_write: bool = False,
) -> subprocess.Popen[bytes]:
    if (post_ack_notify_fd is None) != (post_ack_release_fd is None):
        raise ValueError("post-ACK barrier FDs must be paired")
    barrier = ""
    if post_ack_notify_fd is not None and post_ack_release_fd is not None:
        barrier = f"""
_original_wait = worker.IngressAckMailbox.wait
def _barrier_wait(self, pending, *, timeout):
    ack = _original_wait(self, pending, timeout=timeout)
    os.write({post_ack_notify_fd}, b'1')
    os.read({post_ack_release_fd}, 1)
    return ack
worker.IngressAckMailbox.wait = _barrier_wait
"""
    sdk_write_fault = ""
    if fail_sdk_write:
        sdk_write_fault = """
from src.autonomous.ingress import sdk_capability as _sdk_capability
_original_collect_identity = _sdk_capability.collect_sdk_distribution_identity
def _collect_then_inject_sdk_write(*args, **kwargs):
    identity = _original_collect_identity(*args, **kwargs)
    from lark_channel.ws import Client as _InjectedSDKClient
    async def _fail_post_callback_sdk_write(self, data):
        raise OSError('injected post-callback SDK write failure')
    _InjectedSDKClient._write_message = _fail_post_callback_sdk_write
    return identity
_sdk_capability.collect_sdk_distribution_identity = _collect_then_inject_sdk_write
"""
    code = f"""
import os
import sys
sys.path.insert(0, {str(Path.cwd())!r})
from src.autonomous.provisioning import channel_worker as worker
{barrier}
{sdk_write_fault}
worker.run_low_level_employee_channel(
    {bootstrap_r}, {control_r}, {event_w},
    domain={harness.domain!r},
    allow_test_local_endpoint_discovery=True,
)
"""
    env = {"PYTHONUTF8": "1", "SSL_CERT_FILE": str(harness.ca_file)}
    barrier_fds = tuple(
        fd
        for fd in (post_ack_notify_fd, post_ack_release_fd)
        if fd is not None
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        close_fds=True,
        pass_fds=(bootstrap_r, control_r, event_w, *barrier_fds),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for fd in (bootstrap_r, control_r, event_w, *barrier_fds):
        os.close(fd)
    os.write(
        bootstrap_w,
        encode_bootstrap(
            ChannelBootstrap(
                agent_id="agt_capability",
                app_id="cli_capability",
                generation=generation,
                app_secret="test-only-secret",
                tenant_key="tenant-capability",
                bot_principal_id="bot_capability",
                ack_timeout_seconds=1.5,
            )
        ),
    )
    os.close(bootstrap_w)
    parent.start()
    return process


@contextmanager
def _bridge_delivery(
    tmp_path: Path,
    service: EmployeeIngressService,
    payload: dict[str, object],
    *,
    write_ack: bool = True,
    generation: int = 3,
    harness_type: type[_WireHarness] = _WireHarness,
    fail_ack_encode: bool = False,
    fail_ack_write: bool = False,
):
    bootstrap_r, bootstrap_w = os.pipe()
    control_r, control_w = os.pipe()
    event_r, event_w = os.pipe()
    with harness_type(tmp_path, payload) as harness:
        parent = _ParentDurableAck(
            event_fd=event_r,
            control_fd=control_w,
            service=service,
            write_ack=write_ack,
            fail_ack_encode=fail_ack_encode,
            fail_ack_write=fail_ack_write,
        )
        process = _start_production_bridge(
            harness,
            parent,
            bootstrap_w=bootstrap_w,
            bootstrap_r=bootstrap_r,
            control_r=control_r,
            event_w=event_w,
            generation=generation,
        )
        try:
            yield harness, parent, process
        finally:
            parent.release_commit.set()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
            parent.close()
            assert parent.ingress_before_ready is False
            assert parent.execution_calls == []


@contextmanager
def _bridge_delivery_with_post_ack_barrier(
    tmp_path: Path,
    service: EmployeeIngressService,
    payload: dict[str, object],
):
    bootstrap_r, bootstrap_w = os.pipe()
    control_r, control_w = os.pipe()
    event_r, event_w = os.pipe()
    notify_r, notify_w = os.pipe()
    release_r, release_w = os.pipe()
    with _WireHarness(tmp_path, payload) as harness:
        parent = _ParentDurableAck(
            event_fd=event_r,
            control_fd=control_w,
            service=service,
        )
        process = _start_production_bridge(
            harness,
            parent,
            bootstrap_w=bootstrap_w,
            bootstrap_r=bootstrap_r,
            control_r=control_r,
            event_w=event_w,
            post_ack_notify_fd=notify_w,
            post_ack_release_fd=release_r,
        )
        try:
            yield harness, parent, process, notify_r, release_w
        finally:
            parent.release_commit.set()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
            parent.close()
            assert parent.ingress_before_ready is False
            assert parent.execution_calls == []
            for fd in (notify_r, release_w):
                try:
                    os.close(fd)
                except OSError:
                    pass


def _assert_wire_response_waits_for_durable_parent_ack(
    tmp_path: Path,
    *,
    event_kind: str,
) -> None:
    payload = _message_payload() if event_kind == "message" else _card_payload()
    service, writer = _service(tmp_path)
    bootstrap_r, bootstrap_w = os.pipe()
    control_r, control_w = os.pipe()
    event_r, event_w = os.pipe()
    with _WireHarness(tmp_path, payload) as harness:
        parent = _ParentDurableAck(
            event_fd=event_r,
            control_fd=control_w,
            service=service,
        )
        process = _start_production_bridge(
            harness,
            parent,
            bootstrap_w=bootstrap_w,
            bootstrap_r=bootstrap_r,
            control_r=control_r,
            event_w=event_w,
        )
        try:
            ready = parent.ready.wait(_WAIT_SECONDS)
            diagnostic = b""
            if not ready and process.poll() is not None and process.stderr is not None:
                diagnostic = process.stderr.read()
            assert ready, (process.poll(), diagnostic.decode(errors="replace"))
            assert parent.ingress_received.wait(_WAIT_SECONDS)
            assert parent.frame_types[:2] == [FrameType.READY, FrameType.INGRESS]
            assert not harness.response_received.wait(0.2)
            assert not parent.anchored.is_set()
            parent.release_commit.set()
            assert parent.anchored.wait(_WAIT_SECONDS)
            assert harness.response_received.wait(_WAIT_SECONDS)
            response = harness.response_frame()
            assert json.loads(response.payload)["code"] == HTTPStatus.OK
            records = tuple(service.state.by_acceptance_id.values())
            assert len(records) == 1
            anchor = FileAnchor(tmp_path / "anchor.json").read()
            assert anchor.sequence == records[0].acceptance.journal_sequence == 1
            assert anchor.frame_hash == records[0].acceptance.journal_frame_hash
            assert harness.connection_count == 1
            assert parent.accept_invocations == 1
            assert parent.execution_calls == []
        finally:
            parent.release_commit.set()
            process.terminate()
            process.wait(timeout=2)
            parent.close()
            service.close()
            writer.close()


@pytest.mark.integration
def test_message_wire_response_waits_for_durable_parent_ack(tmp_path: Path) -> None:
    _assert_wire_response_waits_for_durable_parent_ack(tmp_path, event_kind="message")


@pytest.mark.integration
def test_card_action_wire_response_waits_for_durable_parent_ack(tmp_path: Path) -> None:
    _assert_wire_response_waits_for_durable_parent_ack(tmp_path, event_kind="card")


@pytest.mark.integration
def test_oversized_single_frame_is_wire_500_with_zero_durable_side_effects(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    payload["event"]["message"]["content"] = "x" * (256 * 1024 + 1)
    try:
        with _bridge_delivery(tmp_path, service, payload) as (harness, parent, _process):
            assert parent.ready.wait(_WAIT_SECONDS)
            assert harness.response_received.wait(_WAIT_SECONDS)
            assert json.loads(harness.response_frame().payload)["code"] == 500
            assert not parent.ingress_received.is_set()
            assert service.state.by_acceptance_id == {}
            assert tuple(writer.replay()) == ()
            assert parent.accept_invocations == 0
            assert parent.execution_calls == []
            assert not (parent.ready.is_set() and bool(service.state.by_acceptance_id))
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_event_pipe_backpressure_fails_wire_within_total_ack_budget(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    payload["event"]["message"]["content"] = json.dumps(
        {"text": "x" * (200 * 1024)}, separators=(",", ":")
    )
    bootstrap_r, bootstrap_w = os.pipe()
    control_r, control_w = os.pipe()
    event_r, event_w = os.pipe()
    with _WireHarness(tmp_path, payload) as harness:
        parent = _ReadyOnlyParent(event_fd=event_r, control_fd=control_w)
        process = _start_production_bridge(
            harness,
            parent,  # type: ignore[arg-type]
            bootstrap_w=bootstrap_w,
            bootstrap_r=bootstrap_r,
            control_r=control_r,
            event_w=event_w,
        )
        try:
            assert parent.ready.wait(_WAIT_SECONDS)
            started = time.monotonic()
            assert harness.response_received.wait(2.0)
            elapsed = time.monotonic() - started
            assert json.loads(harness.response_frame().payload)["code"] == 500
            assert elapsed < 2.0
            assert parent.frame_types == [FrameType.READY]
            assert service.state.by_acceptance_id == {}
            assert tuple(writer.replay()) == ()
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
            parent.close()
            service.close()
            writer.close()


@pytest.mark.integration
def test_parent_close_unblocks_pending_callback_and_replay_converges(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery(tmp_path, service, payload) as (
            first_wire,
            first_parent,
            _first_process,
        ):
            assert first_parent.ready.wait(_WAIT_SECONDS)
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_parent.abort()
            assert first_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(first_wire.response_frame().payload)["code"] == 500
            assert first_parent.accept_invocations == 0
            assert service.state.by_acceptance_id == {}

        with _bridge_delivery(tmp_path, service, payload) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is False

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_stop_during_pending_callback_then_generation_rotation_is_fenced(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery(tmp_path, service, payload, generation=3) as (
            first_wire,
            first_parent,
            _first_process,
        ):
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_parent.stop_pending()
            assert first_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(first_wire.response_frame().payload)["code"] == 500
            assert first_parent.accept_invocations == 0

        with _bridge_delivery(tmp_path, service, payload, generation=4) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.channel_generation == 4
            assert replay_parent.accepted_ack.duplicate is False

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_blob_publish_then_anchor_failure_is_wire_500_and_fail_closed_on_replay(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path, fail_anchor=True)
    payload = _message_payload()
    try:
        parents: list[_ParentDurableAck] = []
        for _ in range(2):
            with _bridge_delivery(tmp_path, service, payload) as (
                wire,
                parent,
                _process,
            ):
                parents.append(parent)
                assert parent.ingress_received.wait(_WAIT_SECONDS)
                parent.release_commit.set()
                assert parent.failed.wait(_WAIT_SECONDS)
                assert wire.response_received.wait(_WAIT_SECONDS)
                assert json.loads(wire.response_frame().payload)["code"] == 500
                assert parent.accepted_ack is None

        assert service.state.by_acceptance_id == {}
        assert FileAnchor(tmp_path / "anchor.json").read().sequence == 0
        assert tuple(service.blob_store.iter_blob_ids()) == ()
        assert len(tuple((tmp_path / "ingress-blobs" / "quarantine").iterdir())) == 2
        assert [parent.accept_invocations for parent in parents] == [1, 1]
        assert all(parent.execution_calls == [] for parent in parents)
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_sdk_500_after_lost_ack_redelivers_to_one_duplicate_acceptance(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery(tmp_path, service, payload, write_ack=False) as (
            first_wire,
            first_parent,
            _first_process,
        ):
            assert first_parent.ready.wait(_WAIT_SECONDS)
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            started = time.monotonic()
            first_parent.release_commit.set()
            assert first_parent.anchored.wait(_WAIT_SECONDS)
            assert first_wire.response_received.wait(_WAIT_SECONDS)
            elapsed = time.monotonic() - started
            assert json.loads(first_wire.response_frame().payload)["code"] == 500
            assert 1.2 <= elapsed < 2.0
            assert first_parent.accepted_ack.duplicate is False

        with _bridge_delivery(tmp_path, service, payload) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ready.wait(_WAIT_SECONDS)
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is True

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.accept_invocations == 1
        assert replay_parent.accept_invocations == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


def _assert_post_anchor_parent_ack_failure_replays_duplicate(
    tmp_path: Path,
    *,
    failure: str,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    first_kwargs = {
        "fail_ack_encode": failure == "encode",
        "fail_ack_write": failure == "write",
    }
    try:
        with _bridge_delivery(
            tmp_path,
            service,
            payload,
            **first_kwargs,
        ) as (first_wire, first_parent, _first_process):
            assert first_parent.ready.wait(_WAIT_SECONDS)
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_parent.release_commit.set()
            assert first_parent.anchored.wait(_WAIT_SECONDS)
            assert first_parent.failed.wait(_WAIT_SECONDS)
            assert first_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(first_wire.response_frame().payload)["code"] == 500
            assert first_parent.accepted_ack.duplicate is False
            first_acceptance = first_parent.accepted_ack.acceptance

        with _bridge_delivery(tmp_path, service, payload, generation=4) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ready.wait(_WAIT_SECONDS)
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is True
            assert replay_parent.accepted_ack.acceptance == first_acceptance

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.accept_invocations == replay_parent.accept_invocations == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_post_anchor_ack_encode_failure_is_wire_500_then_duplicate_replay(
    tmp_path: Path,
) -> None:
    _assert_post_anchor_parent_ack_failure_replays_duplicate(
        tmp_path,
        failure="encode",
    )


@pytest.mark.integration
def test_post_anchor_control_pipe_write_failure_is_wire_500_then_duplicate_replay(
    tmp_path: Path,
) -> None:
    _assert_post_anchor_parent_ack_failure_replays_duplicate(
        tmp_path,
        failure="write",
    )


@pytest.mark.integration
def test_post_callback_sdk_write_failure_has_no_wire_success_then_duplicate_replay(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    bootstrap_r, bootstrap_w = os.pipe()
    control_r, control_w = os.pipe()
    event_r, event_w = os.pipe()
    try:
        with _WireHarness(tmp_path, payload, send_event_once=True) as first_wire:
            first_parent = _ParentDurableAck(
                event_fd=event_r,
                control_fd=control_w,
                service=service,
            )
            first_process = _start_production_bridge(
                first_wire,
                first_parent,
                bootstrap_w=bootstrap_w,
                bootstrap_r=bootstrap_r,
                control_r=control_r,
                event_w=event_w,
                fail_sdk_write=True,
            )
            try:
                ready = first_parent.ready.wait(_WAIT_SECONDS)
                diagnostic = b""
                if (
                    not ready
                    and first_process.poll() is not None
                    and first_process.stderr is not None
                ):
                    diagnostic = first_process.stderr.read()
                assert ready, (
                    first_process.poll(),
                    diagnostic.decode(errors="replace"),
                )
                assert first_parent.ingress_received.wait(_WAIT_SECONDS)
                first_parent.release_commit.set()
                assert first_parent.anchored.wait(_WAIT_SECONDS)
                assert first_parent.ack_written.wait(_WAIT_SECONDS)
                assert not first_wire.response_received.wait(0.5)
                assert first_parent.accepted_ack.duplicate is False
                first_acceptance = first_parent.accepted_ack.acceptance
                assert first_parent.frame_types[:2] == [
                    FrameType.READY,
                    FrameType.INGRESS,
                ]
            finally:
                if first_process.poll() is None:
                    first_process.terminate()
                first_process.wait(timeout=2)
                first_parent.close()
                assert first_parent.ingress_before_ready is False
                assert first_parent.execution_calls == []

        with _bridge_delivery(tmp_path, service, payload, generation=4) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ready.wait(_WAIT_SECONDS)
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is True
            assert replay_parent.accepted_ack.acceptance == first_acceptance

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.accept_invocations == replay_parent.accept_invocations == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_anchor_success_then_projection_apply_failure_replays_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    original_apply = service._apply_frame_unlocked
    apply_calls = 0

    def fail_first_apply(result: object) -> None:
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 1:
            raise RuntimeError("injected projection apply failure")
        original_apply(result)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_apply_frame_unlocked", fail_first_apply)
    try:
        with _bridge_delivery(tmp_path, service, payload) as (
            first_wire,
            first_parent,
            _first_process,
        ):
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_parent.release_commit.set()
            assert first_parent.failed.wait(_WAIT_SECONDS)
            assert first_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(first_wire.response_frame().payload)["code"] == 500
            assert first_parent.accepted_ack is None
            assert service.state.by_acceptance_id == {}
            assert writer.anchor.read().sequence == 1

        with _bridge_delivery(tmp_path, service, payload) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is True

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_callback_timeout_precedes_late_parent_commit_and_replay_is_duplicate(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery(tmp_path, service, payload) as (
            first_wire,
            first_parent,
            _first_process,
        ):
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            started = time.monotonic()
            assert first_wire.response_received.wait(_WAIT_SECONDS)
            elapsed = time.monotonic() - started
            assert json.loads(first_wire.response_frame().payload)["code"] == 500
            assert 1.2 <= elapsed < 2.0
            assert first_parent.accept_invocations == 0
            first_parent.release_commit.set()
            assert first_parent.anchored.wait(_WAIT_SECONDS)
            assert first_parent.ack_written.wait(_WAIT_SECONDS)
            assert first_parent.accepted_ack.duplicate is False

        with _bridge_delivery(tmp_path, service, payload) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is True
            assert (
                replay_parent.accepted_ack.acceptance
                == first_parent.accepted_ack.acceptance
            )

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_disconnect_before_ack_prevents_wire_success_and_replay_is_duplicate(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery(
            tmp_path,
            service,
            payload,
            harness_type=_DisconnectBeforeAckHarness,
        ) as (first_wire, first_parent, _first_process):
            assert isinstance(first_wire, _DisconnectBeforeAckHarness)
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_wire.close_requested.set()
            assert first_wire.connection_closed.wait(_WAIT_SECONDS)
            assert first_wire.second_connection.wait(_WAIT_SECONDS)
            first_parent.release_commit.set()
            assert first_parent.anchored.wait(_WAIT_SECONDS)
            assert first_parent.ack_written.wait(_WAIT_SECONDS)
            assert not first_wire.response_received.wait(0.5)
            assert first_wire.second_response_received.wait(_WAIT_SECONDS)
            assert json.loads(first_wire.response_frame().payload)["code"] == 200
            assert [ack.duplicate for ack in first_parent.accepted_acks] == [
                False,
                True,
            ]
            assert (
                first_parent.accepted_acks[0].acceptance
                == first_parent.accepted_acks[1].acceptance
            )
            assert len(first_parent.ingress_frames) == 2
            old_frame, new_frame = first_parent.ingress_frames
            assert old_frame.payload["request_id"] != new_frame.payload["request_id"]
            assert old_frame.payload["connection_id"] != new_frame.payload["connection_id"]
            old_metadata = EmployeeIngressMetadata.from_dict(old_frame.payload["metadata"])
            new_metadata = EmployeeIngressMetadata.from_dict(new_frame.payload["metadata"])
            assert old_metadata.envelope_id == new_metadata.envelope_id
            assert old_metadata.dedup_key == new_metadata.dedup_key

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_child_crash_after_mailbox_ack_before_callback_return_replays_duplicate(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery_with_post_ack_barrier(
            tmp_path, service, payload
        ) as (first_wire, first_parent, first_process, notify_r, _release_w):
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_parent.release_commit.set()
            assert first_parent.ack_written.wait(_WAIT_SECONDS)
            readable, _, _ = select.select([notify_r], [], [], _WAIT_SECONDS)
            assert readable == [notify_r]
            assert os.read(notify_r, 1) == b"1"
            first_process.terminate()
            first_process.wait(timeout=2)
            assert not first_wire.response_received.is_set()
            assert first_parent.accepted_ack.duplicate is False

        with _bridge_delivery(tmp_path, service, payload, generation=4) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is True
            assert (
                replay_parent.accepted_ack.acceptance
                == first_parent.accepted_ack.acceptance
            )

        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_child_crash_before_parent_commit_has_no_success_and_replay_converges(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    try:
        with _bridge_delivery(tmp_path, service, payload) as (
            first_wire,
            first_parent,
            first_process,
        ):
            assert first_parent.ready.wait(_WAIT_SECONDS)
            assert first_parent.ingress_received.wait(_WAIT_SECONDS)
            first_process.terminate()
            first_process.wait(timeout=2)
            assert not first_wire.response_received.is_set()
            assert service.state.by_acceptance_id == {}

        accepted_after_cleanup = len(service.state.by_acceptance_id)
        assert accepted_after_cleanup in {0, 1}

        with _bridge_delivery(tmp_path, service, payload) as (
            replay_wire,
            replay_parent,
            _replay_process,
        ):
            assert replay_parent.ready.wait(_WAIT_SECONDS)
            assert replay_parent.ingress_received.wait(_WAIT_SECONDS)
            replay_parent.release_commit.set()
            assert replay_parent.ack_written.wait(_WAIT_SECONDS)
            assert replay_wire.response_received.wait(_WAIT_SECONDS)
            assert json.loads(replay_wire.response_frame().payload)["code"] == 200
            assert replay_parent.accepted_ack.duplicate is bool(accepted_after_cleanup)
        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert first_parent.execution_calls == replay_parent.execution_calls == []
    finally:
        service.close()
        writer.close()


@pytest.mark.integration
def test_wire_200_then_worker_restart_returns_duplicate_ack_once(
    tmp_path: Path,
) -> None:
    service, writer = _service(tmp_path)
    payload = _message_payload()
    acknowledgements = []
    try:
        for _ in range(2):
            with _bridge_delivery(tmp_path, service, payload) as (
                wire,
                parent,
                _process,
            ):
                assert parent.ready.wait(_WAIT_SECONDS)
                assert parent.ingress_received.wait(_WAIT_SECONDS)
                parent.release_commit.set()
                assert parent.ack_written.wait(_WAIT_SECONDS)
                assert wire.response_received.wait(_WAIT_SECONDS)
                assert json.loads(wire.response_frame().payload)["code"] == 200
                acknowledgements.append(parent.accepted_ack)
        assert [ack.duplicate for ack in acknowledgements] == [False, True]
        assert acknowledgements[0].acceptance == acknowledgements[1].acceptance
        assert len(service.state.by_acceptance_id) == 1
        assert len(tuple(writer.replay())) == 1
        assert parent.execution_calls == []
    finally:
        service.close()
        writer.close()
