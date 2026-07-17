from __future__ import annotations

import pytest

from src.autonomous.runtime import EmployeeRuntimeSupervisor
from src.autonomous.supervisor import (
    EMPLOYEE_RECOVERY_ORDER,
    EmployeeLifecycleSupervisor,
    EmployeeRecoverySnapshot,
)


@pytest.mark.parametrize("employee_count", (1, 10, 50))
def test_cold_employee_restart_matrix_opens_only_after_all_dependencies(employee_count) -> None:
    runtime = EmployeeRuntimeSupervisor()
    calls = []

    def recover_actors(_snapshot):
        for index in range(employee_count):
            runtime.ensure_employee(f"agt_{index}")
        calls.append(("actor_mailboxes", employee_count))

    recoverers = {
        stage: (lambda _snapshot, stage=stage: calls.append((stage, employee_count)))
        for stage in EMPLOYEE_RECOVERY_ORDER
    }
    recoverers["actor_mailboxes"] = recover_actors
    report = EmployeeLifecycleSupervisor(recoverers).recover(
        EmployeeRecoverySnapshot((), employee_count, "a" * 64)
    )
    assert report.ready is True
    assert calls[-1][0] == "admission_open"
    assert all(runtime.status(f"agt_{index}").value == "ready_cold" for index in range(employee_count))
    runtime.close()


def test_pending_knowledge_team_channel_and_fire_stages_are_not_skipped() -> None:
    calls = []
    recoverers = {
        stage: (lambda _snapshot, stage=stage: calls.append(stage))
        for stage in EMPLOYEE_RECOVERY_ORDER
    }
    report = EmployeeLifecycleSupervisor(recoverers).recover(
        EmployeeRecoverySnapshot(("pending-knowledge", "active-team", "fire"), 3, "b" * 64)
    )
    assert report.ready is True
    assert calls.index("workspace_projection") < calls.index("team_coordinator")
    assert calls.index("team_coordinator") < calls.index("employee_channels")

