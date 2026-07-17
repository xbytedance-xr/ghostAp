from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.slock_engine.engine import SlockEngine
from src.slock_engine.models import AgentIdentity


def test_employee_workspace_is_read_only_and_project_is_policy_writable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "agents/agt_1/workspace"
    project.mkdir()
    workspace.mkdir(parents=True)
    engine = SlockEngine.__new__(SlockEngine)
    engine.root_path = str(project)
    engine._settings = SimpleNamespace(  # noqa: SLF001
        slock_tool_path_restrictions=[],
        slock_dangerous_shell_patterns=[],
    )
    engine._dangerous_shell_patterns = None  # noqa: SLF001
    session = MagicMock()
    agent = AgentIdentity(
        agent_id="agt_1",
        name="Atlas",
        workspace_path=str(workspace),
        security_profile="employee_v1",
        permissions=["file_read", "file_write", "shell", "git"],
        capabilities=["file_read", "file_write", "shell", "git"],
    )

    engine._apply_tool_restrictions(session, agent)  # noqa: SLF001
    tool_filter = session.set_tool_filter.call_args.args[0]

    assert tool_filter("file_read", {"path": str(workspace / "IDENTITY.md")}) is True
    assert tool_filter("file_write", {"path": str(workspace / "NOW.md")}) is False
    assert tool_filter("file_write", {"path": str(project / "result.txt")}) is True
    assert tool_filter("file_read", {"path": str(project / ".env")}) is False
    assert tool_filter("file_read", {"path": str(tmp_path / "vault/key")}) is False
    assert tool_filter("shell", {"command": "pwd", "cwd": str(workspace)}) is False
    assert tool_filter("shell", {"command": "pwd", "cwd": str(project)}) is True

