"""Idempotent canonical-to-workspace migration for pre-v1 employees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..workspace import EmployeeWorkspaceProjector, EmployeeWorkspaceSnapshot


class EmployeeWorkspaceMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EmployeeWorkspaceMigrationResult:
    snapshots: tuple[EmployeeWorkspaceSnapshot, ...]


class EmployeeWorkspaceV1Migrator:
    """Reproject canonical identities; legacy mutable files are never inputs."""

    def __init__(self, projector: EmployeeWorkspaceProjector) -> None:
        if not isinstance(projector, EmployeeWorkspaceProjector):
            raise TypeError("workspace projector is required")
        self._projector = projector

    def migrate(
        self,
        employees: tuple[tuple[str, str], ...],
    ) -> EmployeeWorkspaceMigrationResult:
        snapshots: list[EmployeeWorkspaceSnapshot] = []
        root = self._projector.root
        for tenant_key, agent_id in sorted(employees):
            candidate = root / agent_id
            self._preflight(candidate, root)
            try:
                snapshots.append(self._projector.rebuild(tenant_key, agent_id))
            except Exception as exc:
                raise EmployeeWorkspaceMigrationError(
                    "canonical employee workspace migration failed"
                ) from exc
        return EmployeeWorkspaceMigrationResult(tuple(snapshots))

    @staticmethod
    def _preflight(candidate: Path, root: Path) -> None:
        if candidate.parent != root or not candidate.name.startswith("agt_"):
            raise EmployeeWorkspaceMigrationError("invalid employee workspace path")
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise EmployeeWorkspaceMigrationError("unsafe employee workspace path")


__all__ = [
    "EmployeeWorkspaceMigrationError",
    "EmployeeWorkspaceMigrationResult",
    "EmployeeWorkspaceV1Migrator",
]
