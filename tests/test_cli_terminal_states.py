"""Regression tests for CLI backend terminal-state propagation."""

from __future__ import annotations

import io
import threading
import time
from unittest.mock import patch

import pytest

from src.agent_session.claude_cli import ClaudeCLIConfig, SyncClaudeCLISession
from src.agent_session.ttadk_cli import SyncTTADKCLISession


class _CompletedProcess:
    def __init__(self, *, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float = 0) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


class _DelayedOutputProcess:
    def __init__(self, delay: float = 0.05) -> None:
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self._released = threading.Event()
        self._delay = delay
        self.stdout = self._stdout()

    def _stdout(self):
        self._released.wait(self._delay)
        if self.returncode is None:
            yield "late output\n"

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float = 0) -> int:
        if self.returncode is None:
            self.returncode = 0
        self._released.set()
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self._released.set()


class _CancelOnOutputProcess(_DelayedOutputProcess):
    def __init__(self, session: SyncClaudeCLISession) -> None:
        self._session = session
        super().__init__(delay=0)

    def _stdout(self):
        self._session._cancel_event.set()
        yield "partial output\n"


class _SilentTTADKProcess:
    def __init__(self, natural_exit_after: float = 0.5) -> None:
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self.terminate_calls = 0
        self._closed = threading.Event()
        self._natural_exit_after = natural_exit_after
        self.stdout = self._stdout()

    def _stdout(self):
        if not self._closed.wait(self._natural_exit_after):
            self.returncode = 0
            self._closed.set()
        if False:
            yield ""

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float = 0) -> int:
        if self.returncode is None:
            self._closed.wait(timeout)
        if self.returncode is None:
            raise TimeoutError("process did not stop")
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15
        self._closed.set()


def _claude_session() -> SyncClaudeCLISession:
    session = SyncClaudeCLISession(
        cwd="/tmp",
        config=ClaudeCLIConfig(
            command="claude",
            add_dir=False,
            bypass_permissions=False,
        ),
    )
    session.session_id = "session-1"
    return session


def test_claude_timeout_is_not_reported_as_cancelled() -> None:
    session = _claude_session()
    process = _DelayedOutputProcess()

    with patch(
        "src.agent_session.claude_cli.subprocess.Popen",
        return_value=process,
    ):
        result = session.send_prompt("work", timeout=0.01)

    assert result.stop_reason == "timeout"
    assert "超时" in result.text


@pytest.mark.parametrize("terminal_state", ["cancelled", "timeout"])
def test_claude_fresh_retry_propagates_second_terminal_state(
    terminal_state: str,
) -> None:
    session = _claude_session()
    session.is_resumed = True
    missing = _CompletedProcess(
        stdout="",
        stderr="No conversation found with session ID: session-1\n",
        returncode=1,
    )
    retry = (
        _CancelOnOutputProcess(session)
        if terminal_state == "cancelled"
        else _DelayedOutputProcess()
    )

    with (
        patch(
            "src.agent_session.claude_cli.subprocess.Popen",
            side_effect=[missing, retry],
        ),
        patch(
            "src.agent_session.claude_cli.uuid.uuid4",
            return_value="fresh-session",
        ),
    ):
        result = session.send_prompt("work", timeout=0.01)

    assert result.stop_reason == terminal_state


def test_ttadk_silent_process_is_terminated_at_timeout() -> None:
    session = SyncTTADKCLISession(agent_type="ttadk_codex", cwd="/tmp")
    session.session_id = "session-1"
    process = _SilentTTADKProcess()

    with (
        patch(
            "src.agent_session.ttadk_cli.build_ttadk_subprocess_env",
            return_value=({}, None),
        ),
        patch(
            "src.agent_session.ttadk_cli.subprocess.Popen",
            return_value=process,
        ),
    ):
        started_at = time.monotonic()
        result = session.send_prompt("work", timeout=0.01)
        elapsed = time.monotonic() - started_at

    assert result.stop_reason == "timeout"
    assert process.terminate_calls == 1
    assert elapsed < process._natural_exit_after
