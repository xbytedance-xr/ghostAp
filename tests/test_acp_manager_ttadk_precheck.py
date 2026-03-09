import logging

import pytest


def test_acp_session_manager_ttadk_uses_precheck_fn(monkeypatch, caplog):
    """ACP TTADK 启动应通过 coordinator 的 precheck_fn 走统一 helper，并将 validated 才透传 model。"""
    from src.acp.manager import ACPSessionManager

    caplog.set_level(logging.INFO)

    # --- stub ttadk manager ---
    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **k: _Mgr())

    # --- stub SyncACPSession to observe passed model_name ---
    created: list[dict] = []

    class _FakeSyncSession:
        def __init__(self, agent_type: str, cwd: str, model_name=None, agent_cmd=None, agent_args=None):
            created.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
            self._agent_type = agent_type
            self._agent_args = ["-m", model_name] if model_name else []
            self.session_id = "sid"
            self.last_active = 0.0
            self.created_at = 0.0
            self.message_count = 0
            self.last_query = ""
            self.is_resumed = False

        def describe_agent(self) -> str:
            return "fake"

        def start(self, startup_timeout: float = 60) -> str:
            return "sid"

        def load_session(self, session_id: str) -> None:
            return None

        def load_local_history(self, session_id=None, limit: int = 200):
            return []

        def close(self):
            return None

        def to_snapshot(self):
            return {}

        def get_session_info(self):
            return ""

        def is_server_running(self):
            return True

        def is_server_healthy(self, healthcheck_timeout: float = 2.0):
            return True

    monkeypatch.setattr("src.acp.manager.SyncACPSession", _FakeSyncSession)

    # --- stub precheck helper to control passthrough model ---
    def fake_precheck(*, agent_type, cwd, model_intent, manager=None, startup_probe_timeout_s=None):
        assert agent_type == "ttadk_codex"
        assert cwd == "/tmp"
        assert model_intent == "gpt-5.2"
        return {
            "tool": "codex",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
        }

    monkeypatch.setattr("src.ttadk.startup_common.precheck_ttadk_startup_model", fake_precheck)

    # --- run ---
    mgr = ACPSessionManager(agent_type="coco")
    s = mgr.start_session(
        chat_id="c",
        cwd="/tmp",
        startup_timeout=0.1,
        agent_type_override="ttadk_codex",
        model_name="gpt-5.2",
        project_id="p",
    )
    assert s is not None
    assert created
    assert created[-1]["model_name"] == "gpt-5.2-codex-ttadk"


def test_acp_session_manager_ttadk_startup_fail_log_has_non_empty_error_blob(monkeypatch, caplog):
    """防回归：TTADK 启动失败且 str(e)=='' 时，ACP 失败日志仍应包含非空 error_blob。"""
    from src.acp.manager import ACPSessionManager

    caplog.set_level(logging.WARNING)

    # --- stub ttadk manager ---
    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **k: _Mgr())

    # --- stub precheck helper: validated=True to force passthrough_model not None ---
    def fake_precheck(*, agent_type, cwd, model_intent, manager=None, startup_probe_timeout_s=None):
        return {
            "tool": "codex",
            "input_model": model_intent,
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
            "diagnostics": {},
        }

    monkeypatch.setattr("src.ttadk.startup_common.precheck_ttadk_startup_model", fake_precheck)

    # --- stub SyncACPSession to fail with empty str(exception) ---
    class _EmptyStrErr(Exception):
        def __str__(self):
            return ""

    class _FakeSyncSession:
        def __init__(self, agent_type: str, cwd: str, model_name=None, agent_cmd=None, agent_args=None):
            self._agent_type = agent_type
            self._agent_cmd = agent_cmd or "ttadk"
            # keep args minimal; presence doesn't matter for this test
            self._agent_args = ["acp", "serve"]
            self.session_id = "sid"
            self.last_active = 0.0
            self.created_at = 0.0
            self.message_count = 0
            self.last_query = ""
            self.is_resumed = False

        def describe_agent(self) -> str:
            return "fake"

        def start(self, startup_timeout: float = 60) -> str:
            raise _EmptyStrErr()

        def load_session(self, session_id: str) -> None:
            return None

        def load_local_history(self, session_id=None, limit: int = 200):
            return []

        def close(self):
            return None

        def to_snapshot(self):
            return {}

        def get_session_info(self):
            return ""

        def is_server_running(self):
            return False

        def is_server_healthy(self, healthcheck_timeout: float = 2.0):
            return False

    monkeypatch.setattr("src.acp.manager.SyncACPSession", _FakeSyncSession)

    mgr = ACPSessionManager(agent_type="coco")
    with pytest.raises(RuntimeError):
        mgr.start_session(
            chat_id="c",
            cwd="/tmp",
            startup_timeout=0.1,
            agent_type_override="ttadk_codex",
            model_name="gpt-5.2",
            project_id="p",
        )

    logs = "\n".join([r.getMessage() for r in caplog.records])
    assert (
        "Session start failed" in logs
        or "Engine session start failed" in logs
        or "TTADK coordinator failed" in logs
    )
    assert "attempts_summary=" in logs
    # 关键：error_blob 至少为 '(empty)'，不得为空
    assert "(empty)" in logs

    # 新增断言：失败日志必须包含 err_type/err_repr（不依赖 str(err)）
    assert "err_type=" in logs
    assert "err_repr=" in logs


def test_format_ttadk_startup_attempts_truncates_and_redacts(monkeypatch):
    """防回归：attempts_summary 应截断且弱脱敏。"""
    from src.acp.diagnostics import format_attempts_summary

    # 让脱敏/截断行为可预测：将 diagnostics 配置收紧到较小值
    class _Cfg:
        redact_enabled = True
        redact_patterns = [r"(?i)token\s*[:=]\s*[^\s]+"]
        redact_replacement = "***REDACTED***"
        args_limit = 0
        snippet_limit = 120
        total_limit = 260

    monkeypatch.setattr("src.acp.diagnostics.get_diagnostics_config", lambda **kw: _Cfg())

    long = "x" * 2000
    diag = {
        "attempts": [
            {
                "phase": "start",
                "ok": False,
                "fail_phase": "start_failed",
                "decision": "start_failed",
                "error_type": "Boom",
                "stderr_snippet": f"token=abc123 {long}",
            }
        ]
    }
    s = format_attempts_summary(diag.get("attempts"), per_item_limit=120, total_limit=260)
    assert isinstance(s, str)
    # 总长度截断生效
    # 说明：`src.acp.diagnostics.truncate_text()` 会追加 "…(truncated)" 后缀，因此允许略超出 limit。
    assert len(s) <= 280
    # 弱脱敏生效
    assert "***REDACTED***" in s


def test_acp_session_manager_ttadk_startup_fail_diagnostics_summary_is_redacted(monkeypatch, caplog):
    """防回归：diagnostics_summary 必须走脱敏+截断（避免敏感信息泄露）。"""
    from src.acp.manager import ACPSessionManager

    caplog.set_level(logging.WARNING)

    # --- tighten diagnostics config for predictable truncation ---
    class _Cfg:
        acp_diagnostics_redact_enabled = True
        acp_diagnostics_redact_patterns = [r"(?i)token\s*[:=]\s*[^\s]+", r"(?i)api[_-]?key\s*[:=]\s*[^\s]+"]
        acp_diagnostics_redact_replacement = "***REDACTED***"
        acp_diagnostics_args_limit = 40
        acp_diagnostics_snippet_limit = 60
        acp_diagnostics_total_limit = 220

    monkeypatch.setattr("src.acp.manager.get_settings", lambda: _Cfg())
    monkeypatch.setattr("src.acp.sync_adapter.get_settings", lambda: _Cfg())

    # --- stub ttadk manager ---
    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **k: _Mgr())

    # --- stub precheck helper ---
    def fake_precheck(*, agent_type, cwd, model_intent, manager=None, startup_probe_timeout_s=None):
        return {
            "tool": "codex",
            "input_model": model_intent,
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
            "diagnostics": {},
        }

    monkeypatch.setattr("src.ttadk.startup_common.precheck_ttadk_startup_model", fake_precheck)

    # --- stub SyncACPSession to fail with empty str(exception) AND carry sensitive snippets ---
    class _EmptyStrErr(Exception):
        def __str__(self):
            return ""

    class _FakeSyncSession:
        def __init__(self, agent_type: str, cwd: str, model_name=None, agent_cmd=None, agent_args=None):
            self._agent_type = agent_type
            self._agent_cmd = "ttadk"
            self._agent_args = ["acp", "serve", "--token=abc123", "--api_key=sk-secret-1234567890"]
            self.session_id = "sid"
            self.last_active = 0.0
            self.created_at = 0.0
            self.message_count = 0
            self.last_query = ""
            self.is_resumed = False

        def describe_agent(self) -> str:
            return "fake"

        def start(self, startup_timeout: float = 60) -> str:
            e = _EmptyStrErr()
            # Provide raw snippet fields for diagnostics builder
            setattr(e, "stderr", "token=abc123 api_key=sk-secret-1234567890 " + ("x" * 1000))
            raise e

        def load_session(self, session_id: str) -> None:
            return None

        def load_local_history(self, session_id=None, limit: int = 200):
            return []

        def close(self):
            return None

        def to_snapshot(self):
            return {}

        def get_session_info(self):
            return ""

        def is_server_running(self):
            return False

        def is_server_healthy(self, healthcheck_timeout: float = 2.0):
            return False

    monkeypatch.setattr("src.acp.manager.SyncACPSession", _FakeSyncSession)

    mgr = ACPSessionManager(agent_type="coco")
    with pytest.raises(RuntimeError):
        mgr.start_session(
            chat_id="c",
            cwd="/tmp",
            startup_timeout=0.1,
            agent_type_override="ttadk_codex",
            model_name="gpt-5.2",
            project_id="p",
        )

    logs = "\n".join([r.getMessage() for r in caplog.records])
    # 关键：必须脱敏
    assert "abc123" not in logs
    assert "sk-secret" not in logs
    assert "***REDACTED***" in logs
    # 关键：必须有 diagnostics_summary 字段（本用例提供了 cmd/args/stderr）
    assert "diagnostics_summary=" in logs
    # 截断生效：总长不应过长（允许少量浮动）
    assert len(logs) <= 2000
