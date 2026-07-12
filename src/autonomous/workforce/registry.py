"""Read-only, tenant-aware employee registry projected from the Journal."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from src.slock_engine.memory_manager import default_slock_storage_base
from src.slock_engine.models import AgentIdentity

from ..domain import EmployeeDefinition

if TYPE_CHECKING:
    from ..journal.projections import ProjectionState


class AmbiguousEmployeeName(LookupError):
    """More than one projected employee matched a scoped name lookup."""


class ProjectedAgentRegistry:
    """Expose immutable projected identities without legacy write methods."""

    def __init__(
        self,
        state: ProjectionState,
        *,
        storage_base_path: str = "",
    ) -> None:
        self._state = state
        self._storage_base = Path(
            storage_base_path or default_slock_storage_base()
        )

    def get(self, tenant_key: str, agent_id: str) -> EmployeeDefinition | None:
        employee = self._state.employees.get(agent_id)
        if employee is None or employee.tenant_key != tenant_key:
            return None
        return employee

    def find_by_name(
        self,
        tenant_key: str,
        name: str,
        channel_id: str | None = None,
    ) -> EmployeeDefinition | None:
        normalized = name.casefold()
        matches = [
            employee
            for employee in self._state.employees.values()
            if employee.tenant_key == tenant_key
            and employee.name.casefold() == normalized
            and (
                channel_id is None
                or channel_id in employee.member_groups
            )
        ]
        if len(matches) > 1:
            raise AmbiguousEmployeeName(
                f"ambiguous employee name in tenant {tenant_key}: {name}"
            )
        return matches[0] if matches else None

    def list_agents(
        self,
        tenant_key: str,
        channel_id: str | None = None,
    ) -> list[EmployeeDefinition]:
        return [
            employee
            for employee in self._state.employees.values()
            if employee.tenant_key == tenant_key
            and (
                channel_id is None
                or channel_id in employee.member_groups
            )
        ]

    def as_slock_identity(
        self,
        tenant_key: str,
        agent_id: str,
    ) -> AgentIdentity | None:
        employee = self.get(tenant_key, agent_id)
        if employee is None:
            return None
        agent_dir = self._storage_base / "agents" / employee.agent_id
        groups = list(employee.member_groups)
        return AgentIdentity(
            agent_id=employee.agent_id,
            name=employee.name,
            emoji=employee.emoji,
            agent_type=employee.tool,
            model_name=employee.model,
            system_prompt=employee.persona,
            role=employee.role or "custom",
            permissions=list(employee.permissions),
            memory_path=str(agent_dir / "MEMORY.md"),
            notes_path=str(agent_dir / "NOTES.md"),
            workspace_path=str(agent_dir / "workspace"),
            owner_group=groups[0] if groups else "",
            member_groups=groups,
            created_at=employee.created_at,
            personality_traits=list(employee.personality_traits),
        )
