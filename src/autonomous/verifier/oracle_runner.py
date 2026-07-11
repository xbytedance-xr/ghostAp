"""Oracle Runner - deterministic verification runner for acceptance criteria.

This runner executes oracle commands in an isolated sandbox with strict timeouts.
It uses the SandboxRunner for process-group kill semantics and does NOT
import the journal, broker, or any mutable state managers.

SECURITY: Oracle runner operates with a different identity than the execution
runtime. It must not have access to the execution context or model calls.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from ..runtime.runner import SandboxRunner


@dataclass
class OracleResult:
    """Result of an oracle verification run."""

    criterion_id: str = ""
    passed: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    elapsed_seconds: float = 0.0
    output_hash: str = ""
    error: str = ""


class OracleRunner:
    """Runs oracle verification commands in a sandboxed subprocess.

    Each oracle command:
    - Runs in a new process group (kill-on-timeout kills descendants)
    - Has a strict timeout (default 120s)
    - Is executed with minimal environment variables
    - Produces a deterministic output hash for auditability

    This runner does NOT access the journal, model broker, or any
    internal mutable state. It is purely a command executor with
    isolation guarantees.
    """

    def __init__(
        self,
        timeout: float = 120.0,
        use_bwrap: bool = False,
        working_dir: str = "/tmp",
    ):
        """Initialize OracleRunner.

        Args:
            timeout: Maximum execution time per oracle command (seconds).
            use_bwrap: Whether to use bubblewrap sandboxing.
            working_dir: Default working directory for oracle commands.
        """
        self._timeout = timeout
        self._working_dir = working_dir
        self._sandbox = SandboxRunner(use_bwrap=use_bwrap)

    def probe(self) -> dict:
        """Check sandbox availability for oracle execution."""
        sandbox_info = self._sandbox.probe()
        return {
            **sandbox_info,
            "oracle_timeout": self._timeout,
            "working_dir": self._working_dir,
        }

    def run_oracle(
        self,
        criterion_id: str,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> OracleResult:
        """Run an oracle command and determine pass/fail from exit code.

        A command passes if its exit code is 0.
        All output is hashed for deterministic audit trails.

        Args:
            criterion_id: ID of the criterion being verified.
            command: Shell command to execute.
            cwd: Working directory (defaults to self._working_dir).
            env: Environment override (defaults to minimal env).
            timeout: Override timeout for this specific oracle.

        Returns:
            OracleResult with pass/fail, output, and hash.
        """
        if not command:
            return OracleResult(
                criterion_id=criterion_id,
                passed=False,
                error="Empty command",
            )

        effective_timeout = timeout or self._timeout
        effective_cwd = cwd or self._working_dir

        # Run via sandbox runner (process-group kill on timeout)
        run_result = self._sandbox.run(
            argv=["sh", "-c", command],
            timeout=effective_timeout,
            cwd=effective_cwd,
            env=env,
        )

        # Compute output hash for audit
        output_content = f"{run_result.stdout}{run_result.stderr}"
        output_hash = hashlib.sha256(output_content.encode()).hexdigest()[:16]

        return OracleResult(
            criterion_id=criterion_id,
            passed=(run_result.returncode == 0 and not run_result.timed_out),
            exit_code=run_result.returncode,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            timed_out=run_result.timed_out,
            elapsed_seconds=run_result.elapsed_seconds,
            output_hash=output_hash,
        )

    def run_batch(
        self,
        oracles: list[dict],
        cwd: Optional[str] = None,
    ) -> list[OracleResult]:
        """Run multiple oracle commands sequentially.

        Args:
            oracles: List of dicts with keys: criterion_id, command, timeout (optional).
            cwd: Shared working directory.

        Returns:
            List of OracleResult in input order.
        """
        results = []
        for oracle in oracles:
            result = self.run_oracle(
                criterion_id=oracle.get("criterion_id", ""),
                command=oracle.get("command", ""),
                cwd=cwd,
                timeout=oracle.get("timeout"),
            )
            results.append(result)
        return results
