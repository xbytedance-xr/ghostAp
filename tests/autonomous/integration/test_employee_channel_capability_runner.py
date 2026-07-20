"""Integration contract for the standalone Channel capability evidence runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.run_employee_channel_capability as capability_runner
from src.autonomous.ingress.sdk_capability import (
    CAPABILITY_NODEIDS,
    CapabilityDecision,
    CapabilityRunEvidence,
)

_RUNNER_TIMEOUT_SECONDS = 150


@pytest.mark.slow
@pytest.mark.timeout(_RUNNER_TIMEOUT_SECONDS)
def test_runner_collects_exact_nodes_and_writes_development_evidence(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[3]
    output = tmp_path / "channel-capability.json"

    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "run_employee_channel_capability.py"),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=_RUNNER_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = CapabilityRunEvidence.from_json_bytes(output.read_bytes())
    assert evidence.decision is CapabilityDecision.CAPABLE_PINNED_ADAPTER
    assert evidence.requested_nodeids == CAPABILITY_NODEIDS
    assert evidence.collected_nodeids == CAPABILITY_NODEIDS
    assert tuple(outcome.nodeid for outcome in evidence.outcomes) == CAPABILITY_NODEIDS
    assert all(outcome.passed for outcome in evidence.outcomes)
    assert evidence.promotable is False


@pytest.mark.slow
def test_runner_rejects_repository_output(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    forbidden = repository / ".employee-channel-capability.json"

    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "run_employee_channel_capability.py"),
            "--output",
            str(forbidden),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "outside the repository" in result.stderr
    assert not forbidden.exists()


def test_runner_reports_stable_sdk_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        capability_runner,
        "collect_sdk_distribution_identity",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("sensitive local path")),
    )

    assert capability_runner._collect_sdk_or_report() is None
    assert json.loads(capsys.readouterr().out) == {
        "blocker": "employee_channel_sdk_capability_mismatch",
        "decision": CapabilityDecision.CAPABILITY_RED.value,
    }


def test_runner_removes_stale_green_before_identity_check(tmp_path: Path) -> None:
    stale = tmp_path / "capability.json"
    stale.write_text('{"decision":"CAPABLE_PINNED_ADAPTER"}', encoding="utf-8")

    assert capability_runner._prepare_output_path(stale) == stale
    assert not stale.exists()
