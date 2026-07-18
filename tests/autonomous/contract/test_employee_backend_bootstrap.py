from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_session import factory as session_factory
from src.agent_session.factory import employee_session_environment
from src.autonomous.gateway.env_scope import build_employee_process_env
from src.autonomous.runtime.employee_session import EmployeeSessionBootstrap
from src.slock_engine.models import AgentIdentity


@pytest.mark.parametrize(
    "backend",
    ("codex", "coco", "traex", "claude", "gemini", "ttadk_codex"),
)
def test_every_backend_receives_same_explicit_employee_bootstrap(
    tmp_path: Path,
    backend: str,
) -> None:
    employee = tmp_path / "agents/agt_boot"
    workspace = employee / "workspace"
    workspace.mkdir(parents=True)
    instruction = b"# Employee: Atlas\n\nUse durable task state.\n"
    (workspace / "AGENTS.md").write_bytes(instruction)
    (employee / "runtime/codex-home").mkdir(parents=True)
    agent = AgentIdentity(
        agent_id="agt_boot",
        name="Atlas",
        agent_type=backend,
        model_name="model",
        workspace_path=str(workspace),
        security_profile="employee_v1",
        permissions=["file_read"],
        capabilities=["file_read"],
    )

    bootstrap = EmployeeSessionBootstrap.from_agent(
        tenant_key="tenant_1",
        agent=agent,
        project_root=str(tmp_path / "project"),
        identity_version=7,
    )

    assert bootstrap.instruction_digest == hashlib.sha256(instruction).hexdigest()
    assert bootstrap.session_key.identity_version == 7
    assert bootstrap.session_key.instruction_digest == bootstrap.instruction_digest
    assert bootstrap.session_key.effort == agent.reasoning_effort
    assert bootstrap.session_key.backend == backend
    assert bootstrap.instruction_digest in bootstrap.wrap_prompt("do work")
    assert "agt_boot" in bootstrap.wrap_prompt("do work")
    assert bootstrap.workspace_root == str(workspace.resolve())


def test_codex_home_is_explicit_not_inherited(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "runtime/codex-home"
    env = build_employee_process_env(
        {"PATH": "/usr/bin", "CODEX_HOME": "/manager/codex"},
        employee_home=str(home),
        codex_home=str(codex_home),
    )
    assert env["HOME"] == str(home)
    assert env["CODEX_HOME"] == str(codex_home)


@pytest.mark.parametrize("backend", ("claude", "ttadk_codex"))
def test_employee_cli_backend_captures_explicit_env_without_switching_to_acp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    env = {"PATH": "/usr/bin", "HOME": str(tmp_path / "employee")}
    monkeypatch.setattr(
        session_factory,
        "get_settings",
        lambda: SimpleNamespace(
            rate_limit_retry_enabled=False,
            acp_startup_timeout=1.0,
            ttadk_cwd_debug_enabled=False,
        ),
    )
    monkeypatch.setattr(session_factory.SyncClaudeCLISession, "start", lambda self: "sid")
    monkeypatch.setattr(session_factory.SyncTTADKCLISession, "start", lambda self: "sid")
    monkeypatch.setattr(
        "src.ttadk.startup_common.precheck_ttadk_startup_model",
        lambda **_kwargs: {
            "tool": "codex",
            "input_model": "",
            "model": None,
            "validated": True,
            "source": "test",
            "warnings": (),
        },
    )

    with employee_session_environment(env):
        session = session_factory.create_engine_session(
            agent_type=backend,
            cwd=str(tmp_path),
            require_tool_filter=True,
        )

    assert "backend=cli" in session.describe_agent()
    assert session.employee_process_env == env
