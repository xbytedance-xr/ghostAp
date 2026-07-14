from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.autonomous.supervisor.employee_channels import (
    ChannelProcessState,
    ChannelSandboxUnavailable,
    EmployeeChannelSupervisor,
    SandboxAttestation,
)


def _worker(tmp_path: Path) -> Path:
    path = tmp_path / "security_worker.py"
    path.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            bootstrap_fd, control_fd, event_fd = map(int, sys.argv[1:])
            bootstrap = json.loads(os.fdopen(bootstrap_fd, 'rb', buffering=0).readline())
            frame = {'v': 1, 'type': 'READY', 'agent_id': bootstrap['agent_id'],
                     'generation': bootstrap['generation'], 'sequence': 1,
                     'payload': {'identity': {'app_id': bootstrap['app_id']},
                                 'environment': dict(os.environ)}}
            os.write(event_fd, (json.dumps(frame, separators=(',', ':')) + '\\n').encode())
            control = os.fdopen(control_fd, 'rb', buffering=0)
            control.readline()
            """
        ),
        encoding="utf-8",
    )
    return path


def _accepted(pid: int) -> SandboxAttestation:
    return SandboxAttestation(pid=pid, verified=True, mechanism="test-fixture")


def test_default_sandbox_attestation_fails_closed_before_secret_resolution(tmp_path: Path) -> None:
    resolved = False

    def resolve(*_args: str) -> str:
        nonlocal resolved
        resolved = True
        return "must-not-be-delivered"

    supervisor = EmployeeChannelSupervisor(
        secret_resolver=resolve,
        worker_path=_worker(tmp_path),
        ready_timeout=0.5,
        sandbox_prefix=(),
    )
    try:
        with pytest.raises(ChannelSandboxUnavailable):
            supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)
        assert resolved is False
        assert supervisor.status("agt_1").state is ChannelProcessState.FAILED
        assert supervisor.status("agt_1").sandbox.verified is False
    finally:
        supervisor.close()


def test_default_launcher_prefers_bwrap_and_falls_back_to_process_isolation(
    tmp_path: Path,
) -> None:
    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: "employee-secret",
        worker_path=_worker(tmp_path),
        ready_timeout=1.0,
    )
    try:
        status = supervisor.start(
            "agt_1", "cli_1", "cred_1", 1, lambda _: None
        )
        assert status.state is ChannelProcessState.READY
        assert status.sandbox is not None
        if status.sandbox.verified:
            assert status.sandbox.mechanism == "bwrap-filesystem"
        else:
            assert status.sandbox.mechanism == "process-fallback"
    finally:
        supervisor.close()


def test_secret_and_parent_environment_never_enter_argv_or_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "employee-secret-sentinel"
    monkeypatch.setenv("AUTONOMOUS_VAULT_MASTER_KEY", "master-key-sentinel")
    launches: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def launch(argv, **kwargs):
        launches.append((tuple(argv), dict(kwargs)))
        return subprocess.Popen(argv, **kwargs)

    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: secret,
        worker_path=_worker(tmp_path),
        launcher=launch,
        sandbox_attestor=_accepted,
        ready_timeout=1.0,
    )
    try:
        status = supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)

        argv, kwargs = launches[0]
        joined = " ".join(argv)
        assert argv[:2] == (sys.executable, "-I")
        assert Path(argv[2]).is_absolute()
        assert secret not in joined
        assert "cred_1" not in joined
        assert kwargs["close_fds"] is True
        assert len(kwargs["pass_fds"]) == 3
        assert kwargs["env"] == {"PYTHONUTF8": "1"}
        child_env = status.ready_metadata["environment"]
        assert child_env["PYTHONUTF8"] == "1"
        assert "AUTONOMOUS_VAULT_MASTER_KEY" not in child_env
        assert set(child_env) <= {"PYTHONUTF8", "LC_CTYPE"}
    finally:
        supervisor.close()


def test_status_and_failures_never_echo_secret(tmp_path: Path) -> None:
    secret = "do-not-echo-this-secret"

    def broken_attestor(pid: int) -> SandboxAttestation:
        return SandboxAttestation(pid=pid, verified=False, mechanism="none")

    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: secret,
        worker_path=_worker(tmp_path),
        sandbox_attestor=broken_attestor,
    )
    try:
        with pytest.raises(ChannelSandboxUnavailable) as raised:
            supervisor.start("agt_1", "cli_1", "cred_secret_name", 1, lambda _: None)
        assert secret not in str(raised.value)
        assert "cred_secret_name" not in repr(supervisor.status("agt_1"))
    finally:
        supervisor.close()


def test_send_payload_rejects_credential_material_before_ipc(tmp_path: Path) -> None:
    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: "employee-secret",
        worker_path=_worker(tmp_path),
        sandbox_attestor=_accepted,
        ready_timeout=1.0,
    )
    try:
        supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)
        with pytest.raises(ValueError, match="unsafe send payload"):
            supervisor.send(
                "agt_1",
                generation=1,
                target="ou_requester",
                message={"app_secret": "must-not-cross-ipc"},
            )
    finally:
        supervisor.close()


def test_launcher_failure_closes_every_parent_and_child_pipe_fd(tmp_path: Path) -> None:
    before = len(tuple(Path("/proc/self/fd").iterdir()))

    def fail_launch(*_args, **_kwargs):
        raise OSError("injected launcher failure")

    supervisor = EmployeeChannelSupervisor(
        secret_resolver=lambda *_: "must-not-resolve",
        worker_path=_worker(tmp_path),
        launcher=fail_launch,
        sandbox_attestor=_accepted,
    )
    with pytest.raises(RuntimeError, match="launch failed"):
        supervisor.start("agt_1", "cli_1", "cred_1", 1, lambda _: None)

    assert len(tuple(Path("/proc/self/fd").iterdir())) == before
    supervisor.close()
