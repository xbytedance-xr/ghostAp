"""Deterministic projector for durable logical employee workspaces."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain import EmployeeState
from .layout import atomic_write_relative, open_child_directory, open_directory_tree
from .lint import lint_employee_workspace
from .models import (
    EmployeeWorkspaceSnapshot,
    EmployeeWorkspaceSource,
    WorkspaceProjectionError,
)
from .templates import render_workspace_files


class EmployeeWorkspaceProjector:
    def __init__(
        self,
        agents_root: str | Path,
        *,
        state_provider: Callable[[], Any] | None = None,
        source_provider: Callable[[str, str], EmployeeWorkspaceSource] | None = None,
    ) -> None:
        self._root = Path(agents_root).expanduser().absolute()
        self._state_provider = state_provider
        self._source_provider = source_provider

    @property
    def root(self) -> Path:
        return self._root

    def project(self, source: EmployeeWorkspaceSource) -> EmployeeWorkspaceSnapshot:
        if not isinstance(source, EmployeeWorkspaceSource):
            raise TypeError("source must be EmployeeWorkspaceSource")
        files = render_workspace_files(source)
        agents_content = files["workspace/AGENTS.md"]
        if len(agents_content) > 8192:
            raise WorkspaceProjectionError("AGENTS.md exceeds 8192 bytes")
        root_fd = open_directory_tree(self._root)
        try:
            agent_fd = open_child_directory(root_fd, source.agent_id)
            try:
                for relative_path in sorted(files):
                    atomic_write_relative(agent_fd, relative_path, files[relative_path])
                runtime_fd = open_child_directory(agent_fd, "runtime")
                try:
                    checkpoint_fd = open_child_directory(runtime_fd, "checkpoints")
                    os.close(checkpoint_fd)
                finally:
                    os.close(runtime_fd)
                os.fsync(agent_fd)
            finally:
                os.close(agent_fd)
        finally:
            os.close(root_fd)
        snapshot = EmployeeWorkspaceSnapshot(
            agent_id=source.agent_id,
            identity_version=source.identity_version,
            knowledge_generation=source.knowledge_generation,
            active_assignment_id=source.active_assignment_id,
            instruction_digest=hashlib.sha256(agents_content).hexdigest(),
            projection_sequence=source.projection_sequence,
            projection_hash=source.projection_hash,
        )
        self.verify(snapshot)
        return snapshot

    def rebuild(self, tenant_key: str, agent_id: str) -> EmployeeWorkspaceSnapshot:
        if self._state_provider is None:
            raise WorkspaceProjectionError("state provider is unavailable")
        state = self._state_provider()
        employee = state.employees.get(agent_id)
        if (
            employee is None
            or employee.tenant_key != tenant_key
            or employee.state is EmployeeState.ARCHIVED
        ):
            raise KeyError(agent_id)
        if self._source_provider is not None:
            source = self._source_provider(tenant_key, agent_id)
            if source.tenant_key != tenant_key or source.agent_id != agent_id:
                raise WorkspaceProjectionError("workspace source authority mismatch")
            return self.project(source)
        return self.project(
            EmployeeWorkspaceSource(
                tenant_key=employee.tenant_key,
                agent_id=employee.agent_id,
                name=employee.name,
                role=employee.role,
                persona=employee.persona,
                personality_traits=employee.personality_traits,
                capabilities=employee.capabilities,
                permissions=employee.permissions,
                tool=employee.tool,
                model=employee.model,
                identity_version=employee.aggregate_version,
                projection_sequence=int(getattr(state, "cursor_sequence", 0)),
                projection_hash=str(getattr(state, "cursor_hash", "")),
            )
        )

    def rebuild_all(self) -> tuple[EmployeeWorkspaceSnapshot, ...]:
        if self._state_provider is None:
            raise WorkspaceProjectionError("state provider is unavailable")
        state = self._state_provider()
        return tuple(
            self.rebuild(employee.tenant_key, agent_id)
            for agent_id, employee in sorted(state.employees.items())
            if employee.state is not EmployeeState.ARCHIVED
        )

    def verify(self, snapshot: EmployeeWorkspaceSnapshot) -> None:
        lint_employee_workspace(snapshot, self._root)


__all__ = ["EmployeeWorkspaceProjector"]
