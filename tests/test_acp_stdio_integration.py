"""End-to-end ACP stdio integration test.

This validates that GhostAP's ACP client sends JSON-RPC params that match
`agent-client-protocol`'s pydantic schema (incl. aliases like `sessionId`).

It spins up a minimal ACP agent in a subprocess (python -c) using
`acp.stdio.AgentSideConnection` and exercises:
- initialize
- new_session
- load_session (used by GhostAP health_check)
- prompt
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from src.acp.session import ACPSession


_FAKE_AGENT_CODE = textwrap.dedent(
    r"""
    import asyncio
    import sys
    import uuid

    from acp.helpers import update_agent_message_text
    from acp.schema import InitializeResponse, LoadSessionResponse, NewSessionResponse, PromptResponse
    from acp.stdio import AgentSideConnection


    class FakeAgent:
        def __init__(self):
            self._conn = None
            self._sessions = set()

        def on_connect(self, conn):
            self._conn = conn

        async def initialize(self, protocol_version: int, client_capabilities=None, client_info=None, **kwargs):
            return InitializeResponse(protocol_version=protocol_version)

        async def new_session(self, cwd: str, mcp_servers=None, **kwargs):
            sid = "s_" + uuid.uuid4().hex[:8]
            self._sessions.add(sid)
            return NewSessionResponse(session_id=sid)

        async def load_session(self, cwd: str, session_id: str, mcp_servers=None, **kwargs):
            self._sessions.add(session_id)
            return LoadSessionResponse()

        async def prompt(self, prompt, session_id: str, **kwargs):
            # Emit a streaming message chunk so GhostAP can aggregate text.
            if self._conn is not None:
                await self._conn.session_update(session_id=session_id, update=update_agent_message_text("hello-from-fake"))
            return PromptResponse(stop_reason="end_turn")


    async def _make_stdio_streams():
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        reader_protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)

        transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        return reader, writer


    async def main():
        reader, writer = await _make_stdio_streams()
        conn = AgentSideConnection(FakeAgent(), writer, reader, listening=True)
        await conn.listen()


    if __name__ == "__main__":
        asyncio.run(main())
    """
).strip()


@pytest.mark.asyncio
async def test_acp_stdio_prompt_and_health_check(tmp_path):
    # Use a temp cwd so the agent is sandboxed.
    cwd = str(tmp_path)

    s = ACPSession(
        agent_cmd=sys.executable,
        agent_args=["-u", "-c", _FAKE_AGENT_CODE],
        cwd=cwd,
    )

    try:
        session_id = await s.start()
        assert session_id

        # health_check internally calls `session/load`.
        assert await s.health_check(timeout=2.0) is True

        r = await s.prompt("ping")
        assert r.stop_reason
        assert "hello-from-fake" in (r.text or "")
    finally:
        await s.close()


def test_start_session_with_retry_logs_diagnostics_on_empty_error(monkeypatch, caplog):
    """回归：启动失败但异常 message 为空时，日志仍应包含 error_type/上下文，避免线上不可定位。"""
    import logging
    from src.acp.sync_adapter import start_session_with_retry

    class _EmptyError(RuntimeError):
        def __str__(self):
            return ""

    class _FakeSyncSession:
        def __init__(self, agent_type: str, cwd: str, model_name=None):
            self._agent_type = agent_type
            self._cwd = cwd
            self._agent_cmd = "ttadk"
            self._agent_args = ["code", "-t", "codex"]

        def describe_agent(self) -> str:
            return f"cmd={self._agent_cmd} args={' '.join(self._agent_args)} cwd={self._cwd}"

        def start(self, startup_timeout: float = 60) -> str:
            raise _EmptyError()

        def close(self):
            return

    # Patch SyncACPSession used inside start_session_with_retry
    monkeypatch.setattr("src.acp.sync_adapter.SyncACPSession", _FakeSyncSession)
    monkeypatch.setattr("src.acp.sync_adapter.get_settings", lambda: type("S", (), {"acp_startup_retries": 1})())

    caplog.set_level(logging.WARNING)
    with pytest.raises(Exception):
        start_session_with_retry(agent_type="ttadk_codex", cwd="/tmp", startup_timeout=0.1, model_name="m")

    blob = "\n".join([r.getMessage() for r in caplog.records])
    assert "Engine session start failed" in blob
    assert "error_type" in blob
    assert "ttadk_codex" in blob


def test_acp_manager_ensure_session_start_failure_empty_exc_has_non_empty_detail(monkeypatch, caplog):
    """回归：ACPSessionManager 启动失败且异常 message 为空时，不应出现空原因。

    - 日志：必须包含稳定字段 fail_reason/error_text
    - 抛错：最终 RuntimeError 的 detail 不得为空（避免线上 `...: ` 空串）
    """

    import logging
    import pytest

    from src.acp.manager import ACPSessionManager

    class _EmptyErr(RuntimeError):
        def __str__(self):
            return ""

    class _FakeSyncSession:
        def __init__(self, *args, **kwargs):
            self._agent_cmd = "ttadk"
            self._agent_args = ["acp", "serve"]
            self._cwd = str(kwargs.get("cwd") or ".")

        def describe_agent(self) -> str:
            return f"cmd={self._agent_cmd} args={' '.join(self._agent_args)} cwd={self._cwd}"

        def start(self, startup_timeout: float = 60) -> str:
            raise _EmptyErr()

        def close(self):
            return

        def load_local_history(self, *args, **kwargs):
            return []

    # Ensure we don't touch real ACP or external binaries.
    monkeypatch.setattr("src.acp.manager.SyncACPSession", _FakeSyncSession)
    monkeypatch.setattr("src.acp.manager.SyncClaudeCLISession", _FakeSyncSession)
    monkeypatch.setattr(
        "src.acp.manager.get_settings",
        lambda: type(
            "S",
            (),
            {
                "acp_startup_retries": 1,
                "acp_startup_timeout": 0.1,
                # diagnostics config defaults
                "acp_diagnostics_redact_enabled": True,
                "acp_diagnostics_redact_patterns": [],
                "acp_diagnostics_redact_replacement": "***REDACTED***",
                "acp_diagnostics_args_limit": 200,
                "acp_diagnostics_snippet_limit": 200,
                "acp_diagnostics_total_limit": 800,
            },
        )(),
    )

    caplog.set_level(logging.WARNING)
    # 注入 fake starter：冻结“可注入启动器”接口形状，避免未来解耦重构时回归。
    def _starter(**kw):
        assert kw.get("agent_type") == "coco"
        return (_FakeSyncSession(cwd=kw.get("cwd")), "", {"attempts": []})

    mgr = ACPSessionManager("coco", session_starter=_starter)

    with pytest.raises(RuntimeError) as ei:
        mgr.ensure_session(chat_id="c", project_id="p", cwd="/tmp", startup_timeout=0.1)

    # 1) log line must have non-empty error_text
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Session start failed" in logs
    assert "error_text=" in logs
    # 2) raised error detail must be non-empty
    assert str(ei.value).strip()
    assert ":" in str(ei.value)


def test_resolve_agent_spec_ttadk_adds_pty_flag_when_enabled(monkeypatch):
    """回归：TTADK agent spec 在启用 PTY 时应注入 wrapper `--pty`。"""
    from src.acp.sync_adapter import resolve_agent_spec

    # 避免触发真实外部二进制探测
    monkeypatch.setattr("src.acp.sync_adapter._resolve_ttadk_passthrough_args", lambda tool: "acp serve")

    cmd, args = resolve_agent_spec("ttadk_codex", model_name=None, ttadk_use_pty=True)
    assert cmd in ("python3", "python")
    # TTADK wrapper should be launched via module mode to avoid script/import drift.
    assert args[:2] == ["-m", "src.utils.ttadk_wrapper"]
    assert "--pty" in args


def test_start_ttadk_session_with_pty_retry_on_stdin_not_tty(monkeypatch, caplog):
    """TTADK 启动：首次遇到 stdin-not-tty 应触发 PTY 重试一次。"""
    import logging
    from src.acp import sync_adapter

    calls: list[dict] = []

    def _fake_start_session_with_retry(*, agent_type, cwd, startup_timeout, model_name, session_cls=None, ttadk_use_pty=False, log_failures=True, **kwargs):
        calls.append({
            "agent_type": agent_type,
            "cwd": cwd,
            "model_name": model_name,
            "pty": bool(ttadk_use_pty),
            "log_failures": bool(log_failures),
        })
        # 第一次失败：stdin is not a terminal
        if not ttadk_use_pty:
            e = RuntimeError("stdin is not a terminal")
            setattr(e, "stderr_snippet", "stdin is not a terminal")
            raise e

        # 第二次（PTY）成功：返回一个最小 session
        class _S:
            session_id = "sid"

            def describe_agent(self):
                return "fake"

        return _S()

    monkeypatch.setattr(sync_adapter, "start_session_with_retry", _fake_start_session_with_retry)
    monkeypatch.setattr(sync_adapter, "get_settings", lambda: type("S", (), {
        "ttadk_pty_enabled": True,
        "ttadk_pty_retry_once": True,
        "ttadk_pty_retry_cooldown_s": 0.0,
    })())

    caplog.set_level(logging.WARNING)
    s = sync_adapter.start_ttadk_session_with_pty_retry(
        agent_type="ttadk_codex",
        cwd="/tmp",
        startup_timeout=0.1,
        model_name="gpt-5.2-codex-ttadk",
        session_cls=None,
        log_failures=True,
    )
    assert getattr(s, "session_id", "") == "sid"
    assert len(calls) == 2
    assert calls[0]["pty"] is False
    assert calls[1]["pty"] is True
    assert "stdin-not-tty" in caplog.text


def test_ttadk_wrapper_filters_banner_and_passes_json(tmp_path):
    """wrapper: 输出包含 banner + JSON 时，仅透传 JSON 行。"""
    import subprocess
    import sys

    cmd = [
        sys.executable,
        "-m",
        "src.utils.ttadk_wrapper",
        "--pty",
        sys.executable,
        "-c",
        'import sys; sys.stdout.write("BANNER\\n"); sys.stdout.write("{\\"jsonrpc\\":\\"2.0\\"}\\n")',
    ]

    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=6)
    assert b"BANNER" not in out
    assert b"jsonrpc" in out


def test_ttadk_start_session_with_pty_retry_compat_downgrade_kw_only(monkeypatch):
    """兼容：当 start_session_with_retry 是 kw-only 且不接受新参数时，应按约定顺序降参并成功调用。"""

    from src.acp import sync_adapter

    calls = []

    # 仅接受最老的一组关键参数（不支持 log_failures / ttadk_use_pty / session_cls）
    def kw_only_legacy_start_session_with_retry(*, agent_type, cwd, startup_timeout, model_name):
        calls.append({
            "agent_type": agent_type,
            "cwd": cwd,
            "startup_timeout": startup_timeout,
            "model_name": model_name,
        })

        class _S:
            session_id = "sid"

            def describe_agent(self):
                return "fake"

        return _S()

    # 降参顺序断言：先去 log_failures，再去 ttadk_use_pty，再去 session_cls
    observed = {"step": 0}

    def start_session_with_retry_probe(**kw):
        step = int(observed["step"])
        if step == 0:
            assert "log_failures" in kw
            observed["step"] = 1
            raise TypeError("no log_failures")
        if step == 1:
            assert "log_failures" not in kw
            assert "ttadk_use_pty" in kw
            observed["step"] = 2
            raise TypeError("no ttadk_use_pty")
        if step == 2:
            assert "log_failures" not in kw
            assert "ttadk_use_pty" not in kw
            assert "session_cls" in kw
            observed["step"] = 3
            raise TypeError("no session_cls")
        # 最终：仅保留老签名参数
        assert set(kw.keys()) == {"agent_type", "cwd", "startup_timeout", "model_name"}
        return kw_only_legacy_start_session_with_retry(**kw)

    monkeypatch.setattr(sync_adapter, "start_session_with_retry", start_session_with_retry_probe)
    monkeypatch.setattr(sync_adapter, "get_settings", lambda: type("S", (), {"ttadk_pty_enabled": True, "ttadk_pty_retry_once": False})())

    s = sync_adapter.start_ttadk_session_with_pty_retry(
        agent_type="ttadk_codex",
        cwd="/tmp",
        startup_timeout=0.1,
        model_name="m",
        # 用非 None 确保降参链路里能观测到 session_cls 的存在与移除
        session_cls=type("DummySession", (), {}),
        log_failures=True,
    )

    assert getattr(s, "session_id", "") == "sid"
    assert len(calls) == 1
    assert calls[0]["agent_type"] == "ttadk_codex"
    assert observed["step"] == 3


def test_ttadk_wrapper_emits_banner_tail_on_failure(tmp_path):
    """wrapper: 子进程非 0 退出且未输出 JSON 时，应打印 banner_tail 与 rc/cmd。"""
    import subprocess
    import sys

    cmd = [
        sys.executable,
        "-m",
        "src.utils.ttadk_wrapper",
        sys.executable,
        "-c",
        'import sys; sys.stdout.write("BANNER\\n"); sys.exit(2)',
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
    assert p.returncode == 2
    # banner_tail 与 child exited 诊断必须存在
    assert "banner_tail" in (p.stderr or "")
    assert "child exited rc=2" in (p.stderr or "")


def test_ttadk_wrapper_state_does_not_leak_between_runs():
    """wrapper: pump_filtered_stream 不应依赖全局状态，不同 state 互不影响。"""
    from src.utils import ttadk_wrapper

    class _Reader:
        def __init__(self, lines: list[bytes]):
            self._lines = list(lines)
            self._after = b""

        def readline(self):
            return self._lines.pop(0) if self._lines else b""

        def read(self, n: int):
            # no extra chunk after json
            return b""

    s1 = ttadk_wrapper.WrapperState()
    s2 = ttadk_wrapper.WrapperState()

    r1 = _Reader([b"BANNER\n", b"{\"jsonrpc\":\"2.0\"}\n"])
    r2 = _Reader([b"ONLY_BANNER\n"])

    import io

    w1 = io.BytesIO()
    w2 = io.BytesIO()
    ttadk_wrapper.pump_filtered_stream(r1, w1, s1)
    ttadk_wrapper.pump_filtered_stream(r2, w2, s2)

    assert s1.json_started is True
    assert s2.json_started is False
    assert b"jsonrpc" in w1.getvalue()
    assert w2.getvalue() == b""
