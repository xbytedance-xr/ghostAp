"""Integrity and content lint for generated employee workspaces."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .layout import REQUIRED_WORKSPACE_FILES
from .models import EmployeeWorkspaceSnapshot

_SECRET_PATTERNS = (
    re.compile(rb"app[_-]?secret\s*[:=]", re.IGNORECASE),
    re.compile(rb"credential[_-]?ref\s*[:=]", re.IGNORECASE),
    re.compile(rb"(?:access[_-]?)?token\s*[:=]", re.IGNORECASE),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{6,}"),
    re.compile(rb"(?:^|/)\.env(?:\b|/)", re.IGNORECASE),
    re.compile(rb"<(?:think|analysis)>", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class WorkspaceLintIssue:
    code: str
    path: str


class WorkspaceLintError(RuntimeError):
    def __init__(self, issues: tuple[WorkspaceLintIssue, ...]) -> None:
        self.issues = issues
        super().__init__("employee workspace lint failed")


def lint_employee_workspace(
    snapshot: EmployeeWorkspaceSnapshot,
    agents_root: str | Path,
) -> None:
    issues = inspect_employee_workspace(snapshot, agents_root)
    if issues:
        raise WorkspaceLintError(issues)


def inspect_employee_workspace(
    snapshot: EmployeeWorkspaceSnapshot,
    agents_root: str | Path,
) -> tuple[WorkspaceLintIssue, ...]:
    """Return secret-free lint codes for operational status surfaces."""

    root = Path(agents_root).expanduser().absolute() / snapshot.agent_id
    issues: list[WorkspaceLintIssue] = []
    contents: dict[str, bytes] = {}
    for relative in REQUIRED_WORKSPACE_FILES:
        path = root / relative
        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            issues.append(WorkspaceLintIssue("missing_file", relative))
            continue
        if path.is_symlink() or not path.is_file():
            issues.append(WorkspaceLintIssue("unsafe_type", relative))
            continue
        if stat_result.st_mode & 0o777 != 0o600:
            issues.append(WorkspaceLintIssue("unsafe_mode", relative))
        content = path.read_bytes()
        contents[relative] = content
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            issues.append(WorkspaceLintIssue("secret_content", relative))
    agents = contents.get("workspace/AGENTS.md", b"")
    if len(agents) > 8192:
        issues.append(WorkspaceLintIssue("agents_too_large", "workspace/AGENTS.md"))
    codex = contents.get("runtime/codex-home/AGENTS.md", b"")
    if agents and codex and agents != codex:
        issues.append(WorkspaceLintIssue("instruction_mismatch", "runtime/codex-home/AGENTS.md"))
    if agents and hashlib.sha256(agents).hexdigest() != snapshot.instruction_digest:
        issues.append(WorkspaceLintIssue("instruction_digest", "workspace/AGENTS.md"))
    for directory, subdirectories, _files in os.walk(root, followlinks=False):
        if Path(directory).is_symlink():
            issues.append(WorkspaceLintIssue("unsafe_type", str(Path(directory).relative_to(root))))
        for name in subdirectories:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                issues.append(WorkspaceLintIssue("unsafe_type", str(candidate.relative_to(root))))
            elif candidate.stat().st_mode & 0o777 != 0o700:
                issues.append(WorkspaceLintIssue("unsafe_mode", str(candidate.relative_to(root))))
    return tuple(issues)


__all__ = [
    "WorkspaceLintError",
    "WorkspaceLintIssue",
    "inspect_employee_workspace",
    "lint_employee_workspace",
]
