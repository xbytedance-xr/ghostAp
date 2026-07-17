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


def test_workspace_snapshot_is_frozen(tmp_path: Path) -> None:
    snapshot = EmployeeWorkspaceProjector(tmp_path / "agents").project(_source())
    with pytest.raises(FrozenInstanceError):
        snapshot.identity_version = 3  # type: ignore[misc]
    assert isinstance(snapshot, EmployeeWorkspaceSnapshot)

