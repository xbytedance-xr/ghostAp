"""Sandbox Runner - process-isolated execution with timeout and process-group kill.

Provides bubblewrap detection, subprocess execution in a new process group,
and reliable timeout handling via process-group SIGKILL.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RunResult:
    """Result of a sandboxed process execution."""

    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    elapsed_seconds: float = 0.0
    pid: int = 0


class SandboxRunner:
    """Runs subprocesses in a new process group with configurable timeout.

    On timeout, sends SIGKILL to the entire process group to ensure
    all descendant processes are terminated.

    Optionally uses bubblewrap (bwrap) for filesystem/network isolation
    if available on the system.
    """

    def __init__(self, use_bwrap: bool = False, bwrap_args: Optional[list[str]] = None):
        """Initialize SandboxRunner.

        Args:
            use_bwrap: Whether to attempt bubblewrap sandboxing.
            bwrap_args: Additional bwrap arguments for filesystem binding.
        """
        self._use_bwrap = use_bwrap
        self._bwrap_args = bwrap_args or []

    def probe(self) -> dict:
        """Check whether sandbox capabilities are available.

        Returns:
            Dict with availability info:
            - bwrap_available: bool - whether bubblewrap is installed
            - process_group_available: bool - always True on Linux
            - bwrap_path: str - path to bwrap binary if found
        """
        bwrap_path = shutil.which("bwrap")
        return {
            "bwrap_available": bwrap_path is not None,
            "process_group_available": True,
            "bwrap_path": bwrap_path or "",
        }

    def run(
        self,
        argv: list[str],
        timeout: float = 60.0,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stdin_data: Optional[bytes] = None,
    ) -> RunResult:
        """Run a command in a sandboxed subprocess with process-group kill on timeout.

        Args:
            argv: Command and arguments to execute.
            timeout: Maximum execution time in seconds.
            cwd: Working directory for the subprocess.
            env: Environment variables (if None, inherits minimal env).
            stdin_data: Data to pass to stdin (if any).

        Returns:
            RunResult with output, exit code, and timeout flag.
        """
        if not argv:
            return RunResult(returncode=-1, stderr="Empty argv")

        # Build the actual command (optionally wrap with bwrap)
        effective_argv = self._build_argv(argv)

        # Build a restricted environment if none provided
        effective_env = env if env is not None else self._minimal_env()

        start = time.time()
        try:
            proc = subprocess.Popen(
                effective_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=effective_env,
                start_new_session=True,  # creates new process group
            )
        except (OSError, ValueError) as exc:
            return RunResult(
                returncode=-1,
                stderr=f"Failed to start process: {exc}",
                elapsed_seconds=time.time() - start,
            )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=stdin_data, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill entire process group
            self._kill_process_group(proc.pid)
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_bytes, stderr_bytes = proc.communicate()

        elapsed = time.time() - start

        return RunResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else "",
            stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else "",
            timed_out=timed_out,
            elapsed_seconds=elapsed,
            pid=proc.pid,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_argv(self, argv: list[str]) -> list[str]:
        """Optionally wrap argv with bwrap for sandboxing."""
        if not self._use_bwrap:
            return argv

        bwrap_path = shutil.which("bwrap")
        if not bwrap_path:
            # Fall back to direct execution if bwrap unavailable
            return argv

        # Minimal bwrap: read-only root, private /tmp, no network
        bwrap_cmd = [
            bwrap_path,
            "--ro-bind", "/", "/",
            "--tmpfs", "/tmp",
            "--unshare-net",
            "--die-with-parent",
            *self._bwrap_args,
            "--",
            *argv,
        ]
        return bwrap_cmd

    def _minimal_env(self) -> dict[str, str]:
        """Create a minimal safe environment for sandboxed execution."""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        }

    @staticmethod
    def _kill_process_group(pid: int) -> None:
        """Kill the entire process group rooted at pid."""
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # Process already dead or we lack permission
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
