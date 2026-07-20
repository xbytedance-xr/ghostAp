"""Unit tests for Verifier and OracleRunner."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from src.autonomous.domain import (
    GoalCriterion,
    OracleType,
    VerificationResult,
)
from src.autonomous.verifier.verifier import OracleRunner, Verifier


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def write_event(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def journal() -> FakeJournal:
    return FakeJournal()


@pytest.fixture
def verifier(journal: FakeJournal) -> Verifier:
    return Verifier(workspace_dir="/tmp", journal=journal)


def _make_criterion(
    oracle_type: OracleType = OracleType.COMMAND,
    oracle_config: dict | None = None,
    criterion_id: str = "crit_test",
) -> GoalCriterion:
    return GoalCriterion(
        criterion_id=criterion_id,
        description="Test criterion",
        oracle_type=oracle_type,
        oracle_config=oracle_config or {},
    )


# ---------------------------------------------------------------------------
# OracleRunner tests
# ---------------------------------------------------------------------------


class TestOracleRunner:
    @pytest.mark.asyncio
    async def test_run_command_success(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        exit_code, stdout, stderr = await runner.run_command("echo hello", "/tmp", 10.0)
        assert exit_code == 0
        assert "hello" in stdout

    @pytest.mark.asyncio
    async def test_run_command_failure(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        exit_code, stdout, stderr = await runner.run_command("exit 1", "/tmp", 10.0)
        assert exit_code == 1

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_run_command_timeout(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        with pytest.raises(asyncio.TimeoutError):
            await runner.run_command("sleep 10", "/tmp", 0.1)

    @pytest.mark.asyncio
    async def test_check_resource_exists(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            path = f.name
        try:
            exists, content_hash = await runner.check_resource(path)
            assert exists is True
            assert len(content_hash) == 16
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_check_resource_missing(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        exists, content_hash = await runner.check_resource("/nonexistent/path")
        assert exists is False
        assert content_hash == ""

    @pytest.mark.asyncio
    async def test_validate_schema_pass(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        valid, missing = await runner.validate_schema(
            {"key1": "val1", "key2": "val2"}, ["key1", "key2"]
        )
        assert valid is True
        assert missing == []

    @pytest.mark.asyncio
    async def test_validate_schema_fail(self) -> None:
        runner = OracleRunner(workspace_dir="/tmp")
        valid, missing = await runner.validate_schema(
            {"key1": "val1"}, ["key1", "key2"]
        )
        assert valid is False
        assert "key2" in missing


# ---------------------------------------------------------------------------
# Verifier tests
# ---------------------------------------------------------------------------


class TestVerifier:
    @pytest.mark.asyncio
    async def test_verify_command_pass(self, verifier: Verifier, journal: FakeJournal) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "echo pass", "timeout_seconds": 10.0},
        )
        att = await verifier.verify_criterion(criterion, {})
        assert att.result == VerificationResult.PASSED
        assert att.criterion_id == "crit_test"
        assert att.oracle_type == OracleType.COMMAND
        # Journal events logged
        event_types = [e[0] for e in journal.events]
        assert "verifier.criterion_start" in event_types
        assert "verifier.criterion_complete" in event_types

    @pytest.mark.asyncio
    async def test_verify_command_fail(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "exit 1", "timeout_seconds": 10.0},
        )
        att = await verifier.verify_criterion(criterion, {})
        assert att.result == VerificationResult.EXECUTION_DEFECT
        assert "Exit code 1" in att.reason

    @pytest.mark.asyncio
    async def test_verify_command_no_command(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={},
        )
        att = await verifier.verify_criterion(criterion, {})
        assert att.result == VerificationResult.UNVERIFIABLE
        assert "No command specified" in att.reason

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_verify_command_timeout(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "sleep 10", "timeout_seconds": 0.1},
        )
        att = await verifier.verify_criterion(criterion, {})
        assert att.result == VerificationResult.ENVIRONMENT_BLOCKED
        assert "timed out" in att.reason

    @pytest.mark.asyncio
    async def test_verify_resource_pass(self, verifier: Verifier) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"resource content")
            path = f.name
        try:
            criterion = _make_criterion(
                oracle_type=OracleType.RESOURCE,
                oracle_config={"resource_url": path},
            )
            att = await verifier.verify_criterion(criterion, {})
            assert att.result == VerificationResult.PASSED
            assert att.artifact_hash != ""
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_verify_resource_missing(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.RESOURCE,
            oracle_config={"resource_url": "/nonexistent/file"},
        )
        att = await verifier.verify_criterion(criterion, {})
        assert att.result == VerificationResult.EXECUTION_DEFECT

    @pytest.mark.asyncio
    async def test_verify_schema_pass(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.SCHEMA,
            oracle_config={"expected_schema": {"required_keys": ["output", "summary"]}},
        )
        att = await verifier.verify_criterion(
            criterion, {"output": "data", "summary": "done"}
        )
        assert att.result == VerificationResult.PASSED

    @pytest.mark.asyncio
    async def test_verify_schema_fail(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.SCHEMA,
            oracle_config={"expected_schema": {"required_keys": ["output", "missing_key"]}},
        )
        att = await verifier.verify_criterion(criterion, {"output": "data"})
        assert att.result == VerificationResult.EXECUTION_DEFECT
        assert "missing_key" in att.reason

    @pytest.mark.asyncio
    async def test_verify_human_oracle(self, verifier: Verifier) -> None:
        criterion = _make_criterion(oracle_type=OracleType.HUMAN)
        att = await verifier.verify_criterion(criterion, {})
        assert att.result == VerificationResult.UNVERIFIABLE
        assert "human" in att.reason.lower()

    @pytest.mark.asyncio
    async def test_verify_all(self, verifier: Verifier, journal: FakeJournal) -> None:
        criteria = [
            _make_criterion(
                criterion_id="c1",
                oracle_type=OracleType.COMMAND,
                oracle_config={"command": "echo ok", "timeout_seconds": 10.0},
            ),
            _make_criterion(
                criterion_id="c2",
                oracle_type=OracleType.COMMAND,
                oracle_config={"command": "echo also_ok", "timeout_seconds": 10.0},
            ),
        ]
        report = await verifier.verify_all(criteria, {}, "run_123")
        assert report.all_passed is True
        assert report.run_id == "run_123"
        assert len(report.attestations) == 2
        assert report.pending == []
        assert report.unverifiable == []

    @pytest.mark.asyncio
    async def test_verify_all_partial_failure(self, verifier: Verifier) -> None:
        criteria = [
            _make_criterion(
                criterion_id="pass_c",
                oracle_type=OracleType.COMMAND,
                oracle_config={"command": "echo ok", "timeout_seconds": 10.0},
            ),
            _make_criterion(
                criterion_id="fail_c",
                oracle_type=OracleType.COMMAND,
                oracle_config={"command": "exit 2", "timeout_seconds": 10.0},
            ),
        ]
        report = await verifier.verify_all(criteria, {}, "run_456")
        assert report.all_passed is False
        assert "fail_c" in report.pending

    @pytest.mark.asyncio
    async def test_attestation_log(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "echo test", "timeout_seconds": 10.0},
        )
        await verifier.verify_criterion(criterion, {})
        log = verifier.get_attestation_log()
        assert len(log) == 1
        assert log[0].result == VerificationResult.PASSED

    @pytest.mark.asyncio
    async def test_attestation_to_dict(self, verifier: Verifier) -> None:
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "echo ok", "timeout_seconds": 10.0},
        )
        att = await verifier.verify_criterion(criterion, {})
        d = att.to_dict()
        assert d["criterion_id"] == "crit_test"
        assert d["result"] == "passed"
        assert d["oracle_type"] == "command"

    @pytest.mark.asyncio
    async def test_verifier_no_journal(self) -> None:
        """Verifier works without a journal (journal is optional)."""
        v = Verifier(workspace_dir="/tmp", journal=None)
        criterion = _make_criterion(
            oracle_type=OracleType.COMMAND,
            oracle_config={"command": "echo hi", "timeout_seconds": 10.0},
        )
        att = await v.verify_criterion(criterion, {})
        assert att.result == VerificationResult.PASSED
