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


def test_production_data_recovery_requeues_pending_knowledge() -> None:
    calls: list[str] = []

    class _DataService:
        def rebuild_projection(self) -> None:
            calls.append("data_projection")

    class _Knowledge:
        def recover(self) -> int:
            calls.append("knowledge_queue")
            return 1

    class _Data:
        service = _DataService()
        state = SimpleNamespace(data_authority=SimpleNamespace(mode="canonical"))
        knowledge_service = _Knowledge()

        def rebuild_all(self) -> None:
            calls.append("workspace_projection")

    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    runtime = EmployeeDepartmentRuntime()
    runtime._data = _Data()  # type: ignore[assignment]  # noqa: SLF001
    runtime._recover_employee_data(SimpleNamespace(employees={}))  # noqa: SLF001

    assert calls == ["data_projection", "workspace_projection", "knowledge_queue"]


def test_team_recovery_waits_for_shared_dispatch_projections() -> None:
    calls: list[str] = []
    empty_state = SimpleNamespace(by_acceptance_id={})
    workforce = SimpleNamespace(employees={}, bot_principals={})

    class _Service:
        projection_state = workforce

        def recover(self):
            return SimpleNamespace(states={})

        def recover_manifest_reauthorizations(self):
            return ()

        def list_states(self):
            return ()

        def mark_runtime_recovered(self):
            calls.append("admission_open")

    class _Ingress:
        state = empty_state

        def rebuild_projection(self):
            calls.append("ingress_projection")

        def gc_terminal_payloads(self):
            return 0

    class _Router:
        state = empty_state

        def rebuild_projection(self):
            calls.append("router_projection")

        def recover_terminal_attachments(self):
            return 0

    class _Outbox:
        def rebuild_projection(self):
            calls.append("outbox_projection")

    class _Dispatch:
        employee_runtime = None

        def recover_incomplete_attempts(self):
            calls.append("dispatch_projection")

        def reconcile_terminal_snapshots(self):
            return 0

    class _Team:
        def recover(self):
            calls.append("team_coordinator")
            return 0

    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    runtime = EmployeeDepartmentRuntime()
    runtime._service = _Service()  # type: ignore[assignment]  # noqa: SLF001
    runtime._ingress = _Ingress()  # type: ignore[assignment]  # noqa: SLF001
    runtime._router = _Router()  # type: ignore[assignment]  # noqa: SLF001
    runtime._outbox = _Outbox()  # type: ignore[assignment]  # noqa: SLF001
    runtime._dispatch = _Dispatch()  # type: ignore[assignment]  # noqa: SLF001
    runtime._team = _Team()  # type: ignore[assignment]  # noqa: SLF001
    runtime.recover()

    assert calls.index("dispatch_projection") < calls.index("team_coordinator")
    assert calls[-1] == "admission_open"


def test_production_dispatch_tick_reaps_idle_employee_sessions() -> None:
    calls: list[str] = []
    empty_state = SimpleNamespace(by_acceptance_id={})

    class _Projection:
        state = empty_state

        def rebuild_projection(self):
            return None

    class _Ingress(_Projection):
        def gc_terminal_payloads(self):
            return 0

    class _Runtime:
        def sweep_idle(self):
            calls.append("sweep_idle")
            return 1

    class _Dispatch:
        employee_runtime = _Runtime()

        def dispatch_next(self):
            return None

    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    runtime = EmployeeDepartmentRuntime()
    runtime._ingress = _Ingress()  # type: ignore[assignment]  # noqa: SLF001
    runtime._router = _Projection()  # type: ignore[assignment]  # noqa: SLF001
    runtime._dispatch = _Dispatch()  # type: ignore[assignment]  # noqa: SLF001

    runtime._drain_employee_dispatch_once()  # noqa: SLF001

    assert calls == ["sweep_idle"]


def test_enabled_runtime_fails_closed_when_group_ledger_cannot_anchor() -> None:
    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    runtime = EmployeeDepartmentRuntime(runtime_enabled=True)

    with pytest.raises(RuntimeError, match="group ledger"):
        runtime.record_group_event(
            tenant_key="tenant_1",
            chat_id="oc_team",
            thread_id="",
            message_id="om_event",
            sender_id="ou_user",
            text="team task",
        )
