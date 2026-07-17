from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.autonomous.migration.employee_workspace_v1 import (
    EmployeeWorkspaceMigrationError,
    EmployeeWorkspaceV1Migrator,
)
from src.autonomous.supervisor import (
    EMPLOYEE_RECOVERY_ORDER,
    EmployeeLifecycleSupervisor,
    EmployeeRecoverySnapshot,
)
from src.autonomous.workspace import EmployeeWorkspaceProjector
from tests.autonomous.workforce_helpers import (
    commit_events,
    employee_created,
    make_writer,
    replay_state,
)


def test_employee_recovery_and_shutdown_use_fixed_inverse_order() -> None:
    calls = []
    closes = []
    recoverers = {
        stage: (lambda _snapshot, stage=stage: calls.append(stage))
        for stage in EMPLOYEE_RECOVERY_ORDER
    }
    closers = {
        stage: (lambda stage=stage: closes.append(stage))
        for stage in EMPLOYEE_RECOVERY_ORDER
    }
    supervisor = EmployeeLifecycleSupervisor(recoverers, closers)
    report = supervisor.recover(EmployeeRecoverySnapshot((), 0, ""))
    assert report.ready is True
    assert tuple(calls) == EMPLOYEE_RECOVERY_ORDER
    assert supervisor.shutdown() == tuple(reversed(EMPLOYEE_RECOVERY_ORDER))
    assert tuple(closes) == tuple(reversed(EMPLOYEE_RECOVERY_ORDER))


@pytest.mark.parametrize(
    "failed_stage",
    ("workspace_projection", "data_projection", "actor_mailboxes"),
)
def test_employee_ready_fails_closed_at_first_broken_dependency(failed_stage) -> None:
    calls = []

    def recover(stage):
        calls.append(stage)
        if stage == failed_stage:
            raise RuntimeError("broken")

    supervisor = EmployeeLifecycleSupervisor(
        {
            stage: (lambda _snapshot, stage=stage: recover(stage))
            for stage in EMPLOYEE_RECOVERY_ORDER
        }
    )
    report = supervisor.recover(EmployeeRecoverySnapshot((), 0, ""))
    assert report.ready is False
    assert report.blocker == failed_stage
    assert "admission_open" not in calls


def test_legacy_employee_workspace_migration_is_canonical_and_idempotent(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = SimpleNamespace()
    projection = replay_state(writer)
    commit_events(writer, projection, employee_created("agt_legacy", "Legacy"))
    projector = EmployeeWorkspaceProjector(
        tmp_path / "agents", state_provider=lambda: replay_state(writer)
    )
    migrator = EmployeeWorkspaceV1Migrator(projector)
    first = migrator.migrate((("tenant_1", "agt_legacy"),))
    agents = tmp_path / "agents" / "agt_legacy" / "workspace" / "AGENTS.md"
    agents.write_text("legacy mutable escalation", encoding="utf-8")
    second = migrator.migrate((("tenant_1", "agt_legacy"),))
    assert first.snapshots == second.snapshots
    assert "legacy mutable escalation" not in agents.read_text(encoding="utf-8")
    del state
    writer.close()


def test_legacy_employee_workspace_migration_rejects_symlink(tmp_path) -> None:
    writer = make_writer(tmp_path)
    projection = replay_state(writer)
    commit_events(writer, projection, employee_created("agt_legacy", "Legacy"))
    root = tmp_path / "agents"
    root.mkdir()
    (root / "agt_legacy").symlink_to(tmp_path / "outside")
    migrator = EmployeeWorkspaceV1Migrator(
        EmployeeWorkspaceProjector(root, state_provider=lambda: replay_state(writer))
    )
    with pytest.raises(EmployeeWorkspaceMigrationError, match="unsafe"):
        migrator.migrate((("tenant_1", "agt_legacy"),))
    writer.close()

