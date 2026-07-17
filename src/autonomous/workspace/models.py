"""Frozen inputs and outputs for employee workspace projections."""

from __future__ import annotations

import re
from dataclasses import dataclass

_AGENT_ID = re.compile(r"agt_[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class WorkspaceProjectionError(RuntimeError):
    """A workspace cannot be projected without violating its contract."""


@dataclass(frozen=True, slots=True)
class EmployeeWorkspaceSource:
    tenant_key: str
    agent_id: str
    name: str
    role: str
    persona: str
    personality_traits: tuple[str, ...]
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...]
    tool: str
    model: str
    identity_version: int
    projection_sequence: int
    projection_hash: str
    knowledge_generation: int = 0
    active_assignment_id: str = ""
    checkpoint_ref: str = ""
    source_refs: tuple[tuple[str, str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if _AGENT_ID.fullmatch(self.agent_id) is None:
            raise WorkspaceProjectionError("agent_id must be a canonical safe component")
        for field_name in ("tenant_key", "name", "tool"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise WorkspaceProjectionError(f"{field_name} is required")
        if not isinstance(self.role, str) or not isinstance(self.persona, str):
            raise WorkspaceProjectionError("identity text must be strings")
        for field_name in ("personality_traits", "capabilities", "permissions"):
            values = tuple(getattr(self, field_name))
            if any(not isinstance(value, str) or not value for value in values):
                raise WorkspaceProjectionError(f"invalid {field_name}")
            object.__setattr__(self, field_name, values)
        if (
            isinstance(self.identity_version, bool)
            or self.identity_version < 0
            or isinstance(self.knowledge_generation, bool)
            or self.knowledge_generation < 0
            or isinstance(self.projection_sequence, bool)
            or self.projection_sequence < 0
        ):
            raise WorkspaceProjectionError("projection versions must be non-negative")
        if self.projection_hash and _HASH.fullmatch(self.projection_hash) is None:
            raise WorkspaceProjectionError("projection_hash must be sha256")
        object.__setattr__(self, "source_refs", tuple(self.source_refs))


@dataclass(frozen=True, slots=True)
class EmployeeWorkspaceSnapshot:
    agent_id: str
    identity_version: int
    knowledge_generation: int
    active_assignment_id: str
    instruction_digest: str
    projection_sequence: int
    projection_hash: str


__all__ = [
    "EmployeeWorkspaceSnapshot",
    "EmployeeWorkspaceSource",
    "WorkspaceProjectionError",
]
