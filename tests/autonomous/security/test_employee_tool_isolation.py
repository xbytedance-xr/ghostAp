from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agent_session.employee_cli_sandbox import (
    EmployeeCLISandbox,
    EmployeeCLISandboxError,
)
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


def test_employee_cli_namespace_hides_sensitive_and_peer_employee_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    employee = tmp_path / "agents/agt_1"
    workspace = employee / "workspace"
    peer = tmp_path / "agents/agt_2"
    project.mkdir()
    workspace.mkdir(parents=True)
    peer.mkdir(parents=True)
    (project / ".env").write_text("SECRET=never-visible", encoding="utf-8")
    (project / "vault").mkdir()
    (project / "journal").mkdir()
    sandbox = EmployeeCLISandbox(
        cwd=str(project),
        process_env={"PATH": "/usr/bin", "HOME": str(employee)},
    )

    sandbox.configure(
        command="sh",
        read_only_roots=(str(project), str(workspace)),
        writable_roots=(str(project),),
    )
    output = project / "output.txt"
    argv = sandbox.wrap_argv(
        ["sh", "-c", f"test ! -s {project / '.env'} && printf ok > {output}"]
    )
    rendered = "\0".join(argv)

    assert argv[0].endswith("bwrap")
    assert f"--bind\0{project}\0{project}" in rendered
    assert f"--ro-bind\0{workspace}\0{workspace}" in rendered
    assert f"--ro-bind\0/dev/null\0{project / '.env'}" in rendered
    assert f"--tmpfs\0{project / 'vault'}" in rendered
    assert f"--tmpfs\0{project / 'journal'}" in rendered
    assert str(peer) not in rendered
    assert subprocess.run(argv, check=False).returncode == 0
    assert output.read_text(encoding="utf-8") == "ok"


def test_employee_cli_refuses_spawn_before_filesystem_policy() -> None:
    sandbox = EmployeeCLISandbox(
        cwd="/tmp",
        process_env={"PATH": "/usr/bin", "HOME": "/tmp/employee"},
    )

    with pytest.raises(EmployeeCLISandboxError, match="not configured"):
        sandbox.wrap_argv(["true"])
