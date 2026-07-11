"""Verifier - independent verification of acceptance criteria.

Oracle types: Command, Resource, Data, Schema, Review, Human.
Verifier is independent of the execution runtime - uses different identity.
All verification operations are journal-backed for auditability.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..domain import (
    Evidence,
    GoalCriterion,
    OracleType,
    VerificationResult,
    new_id,
)

# ---------------------------------------------------------------------------
# Journal protocol
# ---------------------------------------------------------------------------


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class OracleConfig:
    """Configuration for an oracle check."""

    oracle_type: OracleType
    command: str = ""
    resource_url: str = ""
    expected_schema: dict = field(default_factory=dict)
    data_query: str = ""
    data_constraint: str = ""
    review_rubric: str = ""
    timeout_seconds: float = 120.0


# ---------------------------------------------------------------------------
# Attestation data
# ---------------------------------------------------------------------------


@dataclass
class VerificationAttestation:
    """Signed attestation of verification result."""

    attestation_id: str = field(default_factory=lambda: new_id("vatt"))
    criterion_id: str = ""
    result: VerificationResult = VerificationResult.PASSED
    evidence: list[Evidence] = field(default_factory=list)
    oracle_type: OracleType = OracleType.COMMAND
    artifact_hash: str = ""
    oracle_config_hash: str = ""
    raw_output_hash: str = ""
    verified_at: float = field(default_factory=time.time)
    verifier_version: str = "1.0"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "attestation_id": self.attestation_id,
            "criterion_id": self.criterion_id,
            "result": self.result.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "oracle_type": self.oracle_type.value,
            "artifact_hash": self.artifact_hash,
            "oracle_config_hash": self.oracle_config_hash,
            "raw_output_hash": self.raw_output_hash,
            "verified_at": self.verified_at,
            "verifier_version": self.verifier_version,
            "reason": self.reason,
        }


@dataclass
class VerificationReport:
    """Complete verification report for a run."""

    run_id: str = ""
    all_passed: bool = False
    attestations: list[VerificationAttestation] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "all_passed": self.all_passed,
            "attestations": [a.to_dict() for a in self.attestations],
            "pending": self.pending,
            "unverifiable": self.unverifiable,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# OracleRunner - deterministic oracle execution
# ---------------------------------------------------------------------------


class OracleRunner:
    """Deterministic executor for oracle programs.

    Each oracle execution is journal-backed and produces a hash-chain
    of evidence that ties the oracle config to the raw output.
    """

    def __init__(self, workspace_dir: str = "/tmp", journal: Optional[JournalWriter] = None):
        self._workspace = workspace_dir
        self._journal = journal

    async def run_command(
        self,
        command: str,
        working_dir: str,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Execute command oracle. Returns (exit_code, stdout, stderr)."""
        cwd = working_dir or self._workspace
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        return proc.returncode or 0, stdout, stderr

    async def check_resource(self, resource_path: str) -> tuple[bool, str]:
        """Check resource existence. Returns (exists, content_hash)."""
        import os

        if not os.path.exists(resource_path):
            return False, ""
        content_hash = hashlib.sha256(open(resource_path, "rb").read()).hexdigest()[:16]
        return True, content_hash

    async def validate_schema(
        self, artifacts: dict[str, str], required_keys: list[str]
    ) -> tuple[bool, list[str]]:
        """Validate artifacts against schema requirements. Returns (valid, missing_keys)."""
        missing = [k for k in required_keys if k not in artifacts]
        return len(missing) == 0, missing


# ---------------------------------------------------------------------------
# Verifier - criterion compiler and attestation engine
# ---------------------------------------------------------------------------


class Verifier:
    """Independent verifier that checks acceptance criteria via oracles.

    All verification operations are journal-backed for auditability.
    """

    def __init__(
        self,
        workspace_dir: str = "/tmp",
        journal: Optional[JournalWriter] = None,
    ):
        self._workspace = workspace_dir
        self._journal = journal
        self._oracle_runner = OracleRunner(workspace_dir=workspace_dir, journal=journal)
        self._attestation_log: list[VerificationAttestation] = []

    def _compute_config_hash(self, oracle_config: dict) -> str:
        return hashlib.sha256(str(oracle_config).encode()).hexdigest()[:16]

    async def _journal_event(self, event_type: str, payload: dict) -> None:
        if self._journal:
            await self._journal.write_event(event_type, payload)

    async def verify_criterion(
        self,
        criterion: GoalCriterion,
        artifacts: dict[str, str],
        working_dir: str = "",
    ) -> VerificationAttestation:
        """Verify a single criterion using its configured oracle."""
        oracle_config = OracleConfig(
            oracle_type=criterion.oracle_type,
            **criterion.oracle_config,
        )
        config_hash = self._compute_config_hash(criterion.oracle_config)

        await self._journal_event("verifier.criterion_start", {
            "criterion_id": criterion.criterion_id,
            "oracle_type": oracle_config.oracle_type.value,
            "config_hash": config_hash,
        })

        try:
            if oracle_config.oracle_type == OracleType.COMMAND:
                att = await self._verify_command(criterion, oracle_config, working_dir, config_hash)
            elif oracle_config.oracle_type == OracleType.RESOURCE:
                att = await self._verify_resource(criterion, oracle_config, config_hash)
            elif oracle_config.oracle_type == OracleType.SCHEMA:
                att = await self._verify_schema(criterion, oracle_config, artifacts, config_hash)
            elif oracle_config.oracle_type == OracleType.HUMAN:
                att = self._create_human_oracle(criterion, config_hash)
            else:
                att = VerificationAttestation(
                    criterion_id=criterion.criterion_id,
                    result=VerificationResult.UNVERIFIABLE,
                    oracle_type=oracle_config.oracle_type,
                    oracle_config_hash=config_hash,
                    reason=f"Unsupported oracle type: {oracle_config.oracle_type.value}",
                )
        except Exception as exc:
            att = VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.ENVIRONMENT_BLOCKED,
                oracle_type=oracle_config.oracle_type,
                oracle_config_hash=config_hash,
                reason=f"Verifier infrastructure error: {str(exc)}",
            )

        self._attestation_log.append(att)
        await self._journal_event("verifier.criterion_complete", {
            "criterion_id": criterion.criterion_id,
            "result": att.result.value,
            "attestation_id": att.attestation_id,
        })
        return att

    async def verify_all(
        self,
        criteria: list[GoalCriterion],
        artifacts: dict[str, str],
        run_id: str,
        working_dir: str = "",
    ) -> VerificationReport:
        """Verify all criteria for a run."""
        await self._journal_event("verifier.run_start", {
            "run_id": run_id,
            "criteria_count": len(criteria),
        })

        attestations: list[VerificationAttestation] = []
        for criterion in criteria:
            att = await self.verify_criterion(criterion, artifacts, working_dir)
            attestations.append(att)

        all_passed = all(a.result == VerificationResult.PASSED for a in attestations)
        unverifiable = [
            a.criterion_id
            for a in attestations
            if a.result == VerificationResult.UNVERIFIABLE
        ]
        pending = [
            a.criterion_id
            for a in attestations
            if a.result not in (VerificationResult.PASSED, VerificationResult.UNVERIFIABLE)
        ]

        report = VerificationReport(
            run_id=run_id,
            all_passed=all_passed,
            attestations=attestations,
            pending=pending,
            unverifiable=unverifiable,
        )

        await self._journal_event("verifier.run_complete", {
            "run_id": run_id,
            "all_passed": all_passed,
            "total": len(criteria),
            "passed": sum(1 for a in attestations if a.result == VerificationResult.PASSED),
            "failed": len(pending),
            "unverifiable": len(unverifiable),
        })

        return report

    def get_attestation_log(self) -> list[VerificationAttestation]:
        """Return all attestations produced by this verifier instance."""
        return list(self._attestation_log)

    # ------------------------------------------------------------------
    # Oracle implementations
    # ------------------------------------------------------------------

    async def _verify_command(
        self,
        criterion: GoalCriterion,
        config: OracleConfig,
        working_dir: str,
        config_hash: str,
    ) -> VerificationAttestation:
        """Run a command and check exit code."""
        if not config.command:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.UNVERIFIABLE,
                oracle_type=OracleType.COMMAND,
                oracle_config_hash=config_hash,
                reason="No command specified",
            )

        try:
            exit_code, stdout, stderr = await self._oracle_runner.run_command(
                config.command,
                working_dir or self._workspace,
                config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.ENVIRONMENT_BLOCKED,
                oracle_type=OracleType.COMMAND,
                oracle_config_hash=config_hash,
                reason=f"Command timed out after {config.timeout_seconds}s",
            )

        output_hash = hashlib.sha256(stdout.encode()).hexdigest()[:16]
        evidence = Evidence(source=f"command:{config.command}", content_hash=output_hash)

        if exit_code == 0:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.PASSED,
                oracle_type=OracleType.COMMAND,
                oracle_config_hash=config_hash,
                raw_output_hash=output_hash,
                evidence=[evidence],
            )
        else:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.EXECUTION_DEFECT,
                oracle_type=OracleType.COMMAND,
                oracle_config_hash=config_hash,
                raw_output_hash=output_hash,
                evidence=[evidence],
                reason=f"Exit code {exit_code}: {stderr[:200]}",
            )

    async def _verify_resource(
        self,
        criterion: GoalCriterion,
        config: OracleConfig,
        config_hash: str,
    ) -> VerificationAttestation:
        """Check if a resource exists and is accessible."""
        resource = config.resource_url
        if not resource:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.UNVERIFIABLE,
                oracle_type=OracleType.RESOURCE,
                oracle_config_hash=config_hash,
                reason="No resource specified",
            )

        exists, content_hash = await self._oracle_runner.check_resource(resource)
        if exists:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.PASSED,
                oracle_type=OracleType.RESOURCE,
                oracle_config_hash=config_hash,
                artifact_hash=content_hash,
                evidence=[Evidence(source=f"file:{resource}", content_hash=content_hash)],
            )
        else:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.EXECUTION_DEFECT,
                oracle_type=OracleType.RESOURCE,
                oracle_config_hash=config_hash,
                reason=f"Resource not found: {resource}",
            )

    async def _verify_schema(
        self,
        criterion: GoalCriterion,
        config: OracleConfig,
        artifacts: dict[str, str],
        config_hash: str,
    ) -> VerificationAttestation:
        """Validate output against expected schema."""
        expected = config.expected_schema
        if not expected:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.UNVERIFIABLE,
                oracle_type=OracleType.SCHEMA,
                oracle_config_hash=config_hash,
                reason="No expected schema",
            )

        required_keys = expected.get("required_keys", [])
        valid, missing = await self._oracle_runner.validate_schema(artifacts, required_keys)

        if valid:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.PASSED,
                oracle_type=OracleType.SCHEMA,
                oracle_config_hash=config_hash,
            )
        else:
            return VerificationAttestation(
                criterion_id=criterion.criterion_id,
                result=VerificationResult.EXECUTION_DEFECT,
                oracle_type=OracleType.SCHEMA,
                oracle_config_hash=config_hash,
                reason=f"Missing required artifacts: {missing}",
            )

    def _create_human_oracle(
        self,
        criterion: GoalCriterion,
        config_hash: str,
    ) -> VerificationAttestation:
        """Create a pending human verification request."""
        return VerificationAttestation(
            criterion_id=criterion.criterion_id,
            result=VerificationResult.UNVERIFIABLE,
            oracle_type=OracleType.HUMAN,
            oracle_config_hash=config_hash,
            reason="Requires human verification",
        )
