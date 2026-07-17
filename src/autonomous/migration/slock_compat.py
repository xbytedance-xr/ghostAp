"""Slock compatibility layer for gradual migration.

Provides read-only command translations from legacy Slock commands to
autonomous kernel projections.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class CompatibilityMode(str, Enum):
    LEGACY = "legacy"
    SHADOW_READ = "shadow_read"
    MANAGER_ONLY = "manager_only"
    DISABLED = "disabled"


class ProjectionQuery(Protocol):
    def goal(self, goal_id: str) -> Any: ...
    def run(self, run_id: str) -> Any: ...
    def list_goals(self, tenant_key: str) -> list[Any]: ...


class EmployeeWorkspaceMigrator(Protocol):
    def migrate(self, employees: tuple[tuple[str, str], ...]) -> Any: ...


@dataclass
class CompatResult:
    handled: bool = False
    response: str = ""
    data: dict[str, Any] | None = None
    forward_to_manager: bool = False


class SlockCompatLayer:
    """Translates legacy Slock commands to autonomous kernel queries.

    Modes:
    - LEGACY: unchanged behavior, no autonomous kernel involvement
    - SHADOW_READ: legacy + compare with projections (diagnostic)
    - MANAGER_ONLY: forwards to autonomous kernel, no legacy writes
    - DISABLED: removes passive auto-activation entirely
    """

    def __init__(
        self,
        mode: CompatibilityMode,
        projections: ProjectionQuery | None = None,
        workspace_migrator: EmployeeWorkspaceMigrator | None = None,
    ) -> None:
        self._mode = mode
        self._projections = projections
        self._write_log: list[dict[str, Any]] = []
        self._workspace_migrator = workspace_migrator

    @property
    def mode(self) -> CompatibilityMode:
        return self._mode

    @property
    def write_log(self) -> list[dict[str, Any]]:
        return list(self._write_log)

    def handle_command(
        self,
        command: str,
        args: str = "",
        *,
        tenant_key: str = "",
        chat_id: str = "",
    ) -> CompatResult:
        if self._mode is CompatibilityMode.LEGACY:
            return CompatResult(handled=False)

        if self._mode is CompatibilityMode.DISABLED:
            return CompatResult(handled=True, response="autonomous mode is disabled")

        if self._mode is CompatibilityMode.MANAGER_ONLY:
            return self._forward_to_manager(command, args, tenant_key, chat_id)

        if self._mode is CompatibilityMode.SHADOW_READ:
            return CompatResult(handled=False)

        return CompatResult(handled=False)

    def intercept_write(
        self,
        operation: str,
        data: dict[str, Any],
    ) -> bool:
        """Returns True if the write should be blocked (MANAGER_ONLY mode)."""
        if self._mode is CompatibilityMode.MANAGER_ONLY:
            return True
        if self._mode is CompatibilityMode.DISABLED:
            return True
        self._write_log.append({"operation": operation, "data": data})
        return False

    def migrate_employee_workspaces(
        self,
        employees: tuple[tuple[str, str], ...],
    ) -> Any:
        """Invoke the canonical projector; no legacy file payload is accepted."""

        if self._workspace_migrator is None:
            raise RuntimeError("employee workspace migrator is unavailable")
        return self._workspace_migrator.migrate(employees)

    def _forward_to_manager(
        self,
        command: str,
        args: str,
        tenant_key: str,
        chat_id: str,
    ) -> CompatResult:
        cmd_map = {
            "create": "goal.create",
            "list": "goal.list",
            "status": "run.status",
            "cancel": "goal.cancel",
            "pause": "goal.pause",
            "resume": "goal.resume",
        }
        normalized = command.lower().strip()
        manager_op = cmd_map.get(normalized)

        if manager_op:
            return CompatResult(
                handled=True,
                forward_to_manager=True,
                data={
                    "operation": manager_op,
                    "args": args,
                    "tenant_key": tenant_key,
                    "chat_id": chat_id,
                },
            )

        # Unknown command in manager_only mode: read from projections
        if self._projections and normalized == "show":
            return CompatResult(
                handled=True,
                response="forwarded to projection query",
                forward_to_manager=True,
            )

        return CompatResult(handled=False)
