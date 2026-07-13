from __future__ import annotations

import hashlib
import os
import textwrap
import time
from pathlib import Path

import pytest

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
