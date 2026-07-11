"""Employee lifecycle, worker types, and collaboration planner."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .domain.ids import new_id


class WorkerType(str, Enum):
    LOGICAL = "logical"
    VISIBLE = "visible"
    EPHEMERAL = "ephemeral"


class EmployeeState(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISMISSED = "dismissed"


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


@dataclass
class Employee:
    employee_id: str = field(default_factory=lambda: new_id("emp"))
    name: str = ""
    role: str = ""
    worker_type: WorkerType = WorkerType.LOGICAL
    state: EmployeeState = EmployeeState.DRAFT
    tool: str = ""
    model: str = ""
    bot_principal_id: str | None = None
    capabilities: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    dismissed_at: float | None = None
    aggregate_version: int = 0


class EmployeeManager:
    """Manages the lifecycle of logical and visible employees."""

    def __init__(self, journal: JournalWriter) -> None:
        self._journal = journal
        self._employees: dict[str, Employee] = {}

    async def hire(
        self,
        *,
        name: str = "",
        role: str = "",
        template: str = "",
        tool: str = "",
        model: str = "",
        worker_type: WorkerType = WorkerType.LOGICAL,
    ) -> Employee:
        employee = Employee(
            name=name or template,
            role=role or template,
            worker_type=worker_type,
            tool=tool,
            model=model,
            state=EmployeeState.ACTIVE,
        )
        self._employees[employee.employee_id] = employee

        await self._journal.write_event("employee.hired", {
            "employee_id": employee.employee_id,
            "name": employee.name,
            "role": employee.role,
            "worker_type": worker_type.value,
            "tool": tool,
            "model": model,
        })
        return employee

    async def dismiss(self, employee_id: str) -> Employee:
        emp = self._get(employee_id)
        if emp.state is EmployeeState.DISMISSED:
            raise ValueError("employee already dismissed")
        emp.state = EmployeeState.DISMISSED
        emp.dismissed_at = time.time()
        emp.aggregate_version += 1

        await self._journal.write_event("employee.dismissed", {
            "employee_id": employee_id,
        })
        return emp

    async def suspend(self, employee_id: str) -> Employee:
        emp = self._get(employee_id)
        if emp.state is not EmployeeState.ACTIVE:
            raise ValueError(f"cannot suspend employee in state {emp.state.value}")
        emp.state = EmployeeState.SUSPENDED
        emp.aggregate_version += 1

        await self._journal.write_event("employee.suspended", {
            "employee_id": employee_id,
        })
        return emp

    async def reactivate(self, employee_id: str) -> Employee:
        emp = self._get(employee_id)
        if emp.state is not EmployeeState.SUSPENDED:
            raise ValueError(f"cannot reactivate employee in state {emp.state.value}")
        emp.state = EmployeeState.ACTIVE
        emp.aggregate_version += 1

        await self._journal.write_event("employee.reactivated", {
            "employee_id": employee_id,
        })
        return emp

    def get(self, employee_id: str) -> Employee | None:
        return self._employees.get(employee_id)

    def list_active(self) -> list[Employee]:
        return [
            e for e in self._employees.values()
            if e.state is EmployeeState.ACTIVE
        ]

    def list_by_role(self, role: str) -> list[Employee]:
        return [
            e for e in self._employees.values()
            if e.role == role and e.state is EmployeeState.ACTIVE
        ]

    def _get(self, employee_id: str) -> Employee:
        emp = self._employees.get(employee_id)
        if emp is None:
            raise ValueError(f"employee not found: {employee_id}")
        return emp


class CollaborationPlanner:
    """Plans multi-worker collaboration for complex goals.

    Assigns steps to appropriate employees based on role/capability matching.
    Ensures reviewer != implementer for verification separation.
    """

    def __init__(self, employee_manager: EmployeeManager) -> None:
        self._employees = employee_manager

    def assign_steps(
        self,
        steps: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        active = self._employees.list_active()
        if not active:
            return steps

        assignments: list[dict[str, Any]] = []
        for step in steps:
            required_role = step.get("required_role", "")
            candidates = (
                self._employees.list_by_role(required_role)
                if required_role
                else active
            )
            assigned_employee = candidates[0] if candidates else None
            assignments.append({
                **step,
                "assigned_employee_id": (
                    assigned_employee.employee_id if assigned_employee else None
                ),
            })
        return assignments

    def ensure_verification_separation(
        self,
        implementer_id: str,
        verification_step: dict[str, Any],
    ) -> dict[str, Any]:
        """Ensure reviewer is different from implementer."""
        reviewers = [
            e for e in self._employees.list_by_role("reviewer")
            if e.employee_id != implementer_id
        ]
        if not reviewers:
            all_others = [
                e for e in self._employees.list_active()
                if e.employee_id != implementer_id
            ]
            reviewers = all_others

        reviewer = reviewers[0] if reviewers else None
        return {
            **verification_step,
            "assigned_employee_id": reviewer.employee_id if reviewer else None,
            "verification_separation": reviewer is not None,
        }
