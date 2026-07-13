from __future__ import annotations

import hashlib
import os
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.autonomous.ingress.projection import IngressProjectionState
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.journal.anchor import MemoryAnchor
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.provisioning.channel_protocol import (
    ChannelFrame,
    FrameType,
    decode_frame,
)
from src.autonomous.supervisor import employee_channels as employee_channel_module
from src.autonomous.supervisor.employee_channels import (
    ChannelProcessState,
    DesiredEmployeeChannel,
    EmployeeChannelSupervisor,
    SandboxAttestation,
)


def _accepted_attestation(pid: int) -> SandboxAttestation:
    return SandboxAttestation(
        pid=pid,
        verified=True,
        mechanism="test-fixture",
        details=("fixture process contains no production credential source",),
    )


def _worker(tmp_path: Path, *, behavior: str = "normal") -> Path:
    path = tmp_path / f"fixture_worker_{behavior}.py"
    path.write_text(
        textwrap.dedent(
            f"""
            import hashlib
            import json
            import os
            import sys
            import time

            bootstrap_fd, control_fd, event_fd = map(int, sys.argv[1:])
            bootstrap_stream = os.fdopen(bootstrap_fd, 'rb', buffering=0)
            bootstrap = json.loads(bootstrap_stream.readline())
            agent_id = bootstrap['agent_id']
            generation = bootstrap['generation']
            secret_digest = hashlib.sha256(bootstrap['app_secret'].encode()).hexdigest()
            eof = bootstrap_stream.read(1) == b''
            bootstrap_stream.close()
            sequence = 0
            def emit(kind, payload, gen=None):
                global sequence
                sequence += 1
                frame = {{'v': 1, 'type': kind, 'agent_id': agent_id,
                         'generation': generation if gen is None else gen,
                         'sequence': sequence, 'payload': payload}}
                os.write(event_fd, (json.dumps(frame, separators=(',', ':')) + '\\n').encode())

            if {behavior!r} == 'timeout':
                time.sleep(5)
                raise SystemExit(0)
            if {behavior!r} == 'crash':
                emit('READY', {{'identity': {{'app_id': bootstrap['app_id']}},
                               'secret_digest': secret_digest, 'bootstrap_eof': eof}})
                time.sleep(0.1)
                raise SystemExit(23)
            if {behavior!r} == 'stale':
                emit('READY', {{'identity': {{'app_id': 'stale'}}}}, gen=generation - 1)
            emit('READY', {{'identity': {{'app_id': bootstrap['app_id']}},
                           'secret_digest': secret_digest, 'bootstrap_eof': eof,
                           'connection_id': 'conn-fixture',
                           'pid': os.getpid()}})
            emit('EVENT', {{'event': 'health-fixture', 'data': {{'pid': os.getpid()}}}})
            control = os.fdopen(control_fd, 'rb', buffering=0)
            while True:
                line = control.readline()
                if not line:
                    break
                frame = json.loads(line)
                if frame['type'] == 'STOP':
                    break
                if frame['type'] == 'SEND':
                    emit('HEALTH', {{'operation': 'send',
                                    'request_id': frame['payload']['request_id'],
                                    'success': True,
                                    'app_id': bootstrap['app_id'],
                                    'generation': generation,
                                    'connection_id': 'conn-fixture',
                                    'message_id': 'om-fixture-reply'}})
            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    return path


def _ingress_worker(tmp_path: Path) -> Path:
    path = tmp_path / "fixture_worker_ingress.py"
    path.write_text(
        textwrap.dedent(
            """
            import hashlib
            import json
            import os
            import sys

            bootstrap_fd, control_fd, event_fd = map(int, sys.argv[1:])
            bootstrap = json.loads(os.fdopen(bootstrap_fd, 'rb', buffering=0).readline())
            agent_id = bootstrap['agent_id']
            app_id = bootstrap['app_id']
            generation = bootstrap['generation']
            connection_id = 'conn_fixture'
            sequence = 0
            def emit(kind, body, *, outer_app=None):
                global sequence
                sequence += 1
                frame = {'v': 1, 'type': kind, 'agent_id': agent_id,
                         'generation': generation, 'sequence': sequence, 'payload': body}
                os.write(event_fd, (json.dumps(frame, separators=(',', ':')) + '\\n').encode())

            emit('READY', {'identity': {'app_id': app_id},
                           'connection_id': connection_id,
                           'connection': {'observed': True}})
            payload = {
                'schema_version': 1,
                'envelope_id': 'ing_' + '1' * 64,
                'normalized_parts': [{'type': 'text', 'text': 'durable'}],
                'attachment_descriptors': [],
            }
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()
            digest = hashlib.sha256(raw).hexdigest()
            metadata = {
                'schema_version': 1, 'envelope_id': payload['envelope_id'],
                'tenant_key': bootstrap['tenant_key'], 'agent_id': agent_id,
                'bot_principal_id': bootstrap['bot_principal_id'], 'app_id': app_id,
                'channel_generation': generation, 'connection_id': connection_id,
                'event_id': 'evt_fixture', 'message_id': 'om_fixture',
                'event_type': 'im.message.receive_v1', 'action_identity': '',
                'chat_id': 'oc_fixture', 'thread_root_message_id': '',
                'sender_principal_id': 'ou_requester',
                'received_at': '2026-07-13T00:00:00Z',
                'semantic_digest': digest, 'payload_sha256': digest,
                'payload_size_bytes': len(raw), 'attachment_count': 0,
                'attachment_total_bytes': 0,
            }
            emit('INGRESS', {'request_id': 'req_fixture', 'app_id': app_id,
                             'connection_id': connection_id, 'metadata': metadata,
                             'payload': payload, 'action_correlation': None})
            control = os.fdopen(control_fd, 'rb', buffering=0)
            while line := control.readline():
                frame = json.loads(line)
                if frame['type'] == 'INGRESS_ACK':
                    emit('HEALTH', {'operation': 'ingress-ack', 'success': True,
                                    'acceptance_id': frame['payload']['ack']['acceptance']['acceptance_id']})
                elif frame['type'] == 'STOP':
                    break
            """
        ),
        encoding="utf-8",
    )
    return path


def _partial_ingress_worker(tmp_path: Path) -> Path:
    path = tmp_path / "fixture_worker_partial_ingress.py"
    path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            bootstrap_fd, control_fd, event_fd = map(int, sys.argv[1:])
            bootstrap = json.loads(os.fdopen(bootstrap_fd, 'rb', buffering=0).readline())
            ready = {'v': 1, 'type': 'READY', 'agent_id': bootstrap['agent_id'],
                     'generation': bootstrap['generation'], 'sequence': 1,
                     'payload': {'identity': {'app_id': bootstrap['app_id']},
                                 'connection_id': 'conn_partial'}}
            os.write(event_fd, (json.dumps(ready, separators=(',', ':')) + '\\n').encode())
            partial = {'v': 1, 'type': 'INGRESS', 'agent_id': bootstrap['agent_id'],
                       'generation': bootstrap['generation'], 'sequence': 2,
                       'payload': {'request_id': 'req_partial'}}
            raw = json.dumps(partial, separators=(',', ':')).encode()
            os.write(event_fd, raw[:len(raw) // 2])
            """
        ),
        encoding="utf-8",
    )
    return path


def _ingress_service(tmp_path: Path) -> tuple[EmployeeIngressService, JournalWriter]:
    writer = JournalWriter.open(
        tmp_path / "ingress-journal",
        anchor=MemoryAnchor(),
        hmac_key=b"employee-channel-process-hmac-key-32",
        writer_epoch=1,
    )
    service = EmployeeIngressService(
        writer=writer,
        blob_store=BlobStore(
            tmp_path / "ingress-blobs",
            AesGcmEncryptionProvider(
                lambda _ref: b"employee-channel-process-key-32b"
            ),
        ),
        ingress_state=IngressProjectionState(),
        active_key_id="k1",
    )
    return service, writer


def _supervisor(tmp_path: Path, *, behavior: str = "normal", timeout: float = 2.0, events=None):
    secret = "fixture-secret-value"
    calls: list[tuple[str, str, str]] = []

    def resolve(credential_ref: str, agent_id: str, app_id: str) -> str:
        calls.append((credential_ref, agent_id, app_id))
        return secret

    supervisor = EmployeeChannelSupervisor(
        secret_resolver=resolve,
        worker_path=_worker(tmp_path, behavior=behavior),
        sandbox_attestor=_accepted_attestation,
        ready_timeout=timeout,
        stop_timeout=1.0,
    )
    return supervisor, secret, calls


def test_two_employees_get_distinct_fresh_processes_and_one_shot_credentials(tmp_path: Path) -> None:
    supervisor, secret, calls = _supervisor(tmp_path)
    try:
        first = supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)
        second = supervisor.start("agt_2", "cli_2", "cred_2", 1, lambda _: None)

        assert first.state is ChannelProcessState.READY
        assert second.state is ChannelProcessState.READY
        assert first.pid != second.pid != os.getpid()
        assert first.identity == {"app_id": "cli_1"}
        assert first.ready_metadata["secret_digest"] == hashlib.sha256(secret.encode()).hexdigest()
        assert first.ready_metadata["bootstrap_eof"] is True
        assert calls == [("cred_1", "agt_1", "cli_1"), ("cred_2", "agt_2", "cli_2")]
    finally:
        supervisor.close()


def test_ready_timeout_fails_closed_and_reaps_child(tmp_path: Path) -> None:
    supervisor, _secret, _calls = _supervisor(tmp_path, behavior="timeout", timeout=0.1)

    status = supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)

    assert status.state is ChannelProcessState.FAILED
    assert status.pid > 0
    assert status.error_code == "ready-timeout"
    assert supervisor.status("agt_1") == status
    supervisor.close()


def test_clean_stop_and_crash_detection(tmp_path: Path) -> None:
    supervisor, _secret, _calls = _supervisor(tmp_path)
    ready = supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)
    stopped = supervisor.stop("agt_1")
    assert ready.state is ChannelProcessState.READY
    assert stopped is not None and stopped.state is ChannelProcessState.STOPPED

    crashing, _secret, _calls = _supervisor(tmp_path, behavior="crash")
    try:
        crashing.start("agt_2", "cli_2", "cred_2", 1, lambda _: None)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = crashing.status("agt_2")
            if status is not None and status.state is ChannelProcessState.CRASHED:
                break
            time.sleep(0.02)
        assert status is not None and status.state is ChannelProcessState.CRASHED
        assert status.exit_code == 23
    finally:
        crashing.close()


def test_stale_generation_frames_are_rejected_and_events_are_delivered(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    supervisor, _secret, _calls = _supervisor(tmp_path, behavior="stale")
    try:
        status = supervisor.start("agt_1", "cli_1", "cred_1", 5, events.append)
        deadline = time.monotonic() + 1
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)

        assert status.state is ChannelProcessState.READY
        assert status.identity == {"app_id": "cli_1"}
        assert supervisor.status("agt_1").stale_frames == 1
        assert events == [{"event": "health-fixture", "data": {"pid": status.pid}}]
    finally:
        supervisor.close()


def test_recover_matches_desired_set_and_fences_generation_reuse(tmp_path: Path) -> None:
    supervisor, _secret, _calls = _supervisor(tmp_path)
    desired = [
        DesiredEmployeeChannel("agt_1", "cli_1", "cred_1", 2, lambda _: None),
        DesiredEmployeeChannel("agt_2", "cli_2", "cred_2", 4, lambda _: None),
    ]
    try:
        recovered = supervisor.recover(desired)
        assert set(recovered) == {"agt_1", "agt_2"}
        assert all(item.state is ChannelProcessState.READY for item in recovered.values())
        same = supervisor.start("agt_1", "cli_1", "cred_1", 2, lambda _: None)
        assert same.pid == recovered["agt_1"].pid
        supervisor.stop("agt_1")
        with pytest.raises(ValueError, match="generation"):
            supervisor.start("agt_1", "cli_1", "cred_1", 2, lambda _: None)
    finally:
        supervisor.close()


def test_send_is_generation_fenced_and_waits_for_employee_worker_receipt(
    tmp_path: Path,
) -> None:
    supervisor, _secret, _calls = _supervisor(tmp_path)
    try:
        supervisor.start("agt_1", "cli_1", "cred_1", 3, lambda _: None)

        receipt = supervisor.send(
            "agt_1",
            generation=3,
            target="ou_requester",
            message={"text": "status: ready"},
        )

        assert receipt.success is True
        assert receipt.request_id
        assert receipt.app_id == "cli_1"
        assert receipt.generation == 3
        assert receipt.connection_id == "conn-fixture"
        assert receipt.message_id == "om-fixture-reply"
        with pytest.raises(ValueError, match="generation"):
            supervisor.send(
                "agt_1",
                generation=2,
                target="ou_requester",
                message={"text": "stale"},
            )
    finally:
        supervisor.close()


def test_parent_supervisor_anchors_runtime_bound_ingress_before_ack(
    tmp_path: Path,
) -> None:
    service, writer = _ingress_service(tmp_path)
    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: "employee-secret",
        worker_path=_ingress_worker(tmp_path),
        sandbox_attestor=_accepted_attestation,
        ready_timeout=1.0,
        stop_timeout=1.0,
        ingress_service=service,
        ingress_binding_resolver=lambda agent_id, app_id: (
            "tenant-fixture",
            "bot_fixture",
        ),
    )
    try:
        status = supervisor.start("agt_fixture", "cli_fixture", "cred_1", 3, lambda _: None)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = supervisor.status("agt_fixture")
            if (
                status is not None
                and status.ready_metadata.get("health", {}).get("operation")
                == "ingress-ack"
            ):
                break
            time.sleep(0.01)

        assert status is not None
        assert "health" in status.ready_metadata, status
        assert status.ready_metadata["health"]["success"] is True
        records = tuple(service.state.by_acceptance_id.values())
        assert len(records) == 1
        assert records[0].metadata.trusted_worker_binding == (
            "tenant-fixture",
            "agt_fixture",
            "bot_fixture",
            "cli_fixture",
            3,
            "conn_fixture",
        )
        assert writer.anchor.read().sequence == records[0].acceptance.journal_sequence
    finally:
        supervisor.close()
        service.close()
        writer.close()


def test_partial_ingress_ipc_frame_crashes_without_acceptance_or_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, writer = _ingress_service(tmp_path)
    accept_calls = 0

    def reject_unexpected_accept(*_args: object, **_kwargs: object) -> object:
        nonlocal accept_calls
        accept_calls += 1
        raise AssertionError("partial ingress reached durable admission")

    monkeypatch.setattr(service, "accept", reject_unexpected_accept)
    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: "employee-secret",
        worker_path=_partial_ingress_worker(tmp_path),
        sandbox_attestor=_accepted_attestation,
        ready_timeout=1.0,
        stop_timeout=1.0,
        ingress_service=service,
        ingress_binding_resolver=lambda *_: ("tenant-fixture", "bot_fixture"),
    )
    try:
        supervisor.start("agt_partial", "cli_partial", "cred_1", 1, lambda _: None)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = supervisor.status("agt_partial")
            if status is not None and status.state is ChannelProcessState.CRASHED:
                break
            time.sleep(0.01)

        assert status is not None and status.state is ChannelProcessState.CRASHED
        assert status.exit_code == 0
        assert accept_calls == 0
        assert service.state.by_acceptance_id == {}
        assert writer.anchor.read().sequence == 0
        assert tuple(writer.replay()) == ()
    finally:
        supervisor.close()
        service.close()
        writer.close()


def test_parent_control_ack_stop_send_share_one_noninterleaving_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, writer = _ingress_service(tmp_path)
    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: "employee-secret",
        worker_path=_ingress_worker(tmp_path),
        sandbox_attestor=_accepted_attestation,
        ready_timeout=1.0,
        stop_timeout=1.0,
        ingress_service=service,
        ingress_binding_resolver=lambda *_: ("tenant-fixture", "bot_fixture"),
    )
    try:
        status = supervisor.start(
            "agt_fixture", "cli_fixture", "cred_1", 3, lambda _: None
        )
        deadline = time.monotonic() + 2
        while not service.state.by_acceptance_id and time.monotonic() < deadline:
            time.sleep(0.01)
        assert status.state is ChannelProcessState.READY
        record = next(iter(service.state.by_acceptance_id.values()))
        duplicate_ack = service.accept(
            record.metadata,
            service.get_payload(record.acceptance.acceptance_id),
            request_id="req_concurrent",
        )
        runtime = supervisor._runtimes["agt_fixture"]
        captured = bytearray()

        def capture_fragmented(_fd: int, raw: bytes) -> None:
            for octet in raw:
                captured.append(octet)
                time.sleep(0)

        monkeypatch.setattr(employee_channel_module, "_write_all", capture_fragmented)
        calls = (
            (
                FrameType.INGRESS_ACK,
                {
                    "request_id": duplicate_ack.request_id,
                    "app_id": duplicate_ack.app_id,
                    "connection_id": duplicate_ack.connection_id,
                    "ack": duplicate_ack.to_dict(),
                },
            ),
            (
                FrameType.SEND,
                {
                    "request_id": "send_concurrent",
                    "target": "ou_requester",
                    "message": {"text": "fixture"},
                    "options": None,
                },
            ),
            (FrameType.STOP, {}),
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = tuple(
                pool.submit(supervisor._send_control, runtime, kind, body)
                for kind, body in calls
            )
            results = tuple(future.result() for future in futures)

        frames = tuple(decode_frame(line + b"\n") for line in bytes(captured).splitlines())
        assert results == (True, True, True)
        assert {frame.frame_type for frame in frames} == {
            FrameType.INGRESS_ACK,
            FrameType.SEND,
            FrameType.STOP,
        }
        assert [frame.sequence for frame in frames] == sorted(
            frame.sequence for frame in frames
        )
        assert len({frame.sequence for frame in frames}) == 3
        assert supervisor.status("agt_fixture").state is ChannelProcessState.READY
    finally:
        monkeypatch.undo()
        supervisor.close()
        service.close()
        writer.close()


def test_reconnect_event_revokes_readiness_until_new_observed_ready(
    tmp_path: Path,
) -> None:
    supervisor, _secret, _calls = _supervisor(tmp_path)
    try:
        initial = supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)
        runtime = supervisor._runtimes["agt_1"]
        supervisor._accept_frame(
            runtime,
            ChannelFrame(
                FrameType.EVENT,
                "agt_1",
                1,
                3,
                {"event": "reconnecting", "data": {}},
            ),
        )

        reconnecting = supervisor.status("agt_1")
        assert initial.state is ChannelProcessState.READY
        assert reconnecting is not None
        assert reconnecting.state is ChannelProcessState.STARTING
        assert reconnecting.ready_at is None
        assert reconnecting.error_code == "channel-reconnecting"
        assert not runtime.ready.is_set()

        supervisor._accept_frame(
            runtime,
            ChannelFrame(
                FrameType.READY,
                "agt_1",
                1,
                4,
                {
                    "identity": {"app_id": "cli_1"},
                    "connection_id": "conn-fixture",
                    "connection": {
                        "observed": True,
                        "secure": True,
                        "sdk_connection_id": "dev-reconnected",
                    },
                },
            ),
        )

        recovered = supervisor.status("agt_1")
        assert recovered is not None
        assert recovered.state is ChannelProcessState.READY
        assert recovered.error_code == ""
        assert runtime.ready.is_set()
    finally:
        supervisor.close()
