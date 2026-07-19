"""Workspace content and integrity lint contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.autonomous.workspace import (
    EmployeeWorkspaceProjector,
    EmployeeWorkspaceSnapshot,
    EmployeeWorkspaceSource,
    WorkspaceLintError,
    inspect_employee_workspace,
    lint_employee_workspace,
)


def _source() -> EmployeeWorkspaceSource:
    return EmployeeWorkspaceSource(
        tenant_key="tenant_1",
        agent_id="agt_lint",
        name="Lint",
        role="reviewer",
        persona="Review evidence",
        personality_traits=("严谨",),
        capabilities=("review", "file_read"),
        permissions=("file_read",),
        tool="claude",
        model="",
        identity_version=2,
        projection_sequence=9,
        projection_hash="b" * 64,
    )


def test_lint_detects_secret_oversize_and_permission_drift(tmp_path: Path) -> None:
    snapshot = EmployeeWorkspaceProjector(tmp_path / "agents").project(_source())
    workspace = tmp_path / "agents/agt_lint/workspace"

    identity = workspace / "IDENTITY.md"
    identity.write_text("app_secret: sk-secret", encoding="utf-8")
    identity.chmod(0o644)
    agents = workspace / "AGENTS.md"
    agents.write_bytes(b"x" * 8193)

    with pytest.raises(WorkspaceLintError) as raised:
        lint_employee_workspace(snapshot, tmp_path / "agents")

    codes = {issue.code for issue in raised.value.issues}
    assert {"secret_content", "agents_too_large", "unsafe_mode"} <= codes
    assert {issue.code for issue in inspect_employee_workspace(snapshot, tmp_path / "agents")} == codes


def test_workspace_snapshot_is_frozen(tmp_path: Path) -> None:
    snapshot = EmployeeWorkspaceProjector(tmp_path / "agents").project(_source())
    with pytest.raises(FrozenInstanceError):
        snapshot.identity_version = 3  # type: ignore[misc]
    assert isinstance(snapshot, EmployeeWorkspaceSnapshot)


def test_lint_allows_backend_modes_below_private_trae_home(tmp_path: Path) -> None:
    snapshot = EmployeeWorkspaceProjector(tmp_path / "agents").project(_source())
    trae_home = tmp_path / "agents/agt_lint/runtime/trae-home"
    sessions = trae_home / "cli/sessions/2026/07/19"
    sessions.mkdir(parents=True)
    trae_home.chmod(0o700)
    (trae_home / "cli").chmod(0o755)
    (trae_home / "cli/sessions").chmod(0o755)
    (trae_home / "cli/sessions/2026").chmod(0o755)
    (trae_home / "cli/sessions/2026/07").chmod(0o755)
    sessions.chmod(0o755)
    rollout = sessions / "rollout.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    rollout.chmod(0o644)

    lint_employee_workspace(snapshot, tmp_path / "agents")

    trae_home.chmod(0o755)
    with pytest.raises(WorkspaceLintError) as raised:
        lint_employee_workspace(snapshot, tmp_path / "agents")
    assert ("unsafe_mode", "runtime/trae-home") in {
        (issue.code, issue.path) for issue in raised.value.issues
    }
