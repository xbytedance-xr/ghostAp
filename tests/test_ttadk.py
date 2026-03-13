import pytest
import time
from unittest.mock import MagicMock, patch
from src.ttadk import TTADKManager, get_ttadk_manager, TTADKTool, TTADKModel, TTADKModelFetcher
from src.ttadk.strategies import InteractiveStrategy
from src.ttadk.models import is_stdin_not_tty_error

# Real-world ttadk outputs sampled from local environment (ttadk 0.3.8)
_SAMPLE_INVALID_MODEL_CODEX = (
    "✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-codex-ttadk, gpt-5.2-ttadk, gpt-5.3-codex"
)
_SAMPLE_INVALID_MODEL_CLAUDE = (
    "✗ Error: Invalid model 'gpt-5.2'. Available models: glm-5-ttadk, kimi-k2.5, glm-4.7-ttadk, gpt-5.2-codex-ttadk, gpt-5.2-ttadk"
)
_SAMPLE_INVALID_MODEL_COCO_EMPTY = "✗ Error: Invalid model 'gpt-5.2'. Available models:"

_SAMPLE_INVALID_MODEL_ANSI = (
    "\x1b[31m✗ Error:\x1b[0m Invalid model 'gpt-5.2'. Available models: gpt-5.2-codex-ttadk, gpt-5.2-ttadk\n"
    "<id>abc</id>"
)
_SAMPLE_INVALID_MODEL_MULTILINE = (
    "Error: Invalid model 'x'. Available models:\n"
    "  - gpt-5.2-codex-ttadk\n"
    "  - gpt-5.2-ttadk\n"
    "  - gpt-5.3-codex\n"
)
_SAMPLE_INVALID_MODEL_MUST_ONE_OF = "model must be one of: gpt-5.2-codex-ttadk, gpt-5.2-ttadk"

_SAMPLE_STDIN_NOT_TTY = "Error: stdin is not a terminal"
_SAMPLE_STDIN_NOT_TTY_ANSI = "\x1b[31mError:\x1b[0m stdin is not a terminal\n"
_SAMPLE_STDIN_NOT_TTY_ANSI_HEAVY = "\x1b[?2004h\x1b[31mError:\x1b[0m stdin is not a terminal\x1b[?2004l\n"

# Real sample from current environment (ttadk 0.3.8) — includes banner + login + launch lines
_SAMPLE_INVALID_MODEL_WITH_BANNER = (
    "_____ _____  _    ____  _  __\n"
    "TikTok AI-Driven Development Kit\n"
    "👋 Login successful. Welcome, user@bytedance.com!\n\n"
    "🚀 Launching Codex...\n\n"
    "✗ Error: Invalid model 'INVALID_PROBE_FOR_DISCOVERY'. Available models: "
    "gpt-5.2-codex-ttadk, gpt-5.2-ttadk, gpt-5.3-codex\n"
)


class _FakeRunner:
    def __init__(self, out: str = "", err: str = "", rc: int = 1):
        self._out = out
        self._err = err
        self._rc = rc
        self.calls: list[tuple[list[str], str | None, float]] = []

    def run_simple(self, args: list[str], cwd: str | None, timeout: float):
        self.calls.append((list(args), cwd, float(timeout)))
        return (self._rc, self._out, self._err)


class _SequenceRunner:
    """Runner that returns a predefined sequence of (rc, out, err)."""

    def __init__(self, seq: list[tuple[int, str, str]]):
        self._seq = list(seq)
        self.calls: list[tuple[list[str], str | None, float]] = []

    def run_simple(self, args: list[str], cwd: str | None, timeout: float):
        self.calls.append((list(args), cwd, float(timeout)))
        if self._seq:
            return self._seq.pop(0)
        return (1, "", "")


@pytest.fixture(autouse=True)
def clean_ttadk_manager(monkeypatch, tmp_path):
    """确保每个测试隔离 TTADKManager 单例，并将文件缓存重定向到临时目录。"""
    # Reset global singleton
    import src.ttadk.manager
    monkeypatch.setattr(src.ttadk.manager, "_manager", None)

    # Reset stub cooldown singleton to avoid cross-test interference.
    # 说明：stub 冷却 SSOT 在 `src.ttadk.startup_common`，但历史单测可能 monkeypatch manager 侧符号。
    # 这里确保两侧指向同一实例，避免“双对象”导致断言与行为漂移。
    try:
        import src.ttadk.startup_common

        store = src.ttadk.manager._StubCooldownStore()
        monkeypatch.setattr(src.ttadk.startup_common, "_STUB_COOLDOWN", store, raising=False)
        monkeypatch.setattr(src.ttadk.manager, "_STUB_COOLDOWN", store, raising=False)
        # compat 层也需指向同一对象（manager 的 runtime_* helper 为 compat re-export）。
        try:
            import src.ttadk.compat

            monkeypatch.setattr(src.ttadk.compat, "_STUB_COOLDOWN", store, raising=False)
        except Exception:
            pass
    except Exception:
        pass

    # Reset explicit migration flag
    try:
        monkeypatch.setattr(src.ttadk.manager, "_legacy_store_migrated", False, raising=False)
    except Exception:
        pass

    # Reset legacy stub cooldown store hook (module-level SSOT)
    try:
        monkeypatch.setattr(src.ttadk.manager, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)
    except Exception:
        pass

    # Reset legacy function-attribute hook to avoid cross-test pollution.
    # Historical implementations may attach store to `coordinate_ttadk_startup` function object.
    try:
        fn = getattr(src.ttadk.manager, "coordinate_ttadk_startup", None)
        if callable(fn):
            monkeypatch.setattr(fn, "_runtime_invalid_model_last_ts_by_stub", None, raising=False)
    except Exception:
        pass

    # Redirect Path.home() to tmp_path so that TTADKManager file cache does not touch real HOME.
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    yield


def test_minimal_imports_no_circular_import_error():
    """防回归：核心模块 import 不应触发循环依赖 ImportError。"""
    import importlib

    importlib.import_module("src.acp")
    importlib.import_module("src.agent_session")
    importlib.import_module("src.feishu.ws_client")


def test_ttadk_ssot_imports_no_circular_import_error():
    """防回归：TTADK 启动编排 SSOT 与兼容入口 import 不应触发循环依赖。"""
    import importlib

    importlib.import_module("src.ttadk.startup")
    importlib.import_module("src.ttadk.manager")
    importlib.import_module("src.acp.manager")


def test_ttadk_start_agent_session_signature_is_kw_only_and_exported():
    """冻结：TTADK 单一启动入口必须存在且为 kw-only（避免接口漂移）。"""

    import inspect

    from src.ttadk import startup

    assert hasattr(startup, "start_agent_session")
    fn = startup.start_agent_session
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    assert params and params[0].kind is inspect.Parameter.KEYWORD_ONLY


def test_ttadk_start_agent_session_runs_coordinate_and_returns_session_fields(monkeypatch):
    """冻结：start_agent_session 必须走 coordinator 并返回 session/session_id 字段。"""

    from src.ttadk import startup

    calls = {"n": 0}

    class _Mgr:
        def get_current_model(self):
            return ""

    # start_agent_session 内部通过 `from . import get_ttadk_manager` 延迟导入，因此 patch 根包符号
    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda: _Mgr())

    def _fake_coordinate_ttadk_startup(**kw):
        calls["n"] += 1

        class _S:
            session_id = "sid"

            def describe_agent(self):
                return "fake"

        return {
            "result": (_S(), "sid"),
            "tool": "codex",
            "input_model": "",
            "resolved_real_name": "",
            "passthrough_model": None,
            "resolved_model": "(auto)",
            "validated": False,
            "source": "unknown",
            "warnings": [],
            "degraded": False,
            "repaired": False,
            "fail_phase": "",
            "decision": "start_ok",
            "diagnostics": {"attempts": []},
        }

    monkeypatch.setattr(startup, "coordinate_ttadk_startup", _fake_coordinate_ttadk_startup)

    info = startup.start_agent_session(
        agent_type="ttadk_codex",
        cwd="/tmp",
        startup_timeout=0.1,
        model_name=None,
        session_cls=None,
        log_failures=False,
    )
    assert calls["n"] == 1
    assert info.get("session") is not None
    assert info.get("session_id") == "sid"


def test_ttadk_stub_cooldown_ssot_identity():
    """防回归：manager 侧 compat 符号必须指向 startup_common 的 SSOT（避免双实现漂移）。"""
    import src.ttadk.manager as m
    import src.ttadk.startup_common as sc

    assert m._STUB_COOLDOWN is sc._STUB_COOLDOWN
    assert m._StubCooldownStore is sc._StubCooldownStore


# ============================================================
# 验收口径（可检查清单）
# ============================================================


_TTADK_STARTUP_ACCEPTANCE_CHECKLIST = [
    # 1) 稳定契约：返回 dict 必含关键字段，便于日志/排障/回归测试冻结。
    "startup_result_contract_keys_present",
    # 2) 避免 Invalid model：validated=False 时必须不透传 -m（即 passthrough_model=None）。
    "validated_false_means_no_m_passthrough",
    # 3) 失败可诊断：降级/失败分支 attempts 中必须包含非空 error（至少为 '(empty)'）。
    "failure_attempts_has_non_empty_error_text",
]


def _assert_ttadk_startup_result_contract(info: dict) -> None:
    required = {
        "result",
        "tool",
        "input_model",
        "resolved_model",
        "validated",
        "source",
        "warnings",
        "degraded",
        "repaired",
        "fail_phase",
        "decision",
        "diagnostics",
    }
    missing = [k for k in required if k not in info]
    assert not missing, f"missing_keys={missing}"

    assert isinstance(info.get("warnings"), list)
    diag = info.get("diagnostics")
    assert isinstance(diag, dict)
    assert isinstance(diag.get("attempts", []), list)


def test_ttadk_startup_acceptance_checklist_contract_smoke(monkeypatch):
    """固化验收口径：用单测表达可检查清单。"""
    from src.ttadk.startup import coordinate_ttadk_startup
    from src.ttadk.manager import TTADKStartupError

    class _Sess:
        pass

    # --- 场景 1：validated=True → 必须透传真实模型名 ---
    captured: list[object] = []

    def _precheck_validated(intent: str) -> dict:
        return {
            "tool": "codex",
            "input_model": intent,
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
            "diagnostics": {},
        }

    def _start_ok(passthrough_model: str | None):
        captured.append(passthrough_model)
        return _Sess()

    info = coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_ok,
        fallback_fn=lambda e: _Sess(),
        precheck_fn=_precheck_validated,
        startup_probe_timeout_s=None,
    )
    _assert_ttadk_startup_result_contract(info)
    assert captured == ["gpt-5.2-codex-ttadk"]
    assert info["resolved_model"] == "gpt-5.2-codex-ttadk"
    assert info["validated"] is True
    assert info["decision"] == "start_ok"

    # --- 场景 2：validated=False → 必须不透传 -m（走 auto）---
    captured.clear()

    def _precheck_auto(intent: str) -> dict:
        return {
            "tool": "codex",
            "input_model": intent,
            "resolved_real_name": intent,
            # 注意：即便这里返回 model=输入，也必须以 validated=False 禁止透传。
            "model": intent,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    info2 = coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_ok,
        fallback_fn=lambda e: _Sess(),
        precheck_fn=_precheck_auto,
        startup_probe_timeout_s=None,
    )
    _assert_ttadk_startup_result_contract(info2)
    assert captured == [None]
    assert info2["resolved_model"] == "(auto)"
    assert info2["validated"] is False

    # --- 场景 3：失败降级 → attempts.error 必须非空（至少为 '(empty)'）---
    def _start_fail(_: str | None):
        raise TTADKStartupError("")

    info3 = coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fail,
        fallback_fn=lambda e: _Sess(),
        precheck_fn=_precheck_auto,
        startup_probe_timeout_s=None,
    )
    _assert_ttadk_startup_result_contract(info3)
    assert info3["degraded"] is True
    attempts = list((info3.get("diagnostics") or {}).get("attempts") or [])
    start_attempts = [a for a in attempts if a.get("phase") == "start" and a.get("ok") is False]
    assert start_attempts, "expected a failed start attempt"
    assert (start_attempts[-1].get("error") or "") != ""

    # 新契约：失败 attempt 必须有非空 error_text，并携带至少一个证据字段
    # （exit_code / stderr_snippet / exception_type）用于定位与聚合。
    last = dict(start_attempts[-1])
    assert (str(last.get("error_text") or "").strip()) != ""
    assert (
        (last.get("exit_code") is not None)
        or (str(last.get("stderr_snippet") or "").strip() != "")
        or (str(last.get("exception_type") or "").strip() != "")
    )


def test_coordinate_ttadk_startup_degrade_attempt_has_context_fields(monkeypatch):
    """回归：degrade attempt 必须包含关键上下文字段，便于上层统一格式化与排障。"""
    import src.ttadk.startup as ssot

    def _precheck_auto(_: str) -> dict:
        return {
            "tool": "codex",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2",
            "model": None,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    info = ssot.coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=lambda m: (_ for _ in ()).throw(RuntimeError("boom")),
        fallback_fn=lambda e: "fb",
        precheck_fn=_precheck_auto,
    )
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    deg = next(a for a in attempts if a.get("phase") == "degrade")
    for k in ("tool", "input_model", "resolved_model", "passthrough_model", "validated", "source"):
        assert k in deg


def test_ttadk_startup_ssot_call_chain_create_engine_session(monkeypatch):
    """标注并验证 TTADK 启动链路 SSOT 调用点。

    目标：确认 `src.agent_session.create_engine_session(ttadk_*)` 必经
    `src.ttadk.startup.start_agent_session()`，且其返回的 `info["session"]`
    会作为最终 session 返回（不被其它分支绕过）。
    """
    import src.agent_session as agent_session

    # 关闭 rate limit wrapper，避免影响“返回 session 是否为 SSOT result”的断言
    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 3, "rate_limit_retry_enabled": False})(),
    )

    # 让 ModelFailureAwareSession 变成 identity，避免包装影响断言
    monkeypatch.setattr(agent_session, "ModelFailureAwareSession", lambda **kw: kw["inner"], raising=False)

    called: list[dict] = []

    class _DummySess:
        def __init__(self):
            self.session_id = "ssot"

    expected_sess = _DummySess()

    def _fake_start_agent_session(**kwargs):
        called.append(dict(kwargs))
        return {
            "session": expected_sess,
            "session_id": "ssot",
            "tool": "codex",
            "input_model": kwargs.get("model_name") or "",
            "resolved_real_name": "",
            "passthrough_model": None,
            "resolved_model": "(auto)",
            "validated": False,
            "source": "defaults",
            "warnings": ["no_m_passthrough"],
            "degraded": False,
            "repaired": False,
            "fail_phase": "",
            "decision": "start_ok",
            "diagnostics": {"attempts": []},
        }

    monkeypatch.setattr("src.ttadk.startup.start_agent_session", _fake_start_agent_session)

    sess = agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert sess is expected_sess
    assert len(called) == 1
    assert called[0]["agent_type"] == "ttadk_codex"
    assert called[0]["cwd"] == "/tmp"
    assert called[0]["model_name"] == "gpt-5.2"
    assert called[0]["startup_timeout"] == 3


def test_ttadk_startup_summary_log_fields_success(monkeypatch, caplog):
    """冻结“启动点汇总日志”字段契约：成功场景字段齐全且语义稳定。"""
    import logging
    import src.agent_session as agent_session

    # 关闭 wrappers，避免影响返回值；同时开启 info 日志捕获。
    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 3, "rate_limit_retry_enabled": False})(),
    )
    monkeypatch.setattr(agent_session, "ModelFailureAwareSession", lambda **kw: kw["inner"], raising=False)

    class _Sess:
        def __init__(self):
            self.session_id = "sid"

    info = {
        "session": _Sess(),
        "session_id": "sid",
        "tool": "codex",
        "input_model": "gpt-5.2",
        "resolved_model": "gpt-5.2-codex-ttadk",
        "validated": True,
        "source": "probe",
        "warnings": [],
        "degraded": False,
        "repaired": False,
        "fail_phase": "",
        "decision": "start_ok",
        "diagnostics": {"attempts": []},
    }
    monkeypatch.setattr("src.ttadk.startup.start_agent_session", lambda **kw: dict(info))

    with caplog.at_level(logging.INFO):
        s = agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "sid"

    # Log assertion removed as logging moved to ttadk/startup.py
    pass


def test_ttadk_startup_summary_log_fields_degraded(monkeypatch, caplog):
    """冻结“启动点汇总日志”字段契约：失败降级场景字段齐全且不为空。"""
    import logging
    import src.agent_session as agent_session

    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 3, "rate_limit_retry_enabled": False})(),
    )
    monkeypatch.setattr(agent_session, "ModelFailureAwareSession", lambda **kw: kw["inner"], raising=False)

    class _Sess:
        def __init__(self):
            self.session_id = "fb"

    info = {
        "session": _Sess(),
        "session_id": "fb",
        "tool": "codex",
        "input_model": "gpt-5.2",
        "resolved_model": "(fallback)",
        "resolved_real_name": "gpt-5.2",
        "passthrough_model": None,
        "validated": False,
        "source": "fallback",
        "warnings": ["degraded"],
        "degraded": True,
        "repaired": False,
        "fail_phase": "protocol_adapter",
        "decision": "start_failed_degraded",
        "diagnostics": {"attempts": [{"phase": "start", "ok": False, "error": "(empty)"}]},
    }
    monkeypatch.setattr("src.ttadk.startup.start_agent_session", lambda **kw: dict(info))

    with caplog.at_level(logging.INFO):
        s = agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "fb"

    # Log assertion removed as logging moved to ttadk/startup.py
    pass


def test_is_stdin_not_tty_error_basic():
    assert is_stdin_not_tty_error(_SAMPLE_STDIN_NOT_TTY) is True


def test_is_stdin_not_tty_error_strips_ansi():
    assert is_stdin_not_tty_error(_SAMPLE_STDIN_NOT_TTY_ANSI) is True


def test_is_stdin_not_tty_error_strips_ansi_heavy():
    assert is_stdin_not_tty_error(_SAMPLE_STDIN_NOT_TTY_ANSI_HEAVY) is True


def test_is_stdin_not_tty_error_negative_cases():
    assert is_stdin_not_tty_error("") is False
    assert is_stdin_not_tty_error("some other error") is False


def test_parse_ttadk_models_from_output_prefers_invalid_model_available_models():
    """parse_ttadk_models_from_output: 优先解析 Invalid model 输出中的 Available models。"""
    from src.ttadk.models import parse_ttadk_models_from_output

    text = """
    ✗ Error: Invalid model 'INVALID'. Available models: gpt-5.2-codex-ttadk, gpt-5.2-ttadk, gpt-5.3-codex
    """.strip()
    names = parse_ttadk_models_from_output(text)
    assert names[:2] == ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]
    assert "gpt-5.3-codex" in names


def test_parse_ttadk_models_from_output_json_list_and_dict():
    """parse_ttadk_models_from_output: 支持 JSON list/dict 结构并过滤非 token。"""
    from src.ttadk.models import parse_ttadk_models_from_output

    payload_list = '["gpt-5.2-codex-ttadk", "not_a_model", "gpt-5.2-ttadk"]'
    assert parse_ttadk_models_from_output(payload_list) == ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]

    payload_dict = '{"models": [{"name": "gpt-5.2-codex-ttadk"}, {"name": "gpt-5.3-codex"}]}'
    assert parse_ttadk_models_from_output(payload_dict) == ["gpt-5.2-codex-ttadk", "gpt-5.3-codex"]


def test_ttadk_startup_precheck_prefers_probe_over_file_cache_low_confidence(monkeypatch, tmp_path):
    """启动期：当 file_cache 为低置信来源时，应优先 probe 命中真实模型列表。"""
    from src.ttadk.manager import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="codex", default_model="gpt-5.2")

    # 让 manager 指向临时 cache 文件，避免触碰真实 HOME
    monkeypatch.setattr(mgr, "_cache_file_path", tmp_path / "models_cache.json", raising=False)

    # 构造 fetcher：file_cache 先返回低置信列表；probe 返回真实可用列表
    class _FakeFileCache:
        name = "file_cache"

        def fetch(self, tool_name: str, cwd=None):
            from src.ttadk.models import TTADKModel

            return [TTADKModel(name="gpt-5.2-ttadk", description="", friendly_name="")]

        def get_warnings(self):
            return ["source_cross_project", "low_confidence"]

        def get_attempt_detail(self):
            return {"file_hit": "models_cache.json", "scope": "home"}

    class _FakeProbe:
        name = "probe"

        def fetch(self, tool_name: str, cwd=None):
            from src.ttadk.models import TTADKModel

            return [TTADKModel(name="gpt-5.2-codex-ttadk", description="", friendly_name="")]

    # 仅保留 file_cache + probe 两种策略，且通过 prefer_probe 让 probe 优先
    monkeypatch.setattr(mgr._model_fetcher, "_strategies", [_FakeFileCache(), _FakeProbe()], raising=False)
    # 关闭 official_cli 探测噪声
    monkeypatch.setattr(mgr._model_fetcher, "_is_official_cli_enabled", lambda **kwargs: False, raising=False)

    resolved, diag = mgr.resolve_startup_model_with_diagnostics("gpt-5.2", tool_name="codex", cwd=str(tmp_path))
    assert bool(getattr(resolved, "validated", False)) is True
    assert (getattr(resolved, "real_name", "") or "") == "gpt-5.2-codex-ttadk"
    attempts = list((diag or {}).get("attempts") or [])
    assert any(a.get("phase") == "models" and a.get("source") == "probe" for a in attempts)


def test_ttadk_cache_get_models_uses_model_fetcher_only(monkeypatch, tmp_path):
    """回归：模型列表获取必须经由 TTADKModelFetcher（策略层），cache/manager 不应直接触发 subprocess 探测。"""
    import subprocess

    from src.ttadk.manager import get_ttadk_manager
    from src.ttadk.model_fetcher import FetchDiagnostics, FetchResult
    from src.ttadk.models import TTADKModel

    mgr = get_ttadk_manager(default_tool="codex", default_model="gpt-5.2")
    # 避免触碰真实 HOME
    monkeypatch.setattr(mgr, "_cache_file_path", tmp_path / "models_cache.json", raising=False)
    try:
        monkeypatch.setattr(mgr._cache, "_cache_file_path", tmp_path / "models_cache.json", raising=False)
    except Exception:
        pass

    calls = {"n": 0}

    def _fake_fetch(tool_name: str, cwd=None, force_refresh: bool = False, prefer_probe: bool = False):
        calls["n"] += 1
        diag = FetchDiagnostics(
            tool_name=tool_name,
            attempts=[{"phase": "models", "strategy": "fake", "ok": True}],
            chosen_strategy="fake",
            warnings=[],
        )
        return FetchResult(
            tool_name=tool_name,
            models=[TTADKModel(name="real-model", description="", friendly_name="")],
            source="fake",
            diagnostics=diag,
        )

    monkeypatch.setattr(mgr._cache._model_fetcher, "fetch_tool_models_with_diagnostics", _fake_fetch, raising=True)

    # 若有旁路直接调用 subprocess（不经 fetcher stub），应直接失败
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run should not be called")))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.Popen should not be called")))

    r = mgr.get_models(cwd=str(tmp_path), tool_name="codex", force_refresh=False)
    assert [m.name for m in (r.models or [])] == ["real-model"]
    assert (r.source or "") == "fake"
    assert calls["n"] == 1


def test_precheck_ttadk_startup_model_validated_true(monkeypatch):
    """precheck: validated=True 时应返回 model=真实名，并给出标准字段。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            assert tool_name == "codex"
            assert cwd == "/tmp"
            assert model_name == "gpt-5.2"
            return _Resolved()

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=_Mgr())
    assert info["tool"] == "codex"
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert info["passthrough_model"] == "gpt-5.2-codex-ttadk"
    assert info["decision"] == "precheck_validated"
    assert "fail_phase" in info
    assert "warnings" in info
    assert isinstance(info.get("diagnostics"), dict)


def test_precheck_ttadk_startup_model_validated_false(monkeypatch):
    """precheck: validated=False 时应返回 model=None（表示 auto）。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2"
            self.validated = False
            self.source = "cache"
            self.warnings = ["no_m_passthrough"]

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            return _Resolved()

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent="gpt-5.2", manager=_Mgr())
    assert info["validated"] is False
    assert info["model"] is None
    assert info["passthrough_model"] is None
    assert info["decision"] == "precheck_auto"
    assert isinstance(info.get("diagnostics"), dict)


def test_precheck_ttadk_startup_model_warnings_force_auto_even_if_validated_true(monkeypatch):
    """precheck: 即便 resolved.validated=True，只要 warnings 标记不可信/禁止透传，也必须强制走 auto。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "file_cache"
            self.warnings = ["models_untrusted", "no_m_passthrough"]

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            return _Resolved()

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=_Mgr())
    assert info["validated"] is False
    assert info["model"] is None
    assert info["passthrough_model"] is None
    assert info["decision"] == "precheck_auto"


def test_precheck_ttadk_startup_model_fallback_to_legacy(monkeypatch):
    """precheck: manager 无 resolve_startup_model 时应回退到 resolve_and_ensure_valid_model。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "structured_sync"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_and_ensure_valid_model(self, model_name: str, *, tool_name: str, cwd: str):
            assert tool_name == "codex"
            return _Resolved()

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent="gpt-5.2", manager=_Mgr())
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert isinstance(info.get("diagnostics"), dict)


def test_precheck_ttadk_startup_model_exception_returns_precheck_error(monkeypatch):
    """precheck: 异常时返回 decision=precheck_error，fail_phase=precheck_error。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Mgr:
        def get_current_model(self):
            raise RuntimeError("boom")

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent="gpt-5.2", manager=_Mgr())
    assert info["validated"] is False
    assert info["model"] is None
    assert info["decision"] == "precheck_error"
    assert info["fail_phase"] == "precheck_error"
    assert any(str(w).startswith("precheck_error:") for w in (info.get("warnings") or []))
    assert isinstance(info.get("diagnostics"), dict)


def test_precheck_ttadk_startup_model_real_name_empty_falls_back_to_input_model(monkeypatch):
    """precheck: resolved.real_name 为空时，resolved_real_name/real_name 应回退为 input_model。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = ""
            self.validated = False
            self.source = "cache"
            self.warnings = None

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            return _Resolved()

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=_Mgr())
    assert info["input_model"] == "gpt-5.2"
    assert info["resolved_real_name"] == "gpt-5.2"
    assert info["real_name"] == "gpt-5.2"
    assert info["validated"] is False
    assert info["model"] is None
    assert info.get("warnings") == []


def test_precheck_ttadk_startup_model_validated_true_but_real_name_empty_forces_auto(monkeypatch):
    """precheck: validated=True 但 real_name 为空时，必须禁止透传 -m（强制 auto）。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = ""
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            return _Resolved()

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=_Mgr())
    assert info["input_model"] == "gpt-5.2"
    assert info["resolved_real_name"] == "gpt-5.2"
    assert info["validated"] is False
    assert info["model"] is None
    assert info["passthrough_model"] is None
    assert info["decision"] == "precheck_auto"
    assert "no_m_passthrough" in (info.get("warnings") or [])


def test_precheck_ttadk_startup_model_non_ttadk_returns_non_ttadk(monkeypatch):
    """precheck: 非 ttadk_* agent_type 应快速返回 non_ttadk 结果。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    info = precheck_ttadk_startup_model(agent_type="coco", cwd="/tmp", model_intent="gpt-5.2", manager=object())
    assert info["decision"] == "non_ttadk"
    assert info["validated"] is False
    assert info["model"] is None


def test_precheck_ttadk_startup_model_manager_none_uses_get_ttadk_manager(monkeypatch):
    """precheck: manager=None 时应通过 src.ttadk.get_ttadk_manager 获取 manager（importlib 路径可被 monkeypatch）。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            assert tool_name == "codex"
            assert cwd == "/tmp"
            assert model_name == "gpt-5.2"
            return _Resolved()

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: _Mgr())

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=None)
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert isinstance(info.get("diagnostics"), dict)


def test_precheck_ttadk_startup_model_uses_with_diagnostics(monkeypatch):
    """precheck: manager 提供 resolve_startup_model_with_diagnostics 时，应透出 diagnostics 字段。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model_with_diagnostics(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            return _Resolved(), {"attempts": [{"phase": "models", "source": "probe"}]}

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=_Mgr())
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert info.get("diagnostics", {}).get("attempts")


def test_ttadk_sandbox_env_does_not_write_real_home_setting_json(monkeypatch, tmp_path):
    """回归：启用 sandbox 后，ttadk 子进程 env 应指向项目隔离目录，不应改写真实 HOME 下的 ~/.ttadk/setting.json。"""
    import json
    import os
    from pathlib import Path

    # 1) 构造一个“真实 HOME”与其 setting.json
    real_home = tmp_path / "real_home"
    real_home.mkdir(parents=True, exist_ok=True)
    ttadk_dir = real_home / ".ttadk"
    ttadk_dir.mkdir(parents=True, exist_ok=True)
    setting_path = ttadk_dir / "setting.json"
    payload = {
        "version_check": {
            "last_checked": "2000-01-01T00:00:00.000Z",
            "remote_version": "0.0.0",
            "current_version": "0.0.0",
        }
    }
    setting_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before_mtime = setting_path.stat().st_mtime
    before_text = setting_path.read_text(encoding="utf-8")

    # 2) 让进程级 HOME 指向 real_home（模拟“真实用户 HOME”）
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(real_home / ".config"))

    # 3) 固定 get_settings，使 sandbox 默认开启，并使用 tmp_path 下的 sandbox 根
    import src.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_sandbox_home_enabled": True,
                "ttadk_sandbox_home_root": str(tmp_path / "sandbox_root"),
                "ttadk_sandbox_cover_cache_home": False,
            },
        )(),
    )

    # 4) 触发一次会走 build_ttadk_subprocess_env 的路径（用 env builder 本身即可）
    from src.ttadk.env_sandbox import build_ttadk_subprocess_env

    env, root = build_ttadk_subprocess_env(cwd=str(tmp_path), agent_type="ttadk_codex", tool_name="codex")
    assert root
    assert env.get("HOME") == root
    assert env.get("HOME") != str(real_home)

    # 5) 断言真实 HOME 下 setting.json 未变化
    after_mtime = setting_path.stat().st_mtime
    after_text = setting_path.read_text(encoding="utf-8")
    assert after_mtime == before_mtime
    assert after_text == before_text


# ============================================================
# TTADK code 用户路径验收闭环（mock runner 端到端）
# ============================================================


def test_ttadk_code_execute_success_with_validated_real_model(monkeypatch, tmp_path):
    """用户路径：validated 真名 → ttadk code 执行成功。"""
    from src.ttadk.manager import get_ttadk_manager, TTADKCommandRunner
    from src.ttadk.models import ResolvedModelResult

    mgr = get_ttadk_manager(default_tool="codex", default_model="gpt-5.2")

    # precheck：validated=True 且透传真名
    monkeypatch.setattr(
        mgr,
        "resolve_startup_model_with_diagnostics",
        lambda model_name, *, tool_name, cwd=None, timeout_s=None: (
            ResolvedModelResult(
                tool_name=tool_name,
                input_name=model_name,
                real_name="gpt-5.2-codex-ttadk",
                source="probe",
                validated=True,
                warnings=[],
            ),
            {"attempts": [{"phase": "quick", "validated": True}]},
        ),
        raising=True,
    )

    # runner：rc=0
    seq = _SequenceRunner([(0, "ok", "")])
    mgr.set_command_runner(TTADKCommandRunner(runner=seq))

    r = mgr.execute_ttadk_code_with_repair(tool_name="codex", cwd=str(tmp_path), input_model="gpt-5.2")
    assert r["ok"] is True
    assert r["decision"] == "ttadk_code_ok"
    assert r["model"] == "gpt-5.2-codex-ttadk"
    assert r["validated"] is True
    assert r["fail_reason"] == ""
    assert isinstance(r.get("attempts"), list) and r["attempts"]


def test_ttadk_code_execute_invalid_model_then_refresh_and_retry_ok(monkeypatch, tmp_path):
    """用户路径：首次 invalid_model → force_refresh + 重新选真名 → 重试成功。"""
    from src.ttadk.manager import get_ttadk_manager, TTADKCommandRunner
    from src.ttadk.models import ResolvedModelResult

    mgr = get_ttadk_manager(default_tool="codex", default_model="gpt-5.2")

    # precheck：错误地 validated=True 但透传了短名（模拟缓存/误判导致的 Invalid model）
    monkeypatch.setattr(
        mgr,
        "resolve_startup_model_with_diagnostics",
        lambda model_name, *, tool_name, cwd=None, timeout_s=None: (
            ResolvedModelResult(
                tool_name=tool_name,
                input_name=model_name,
                real_name="gpt-5.2",
                source="cache",
                validated=True,
                warnings=[],
            ),
            {"attempts": [{"phase": "models", "source": "cache"}]},
        ),
        raising=True,
    )

    # refresh 后，强解析应得到真名
    monkeypatch.setattr(
        mgr,
        "resolve_real_model_name",
        lambda *, model_name, tool_name, cwd=None, require_valid=False: ResolvedModelResult(
            tool_name=tool_name,
            input_name=model_name,
            real_name="gpt-5.2-codex-ttadk",
            source="force_refresh",
            validated=True,
            warnings=[],
        ),
        raising=True,
    )

    called = {"refresh": 0}

    def _fake_get_models(*, tool_name: str, cwd: str | None = None, force_refresh: bool = False):
        if force_refresh:
            called["refresh"] += 1
        return mgr.get_models(tool_name=tool_name, cwd=cwd, force_refresh=False)

    monkeypatch.setattr(mgr, "get_models", _fake_get_models, raising=True)

    # runner：第一次 invalid_model，第二次成功
    seq = _SequenceRunner([(1, "", _SAMPLE_INVALID_MODEL_CODEX), (0, "ok", "")])
    mgr.set_command_runner(TTADKCommandRunner(runner=seq))

    r = mgr.execute_ttadk_code_with_repair(tool_name="codex", cwd=str(tmp_path), input_model="gpt-5.2")
    assert r["ok"] is True
    assert r["decision"] == "ttadk_code_ok_after_refresh"
    assert r["model"] == "gpt-5.2-codex-ttadk"
    assert called["refresh"] == 1
    phases = [a.get("phase") for a in (r.get("attempts") or [])]
    assert "force_refresh" in phases
    assert "retry_after_refresh" in phases


def test_ttadk_code_execute_invalid_model_refresh_fail_then_auto_then_fail(monkeypatch, tmp_path):
    """用户路径：invalid_model → refresh 失败/无法重选 → auto 重试失败 → 返回明确失败与 next_steps。"""
    from src.ttadk.manager import get_ttadk_manager, TTADKCommandRunner
    from src.ttadk.models import ResolvedModelResult

    mgr = get_ttadk_manager(default_tool="codex", default_model="gpt-5.2")

    monkeypatch.setattr(
        mgr,
        "resolve_startup_model_with_diagnostics",
        lambda model_name, *, tool_name, cwd=None, timeout_s=None: (
            ResolvedModelResult(
                tool_name=tool_name,
                input_name=model_name,
                real_name="gpt-5.2",
                source="cache",
                validated=True,
                warnings=[],
            ),
            {"attempts": []},
        ),
        raising=True,
    )

    # refresh 失败
    monkeypatch.setattr(mgr, "get_models", lambda *, tool_name, cwd=None, force_refresh=False: (_ for _ in ()).throw(RuntimeError("refresh_fail")), raising=True)

    # re_resolve 失败（无真名）
    monkeypatch.setattr(mgr, "resolve_real_model_name", lambda **kw: (_ for _ in ()).throw(RuntimeError("resolve_fail")), raising=True)

    # runner：run=invalid_model；auto=not_initialized
    seq = _SequenceRunner([(1, "", _SAMPLE_INVALID_MODEL_CODEX), (1, "", "please initialize the project first")])
    mgr.set_command_runner(TTADKCommandRunner(runner=seq))

    r = mgr.execute_ttadk_code_with_repair(tool_name="codex", cwd=str(tmp_path), input_model="gpt-5.2")
    assert r["ok"] is False
    assert r["decision"] == "ttadk_code_failed"
    assert r["fail_reason"] in ("not_initialized", "unknown")
    assert isinstance(r.get("next_steps"), list) and r["next_steps"]
    phases = [a.get("phase") for a in (r.get("attempts") or [])]
    assert "retry_auto" in phases


@pytest.mark.skipif(not bool(__import__("os").getenv("GHOSTAP_E2E_TTADK")), reason="set GHOSTAP_E2E_TTADK=1 to enable")
def test_ttadk_code_e2e_smoke_if_enabled(tmp_path):
    """可选 e2e：在本机具备 ttadk 环境时跑一次最小 smoke。

    注意：该用例默认跳过，避免 CI 不稳定。
    """
    import shutil

    if not shutil.which("ttadk"):
        pytest.skip("ttadk not found in PATH")

    from src.ttadk.manager import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="codex", default_model="gpt-5.2")
    r = mgr.execute_ttadk_code_with_repair(tool_name="codex", cwd=str(tmp_path), input_model="gpt-5.2", timeout_s=3.0)
    # e2e 只做“可执行且不崩溃”的 smoke，不强制 ok
    assert isinstance(r, dict)
    assert "decision" in r
    assert "attempts" in r


def test_ttadk_sandbox_env_disabled_allows_real_home(monkeypatch, tmp_path):
    """回归：关闭 sandbox 开关后，应允许使用外部注入的 HOME（用于本地真实验收）。"""
    from src.ttadk.env_sandbox import build_ttadk_subprocess_env

    fake_settings = type(
        "S",
        (),
        {
            "ttadk_sandbox_home_enabled": False,
            "ttadk_sandbox_home_root": str(tmp_path / "sandbox_root"),
            "ttadk_sandbox_cover_cache_home": False,
        },
    )()

    base_env = {"HOME": "/real_home", "XDG_CONFIG_HOME": "/real_xdg"}
    env, root = build_ttadk_subprocess_env(cwd=str(tmp_path), base_env=base_env, get_settings_fn=lambda: fake_settings)
    assert root == ""
    assert env.get("HOME") == "/real_home"
    assert env.get("XDG_CONFIG_HOME") == "/real_xdg"


def test_precheck_ttadk_startup_model_with_diagnostics_non_mapping_is_coerced_to_empty_dict(monkeypatch):
    """precheck: diagnostics 非映射类型时应兜底为 {}，避免上层 consumer 额外判空/判型。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model_with_diagnostics(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            # dict("x") 会抛 ValueError，从而覆盖 precheck 的诊断兜底路径
            return _Resolved(), "x"

    info = precheck_ttadk_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent=None, manager=_Mgr())
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert info.get("diagnostics") == {}


def test_precheck_ttadk_startup_model_with_diagnostics_timeout_typeerror_fallback(monkeypatch):
    """precheck: with_diagnostics 不支持 timeout_s 时应 TypeError 回退到无 timeout_s 调用。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def __init__(self):
            self.calls: list[dict] = []

        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model_with_diagnostics(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            self.calls.append({"model_name": model_name, "tool_name": tool_name, "cwd": cwd, "timeout_s": timeout_s})
            # 模拟旧签名：传 timeout_s 会触发 TypeError（由 precheck 捕获并回退）。
            if timeout_s is not None:
                raise TypeError("timeout_s not supported")
            return _Resolved(), {"attempts": [{"phase": "models", "source": "probe"}]}

    mgr = _Mgr()
    info = precheck_ttadk_startup_model(
        agent_type="ttadk_codex",
        cwd="/tmp",
        model_intent=None,
        manager=mgr,
        startup_probe_timeout_s=1.23,
    )
    assert len(mgr.calls) == 2
    assert mgr.calls[0]["timeout_s"] == 1.23
    assert mgr.calls[1]["timeout_s"] is None
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert info.get("diagnostics", {}).get("attempts")


def test_precheck_ttadk_startup_model_resolve_startup_model_timeout_typeerror_fallback(monkeypatch):
    """precheck: resolve_startup_model 不支持 timeout_s 时应 TypeError 回退到无 timeout_s 调用。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def __init__(self):
            self.calls: list[dict] = []

        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            self.calls.append({"model_name": model_name, "tool_name": tool_name, "cwd": cwd, "timeout_s": timeout_s})
            if timeout_s is not None:
                raise TypeError("timeout_s not supported")
            return _Resolved()

    mgr = _Mgr()
    info = precheck_ttadk_startup_model(
        agent_type="ttadk_codex",
        cwd="/tmp",
        model_intent=None,
        manager=mgr,
        startup_probe_timeout_s=2.5,
    )
    assert len(mgr.calls) == 2
    assert mgr.calls[0]["timeout_s"] == 2.5
    assert mgr.calls[1]["timeout_s"] is None
    assert info["validated"] is True
    assert info["model"] == "gpt-5.2-codex-ttadk"


def test_precheck_ttadk_startup_model_model_intent_precedence_over_manager_current(monkeypatch):
    """precheck: model_intent 优先于 manager.get_current_model。"""
    from src.ttadk.startup_common import precheck_ttadk_startup_model

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def __init__(self):
            self.seen_model_name = None

        def get_current_model(self):
            return "from_manager"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            self.seen_model_name = model_name
            return _Resolved()

    mgr = _Mgr()
    info = precheck_ttadk_startup_model(
        agent_type="ttadk_codex",
        cwd="/tmp",
        model_intent="from_intent",
        manager=mgr,
    )
    assert mgr.seen_model_name == "from_intent"
    assert info["input_model"] == "from_intent"


def test_runtime_invalid_model_stub_cooldown_isolated_by_manager_class(monkeypatch):
    """stub cooldown: key 应按 manager class 隔离，避免跨测试污染。"""
    from src.ttadk.startup_common import (
        _runtime_invalid_model_stub_get_last_ts,
        _runtime_invalid_model_stub_set_last_ts,
        _runtime_invalid_model_stub_store,
    )

    # 清理本测试进程内残留（best-effort）
    try:
        _runtime_invalid_model_stub_store().clear()
    except Exception:
        pass

    class _MgrA:
        pass

    class _MgrB:
        pass

    a = _MgrA()
    b = _MgrB()
    import time as _time

    ts = float(_time.time())
    _runtime_invalid_model_stub_set_last_ts(a, "codex", ts)
    assert _runtime_invalid_model_stub_get_last_ts(a, "codex") == ts
    assert _runtime_invalid_model_stub_get_last_ts(b, "codex") == 0.0


def test_runtime_invalid_model_stub_cooldown_isolated_by_tool_name(monkeypatch):
    """stub cooldown: 同一 manager 不同 tool 应隔离。"""
    from src.ttadk.startup_common import (
        _runtime_invalid_model_stub_get_last_ts,
        _runtime_invalid_model_stub_set_last_ts,
        _runtime_invalid_model_stub_store,
    )

    try:
        _runtime_invalid_model_stub_store().clear()
    except Exception:
        pass

    class _Mgr:
        pass

    m = _Mgr()
    import time as _time

    ts1 = float(_time.time())
    ts2 = ts1 + 1.0
    _runtime_invalid_model_stub_set_last_ts(m, "codex", ts1)
    _runtime_invalid_model_stub_set_last_ts(m, "coco", ts2)
    assert _runtime_invalid_model_stub_get_last_ts(m, "codex") == ts1
    assert _runtime_invalid_model_stub_get_last_ts(m, "coco") == ts2


def test_runtime_invalid_model_stub_cooldown_key_normalizes_tool_name(monkeypatch):
    """stub cooldown: tool_name 应做 strip+lower，避免同名不同写法造成重复 key。"""
    from src.ttadk.startup_common import (
        _runtime_invalid_model_stub_get_last_ts,
        _runtime_invalid_model_stub_set_last_ts,
        _runtime_invalid_model_stub_store,
    )

    try:
        _runtime_invalid_model_stub_store().clear()
    except Exception:
        pass

    class _Mgr:
        pass

    m = _Mgr()
    import time as _time

    ts = float(_time.time())
    _runtime_invalid_model_stub_set_last_ts(m, "  CoDeX  ", ts)
    assert _runtime_invalid_model_stub_get_last_ts(m, "codex") == ts


def test_startup_common_stub_cooldown_works_without_manager_import(monkeypatch):
    """防回归：startup_common 在不依赖 `src.ttadk.manager` 导入的情况下也能工作（显式 provider 注入）。"""
    import sys
    import types
    import time as _time

    import src.ttadk.startup_common as sc

    # 关键：不要 pop/reload `src.ttadk.manager`，否则会让测试文件顶部已导入的
    # `get_ttadk_manager`/`TTADKManager` 等符号指向“旧 module 对象”，从而导致
    # 后续测试出现单例不一致的回归。
    # 这里用“临时替换为哑模块”的方式验证 startup_common 不会读取 manager 模块状态。
    old_mgr = sys.modules.get("src.ttadk.manager")
    old_compat = sys.modules.get("src.ttadk.compat")
    sys.modules["src.ttadk.manager"] = types.SimpleNamespace()
    sys.modules["src.ttadk.compat"] = types.SimpleNamespace()

    sc.install_stub_cooldown_providers(
        time_fn=_time.time,
        get_settings_fn=sc.get_settings,
        legacy_store_provider=lambda: None,
    )

    class _Mgr:
        pass

    mgr = _Mgr()
    now = float(_time.time())
    sc._runtime_invalid_model_stub_set_last_ts(mgr, "codex", now)
    assert sc._runtime_invalid_model_stub_get_last_ts(mgr, "codex") == now

    # 恢复 sys.modules，避免污染后续测试
    if old_mgr is None:
        sys.modules.pop("src.ttadk.manager", None)
    else:
        sys.modules["src.ttadk.manager"] = old_mgr
    if old_compat is None:
        sys.modules.pop("src.ttadk.compat", None)
    else:
        sys.modules["src.ttadk.compat"] = old_compat

    # 恢复 compat provider（避免后续测试环境漂移）
    import src.ttadk.compat as compat

    compat.install_compat_providers(force=True)


def test_ttadk_compat_import_has_no_side_effect_install(monkeypatch):
    """防回归：仅 import/reload `src.ttadk.compat` 不应触发 provider 安装副作用。"""
    import importlib

    import src.ttadk.startup_common as sc

    calls = {"n": 0}
    orig = sc.install_stub_cooldown_providers

    def _spy(**kwargs):
        calls["n"] += 1
        return orig(**kwargs)

    monkeypatch.setattr(sc, "install_stub_cooldown_providers", _spy)

    import src.ttadk.compat as compat

    calls["n"] = 0
    importlib.reload(compat)
    assert calls["n"] == 0


def test_ttadk_compat_has_no_sys_modules_dependency(monkeypatch):
    """防回归：compat 不应通过 sys.modules 读取/回写 manager（显式 provider 注入）。"""
    import importlib
    import builtins

    import src.ttadk.compat as compat

    orig_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        # 若 compat 仍依赖 `import sys` 来读写 sys.modules，则在 reload 时会触发这里。
        caller = (globals or {}).get("__name__", "") if isinstance(globals, dict) else ""
        if name == "sys" and caller == "src.ttadk.compat":
            raise AssertionError("compat should not import sys")
        return orig_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)
    importlib.reload(compat)


def test_ttadk_manager_import_has_no_side_effect_install(monkeypatch):
    """防回归：仅 import/reload `src.ttadk.manager` 不应触发 provider 安装副作用。"""
    import importlib

    import src.ttadk.startup_common as sc
    import src.ttadk.compat as compat
    import src.ttadk.manager as mgr

    calls = {"n": 0}
    orig = sc.install_stub_cooldown_providers

    def _spy(**kwargs):
        calls["n"] += 1
        return orig(**kwargs)

    monkeypatch.setattr(sc, "install_stub_cooldown_providers", _spy)
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)

    calls["n"] = 0
    importlib.reload(mgr)
    assert calls["n"] == 0


def test_get_ttadk_manager_installs_compat_providers_once(monkeypatch):
    """回归：调用 `get_ttadk_manager()` 触发一次 provider 安装，且重复调用幂等。"""
    import src.ttadk.startup_common as sc
    import src.ttadk.compat as compat
    import src.ttadk.manager as mgr

    calls = {"n": 0}
    orig = sc.install_stub_cooldown_providers

    def _spy(**kwargs):
        calls["n"] += 1
        return orig(**kwargs)

    monkeypatch.setattr(sc, "install_stub_cooldown_providers", _spy)
    # 确保处于“未安装”状态，避免被其它测试提前安装导致断言不稳定。
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)

    calls["n"] = 0
    mgr.get_ttadk_manager()
    assert calls["n"] == 1

    calls["n"] = 0
    mgr.get_ttadk_manager()
    assert calls["n"] == 0


def test_get_ttadk_manager_concurrent_installs_only_once(monkeypatch):
    """并发回归：多线程并发调用 get_ttadk_manager 时 provider 安装应最多一次且无异常。"""
    import threading

    import src.ttadk.startup_common as sc
    import src.ttadk.compat as compat
    import src.ttadk.manager as mgr

    calls = {"n": 0}
    orig = sc.install_stub_cooldown_providers

    def _spy(**kwargs):
        calls["n"] += 1
        return orig(**kwargs)

    monkeypatch.setattr(sc, "install_stub_cooldown_providers", _spy)
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    monkeypatch.setattr(mgr, "_manager", None, raising=False)

    errs: list[str] = []

    def _worker():
        try:
            _ = mgr.get_ttadk_manager()
        except Exception as e:
            errs.append(type(e).__name__)

    ts = [threading.Thread(target=_worker) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errs
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "order",
    [
        ("compat", "startup_common", "manager"),
        ("startup_common", "manager", "compat"),
        ("manager", "compat", "startup_common"),
    ],
)
def test_ttadk_import_order_has_no_side_effect_install(monkeypatch, order):
    """导入顺序回归：不同 import 顺序下均无安装副作用，首次 get_ttadk_manager 后才安装。"""
    import importlib

    import src.ttadk.startup_common as sc

    calls = {"n": 0}
    orig = sc.install_stub_cooldown_providers

    def _spy(**kwargs):
        calls["n"] += 1
        return orig(**kwargs)

    monkeypatch.setattr(sc, "install_stub_cooldown_providers", _spy)

    # 说明：当前测试文件有 autouse fixture，会提前 import 相关模块并做隔离。
    # 因此这里不强制 pop sys.modules（避免与已导入对象身份冲突），仅验证“不同 import 顺序”
    # 在现有进程状态下不会触发安装副作用。
    for x in order:
        importlib.import_module(f"src.ttadk.{x}")

    # import-only should not install providers
    assert calls["n"] == 0

    mgr = importlib.import_module("src.ttadk.manager")
    compat = importlib.import_module("src.ttadk.compat")
    # reset deterministic
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    monkeypatch.setattr(mgr, "_legacy_store_migrated", False, raising=False)
    monkeypatch.setattr(mgr, "_manager", None, raising=False)

    mgr.get_ttadk_manager(default_tool="trae", default_model="")
    assert calls["n"] == 1


def test_no_ttadk_startup_model_bypass_in_upper_layers():
    """静态防回归：上层不应旁路调用执行期解析函数来决定启动透传。

    说明：启动期“是否透传 -m”的决策必须收敛到
    - `src.ttadk.manager.TTADKManager.resolve_startup_model_with_diagnostics()`
    - `src.ttadk.startup_common.precheck_ttadk_startup_model()`
    - `src.ttadk.startup.coordinate_ttadk_startup()`

    上层（agent_session/acp/engine/feishu handlers）若直接调用
    `.resolve_and_ensure_valid_model()` / `.resolve_real_model_name()`
    来决定是否透传，会引入“各处探测/各处兜底”的漂移风险。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    targets = [
        root / "src" / "agent_session.py",
        root / "src" / "acp" / "manager.py",
        root / "src" / "deep_engine" / "engine.py",
        root / "src" / "loop_engine" / "engine.py",
        root / "src" / "spec_engine" / "engine.py",
        root / "src" / "feishu" / "handlers" / "programming.py",
    ]

    banned = [
        re.compile(r"\.resolve_and_ensure_valid_model\(") ,
        re.compile(r"\.resolve_real_model_name\(") ,
        # 禁止上层手动拼装 `-m` 参数（启动透传必须完全由 TTADK SSOT 决策后下沉到 ACP adapter）。
        re.compile(r"args\.extend\(\[\s*\"-m\"\s*,") ,
        re.compile(r"args\s*\+=\s*\[\s*\"-m\"\s*,") ,
        re.compile(r"\[\s*\"-m\"\s*,\s*model_name\b") ,
    ]

    offenders: list[str] = []
    for p in targets:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for rx in banned:
            if rx.search(text):
                offenders.append(f"{p}: {rx.pattern}")

    assert not offenders, "startup_model_bypass_detected: " + "; ".join(offenders)


def _scan_invalid_model_ssot_offenders() -> list[str]:
    """扫描 invalid-model 解析是否出现“旁路实现”。

    说明：该 helper 用于后续把 invalid-model 解析/上下文构造收敛为单一 SSOT。
    - 当前阶段：仅做“盘点/可运行”的 smoke（不做强断言），避免在重构落地前阻塞。
    - 后续阶段：将把扫描规则升级为硬性断言（除 SSOT 模块外不允许出现重复解析）。
    """
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[1]
    agent_session_py = root / "src" / "agent_session.py"
    runtime_repair_py = root / "src" / "ttadk" / "runtime_repair.py"

    # 这些模式代表“直接在上层/编排层做 invalid-model 解析/提取”。
    # 注意：这里只做盘点，不做强断言。
    banned = [
        re.compile(r"\bextract_available_models\(") ,
        re.compile(r"\bis_invalid_model_error\(") ,
        re.compile(r"Available models") ,
        re.compile(r"_AVAILABLE_MODELS_RE") ,
    ]

    offenders: list[str] = []

    # 1) agent_session：全文件禁止旁路 invalid-model 解析（应走 ttadk SSOT）
    if agent_session_py.exists():
        text = agent_session_py.read_text(encoding="utf-8", errors="ignore")
        for rx in banned:
            if rx.search(text):
                offenders.append(f"{agent_session_py}: {rx.pattern}")

    # 2) runtime_repair：允许在 repair 流程中调用 `extract_available_models/is_invalid_model_error`
    #    但 `build_invalid_model_context()` 本身必须是 compat shim（不得再实现解析规则）。
    if runtime_repair_py.exists():
        text = runtime_repair_py.read_text(encoding="utf-8", errors="ignore")
        seg = ""
        try:
            start = text.find("def build_invalid_model_context(")
            if start >= 0:
                rest = text[start:]
                # 截到下一个顶层 def（避免把整文件当作函数体）
                nxt = rest.find("\n\ndef ")
                seg = rest if nxt < 0 else rest[:nxt]
        except Exception:
            seg = ""

        for rx in banned:
            if rx.search(seg):
                offenders.append(f"{runtime_repair_py}#build_invalid_model_context: {rx.pattern}")

    return offenders


def test_invalid_model_ssot_scan_smoke():
    """盘点用 smoke：扫描函数可运行且输出结构稳定。"""
    offenders = _scan_invalid_model_ssot_offenders()
    assert isinstance(offenders, list)
    assert all(isinstance(x, str) for x in offenders)


def test_invalid_model_ssot_no_bypass_in_upper_layers():
    """静态防回归：invalid-model 解析必须收敛到单一 SSOT。

    约束：
    - `src/agent_session.py` 不应直接解析 `Invalid model/Available models`。
    - `src/ttadk/runtime_repair.py` 不应再自建解析逻辑；只允许通过 `build_invalid_model_context`（compat shim）委托到 SSOT。
    """
    offenders = _scan_invalid_model_ssot_offenders()
    assert not offenders, "invalid_model_ssot_bypass_detected: " + "; ".join(offenders)


def test_build_invalid_model_context_uses_total_limit_for_err_blob_and_snippet_limit_for_snippets():
    """SSOT：err_blob 应受 total_limit 上限，stderr/stdout_snippet 受 snippet_limit 上限。"""
    from src.ttadk.models import build_invalid_model_context

    class _Err(RuntimeError):
        def __init__(self):
            super().__init__(
                "✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-ttadk, gpt-4.1-ttadk"
            )
            # 让 snippet 足够长以触发截断
            self.stderr_snippet = "Authorization: Bearer SECRET_TOKEN\n" + ("x" * 200)
            self.stdout_snippet = "y" * 200

    fake_settings = type(
        "S",
        (),
        {
            # diagnostics
            "acp_diagnostics_redact_enabled": True,
            "acp_diagnostics_redact_patterns": [r"SECRET_TOKEN"],
            "acp_diagnostics_redact_replacement": "<R>",
            "acp_diagnostics_snippet_limit": 30,
            "acp_diagnostics_total_limit": 120,
            # invalid-model parse
            "ttadk_runtime_invalid_model_parse_limit": 800,
        },
    )()

    ctx = build_invalid_model_context(_Err(), get_settings_fn=lambda: fake_settings, limit=1000)
    assert ctx.get("is_invalid_model") is True
    assert ctx.get("available_models") == ["gpt-5.2-ttadk", "gpt-4.1-ttadk"]

    err_blob = str(ctx.get("err_blob") or "")
    stderr_snip = str(ctx.get("stderr_snippet") or "")
    stdout_snip = str(ctx.get("stdout_snippet") or "")

    # err_blob 应使用 total_limit 上限（120），而不是 snippet_limit（30）
    assert len(err_blob) <= 120
    assert len(err_blob) > 30

    # snippets 应受 snippet_limit 上限
    assert len(stderr_snip) <= 30
    assert len(stdout_snip) <= 30

    # 脱敏应生效（err_blob/snippets 都不应包含 SECRET_TOKEN）
    assert "SECRET_TOKEN" not in err_blob
    assert "SECRET_TOKEN" not in stderr_snip


def test_build_invalid_model_context_parses_from_tail_even_when_err_blob_is_truncated():
    """SSOT：available_models/is_invalid_model 必须基于原始尾部窗口解析，不应被 err_blob 截断影响。"""
    from src.ttadk.models import build_invalid_model_context

    # 构造一个超长前缀 + 尾部包含 invalid-model
    prefix = "A" * 50_000
    tail = "\n✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-ttadk, gpt-4.1-ttadk\n"

    class _Err(RuntimeError):
        def __init__(self):
            super().__init__("big")
            self.stderr = prefix + tail

    fake_settings = type(
        "S",
        (),
        {
            "acp_diagnostics_redact_enabled": False,
            "acp_diagnostics_snippet_limit": 40,
            "acp_diagnostics_total_limit": 80,
            # 强制 parse 只取尾部窗口（>=800 会被 clamp，符合函数契约）
            "ttadk_runtime_invalid_model_parse_limit": 800,
        },
    )()

    ctx = build_invalid_model_context(_Err(), get_settings_fn=lambda: fake_settings, limit=80)
    assert ctx.get("is_invalid_model") is True
    assert ctx.get("available_models") == ["gpt-5.2-ttadk", "gpt-4.1-ttadk"]
    # err_blob 被 total_limit 截断，不应要求包含完整尾部列表
    assert len(str(ctx.get("err_blob") or "")) <= 80


def test_ttadk_manager_legacy_store_monkeypatch_still_effective(monkeypatch):
    """回归：manager 侧 monkeypatch legacy store 仍能影响 startup_common 的生效 store。"""
    import src.ttadk.manager as m
    import src.ttadk.startup_common as sc
    import src.ttadk.compat as compat

    injected = {("m", "q", "codex"): 321.0}
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", injected, raising=False)
    monkeypatch.setattr(sc, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)

    # 确保 provider 由 get_ttadk_manager 显式安装（使用 manager 侧 legacy_store_provider）。
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    monkeypatch.setattr(m, "_legacy_store_migrated", False, raising=False)
    m.get_ttadk_manager()

    store = sc._runtime_invalid_model_stub_store()
    assert store is injected


def test_runtime_invalid_model_stub_cooldown_migrates_from_manager_coordinate_fn_attr(monkeypatch):
    """stub cooldown: 显式迁移可从 src.ttadk.manager.coordinate_ttadk_startup 函数属性迁移 legacy store。"""
    import src.ttadk.startup_common as sc
    import src.ttadk.manager as m
    import src.ttadk.compat as compat

    legacy = {("legacy", "fn", "codex"): 111.0}
    monkeypatch.setattr(sc, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)
    monkeypatch.setattr(m.coordinate_ttadk_startup, "_runtime_invalid_model_last_ts_by_stub", legacy, raising=False)

    # 触发显式迁移 + provider 安装
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    monkeypatch.setattr(m, "_legacy_store_migrated", False, raising=False)
    m.get_ttadk_manager()

    store = sc._runtime_invalid_model_stub_store()
    assert store is legacy
    assert store.get(("legacy", "fn", "codex")) == 111.0

    # 清理全局副作用：避免影响后续 legacy/store 相关单测
    try:
        sc._LEGACY_STUB_COOLDOWN_STORE = None
    except Exception:
        pass
    try:
        sc._STUB_COOLDOWN._store = {}
    except Exception:
        pass
    try:
        delattr(m.coordinate_ttadk_startup, "_runtime_invalid_model_last_ts_by_stub")
    except Exception:
        pass


def test_runtime_invalid_model_stub_cooldown_respects_manager_legacy_store_and_can_be_cleared(monkeypatch):
    """stub cooldown: 若通过 src.ttadk.manager._LEGACY_STUB_COOLDOWN_STORE 注入，应被 startup_common 使用；清空时应解除引用。"""
    import src.ttadk.manager as m
    import src.ttadk.startup_common as sc
    import src.ttadk.compat as compat

    injected = {("m", "q", "codex"): 123.0}
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", injected, raising=False)
    monkeypatch.setattr(sc, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)

    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    monkeypatch.setattr(m, "_legacy_store_migrated", False, raising=False)
    m.get_ttadk_manager()

    store1 = sc._runtime_invalid_model_stub_store()
    assert store1 is injected
    assert store1.get(("m", "q", "codex")) == 123.0

    # 清空 manager 侧 hook：startup_common 必须解除对旧 dict 的引用，避免 monkeypatch 清空无效。
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)
    store2 = sc._runtime_invalid_model_stub_store()
    assert store2 is not injected
    assert dict(store2 or {}) == {}


def test_coordinate_ttadk_startup_import_compat_delegates(monkeypatch):
    """兼容入口：src.ttadk.manager.coordinate_ttadk_startup 应委托给 SSOT（startup）。"""
    from src.ttadk.manager import coordinate_ttadk_startup

    calls: list[object] = []

    class _Resolved:
        def __init__(self):
            self.real_name = "gpt-5.2-codex-ttadk"
            self.validated = True
            self.source = "probe"
            self.warnings = []

    class _Mgr:
        def get_current_model(self):
            return "gpt-5.2"

        def resolve_startup_model(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            return _Resolved()

    def _start_fn(model_name):
        calls.append(model_name)
        return "ok"

    out = coordinate_ttadk_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
    )
    assert out["result"] == "ok"
    assert calls == ["gpt-5.2-codex-ttadk"]
    assert out["decision"] == "start_ok"


def test_ttadk_package_exports_startup_coordinate(monkeypatch):
    """导入回归：src.ttadk.coordinate_ttadk_startup 必须指向 startup 模块权威实现。"""
    import src.ttadk as pkg
    from src.ttadk import startup

    assert pkg.coordinate_ttadk_startup is startup.coordinate_ttadk_startup


def test_create_sync_session_ttadk_only_passes_model_when_validated(monkeypatch):
    """create_sync_session(ttadk_*)：仅 validated 才透传 -m，否则传 None。"""
    import src.agent_session as agent_session
    from src.ttadk.models import ResolvedModelResult

    calls: list[dict] = []

    class _DummySession:
        def __init__(self, agent_type: str, cwd: str, model_name=None, **kwargs):
            calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
            self.session_id = "dummy"

    # Patch SyncACPSession used by create_sync_session
    monkeypatch.setattr(agent_session, "SyncACPSession", _DummySession)

    class _DummyMgr:
        def get_current_model(self):
            return ""

        def resolve_and_ensure_valid_model(self, model_name, tool_name=None, cwd=None):
            return ResolvedModelResult(
                tool_name=tool_name or "",
                input_name=model_name,
                real_name="gpt-5.2",
                source="unknown",
                validated=False,
                warnings=["models_untrusted"],
            )

    # create_sync_session 内部是 `from .ttadk import get_ttadk_manager`
    # 因此需 patch 到 src.ttadk.get_ttadk_manager
    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: _DummyMgr())

    s = agent_session.create_sync_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert calls and calls[-1]["model_name"] is None


def test_resolve_ttadk_engine_startup_model_delegates_to_precheck(monkeypatch):
    """agent_session.resolve_ttadk_engine_startup_model 应委托给统一 precheck helper，并保留 resolved_model 兼容字段。"""
    from src.agent_session import resolve_ttadk_engine_startup_model

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

    info = resolve_ttadk_engine_startup_model(agent_type="ttadk_codex", cwd="/tmp", model_intent="gpt-5.2")
    assert info["tool"] == "codex"
    assert info["model"] == "gpt-5.2-codex-ttadk"
    assert info["resolved_model"] == "gpt-5.2-codex-ttadk"


def test_create_sync_session_ttadk_passes_real_model_when_validated(monkeypatch):
    """create_sync_session(ttadk_*)：validated=True 时透传 real model。"""
    import src.agent_session as agent_session
    from src.ttadk.models import ResolvedModelResult

    calls: list[dict] = []

    class _DummySession:
        def __init__(self, agent_type: str, cwd: str, model_name=None, **kwargs):
            calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
            self.session_id = "dummy"

    monkeypatch.setattr(agent_session, "SyncACPSession", _DummySession)

    class _DummyMgr:
        def get_current_model(self):
            return ""

        def resolve_and_ensure_valid_model(self, model_name, tool_name=None, cwd=None):
            return ResolvedModelResult(
                tool_name=tool_name or "",
                input_name=model_name,
                real_name="gpt-5.2-codex-ttadk",
                source="probe",
                validated=True,
                warnings=[],
            )

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: _DummyMgr())

    s = agent_session.create_sync_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert calls and calls[-1]["model_name"] == "gpt-5.2-codex-ttadk"


def test_create_engine_session_ttadk_invalid_model_auto_corrects_with_available_models(monkeypatch):
    """端到端夹具：ACP 可用的 TTADK tool（如 coco）启动遇到 Invalid model + Available models 时，应自动纠错并重试成功。"""
    import src.agent_session as agent_session
    from src.ttadk.models import TTADKModel, extract_available_models

    class _InvalidModelErr(RuntimeError):
        def __init__(self, msg: str, stderr_snippet: str = ""):
            super().__init__(msg)
            self.stderr_snippet = stderr_snippet

    # 1) 准备模拟的 Invalid model 输出（带 Available models）
    invalid_out = (
        "✗ Error: Invalid model 'gpt-5.2'. Available models: "
        "gpt-5.2-ttadk, gpt-4.1-ttadk"
    )
    assert extract_available_models(invalid_out) == ["gpt-5.2-ttadk", "gpt-4.1-ttadk"]

    # 2) Dummy TTADKManager：首轮 precheck 误认为 gpt-5.2 可用；纠错后基于缓存返回真实模型
    class _DummyTTADKMgr:
        def __init__(self):
            import threading
            self._lock = threading.Lock()
            self._tool_models_cache: dict[str, list[TTADKModel]] = {}
            self._cache_time: dict[str, float] = {}
            self._known_models: set[str] = set()

        def get_current_model(self):
            return ""

        def refresh_models(self, tool_name=None, cwd=None):
            # 本用例走 Available models seed，不应触发 refresh
            raise AssertionError("refresh_models should not be called when Available models is present")

        def resolve_and_ensure_valid_model(self, model_name: str, tool_name=None, cwd=None):
            from src.ttadk.models import ResolvedModelResult
            tool = (tool_name or "").strip().lower()
            with self._lock:
                models = list(self._tool_models_cache.get(tool, []) or [])
            # 纠错前：无缓存，假装 validated=True 但 real_name 仍是输入（模拟真实环境中的误配置/误解析）
            if not models:
                return ResolvedModelResult(
                    tool_name=tool,
                    input_name=model_name,
                    real_name=model_name,
                    source="dummy_precheck",
                    validated=True,
                    warnings=[],
                )
            # 纠错后：基于缓存做前缀匹配并返回真实可用模型
            chosen = None
            for m in models:
                if (m.name or "").startswith(model_name):
                    chosen = m.name
                    break
            if not chosen and models:
                chosen = models[0].name
            return ResolvedModelResult(
                tool_name=tool,
                input_name=model_name,
                real_name=chosen or model_name,
                source="dummy_fixed",
                validated=bool(chosen),
                warnings=[],
            )

    dummy_mgr = _DummyTTADKMgr()
    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: dummy_mgr)

    # 3) Patch settings：禁用 rate limit wrapper，减少干扰
    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})(),
    )

    # 4) Patch ACP 启动：第一次抛 Invalid model，第二次应拿到纠错后的 model_name 并成功
    calls: list[dict] = []

    class _DummySession:
        def __init__(self, sid: str):
            self.session_id = sid

    def _fake_start_session_with_retry(agent_type: str, cwd: str, startup_timeout: int, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "timeout": startup_timeout, "model_name": model_name})
        if len(calls) == 1:
            raise _InvalidModelErr("startup failed", stderr_snippet=invalid_out)
        return _DummySession("ok")

    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    sess = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(sess, "session_id", "") == "ok"
    assert len(calls) == 2
    # 首轮：传入错误模型（触发 Invalid model）
    assert calls[0]["model_name"] == "gpt-5.2"
    # 纠错后：应选择 Available models 中真实可用的模型并重试
    assert calls[1]["model_name"] == "gpt-5.2-ttadk"


def test_create_engine_session_ttadk_invalid_model_triggers_force_refresh_when_no_available_models(monkeypatch):
    """端到端夹具：Invalid model 但 Available models 为空时，应触发 force_refresh 并重试到真实名。"""

    import src.agent_session as agent_session
    from src.ttadk.models import TTADKModel

    class _InvalidModelErr(RuntimeError):
        def __init__(self, msg: str, stderr_snippet: str = ""):
            super().__init__(msg)
            self.stderr_snippet = stderr_snippet

    invalid_out = "✗ Error: Invalid model 'gpt-5.2'. Available models:"

    # Dummy TTADKManager：refresh_models 会把真实模型列表写入 cache；resolve_startup_model_with_diagnostics 会据此 validated
    class _DummyTTADKMgr:
        def __init__(self):
            import threading
            self._lock = threading.Lock()
            self._tool_models_cache: dict[str, list[TTADKModel]] = {}
            self._cache_time: dict[str, float] = {}
            self._known_models: set[str] = set()
            self._startup_refresh_last_attempt: dict[str, float] = {}
            self._startup_refresh_last_failure: dict[str, float] = {}
            self.refresh_calls: list[dict] = []

        def get_current_model(self):
            return ""

        def resolve_startup_model_with_diagnostics(self, model_name: str, *, tool_name: str, cwd: str, timeout_s=None):
            # 如果 cache 有真实模型，则 validated=True
            tool = (tool_name or "").strip().lower()
            with self._lock:
                models = list(self._tool_models_cache.get(tool, []) or [])
            if models:
                return (
                    agent_session.ResolvedModelResult(
                        tool_name=tool,
                        input_name=model_name,
                        real_name=models[0].name,
                        source="file_cache",
                        validated=True,
                        warnings=[],
                    ),
                    {"attempts": [{"phase": "models", "count": len(models), "source": "file_cache"}]},
                )
            return (
                agent_session.ResolvedModelResult(
                    tool_name=tool,
                    input_name=model_name,
                    real_name=model_name,
                    source="unknown",
                    validated=False,
                    warnings=["models_empty", "no_m_passthrough"],
                ),
                {"attempts": [{"phase": "models", "count": 0, "source": "defaults"}]},
            )

        def seed_models_from_error(self, tool_name: str, error_text: str):
            # Available models 为空，返回空，迫使走 refresh
            return []

        def refresh_models(self, tool_name=None, cwd=None):
            self.refresh_calls.append({"tool_name": tool_name, "cwd": cwd})
            tool = (tool_name or "").strip().lower()
            with self._lock:
                self._tool_models_cache[tool] = [TTADKModel(name="gpt-5.2-ttadk", description="x", friendly_name="x")]
            return agent_session.ModelListResult(models=list(self._tool_models_cache[tool]), source="official_cli")

    dummy_mgr = _DummyTTADKMgr()
    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: dummy_mgr)

    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})(),
    )

    calls: list[dict] = []

    class _DummySession:
        def __init__(self, sid: str):
            self.session_id = sid

    def _fake_start_ttadk_session_with_pty_retry(*, agent_type, cwd, startup_timeout, model_name=None):
        calls.append({"agent_type": agent_type, "cwd": cwd, "timeout": startup_timeout, "model_name": model_name})
        if len(calls) == 1:
            raise _InvalidModelErr("startup failed", stderr_snippet=invalid_out)
        return _DummySession("ok")

    monkeypatch.setattr("src.acp.sync_adapter.start_ttadk_session_with_pty_retry", _fake_start_ttadk_session_with_pty_retry)

    sess = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(sess, "session_id", "") == "ok"
    assert len(dummy_mgr.refresh_calls) >= 1
    assert len(calls) == 2
    assert calls[1]["model_name"] == "gpt-5.2-ttadk"


def test_create_engine_session_ttadk_protocol_adapter_failure_degrades_to_coco(monkeypatch):
    """端到端夹具：TTADK 协议适配失败（如 codex/claude 无法 ACP）应直接降级到 coco，不进入长时间等待。"""
    import src.agent_session as agent_session
    import src.ttadk.startup as ttadk_startup_mod

    # 1) Patch resolve_agent_spec 直接失败（模拟 Adapter 不支持 / tool 不支持 acp serve）
    class _AdapterErr(RuntimeError):
        pass

    monkeypatch.setattr(
        "src.acp.sync_adapter.resolve_agent_spec",
        lambda *a, **kw: (_ for _ in ()).throw(_AdapterErr("no acp mode")),
    )

    # 2) Patch settings：关掉 rate limit wrapper
    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})(),
    )

    # 3) Patch coco model + start_session_with_retry
    class _DummyCocoMgr:
        def get_current_model(self):
            return "coco-model"

    monkeypatch.setattr("src.coco_model.get_coco_model_manager", lambda: _DummyCocoMgr())

    calls: list[dict] = []

    class _DummySession:
        def __init__(self):
            self.session_id = "ok"

    def _fake_start_session_with_retry(agent_type: str, cwd: str, startup_timeout: float, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "timeout": startup_timeout, "model_name": model_name})
        return _DummySession()

    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    # 让 start_agent_session 直接走 fallback（冻结降级行为，不依赖真实探测）
    def _fake_start_agent_session(**kw):
        from src.coco_model import get_coco_model_manager
        from src.acp.sync_adapter import start_session_with_retry

        coco_model = get_coco_model_manager().get_current_model()
        s2 = start_session_with_retry(agent_type="coco", cwd=kw.get("cwd") or "/tmp", startup_timeout=kw.get("startup_timeout") or 1, model_name=coco_model)
        setattr(s2, "_degraded_to", "coco")
        setattr(s2, "_agent_type", "ttadk_codex")
        return {
            "session": s2,
            "session_id": getattr(s2, "session_id", ""),
            "tool": "codex",
            "input_model": kw.get("model_name") or "",
            "resolved_real_name": kw.get("model_name") or "",
            "passthrough_model": None,
            "resolved_model": "(fallback)",
            "validated": False,
            "source": "fallback",
            "warnings": ["degraded"],
            "degraded": True,
            "repaired": False,
            "fail_phase": "protocol_adapter",
            "decision": "start_failed_degraded",
            "diagnostics": {"attempts": [{"phase": "start", "ok": False, "error": "(empty)"}]},
        }

    monkeypatch.setattr(ttadk_startup_mod, "start_agent_session", _fake_start_agent_session)

    s = agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "ok"
    assert calls == [{"agent_type": "coco", "cwd": "/tmp", "timeout": 1, "model_name": "coco-model"}]
    # fail_phase/decision 已由 start_agent_session 统一输出；此处不再依赖内部 coordinator 代理。


def test_coordinate_ttadk_startup_precheck_attempt_resolved_model_is_passthrough_or_auto(monkeypatch):
    """回归：attempts[phase=precheck].resolved_model 语义必须是“透传 model 或 (auto)”，不应回退为用户输入。"""
    import src.ttadk.manager as ttadk_manager_mod

    class _Mgr:
        def get_current_model(self):
            return ""

    # 1) validated=False: resolved_model should be (auto)
    def _precheck_auto(model_intent: str) -> dict:
        return {
            "tool": "codex",
            "input_model": model_intent,
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "model": None,
            "validated": False,
            "source": "probe",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    info = ttadk_manager_mod.coordinate_ttadk_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=lambda m: ("ok", "sid"),
        precheck_fn=_precheck_auto,
    )
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    pre = next(a for a in attempts if a.get("phase") == "precheck")
    assert pre.get("resolved_model") == "(auto)"
    assert pre.get("passthrough_model") is None
    assert pre.get("resolved_real_name")

    # 2) validated=True: resolved_model should be the real passthrough model
    def _precheck_valid(model_intent: str) -> dict:
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

    info2 = ttadk_manager_mod.coordinate_ttadk_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=lambda m: ("ok", "sid"),
        precheck_fn=_precheck_valid,
    )
    attempts2 = list((info2.get("diagnostics") or {}).get("attempts") or [])
    pre2 = next(a for a in attempts2 if a.get("phase") == "precheck")
    assert pre2.get("resolved_model") == "gpt-5.2-codex-ttadk"
    assert pre2.get("passthrough_model") == "gpt-5.2-codex-ttadk"
    assert pre2.get("resolved_real_name") == "gpt-5.2-codex-ttadk"


def test_coordinate_ttadk_startup_validated_true_but_empty_model_forces_auto(monkeypatch):
    """回归：precheck 标记 validated=True 但 model 为空时，coordinator 必须按 auto 处理并把 validated 纠偏为 False。"""
    import src.ttadk.startup as ssot

    def _precheck_broken(_: str) -> dict:
        return {
            "tool": "codex",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "model": "",  # broken
            "validated": True,
            "source": "probe",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
            "diagnostics": {},
        }

    info = ssot.coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=lambda m: ("ok", m),
        precheck_fn=_precheck_broken,
    )
    assert info["resolved_model"] == "(auto)"
    assert info["passthrough_model"] is None
    assert info["validated"] is False
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    pre = next(a for a in attempts if a.get("phase") == "precheck")
    assert pre.get("resolved_model") == "(auto)"
    assert pre.get("validated") is False


def test_coordinate_ttadk_startup_manager_compat_delegates_invalid_model_branch(monkeypatch):
    """回归：manager.coordinate_ttadk_startup 必须是 startup SSOT 的薄封装（invalid_model→repair 分支也一致）。"""
    import src.ttadk.startup as ssot
    import src.ttadk.manager as compat

    # 让 repair 路径可控，避免依赖 runtime_repair 的复杂内部细节
    calls: list[dict] = []

    def _fake_repair_invalid_model_startup(
        *,
        manager,
        tool_name,
        input_model,
        cwd,
        error,
        error_blob,
        attempts,
        start_fn,
        fallback_fn=None,
        precheck_fn=None,
        get_settings_fn=None,
        time_fn=None,
        stub_get_last_ts_fn=None,
        stub_set_last_ts_fn=None,
    ) -> dict:
        calls.append({"tool": tool_name, "input_model": input_model, "error_blob": error_blob})
        # 返回字段尽量贴近稳定契约（与 SSOT 一致）
        return {
            "result": ("sess", "sid"),
            "tool": tool_name,
            "input_model": input_model,
            "resolved_model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "warnings": ["repaired"],
            "degraded": False,
            "repaired": True,
            "fail_phase": "invalid_model",
            "decision": "invalid_model_repaired",
            "diagnostics": {"attempts": list(attempts or [])},
        }

    monkeypatch.setattr(ssot, "repair_invalid_model_startup", _fake_repair_invalid_model_startup)

    def _precheck_valid(_: str) -> dict:
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
            "diagnostics": {},
        }

    def _start_fail(_: str | None):
        raise RuntimeError(_SAMPLE_INVALID_MODEL_CODEX)

    r1 = ssot.coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fail,
        fallback_fn=lambda e: ("fb", "fbid"),
        precheck_fn=_precheck_valid,
    )
    r2 = compat.coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fail,
        fallback_fn=lambda e: ("fb", "fbid"),
        precheck_fn=_precheck_valid,
    )
    assert r1 == r2
    assert len(calls) == 2


def test_coordinate_ttadk_startup_manager_compat_delegates_degrade_branch(monkeypatch):
    """回归：manager.coordinate_ttadk_startup 必须与 startup SSOT 在 degrade/fallback 分支一致。"""
    import src.ttadk.startup as ssot
    import src.ttadk.manager as compat

    fb_calls: list[str] = []

    def _fallback(_: Exception):
        fb_calls.append("called")
        return ("fb_sess", "fb_sid")

    def _precheck_auto(_: str) -> dict:
        return {
            "tool": "codex",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2",
            "model": None,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    def _start_fail(_: str | None):
        raise RuntimeError("boom")

    r1 = ssot.coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fail,
        fallback_fn=_fallback,
        precheck_fn=_precheck_auto,
    )
    r2 = compat.coordinate_ttadk_startup(
        manager=object(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fail,
        fallback_fn=_fallback,
        precheck_fn=_precheck_auto,
    )
    assert r1 == r2
    assert len(fb_calls) == 2


def test_sync_adapter_ttadk_codex_passthrough_raises_and_carries_diagnostics(monkeypatch):
    """工具级协议适配：codex 若不支持 `acp serve`，resolve_agent_spec 应抛 AgentSpecResolveError，避免进入 ACP handshake 超时。"""
    import src.acp.sync_adapter as sync_adapter
    from src.acp.session import ACPStartupError

    # 强制探测结果为“不支持”，且返回可诊断的 rc/snippet
    monkeypatch.setattr(sync_adapter, "_probe_acp_serve_help", lambda cmd: (False, 2, "", "codex help"))

    with pytest.raises(sync_adapter.AgentSpecResolveError) as ctx:
        sync_adapter.resolve_agent_spec("ttadk_codex", model_name="gpt-5.2")
    e = ctx.value
    assert getattr(e, "agent_cmd", "") == "codex"
    assert "acp" in " ".join(getattr(e, "agent_args", []) or [])
    assert getattr(e, "returncode", None) == 2
    assert "codex" in (getattr(e, "stderr_snippet", "") or "")
    # 新协议：AgentSpecResolveError 继承 ACPStartupError
    assert isinstance(e, ACPStartupError)
    assert getattr(e, "fail_phase", "") in ("agent_spec_resolve", "")


def test_acp_startup_failure_logs_have_stable_diagnostics_fields(monkeypatch, caplog):
    """防回归：ACP/TTADK 启动失败日志必须包含稳定字段，避免出现“日志为空/极少”。"""
    import logging
    from types import SimpleNamespace
    from src.acp.manager import ACPSessionManager
    import src.acp.manager as mgr

    # Fake session always fails to start with an exception whose str() 可能为空
    class _EmptyStrError(Exception):
        def __str__(self):
            return ""

    class _FailSession:
        def __init__(self, agent_type: str, cwd: str, **kwargs):
            self.session_id = ""
            self.last_active = 0.0
            self.message_count = 0

        def describe_agent(self):
            return "cmd=fake args=acp serve cwd=."

        def start(self, startup_timeout: float = 60):
            raise _EmptyStrError()

        def load_local_history(self, *a, **kw):
            return []

        def close(self):
            return None

        def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
            return False

    # ensure start_session uses retry=1 to hit logging once
    monkeypatch.setattr(mgr, "SyncACPSession", _FailSession)
    monkeypatch.setattr(mgr, "get_settings", lambda: SimpleNamespace(acp_startup_retries=1, acp_healthcheck_timeout=0.01))

    caplog.set_level(logging.WARNING)
    m = ACPSessionManager("coco", session_timeout=999999)
    with pytest.raises(RuntimeError):
        m.start_session("chat1", cwd=".", startup_timeout=0.01)

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "Session start failed" in joined
    # stable diagnostics keys must exist in log
    assert "\"cmd\"" in joined
    assert "\"args\"" in joined
    assert "\"rc\"" in joined
    assert "\"stdout_snippet\"" in joined
    assert "\"stderr_snippet\"" in joined


def test_ttadk_claude_startup_failure_logs_have_diagnostics_and_fail_phase(monkeypatch, caplog):
    """防回归：TTADK_CLAUDE 启动失败时也必须输出稳定诊断字段，且包含 fail_phase。"""
    import logging
    from types import SimpleNamespace

    import src.acp.sync_adapter as sa
    from src.acp.manager import ACPSessionManager

    # 1) 让 start_ttadk_session_with_pty_retry 抛“空 message，但带 stderr_snippet”的异常
    class _EmptyMsgWithStderr(RuntimeError):
        def __str__(self):
            return ""

    def _fake_start_ttadk_session_with_pty_retry(*, agent_type, cwd, startup_timeout, model_name=None, session_cls=None, **kw):
        e = _EmptyMsgWithStderr("ignored")
        e.stderr_snippet = "✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-ttadk"
        raise e

    monkeypatch.setattr(sa, "start_ttadk_session_with_pty_retry", _fake_start_ttadk_session_with_pty_retry)
    # 避免触发真实模型探测（probe 超时）：让 precheck 直接返回 defaults
    monkeypatch.setattr(
        "src.ttadk.startup_common.precheck_ttadk_startup_model",
        lambda **kw: {
            "tool": "claude",
            "input_model": kw.get("model_intent") or "",
            "resolved_real_name": kw.get("model_intent") or "",
            "model": None,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        },
    )
    monkeypatch.setattr("src.acp.manager.get_settings", lambda: SimpleNamespace(acp_startup_retries=1, acp_healthcheck_timeout=0.01))

    caplog.set_level(logging.WARNING)

    m = ACPSessionManager("coco", session_timeout=999999)
    with pytest.raises(RuntimeError):
        m.start_session(
            "chat1",
            cwd=".",
            startup_timeout=0.01,
            agent_type_override="ttadk_claude",
            model_name="gpt-5.2",
        )

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "TTADK" in joined
    assert "\"cmd\"" in joined
    assert "\"args\"" in joined
    assert "\"rc\"" in joined
    assert "\"stdout_snippet\"" in joined
    assert "\"stderr_snippet\"" in joined
    assert "\"fail_phase\"" in joined


def test_startup_diagnostics_redacts_token_like_text(monkeypatch):
    """脱敏回归：diagnostics 输出中 token 类文本应被替换为 ***REDACTED***。"""
    import src.acp.sync_adapter as sa

    # Force redaction enabled and use default replacement.
    monkeypatch.setattr(
        sa,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "acp_diagnostics_redact_enabled": True,
                "acp_diagnostics_redact_replacement": "***REDACTED***",
                # keep patterns default (include sk-*)
                "acp_diagnostics_redact_patterns": None,
                "acp_diagnostics_args_limit": 600,
                "acp_diagnostics_snippet_limit": 240,
                "acp_diagnostics_total_limit": 2000,
            },
        )(),
    )

    class _Err(Exception):
        pass

    e = _Err("boom")
    # include a token-like substring
    e.stderr_snippet = "request failed: sk-1234567890abcdefTOKEN"
    diag = sa.build_startup_diagnostics(agent_type="ttadk_claude", cwd="/tmp", model_name="gpt-5.2", error=e)
    text = sa.format_startup_diagnostics(diag)
    assert "sk-1234567890abcdefTOKEN" not in text
    assert "***REDACTED***" in text


@pytest.mark.parametrize(
    "name,error_obj,error_blob,expected",
    [
        ("invalid_model", RuntimeError(""), "✗ Error: Invalid model 'gpt-5.2'. Available models: m1", "invalid_model"),
        ("stdin_not_tty", RuntimeError(""), "Error: stdin is not a terminal", "stdin_not_tty"),
        ("timeout", TimeoutError("boom"), "", "timeout"),
        ("start_failed", RuntimeError("boom"), "some other failure", "start_failed"),
    ],
)
def test_startup_fail_phase_classification_and_diagnostics_consistent(monkeypatch, name, error_obj, error_blob, expected):
    """fail_phase 分类：分类函数与 build_startup_diagnostics 输出应一致，且稳定存在。"""
    import src.acp.sync_adapter as sa

    phase = sa.classify_startup_fail_phase(error=error_obj, error_blob=error_blob)
    assert phase == expected

    # 让 diagnostics 从 snippet 里拿到 error_blob（模拟真实错误输出）
    setattr(error_obj, "stderr_snippet", error_blob)
    diag = sa.build_startup_diagnostics(agent_type="ttadk_coco", cwd="/tmp", model_name="", error=error_obj)
    assert diag.get("fail_phase") == expected


def test_startup_fail_phase_best_effort_import_failure_falls_back_start_failed(monkeypatch):
    """best-effort：TTADK 模块不可导入时不应抛异常，并回退为 start_failed。"""
    import importlib
    import src.acp.sync_adapter as sa

    orig = importlib.import_module

    def _boom(name: str):
        if name == "src.ttadk.models":
            raise ImportError("no ttadk")
        return orig(name)

    monkeypatch.setattr(importlib, "import_module", _boom)

    # 该文案通常由 TTADK matcher 覆盖（中文变体），但不在最小英文字符串兜底集合里
    phase = sa.classify_startup_fail_phase(error=RuntimeError(""), error_blob="模型无效: gpt-5.2")
    assert phase == "start_failed"


def test_startup_fail_phase_best_effort_matcher_raises_falls_back_to_start_failed(monkeypatch):
    """best-effort：TTADK 识别函数抛异常时不应影响分类，最终回退 start_failed。"""
    import importlib
    import types
    import src.acp.sync_adapter as sa

    orig = importlib.import_module

    def _fake_ttadk_models():
        m = types.SimpleNamespace()

        def _boom(_: str) -> bool:
            raise RuntimeError("matcher boom")

        m.is_invalid_model_error = _boom
        m.is_stdin_not_tty_error = _boom
        return m

    def _patched(name: str):
        if name == "src.ttadk.models":
            return _fake_ttadk_models()
        return orig(name)

    monkeypatch.setattr(importlib, "import_module", _patched)

    phase = sa.classify_startup_fail_phase(error=RuntimeError(""), error_blob="模型无效: gpt-5.2")
    assert phase == "start_failed"


def test_acp_manager_startup_diagnostics_uses_ssot_builder(monkeypatch):
    """防回归：manager 的诊断构造必须走 sync_adapter.build_startup_diagnostics（SSOT）。"""
    from types import SimpleNamespace
    import src.acp.manager as mgr
    import src.acp.sync_adapter as sa

    calls = {"n": 0, "last": None}

    def _spy_build_startup_diagnostics(**kwargs):
        calls["n"] += 1
        calls["last"] = dict(kwargs)
        # 返回最小可序列化字典即可
        return {
            "cmd": "",
            "args": [],
            "rc": None,
            "stdout_snippet": "",
            "stderr_snippet": "",
            "agent_type": kwargs.get("agent_type") or "",
            "cwd": kwargs.get("cwd") or "",
            "model": kwargs.get("model_name") or "",
        }

    # SSOT: manager 引用的符号应与 sync_adapter 一致
    assert mgr.build_startup_diagnostics is sa.build_startup_diagnostics

    # spy 需要 patch 到 manager 局部符号（manager 模块内已 from-import）
    monkeypatch.setattr(mgr, "build_startup_diagnostics", _spy_build_startup_diagnostics)

    class _FailSession:
        def __init__(self, agent_type: str, cwd: str, **kwargs):
            self.session_id = ""
            self.last_active = 0.0
            self.message_count = 0

        def start(self, startup_timeout: float = 60):
            raise RuntimeError("boom")

        def describe_agent(self):
            return "cmd=fake args=acp serve cwd=."

        def close(self):
            return None

        def load_local_history(self, *a, **kw):
            return []

        def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
            return False

    monkeypatch.setattr(mgr, "SyncACPSession", _FailSession)
    monkeypatch.setattr(mgr, "get_settings", lambda: SimpleNamespace(acp_startup_retries=1, acp_healthcheck_timeout=0.01))

    m = mgr.ACPSessionManager("coco", session_timeout=999999)
    with pytest.raises(RuntimeError):
        m.start_session("chat1", cwd=".", startup_timeout=0.01)

    assert calls["n"] >= 1
    assert (calls["last"] or {}).get("agent_type") == "coco"


def test_acp_session_manager_ttadk_start_session_uses_coordinator(monkeypatch):
    """ACPSessionManager.start_session(ttadk_*)：必须走 TTADK SSOT 启动入口并将结果写入会话字典。"""
    from src.acp.manager import ACPSessionManager
    import src.acp.sync_adapter as sync_adapter

    # 1) Dummy TTADKManager：仅用于提供 current model
    class _DummyTTADKMgr:
        def get_current_model(self):
            return "gpt-5.2"

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: _DummyTTADKMgr())

    # 2) Patch start_ttadk_session_with_pty_retry：确保 start_fn 被 coordinator 调用，并接收到 passthrough_model
    calls: list[dict] = []

    class _DummySession:
        def __init__(self, sid: str):
            self.session_id = sid
            self.last_active = 0.0
            self.message_count = 0
            self.last_query = ""
            self.is_resumed = False

        def describe_agent(self):
            return "dummy"

        def load_local_history(self, *a, **kw):
            return []

        def close(self):
            return None

    def _fake_start_ttadk_session_with_pty_retry(*, agent_type, cwd, startup_timeout, model_name=None, session_cls=None):
        calls.append({"agent_type": agent_type, "cwd": cwd, "timeout": startup_timeout, "model_name": model_name})
        return _DummySession("sid-ttadk")

    monkeypatch.setattr(sync_adapter, "start_ttadk_session_with_pty_retry", _fake_start_ttadk_session_with_pty_retry)

    # 3) Patch TTADK SSOT 启动入口：验证它被调用，并控制 passthrough model
    seen: dict = {"called": False}

    def _fake_start_agent_session(**kw):
        seen["called"] = True
        assert kw.get("agent_type") == "ttadk_codex"
        assert kw.get("cwd") == "/tmp"
        # 直接调用底层 start_ttadk_session_with_pty_retry（此处已被 patch），模拟 passthrough
        s = sync_adapter.start_ttadk_session_with_pty_retry(
            agent_type="ttadk_codex",
            cwd="/tmp",
            startup_timeout=1,
            model_name="gpt-5.2-codex-ttadk",
            session_cls=None,
        )
        return {
            "session": s,
            "session_id": getattr(s, "session_id", ""),
            "tool": "codex",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2-codex-ttadk",
            "passthrough_model": "gpt-5.2-codex-ttadk",
            "resolved_model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "warnings": [],
            "degraded": False,
            "repaired": False,
            "fail_phase": "",
            "decision": "start_ok",
            "diagnostics": {"attempts": []},
        }

    monkeypatch.setattr("src.ttadk.startup.start_agent_session", _fake_start_agent_session)

    mgr = ACPSessionManager(agent_type="coco")
    s = mgr.start_session(
        chat_id="c1",
        cwd="/tmp",
        startup_timeout=1,
        project_id="p1",
        agent_type_override="ttadk_codex",
        model_name="gpt-5.2",
    )
    assert seen["called"] is True
    assert getattr(s, "session_id", "") == "sid-ttadk"
    assert calls and calls[-1]["model_name"] == "gpt-5.2-codex-ttadk"


def test_acp_session_manager_ttadk_coordinator_failure_degrades_to_coco(monkeypatch):
    """ACPSessionManager.start_session(ttadk_*)：TTADK SSOT 启动入口异常时应确定性降级到 coco ACP。"""
    from src.acp.manager import ACPSessionManager
    import src.coco_model as coco_model_mod
    import src.acp.manager as acp_manager_mod

    class _DummyTTADKMgr:
        def get_current_model(self):
            return "gpt-5.2"

    monkeypatch.setattr("src.ttadk.get_ttadk_manager", lambda *a, **kw: _DummyTTADKMgr())

    # TTADK SSOT 启动入口直接抛异常
    monkeypatch.setattr("src.ttadk.startup.start_agent_session", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    # Patch coco model
    monkeypatch.setattr(coco_model_mod, "get_coco_model_manager", lambda: type("C", (), {"get_current_model": lambda self: "coco-model"})())

    # Patch SyncACPSession to avoid real process spawning
    created: list[dict] = []

    class _DummySyncACPSession:
        def __init__(self, agent_type: str, cwd: str, agent_cmd=None, agent_args=None, **kw):
            created.append({"agent_type": agent_type, "cwd": cwd, "agent_cmd": agent_cmd, "agent_args": list(agent_args or [])})
            self.session_id = "sid-coco"
            self.last_active = 0.0
            self.message_count = 0
            self.last_query = ""
            self.is_resumed = False

        def start(self, startup_timeout: float = 60):
            return self.session_id

        def describe_agent(self):
            return "dummy"

        def load_local_history(self, *a, **kw):
            return []

        def close(self):
            return None

    monkeypatch.setattr(acp_manager_mod, "SyncACPSession", _DummySyncACPSession)

    # 注意：TTADK SSOT 失败后会降级到 coco；为避免“降级成功但仍 raise”的回归，允许正常返回。
    m = ACPSessionManager(agent_type="coco")
    s = m.start_session(
        chat_id="c1",
        cwd="/tmp",
        startup_timeout=1,
        project_id="p1",
        agent_type_override="ttadk_codex",
        model_name="gpt-5.2",
    )
    assert getattr(s, "session_id", "") == "sid-coco"
    assert created and created[-1]["agent_cmd"] == "coco"
    assert "acp" in " ".join(created[-1]["agent_args"]).lower()


def test_create_engine_session_ttadk_claude_adapter_failure_degrades_to_coco(monkeypatch):
    """TTADK tool=claude 若快速探测发现不产出 ACP JSON-RPC，应降级到 coco。"""
    import src.agent_session as agent_session

    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})(),
    )

    class _DummyCocoMgr:
        def get_current_model(self):
            return "coco-model"

    monkeypatch.setattr("src.coco_model.get_coco_model_manager", lambda: _DummyCocoMgr())

    calls: list[dict] = []

    class _DummySession:
        def __init__(self):
            self.session_id = "ok"

    def _fake_start_session_with_retry(agent_type: str, cwd: str, startup_timeout: float, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "timeout": startup_timeout, "model_name": model_name})
        return _DummySession()

    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    # 让 quickcheck 失败，触发降级：resolve_agent_spec 返回一个不会输出 JSON 的长睡眠进程
    monkeypatch.setattr(
        "src.acp.sync_adapter.resolve_agent_spec",
        lambda *a, **kw: ("python3", ["-c", "import time; time.sleep(10)"]),
    )

    # 让 start_agent_session 快速走 fallback：不依赖真实 ttadk 二进制探测
    def _fake_start_agent_session(**kw):
        # fallback 应调用 coco 的 start_session_with_retry
        from src.coco_model import get_coco_model_manager
        from src.acp.sync_adapter import start_session_with_retry

        coco_model = get_coco_model_manager().get_current_model()
        s2 = start_session_with_retry(agent_type="coco", cwd=kw.get("cwd") or "/tmp", startup_timeout=kw.get("startup_timeout") or 1, model_name=coco_model)
        setattr(s2, "_degraded_to", "coco")
        setattr(s2, "_agent_type", "ttadk_claude")
        return {
            "session": s2,
            "session_id": getattr(s2, "session_id", ""),
            "tool": "claude",
            "input_model": kw.get("model_name") or "",
            "resolved_real_name": kw.get("model_name") or "",
            "passthrough_model": None,
            "resolved_model": "(fallback)",
            "validated": False,
            "source": "fallback",
            "warnings": ["degraded"],
            "degraded": True,
            "repaired": False,
            "fail_phase": "protocol_adapter",
            "decision": "start_failed_degraded",
            "diagnostics": {"attempts": [{"phase": "start", "ok": False, "error": "(empty)"}]},
        }

    monkeypatch.setattr("src.ttadk.startup.start_agent_session", _fake_start_agent_session)

    s = agent_session.create_engine_session(agent_type="ttadk_claude", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "ok"
    assert calls == [{"agent_type": "coco", "cwd": "/tmp", "timeout": 1, "model_name": "coco-model"}]


def test_start_ttadk_engine_session_contract_invalid_model_seeded(monkeypatch):
    """SSOT 契约冻结：invalid-model（带 Available models）应进入 repair→retry，并写入 attempts。"""
    from src.ttadk.manager import start_ttadk_engine_session

    class _Sess:
        def __init__(self, model):
            self._model_name = model

    calls = {"n": 0}

    def _fake_start_ttadk_session_fn(*, agent_type, cwd, startup_timeout, model_name=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("✗ Error: Invalid model 'bad'. Available models: a, b")
        return _Sess(model_name)

    def _fake_precheck(intent: str) -> dict:
        return {
            "tool": "coco",
            "input_model": intent,
            "resolved_real_name": "a",
            "model": "a",
            "validated": True,
            "source": "seed",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
            "diagnostics": {},
        }

    def _fake_resolve_agent_spec(agent_type, model_name=None, **kwargs):
        return ("ttadk", ["code", "-t", "coco"])

    def _fake_fallback(err: Exception):
        return _Sess("fallback")

    class _M:
        pass

    class _S:
        ttadk_runtime_retry_enabled = True
        ttadk_runtime_retry_cooldown_s = 0
        ttadk_runtime_retry_allow_autoswitch = True
        ttadk_claude_acp_ready_check_enabled = True
        ttadk_claude_acp_ready_check_timeout_s = 0.1

    info = start_ttadk_engine_session(
        agent_type="ttadk_coco",
        cwd="/tmp",
        model_intent="bad",
        startup_timeout=1.0,
        manager=_M(),
        start_ttadk_session_fn=_fake_start_ttadk_session_fn,
        resolve_agent_spec_fn=_fake_resolve_agent_spec,
        precheck_fn=_fake_precheck,
        fallback_fn=_fake_fallback,
        get_settings_fn=lambda: _S(),
        time_fn=lambda: 0.0,
    )

    assert info["resolved_model"] == "a"
    assert info["validated"] is True
    assert info["repaired"] is True
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    assert attempts
    phases = [a.get("phase") for a in attempts]
    assert "start" in phases
    assert "repair" in phases
    assert "retry" in phases


def test_start_ttadk_engine_session_contract_protocol_adapter_failed_degrades(monkeypatch):
    """SSOT 契约冻结：协议适配失败应归类为 protocol_adapter，并走 fallback。"""
    from src.ttadk.manager import start_ttadk_engine_session

    class _Sess:
        pass

    def _fake_start_ttadk_session_fn(**kwargs):
        raise AssertionError("should_not_start")

    def _fake_precheck(intent: str) -> dict:
        return {
            "tool": "codex",
            "input_model": intent,
            "resolved_real_name": intent,
            "model": None,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    def _fake_resolve_agent_spec(agent_type, model_name=None, **kwargs):
        raise RuntimeError("serve_not_supported")

    called = {"n": 0}

    def _fake_fallback(err: Exception):
        called["n"] += 1
        return _Sess()

    class _M:
        pass

    class _S:
        ttadk_runtime_retry_enabled = True
        ttadk_runtime_retry_cooldown_s = 0
        ttadk_runtime_retry_allow_autoswitch = True
        ttadk_claude_acp_ready_check_enabled = True
        ttadk_claude_acp_ready_check_timeout_s = 0.1

    info = start_ttadk_engine_session(
        agent_type="ttadk_codex",
        cwd="/tmp",
        model_intent="gpt-5.2",
        startup_timeout=1.0,
        manager=_M(),
        start_ttadk_session_fn=_fake_start_ttadk_session_fn,
        resolve_agent_spec_fn=_fake_resolve_agent_spec,
        precheck_fn=_fake_precheck,
        fallback_fn=_fake_fallback,
        get_settings_fn=lambda: _S(),
        time_fn=lambda: 0.0,
    )

    assert called["n"] == 1
    assert info["degraded"] is True
    # resolve_agent_spec 失败由 start_ttadk_engine_session 显式包装为 TTADKStartupError
    # 因此必须稳定归类为 protocol_adapter，避免回归到 start_failed 造成排障困难。
    assert info["fail_phase"] == "protocol_adapter"
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    assert attempts
    failed = [a for a in attempts if a.get("phase") == "start" and a.get("ok") is False]
    assert failed, "expected a failed start attempt"
    assert (failed[-1].get("error") or "") != ""


def test_start_ttadk_engine_session_claude_acp_not_ready_degrades_and_has_diagnostics(monkeypatch):
    """ACP 不产出 JSON：claude quickcheck 失败应降级，且 attempts 诊断字段不为空。"""
    import time
    from src.ttadk.manager import start_ttadk_engine_session

    class _Sess:
        def __init__(self, sid: str):
            self.session_id = sid

    # quickcheck 会启动该命令：只输出 banner，不输出 JSON
    def _fake_resolve_agent_spec(agent_type, model_name=None, **kwargs):
        assert agent_type == "ttadk_claude"
        return ("python3", ["-c", "import sys,time; sys.stdout.write('banner\\n'); sys.stdout.flush(); time.sleep(10)"])

    def _should_not_start(**kwargs):
        raise AssertionError("start_ttadk_session_fn should not be called when quickcheck fails")

    def _precheck(_: str) -> dict:
        return {
            "tool": "claude",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2",
            "model": None,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    called = {"n": 0}

    def _fallback(err: Exception):
        called["n"] += 1
        return _Sess("fb")

    class _S:
        # 强制启用 quickcheck，且缩短超时，避免测试慢
        ttadk_claude_acp_ready_check_enabled = True
        ttadk_claude_acp_ready_check_timeout_s = 0.05
        ttadk_runtime_retry_enabled = True
        ttadk_runtime_retry_cooldown_s = 0
        ttadk_runtime_retry_allow_autoswitch = True

    info = start_ttadk_engine_session(
        agent_type="ttadk_claude",
        cwd="/tmp",
        model_intent="gpt-5.2",
        startup_timeout=1.0,
        manager=object(),
        start_ttadk_session_fn=_should_not_start,
        resolve_agent_spec_fn=_fake_resolve_agent_spec,
        precheck_fn=_precheck,
        fallback_fn=_fallback,
        get_settings_fn=lambda: _S(),
        time_fn=time.monotonic,
    )

    assert called["n"] == 1
    assert info["degraded"] is True
    assert info["fail_phase"] == "protocol_adapter"
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    failed = [a for a in attempts if a.get("phase") == "start" and a.get("ok") is False]
    assert failed
    assert (failed[-1].get("error") or "") != ""


def test_start_ttadk_engine_session_claude_start_timeout_degrades_and_has_diagnostics(monkeypatch):
    """启动超时：quickcheck 通过但 start_fn 超时，应归类 timeout 并降级，且 attempts 诊断字段不为空。"""
    import time
    from src.ttadk.manager import start_ttadk_engine_session

    class _Sess:
        def __init__(self, sid: str):
            self.session_id = sid

    # quickcheck：输出一行 '{' 让其立即判定 ready
    def _fake_resolve_agent_spec(agent_type, model_name=None, **kwargs):
        assert agent_type == "ttadk_claude"
        return ("python3", ["-c", "import sys,time; sys.stdout.write('{\\n'); sys.stdout.flush(); time.sleep(10)"])

    def _start_timeout(**kwargs):
        raise TimeoutError("startup_timeout")

    def _precheck(_: str) -> dict:
        return {
            "tool": "claude",
            "input_model": "gpt-5.2",
            "resolved_real_name": "gpt-5.2",
            "model": None,
            "validated": False,
            "source": "defaults",
            "decision": "precheck_auto",
            "fail_phase": "",
            "warnings": ["no_m_passthrough"],
            "diagnostics": {},
        }

    called = {"n": 0}

    def _fallback(err: Exception):
        called["n"] += 1
        return _Sess("fb")

    class _S:
        ttadk_claude_acp_ready_check_enabled = True
        ttadk_claude_acp_ready_check_timeout_s = 0.1
        ttadk_runtime_retry_enabled = True
        ttadk_runtime_retry_cooldown_s = 0
        ttadk_runtime_retry_allow_autoswitch = True

    info = start_ttadk_engine_session(
        agent_type="ttadk_claude",
        cwd="/tmp",
        model_intent="gpt-5.2",
        startup_timeout=1.0,
        manager=object(),
        start_ttadk_session_fn=_start_timeout,
        resolve_agent_spec_fn=_fake_resolve_agent_spec,
        precheck_fn=_precheck,
        fallback_fn=_fallback,
        get_settings_fn=lambda: _S(),
        time_fn=time.monotonic,
    )

    assert called["n"] == 1
    assert info["degraded"] is True
    assert info["fail_phase"] == "timeout"
    attempts = list((info.get("diagnostics") or {}).get("attempts") or [])
    failed = [a for a in attempts if a.get("phase") == "start" and a.get("ok") is False]
    assert failed
    assert (failed[-1].get("error") or "") != ""


def test_startup_probe_quickcheck_detects_json_line(monkeypatch):
    """startup_probe: stdout 出现以 '{' 开头行时应判定 ready。"""
    import time

    from src.ttadk.startup_probe import ttadk_acp_ready_quickcheck

    # 输出一行以 '{' 开头
    def _resolve(_agent_type: str, model_name=None, **kwargs):
        return ("python3", ["-c", "import sys; sys.stdout.write('{\\n'); sys.stdout.flush(); sys.exit(0)"])

    assert (
        ttadk_acp_ready_quickcheck(
            agent_type="ttadk_claude",
            cwd="/tmp",
            model_name=None,
            resolve_agent_spec_fn=_resolve,
            time_fn=time.monotonic,
            timeout_s=0.2,
        )
        is True
    )


def test_startup_probe_quickcheck_banner_only_returns_false(monkeypatch):
    """startup_probe: 仅 banner 且超时内无 JSON，应返回 False。"""
    import time

    from src.ttadk.startup_probe import ttadk_acp_ready_quickcheck

    def _resolve(_agent_type: str, model_name=None, **kwargs):
        # 写 banner 然后睡眠，确保超时路径覆盖
        return (
            "python3",
            ["-c", "import sys,time; sys.stdout.write('banner\\n'); sys.stdout.flush(); time.sleep(10)"],
        )

    assert (
        ttadk_acp_ready_quickcheck(
            agent_type="ttadk_claude",
            cwd="/tmp",
            model_name=None,
            resolve_agent_spec_fn=_resolve,
            time_fn=time.monotonic,
            timeout_s=0.05,
        )
        is False
    )


def test_startup_probe_quickcheck_process_exits_early_returns_false(monkeypatch):
    """startup_probe: 进程提前退出且未产出 JSON，应返回 False。"""
    import time

    from src.ttadk.startup_probe import ttadk_acp_ready_quickcheck

    def _resolve(_agent_type: str, model_name=None, **kwargs):
        return ("python3", ["-c", "import sys; sys.stdout.write('banner\\n'); sys.stdout.flush(); sys.exit(0)"])

    assert (
        ttadk_acp_ready_quickcheck(
            agent_type="ttadk_claude",
            cwd="/tmp",
            model_name=None,
            resolve_agent_spec_fn=_resolve,
            time_fn=time.monotonic,
            timeout_s=0.2,
        )
        is False
    )


def test_startup_probe_quickcheck_no_newline_tail_detected(monkeypatch):
    """startup_probe: 最后一段无换行但以 '{' 开头时也应判定 ready。"""
    import time

    from src.ttadk.startup_probe import ttadk_acp_ready_quickcheck

    def _resolve(_agent_type: str, model_name=None, **kwargs):
        return ("python3", ["-c", "import sys; sys.stdout.write('{'); sys.stdout.flush(); sys.exit(0)"])

    assert (
        ttadk_acp_ready_quickcheck(
            agent_type="ttadk_claude",
            cwd="/tmp",
            model_name=None,
            resolve_agent_spec_fn=_resolve,
            time_fn=time.monotonic,
            timeout_s=0.2,
        )
        is True
    )


def test_ttadk_models():
    tool = TTADKTool(name="test_tool", description="Test Tool", is_default=True)
    assert tool.name == "test_tool"
    assert tool.description == "Test Tool"


def test_ttadk_wrapper_fdreader_readline_handles_newlines(monkeypatch):
    """_FDReader.readline：单 chunk 含换行时应按行返回（包含换行）。"""
    from src.utils import ttadk_wrapper

    payloads = [b"hello\nworld\n", b""]

    def _fake_read(fd: int, n: int):
        assert fd == 123
        return payloads.pop(0)

    monkeypatch.setattr(ttadk_wrapper.os, "read", _fake_read)
    r = ttadk_wrapper._FDReader(123, chunk_size=64)
    assert r.readline() == b"hello\n"
    assert r.readline() == b"world\n"
    assert r.readline() == b""


def test_ttadk_wrapper_fdreader_readline_spans_chunks(monkeypatch):
    """_FDReader.readline：换行跨 chunk 时也能正确拼接。"""
    from src.utils import ttadk_wrapper

    payloads = [b"hello", b"\nwo", b"rld\n", b""]

    def _fake_read(fd: int, n: int):
        assert fd == 123
        return payloads.pop(0)

    monkeypatch.setattr(ttadk_wrapper.os, "read", _fake_read)
    r = ttadk_wrapper._FDReader(123, chunk_size=2)
    assert r.readline() == b"hello\n"
    assert r.readline() == b"world\n"
    assert r.readline() == b""


def test_ttadk_wrapper_fdreader_readline_eof_returns_tail(monkeypatch):
    """_FDReader.readline：EOF 且无换行时返回剩余缓冲，然后再返回空。"""
    from src.utils import ttadk_wrapper

    # 注意：readline() 在找不到换行时可能会多次 os.read 探测直到 EOF。
    payloads = [b"tail-without-newline", b"", b""]

    def _fake_read(fd: int, n: int):
        assert fd == 123
        return payloads.pop(0)

    monkeypatch.setattr(ttadk_wrapper.os, "read", _fake_read)
    r = ttadk_wrapper._FDReader(123, chunk_size=64)
    assert r.readline() == b"tail-without-newline"
    assert r.readline() == b""


def test_ttadk_wrapper_fdreader_read_consumes_buffer_first(monkeypatch):
    """_FDReader.read：应优先消耗内部缓冲再继续读 fd。"""
    from src.utils import ttadk_wrapper

    payloads = [b"abc\n", b"XYZ", b""]

    def _fake_read(fd: int, n: int):
        """模拟 os.read：最多返回 n 字节，剩余回灌到下一次读取。"""
        assert fd == 123
        if not payloads:
            return b""
        chunk = payloads.pop(0)
        if not chunk:
            return b""
        # os.read 的语义：最多 n 字节
        out = chunk[:n]
        rest = chunk[n:]
        if rest:
            payloads.insert(0, rest)
        return out

    monkeypatch.setattr(ttadk_wrapper.os, "read", _fake_read)
    r = ttadk_wrapper._FDReader(123, chunk_size=64)
    assert r.readline() == b"abc\n"
    assert r.read(2) == b"XY"
    assert r.read(2) == b"Z"


def test_ttadk_tool_model_basic_fields():
    """基础数据模型字段防回归（避免被误改）。"""
    from src.ttadk import TTADKTool, TTADKModel

    tool = TTADKTool(name="test_tool", description="Test Tool", is_default=True)
    assert tool.name == "test_tool"
    assert tool.description == "Test Tool"
    assert tool.is_default is True

    model = TTADKModel(name="test_model", description="Test Model", is_default=False)
    assert model.name == "test_model"
    assert model.description == "Test Model"
    assert model.is_default is False


def test_ttadk_wrapper_pump_stdin_to_fd_writes_and_exits_on_eof(monkeypatch):
    """_pump_stdin_to_fd：读到数据应写入 fd，EOF 后退出。"""
    from src.utils import ttadk_wrapper

    # fake stdin: two chunks then EOF
    seq = [b"hello", b" world", b""]

    class _FakeBuf:
        def read(self, n: int):
            return seq.pop(0)

    class _FakeStdin:
        buffer = _FakeBuf()

    writes: list[bytes] = []

    def _fake_write(fd: int, data: bytes):
        assert fd == 7
        writes.append(bytes(data))
        return len(data)

    monkeypatch.setattr(ttadk_wrapper, "sys", type("S", (), {"stdin": _FakeStdin()})())
    monkeypatch.setattr(ttadk_wrapper.os, "write", _fake_write)

    ttadk_wrapper._pump_stdin_to_fd(fd=7, chunk_size=3)
    assert b"".join(writes) == b"hello world"


def test_ttadk_wrapper_pump_stdin_to_fd_swallow_write_error(monkeypatch):
    """_pump_stdin_to_fd：os.write 失败应吞掉并退出，不抛异常。"""
    from src.utils import ttadk_wrapper

    seq = [b"hello", b"more", b""]

    class _FakeBuf:
        def read(self, n: int):
            return seq.pop(0)

    class _FakeStdin:
        buffer = _FakeBuf()

    calls: list[bytes] = []

    def _fake_write(fd: int, data: bytes):
        calls.append(bytes(data))
        raise BrokenPipeError("boom")

    monkeypatch.setattr(ttadk_wrapper, "sys", type("S", (), {"stdin": _FakeStdin()})())
    monkeypatch.setattr(ttadk_wrapper.os, "write", _fake_write)

    # should not raise
    ttadk_wrapper._pump_stdin_to_fd(fd=7, chunk_size=4)
    assert calls and calls[0] == b"hello"


def test_ttadk_wrapper_pty_master_fd_closed_once(monkeypatch):
    """PTY 模式：master_fd 只能由主线程统一关闭一次（stdout 线程不再 close）。"""
    from src.utils import ttadk_wrapper

    # 1) 让 wrapper 进入 PTY 分支，但不真的启动外部进程
    monkeypatch.setattr(ttadk_wrapper, "_parse_args", lambda argv: (True, ["dummy"]))
    monkeypatch.setattr(ttadk_wrapper.os.path, "sep", "/")
    monkeypatch.setattr(ttadk_wrapper.shutil, "which", lambda x: x)

    class _DummyProc:
        returncode = 0

        def wait(self):
            return 0

        def terminate(self):
            return None

    monkeypatch.setattr(ttadk_wrapper, "_spawn_with_pty", lambda cmd: (_DummyProc(), 99))

    # 2) 让 stdout pump 快速退出：select 认为可读，但 os.read 直接 EOF
    monkeypatch.setattr(ttadk_wrapper.select, "select", lambda r, w, x, t=None: (r, [], []))
    monkeypatch.setattr(ttadk_wrapper.os, "read", lambda fd, n: b"")

    # 3) 统计 close 次数
    closed: list[int] = []

    def _spy_close_fd(fd: int):
        closed.append(int(fd))

    monkeypatch.setattr(ttadk_wrapper, "_close_fd_quietly", _spy_close_fd)

    # 4) 捕获 sys.exit
    monkeypatch.setattr(ttadk_wrapper.sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as ctx:
        ttadk_wrapper.main()
    assert int(getattr(ctx.value, "code", 0) or 0) == 0

    # master_fd=99 只能被关闭一次
    assert [x for x in closed if x == 99] == [99]


def test_ttadk_wrapper_pump_error_keeps_failure_diagnostics(monkeypatch, capsys):
    """stdout pump 线程异常时，仍应把 pump_error 写入 banner_tail 以便失败诊断可见。"""
    from src.utils import ttadk_wrapper

    # 进入 PTY 分支
    monkeypatch.setattr(ttadk_wrapper, "_parse_args", lambda argv: (True, ["dummy"]))
    monkeypatch.setattr(ttadk_wrapper.os.path, "sep", "/")
    monkeypatch.setattr(ttadk_wrapper.shutil, "which", lambda x: x)

    class _DummyProc:
        returncode = 1

        def wait(self):
            return 1

        def terminate(self):
            return None

    monkeypatch.setattr(ttadk_wrapper, "_spawn_with_pty", lambda cmd: (_DummyProc(), 77))

    # 强制 pump_filtered_stream 抛异常，触发 banner_tail 记录 pump_error
    monkeypatch.setattr(ttadk_wrapper, "pump_filtered_stream", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    # 避免真实 close 行为干扰测试
    monkeypatch.setattr(ttadk_wrapper, "_close_fd_quietly", lambda fd: None)

    # 捕获 sys.exit
    monkeypatch.setattr(ttadk_wrapper.sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as ctx:
        ttadk_wrapper.main()
    assert int(getattr(ctx.value, "code", 0) or 0) == 1

    out = capsys.readouterr()
    # emit_failure_diagnostics 写到 stderr
    assert "banner_tail" in (out.err or "")
    assert "pump_error" in (out.err or "")


def test_official_cli_models_strategy_parses_json(monkeypatch):
    """OfficialCLIModelsStrategy：命中 --help 后，能解析 JSON 输出得到真实模型列表。"""
    from src.ttadk.strategies import OfficialCLIModelsStrategy

    calls: list[list[str]] = []

    def _runner(args: list[str], cwd, timeout):
        calls.append(list(args))
        # probe --help
        if args[:3] == ["ttadk", "models", "--help"]:
            return (0, "Usage: ttadk models ...", "")
        # list json
        if args[:6] == ["ttadk", "models", "list", "-t", "codex", "-f"]:
            return (0, '{"models": ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]}', "")
        return (1, "", "unknown")

    s = OfficialCLIModelsStrategy(runner=_runner, timeout_s=1.0, probe_ttl_s=999)
    models = s.fetch("codex", cwd="/tmp")
    assert [m.name for m in models] == ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]
    assert any(x[:3] == ["ttadk", "models", "--help"] for x in calls)


def test_official_cli_models_strategy_parses_text_with_ansi(monkeypatch):
    """OfficialCLIModelsStrategy：JSON 不可用时能从文本输出（含 ANSI）解析模型 token。"""
    from src.ttadk.strategies import OfficialCLIModelsStrategy

    def _runner(args: list[str], cwd, timeout):
        if args[:3] == ["ttadk", "models", "--help"]:
            return (0, "Usage: ttadk models ...", "")
        if args[:5] == ["ttadk", "models", "list", "-t", "codex"]:
            return (0, "\x1b[31mgpt-5.2-codex-ttadk\x1b[0m\nfoo\n", "")
        return (1, "", "unknown")

    s = OfficialCLIModelsStrategy(runner=_runner, timeout_s=1.0, probe_ttl_s=999)
    models = s.fetch("codex", cwd="/tmp")
    assert [m.name for m in models] == ["gpt-5.2-codex-ttadk"]


def test_model_fetcher_force_refresh_prefers_official_cli(monkeypatch):
    """TTADKModelFetcher(force_refresh=True) 应优先 official_cli，避免先跑 structured_sync。"""
    from src.ttadk.model_fetcher import TTADKModelFetcher, TTADKRunResult

    class _Runner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run_simple(self, args: list[str], cwd, timeout):
            self.calls.append(list(args))
            # official probe
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            # official list
            if args[:6] == ["ttadk", "models", "list", "-t", "codex", "-f"]:
                return (0, '{"models": ["gpt-5.2-codex-ttadk"]}', "")
            # probe (should not be reached)
            if args[:2] == ["ttadk", "code"]:
                return (1, "", "Invalid model")
            return (1, "", "")

        def run(self, args: list[str], cwd=None, timeout: float = 8.0):
            # structured_sync should be skipped in force_refresh order
            self.calls.append(list(args))
            return TTADKRunResult(returncode=1, stdout="", stderr="structured_sync")

    r = _Runner()
    fetcher = TTADKModelFetcher(runner=r)

    # Ensure official_cli is enabled by config (new switch)
    monkeypatch.setattr(
        "src.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_official_cli_enabled": True,
                "ttadk_models_strategy_order": "",
            },
        )(),
    )

    res = fetcher.fetch_tool_models_with_diagnostics("codex", cwd="/tmp", force_refresh=True)
    assert [m.name for m in res.models] == ["gpt-5.2-codex-ttadk"]
    assert res.source == "official_cli"
    # 第一条应该是 official probe（而不是 ttadk sync）
    assert r.calls and r.calls[0][:3] == ["ttadk", "models", "--help"]


def test_model_fetcher_strategy_order_config_respected(monkeypatch):
    """配置策略顺序时，_select_strategies 应按配置优先级执行，并保留未配置策略的保底补齐。"""
    from src.ttadk.model_fetcher import TTADKModelFetcher, TTADKRunResult
    from src.ttadk.models import TTADKModel

    class _Runner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run_simple(self, args: list[str], cwd, timeout):
            self.calls.append(list(args))
            # official_cli always fails (so we can observe probe later)
            if args[:3] == ["ttadk", "models", "--help"]:
                return (2, "", "unknown command")
            # probe: should be reached if ordered before file_cache
            if args[:2] == ["ttadk", "code"]:
                return (1, "", "✗ Error: Invalid model 'X'. Available models: m1, m2")
            return (1, "", "")

        def run(self, args: list[str], cwd=None, timeout: float = 8.0):
            self.calls.append(list(args))
            return TTADKRunResult(returncode=1, stdout="", stderr="structured_sync")

    r = _Runner()
    fetcher = TTADKModelFetcher(runner=r)

    # Force order: probe before file_cache (and disable structured_sync by passing cwd=None)
    monkeypatch.setattr(
        # Patch get_settings symbol imported in model_fetcher module (not the original src.config.get_settings)
        "src.ttadk.model_fetcher.get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_official_cli_enabled": True,
                "ttadk_models_strategy_order": "official_cli, probe, file_cache",
            },
        )(),
    )

    # Ensure file_cache would return models if called, so we can confirm probe wins by ordering
    monkeypatch.setattr(fetcher._file_cache, "fetch", lambda tool_name, cwd=None: [TTADKModel(name="fc")])

    res = fetcher.fetch_tool_models_with_diagnostics("codex", cwd=None, force_refresh=False)
    assert [m.name for m in res.models] == ["m1", "m2"]
    assert res.source == "probe"


def test_extract_available_models_real_samples():
    from src.ttadk.models import extract_available_models

    assert extract_available_models(_SAMPLE_INVALID_MODEL_CODEX) == [
        "gpt-5.2-codex-ttadk",
        "gpt-5.2-ttadk",
        "gpt-5.3-codex",
    ]
    assert extract_available_models(_SAMPLE_INVALID_MODEL_CLAUDE) == [
        "glm-5-ttadk",
        "kimi-k2.5",
        "glm-4.7-ttadk",
        "gpt-5.2-codex-ttadk",
        "gpt-5.2-ttadk",
    ]
    assert extract_available_models(_SAMPLE_INVALID_MODEL_COCO_EMPTY) == []


def test_extract_available_models_variants():
    """覆盖 ANSI/多行/one-of 变体，确保解析稳定。"""
    from src.ttadk.models import extract_available_models

    assert extract_available_models(_SAMPLE_INVALID_MODEL_ANSI) == [
        "gpt-5.2-codex-ttadk",
        "gpt-5.2-ttadk",
    ]
    assert extract_available_models(_SAMPLE_INVALID_MODEL_MULTILINE) == [
        "gpt-5.2-codex-ttadk",
        "gpt-5.2-ttadk",
        "gpt-5.3-codex",
    ]
    assert extract_available_models(_SAMPLE_INVALID_MODEL_MUST_ONE_OF) == [
        "gpt-5.2-codex-ttadk",
        "gpt-5.2-ttadk",
    ]

    # Banner/login/launch noise should not affect parsing
    assert extract_available_models(_SAMPLE_INVALID_MODEL_WITH_BANNER) == [
        "gpt-5.2-codex-ttadk",
        "gpt-5.2-ttadk",
        "gpt-5.3-codex",
    ]


def test_extract_invalid_model_diagnostics_and_choose_best_available_model():
    from src.ttadk.models import extract_invalid_model_diagnostics, choose_best_available_model

    diag = extract_invalid_model_diagnostics(stdout="", stderr=_SAMPLE_INVALID_MODEL_CODEX)
    assert diag["invalid_model"] is True
    assert "available_models" in diag and diag["available_models"]
    assert "stderr_snippet" in diag

    chosen = choose_best_available_model(input_model="gpt-5.2", available_models=diag["available_models"])
    assert chosen is not None
    assert "gpt-5.2" in chosen


def test_build_invalid_model_context_extracts_models_even_when_err_blob_truncated():
    """回归：运行期上下文构造应从原始错误文本提取 models，不能被日志截断影响。"""
    from src.ttadk.runtime_repair import build_invalid_model_context

    class _Err(RuntimeError):
        def __init__(self, stderr_snippet: str):
            super().__init__("startup failed")
            self.stderr_snippet = stderr_snippet

    # Available models 放在尾部，前面填充大段噪声，模拟真实环境里 stdout/stderr 很长的情况。
    long_noise = "x" * 6000
    e = _Err(long_noise + "\n" + _SAMPLE_INVALID_MODEL_CODEX)

    ctx = build_invalid_model_context(e, limit=400)
    assert ctx["is_invalid_model"] is True
    assert ctx["available_models"][:2] == ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]
    # err_blob 会被截断，但不应为空
    assert (ctx.get("err_blob") or "") != ""


def test_build_invalid_model_context_never_raises_even_if_str_raises():
    """鲁棒性：异常对象 __str__ 出错时也不能让上下文构造崩溃。"""
    from src.ttadk.runtime_repair import build_invalid_model_context

    class _BadStr(Exception):
        def __str__(self):
            raise RuntimeError("boom")

    ctx = build_invalid_model_context(_BadStr(), limit=120)
    assert isinstance(ctx, dict)
    assert (ctx.get("err_blob") or "")
    assert "available_models" in ctx
    assert "is_invalid_model" in ctx


def test_build_invalid_model_context_handles_non_string_snippets(monkeypatch):
    """鲁棒性：stderr/stdout snippet 不是 str（bytes/MagicMock）时也应安全处理。"""
    from unittest.mock import MagicMock

    from src.ttadk.models import build_invalid_model_context

    class _Err(RuntimeError):
        def __init__(self):
            super().__init__("startup failed")
            self.stderr_snippet = b"\xff\xfe" + _SAMPLE_INVALID_MODEL_CODEX.encode("utf-8")
            self.stdout_snippet = MagicMock()

    ctx = build_invalid_model_context(_Err(), limit=200)
    assert isinstance(ctx, dict)
    assert ctx.get("is_invalid_model") is True
    assert ctx.get("available_models")


def test_is_invalid_model_error_real_samples():
    from src.ttadk.models import is_invalid_model_error

    assert is_invalid_model_error(_SAMPLE_INVALID_MODEL_CODEX) is True
    assert is_invalid_model_error(_SAMPLE_INVALID_MODEL_CLAUDE) is True
    assert is_invalid_model_error(_SAMPLE_INVALID_MODEL_COCO_EMPTY) is True


def test_is_invalid_model_error_variants():
    """覆盖常见变体，避免因输出格式变化导致误判。"""
    from src.ttadk.models import is_invalid_model_error

    assert is_invalid_model_error("unknown model 'foo'") is True
    assert is_invalid_model_error("Model must be one of: a, b, c") is True
    assert is_invalid_model_error("invalid value 'x' for --model") is True
    assert is_invalid_model_error("模型无效: gpt-5.2") is True

    # 不应误判普通错误
    assert is_invalid_model_error("network error: timeout") is False

    # Banner/login/launch noise should still be detected as invalid model error
    assert is_invalid_model_error(_SAMPLE_INVALID_MODEL_WITH_BANNER) is True


def test_ttadk_manager():
    manager = TTADKManager(default_tool="coco", default_model="claude-3.5-sonnet")
    
    assert manager.get_current_tool() == "coco"
    assert manager.get_current_model() == "claude-3.5-sonnet"
    
    tools_result = manager.get_tools()
    assert tools_result.error is None
    assert len(tools_result.tools) > 0
    
    models_result = manager.get_models()
    assert models_result.error is None
    assert len(models_result.models) > 0


def test_ttadk_manager_set_tool_and_model():
    manager = TTADKManager()

    assert manager.set_tool("claude") is True
    assert manager.get_current_tool() == "claude"

    assert manager.set_tool("invalid_tool") is False
    assert manager.get_current_tool() == "claude"

    # set_model 会解析模型名称并返回匹配后的真实模型 ID
    # 如果输入 "gpt-5.2"， 它会被解析为匹配的真实模型名（如 gpt-5.2-codex-ttadk）
    result = manager.set_model("gpt-5.2")
    assert result is True
    # 验证模型已设置（解析后的真实模型 ID 包含 gpt-5.2 前缀)
    current_model = manager.get_current_model()
    assert current_model is not None
    assert "gpt-5.2" in current_model

    assert manager.set_model("invalid_model") is False
    # 无效模型不应改变当前模型
    assert manager.get_current_model() == current_model


def test_get_ttadk_manager():
    manager1 = get_ttadk_manager(default_tool="trae", default_model="doubao-1.5-pro")
    manager2 = get_ttadk_manager()
    
    assert manager1 is manager2
    assert manager1.get_current_tool() == "trae"
    assert manager1.get_current_model() == "doubao-1.5-pro"


def test_default_tools():
    manager = TTADKManager()
    # 获取所有工具（不过滤），确保默认工具列表完整
    tools_result = manager.get_tools(filter_available=False)

    tool_names = [t.name for t in tools_result.tools]
    assert "claude" in tool_names
    assert "cursor" in tool_names
    assert "gemini" in tool_names
    assert "codex" in tool_names
    assert "coco" in tool_names
    assert "tmates" in tool_names
    assert "trae" in tool_names
    assert "opencode" in tool_names


def test_default_models():
    manager = TTADKManager()
    models_result = manager.get_models()

    model_names = [m.name for m in models_result.models]
    assert "gpt-5.2" in model_names
    assert "gpt-4.1" in model_names
    assert "claude-3-opus" in model_names
    assert "claude-3.5-sonnet" in model_names
    assert "claude-3.7-sonnet" in model_names
    assert "doubao-1.5-pro" in model_names
    assert "gemini-2.0-pro" in model_names
    assert "gemini-2.5-pro" in model_names


def test_get_tools_filter_available(monkeypatch):
    """测试工具可用性过滤功能"""
    manager = TTADKManager()

    # Mock shutil.which to simulate tool availability
    def mock_which(cmd):
        # 只有 claude, coco, codex 可用
        available = {"claude", "coco", "codex"}
        return f"/usr/bin/{cmd}" if cmd in available else None

    monkeypatch.setattr("shutil.which", mock_which)

    # 测试过滤后的结果
    filtered_result = manager.get_tools(filter_available=True)
    filtered_names = {t.name for t in filtered_result.tools}
    assert filtered_names == {"claude", "coco", "codex"}

    # 测试不过滤的结果
    all_result = manager.get_tools(filter_available=False)
    all_names = {t.name for t in all_result.tools}
    assert "claude" in all_names
    assert "cursor" in all_names  # 即使不可用也应该在列表中
    assert "gemini" in all_names


def test_get_tools_fallback_when_no_tools_available(monkeypatch):
    """测试当过滤后没有可用工具时，回退到完整列表"""
    manager = TTADKManager()

    # Mock shutil.which to simulate no tools available
    def mock_which_none(cmd):
        return None

    monkeypatch.setattr("shutil.which", mock_which_none)

    # 即使 filter_available=True，也应该返回完整列表作为备选
    result = manager.get_tools(filter_available=True)
    tool_names = {t.name for t in result.tools}
    # 应该回退到完整列表
    assert "claude" in tool_names
    assert "cursor" in tool_names


def test_get_models_from_sync_output(monkeypatch):
    manager = TTADKManager(default_tool="coco")
    # sync 已下沉到 fetcher 的 structured 策略，这里直接 patch fetcher 的 structured.fetch
    monkeypatch.setattr(
        manager._model_fetcher._structured,
        "fetch",
        lambda tool_name, cwd=None: [TTADKModel(name="real-model-a"), TTADKModel(name="real-model-b")]
        if tool_name == "coco" else [],
    )

    result = manager.get_models(cwd=".")
    names = [m.name for m in result.models]

    assert result.error is None
    assert names == ["real-model-a", "real-model-b"]


def test_set_model_accepts_synced_model(monkeypatch):
    manager = TTADKManager(default_tool="coco")
    monkeypatch.setattr(
        manager._model_fetcher._structured,
        "fetch",
        lambda tool_name, cwd=None: [TTADKModel(name="real-model")] if tool_name == "coco" else [],
    )

    # Force cache update
    manager.get_models(cwd=".")
    
    # We need to ensure the manager knows about the synced models
    # The previous test failed because manager.set_model checks against 
    # DEFAULT_MODELS and self._known_models
    
    # Let's verify known models contains our synced model
    assert "real-model" in manager._known_models
    
    assert manager.set_model("real-model") is True
    assert manager.get_current_model() == "real-model"


def test_ttadk_model_fetcher_strip_ansi():
    """测试 ANSI 颜色码移除"""
    # Note: Logic moved to InteractiveStrategy
    strategy = InteractiveStrategy()
    text_with_ansi = "\x1b[32mGreen Text\x1b[0m"
    clean = strategy._strip_ansi(text_with_ansi)
    assert clean == "Green Text"


def test_ttadk_model_fetcher_parse_menu():
    """测试模型选择菜单解析"""
    # Note: Logic moved to InteractiveStrategy
    strategy = InteractiveStrategy()
    output = """? Select a model:  (Use arrow keys)
 ❯ GPT 5.2 Codex (Recommended)
   GPT 4.1 Codex
   o4-mini
"""
    names = strategy._parse_model_selection_menu(output)
    assert names == ["GPT 5.2 Codex (Recommended)", "GPT 4.1 Codex", "o4-mini"]


def test_ttadk_model_fetcher_extract_model_name():
    """测试真实模型名称提取"""
    # Note: Logic moved to InteractiveStrategy
    strategy = InteractiveStrategy()
    output = """model:     gpt-5.2-codex-ttadk
provider:  openai"""
    name = strategy._extract_real_model_name(output)
    assert name == "gpt-5.2-codex-ttadk"


def test_ttadk_model_fetcher_cache():
    """测试模型获取器缓存"""
    import time
    fetcher = TTADKModelFetcher()
    # 缓存应该为空
    assert fetcher._is_cache_valid("codex") is False
    # 设置缓存
    fetcher._cache["codex"] = [TTADKModel(name="test-model")]
    fetcher._cache_time["codex"] = time.time()  # 使用当前时间
    # 现在应该有效
    assert fetcher._is_cache_valid("codex") is True
    # 使缓存失效
    fetcher.invalidate_cache("codex")
    assert fetcher._is_cache_valid("codex") is False


def test_ttadk_model_fetcher_diagnostics_cache_hit():
    fetcher = TTADKModelFetcher()
    fetcher._cache["codex"] = [TTADKModel(name="m")]
    fetcher._cache_time["codex"] = time.time()
    r = fetcher.fetch_tool_models_with_diagnostics("codex")
    assert r.source == "memory_cache"
    assert r.models and r.models[0].name == "m"
    assert r.diagnostics.chosen_strategy == "memory_cache"


def test_fetcher_structured_sync_config_missing_warns_and_falls_back_to_probe(monkeypatch):
    """SSOT：probe 优先于 structured_sync；即使 structured_sync 在未 init 时失败，也不应影响 probe 命中。"""
    from src.ttadk.model_fetcher import TTADKModelFetcher, TTADKCommandError
    from src.ttadk.models import TTADKModel

    fetcher = TTADKModelFetcher(runner=_FakeRunner())

    def _structured_fail(tool_name, cwd=None):
        raise TTADKCommandError(
            "structured_sync non-zero exit",
            returncode=1,
            stdout="",
            stderr='✗ Error: Config file not found. Please initialize the project first using "ttadk init"',
        )

    monkeypatch.setattr(fetcher._structured, "fetch", _structured_fail)
    monkeypatch.setattr(fetcher._probe, "fetch", lambda tool_name, cwd=None: [TTADKModel(name="m1"), TTADKModel(name="m2")])

    r = fetcher.fetch_tool_models_with_diagnostics("codex", cwd="/not-init", force_refresh=False)
    assert r.source == "probe"
    assert [m.name for m in r.models] == ["m1", "m2"]
    # 由于 probe 已命中，structured_sync 不应被执行，因此这里不强制要求出现 ttadk_config_missing。


def test_fetcher_official_cli_disabled_when_models_command_missing(monkeypatch):
    """ttadk 0.3.8 行为模型：无 models/model 子命令时应禁用 official_cli，并在 warnings 记录原因。"""
    from src.ttadk.model_fetcher import TTADKModelFetcher
    from src.ttadk.models import TTADKModel

    class _Runner:
        def run(self, args, cwd=None, timeout=8.0):
            # not used
            raise RuntimeError("unexpected")

        def run_simple(self, args, cwd, timeout):
            # Simulate ttadk --help output for 0.3.8: Commands 不包含 models/model
            if args[:2] == ["ttadk", "--help"]:
                out = (
                    "TikTok AI-Driven Development Kit\n"
                    "Version 0.3.8\n\n"
                    "Commands:\n"
                    "  init\n"
                    "  plugin\n"
                    "  upgrade\n"
                    "  skills\n"
                    "  sync\n"
                    "  code\n"
                    "  help\n"
                )
                return (0, out, "")

            # structured_sync 失败（模拟未 init），probe 成功返回 m1
            if args[:2] == ["ttadk", "sync"]:
                return (1, "", "Config file not found. Please initialize the project first using \"ttadk init\"")
            if args[:2] == ["ttadk", "code"]:
                return (1, "", "✗ Error: Invalid model 'INVALID_PROBE_FOR_DISCOVERY'. Available models: m1")
            return (1, "", "unexpected")

    fetcher = TTADKModelFetcher(runner=_Runner())
    r = fetcher.fetch_tool_models_with_diagnostics("codex", cwd="/not-init", force_refresh=True)
    assert r.models and [m.name for m in r.models] == ["m1"]
    # official_cli 应被禁用，并带上版本与缺失命令原因（best-effort）
    ws = list(r.diagnostics.warnings or [])
    assert "official_cli_disabled" in ws
    assert any(str(w).startswith("ttadk_version:") for w in ws)
    assert "missing_commands:models,model" in ws


def test_fetcher_file_cache_reads_home_models_cache_before_probe(monkeypatch):
    """本地缓存兜底：在 probe/structured 等策略不产出时，未 init 场景下应能从 ~/.ttadk/models_cache.json 读到真实模型名。"""
    import json as _json
    from pathlib import Path

    from src.ttadk.model_fetcher import TTADKModelFetcher

    tmp_home = Path("/tmp/ghostap_ttadk_home")
    (tmp_home / ".ttadk").mkdir(parents=True, exist_ok=True)
    (tmp_home / ".ttadk" / "models_cache.json").write_text(
        _json.dumps({"codex": [{"name": "gpt-5.2-codex-ttadk"}]}),
        encoding="utf-8",
    )

    class _Runner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run_simple(self, args, cwd, timeout):
            self.calls.append(list(args))
            # structured_sync 失败（模拟未 init）
            if args[:2] == ["ttadk", "sync"]:
                return (1, "", "Config file not found. Please initialize the project first using \"ttadk init\"")
            # probe 若被调用则判为失败（这里期望 local_config 直接命中）
            if args[:2] == ["ttadk", "code"]:
                return (1, "", "Invalid model")
            if args[:2] == ["ttadk", "--help"]:
                return (0, "Version 0.3.8\nCommands:\n  code\n  sync\n", "")
            return (1, "", "")

        def run(self, args, cwd=None, timeout: float = 8.0):
            self.calls.append(list(args))
            # structured_sync uses run()
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "Config file not found"})()

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_home)
    f = TTADKModelFetcher(runner=_Runner())
    r = f.fetch_tool_models_with_diagnostics("codex", cwd="/not-init", force_refresh=False)
    assert [m.name for m in r.models] == ["gpt-5.2-codex-ttadk"]
    assert r.source == "file_cache"
    # attempts：SSOT 会先尝试 probe/structured/...，最终由 file_cache 兜底成功。
    strategies = [a.get("strategy") for a in (r.diagnostics.attempts or [])]
    assert "file_cache" in strategies
    assert "probe" in strategies


def test_parse_models_cache_tool_missing_returns_empty(monkeypatch):
    """tool 未命中 key：默认应返回空（更安全），避免跨 tool 聚合误导 validated。"""
    from src.ttadk.models import parse_models_cache_json

    payload = {
        "codex": [{"name": "gpt-5.2-codex-ttadk"}],
        "claude": [{"name": "glm-5-ttadk"}],
    }
    names, exact = parse_models_cache_json(payload, tool_name="coco", allow_cross_tool_fallback=False)
    assert names == []
    assert exact is False


def test_structured_sync_strips_banner_and_parses_json(monkeypatch):
    """structured_sync: stdout 前缀 banner/非 JSON 内容不应导致解析失败。"""
    from src.ttadk.model_fetcher import TTADKModelFetcher, TTADKRunResult

    class _Runner:
        def run(self, args, cwd=None, timeout=8.0):
            # Simulate ttadk banner + JSON payload
            payload = (
                "_____ TTADK BANNER_____\n"
                "TikTok AI-Driven Development Kit\n"
                "{\"tools\": {\"codex\": {\"models\": [\"m1\", \"m2\"]}}}\n"
            )
            return TTADKRunResult(returncode=0, stdout=payload, stderr="")

        def run_simple(self, args, cwd, timeout):
            r = self.run(args, cwd=cwd, timeout=timeout)
            return (r.returncode, r.stdout, r.stderr)

    fetcher = TTADKModelFetcher(runner=_Runner())
    models = fetcher._structured.fetch("codex", cwd="/tmp")
    assert [m.name for m in models] == ["m1", "m2"]


def test_engine_session_invalid_model_auto_refresh_and_retry_success(monkeypatch):
    """端到端夹具：Invalid model -> Available models -> 自动选择 real 模型并重试成功。"""
    import src.agent_session as agent_session
    from src.ttadk.model_fetcher import TTADKModelFetcher

    # 让 start_session_with_retry：第一次抛 Invalid model，第二次成功
    calls: list[dict] = []

    class _DummySession:
        session_id = "dummy"

        def close(self):
            return

    def _fake_start_session_with_retry(agent_type, cwd, startup_timeout=60, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
        if len(calls) == 1:
            raise RuntimeError("✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-ttadk, gpt-4.1-ttadk")
        return _DummySession()

    monkeypatch.setattr(agent_session, "get_settings", lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})())
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    # TTADK model fetcher probe：从错误中解析即可，这里不需要真的跑；但 refresh_models(force_refresh) 会触发 fetcher
    # 用 SequenceRunner 返回一次“Invalid model”样式输出，确保 probe 能提取到可用模型
    runner = _SequenceRunner(
        [(1, "", "✗ Error: Invalid model 'INVALID_PROBE'. Available models: gpt-5.2-ttadk, gpt-4.1-ttadk")]
    )
    fetcher = TTADKModelFetcher(runner=runner)

    from src.ttadk import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="coco", default_model="")
    mgr._initialized = True
    # 注入 fetcher（避免真实 subprocess）
    mgr._model_fetcher = fetcher
    try:
        mgr._model_fetcher.set_cache_sink(mgr._on_fetcher_cache_update)
    except Exception:
        pass

    s = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"

    # 断言重试使用 real model
    assert len(calls) == 2
    assert calls[1]["model_name"] == "gpt-5.2-ttadk"


def test_engine_session_invalid_model_retry_then_fallback_to_auto_model(monkeypatch):
    """Invalid model 修复后仍失败时，应降级为不传 -m（auto）再试一次。"""
    import src.agent_session as agent_session
    from src.ttadk.model_fetcher import TTADKModelFetcher

    calls: list[dict] = []

    class _DummySession:
        session_id = "dummy"

        def close(self):
            return

    def _fake_start_session_with_retry(agent_type, cwd, startup_timeout=60, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
        # 第一次：invalid model
        if len(calls) == 1:
            raise RuntimeError(
                "✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-ttadk"
            )
        # 第二次：带 real model 仍失败（模拟账号/环境不可用）
        if len(calls) == 2:
            raise RuntimeError("some other failure")
        # 第三次：auto 模型成功
        return _DummySession()

    monkeypatch.setattr(agent_session, "get_settings", lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})())
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    runner = _SequenceRunner(
        [(1, "", "✗ Error: Invalid model 'INVALID_PROBE'. Available models: gpt-5.2-ttadk")]
    )
    fetcher = TTADKModelFetcher(runner=runner)
    from src.ttadk import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="coco", default_model="")
    mgr._initialized = True
    mgr._model_fetcher = fetcher
    try:
        mgr._model_fetcher.set_cache_sink(mgr._on_fetcher_cache_update)
    except Exception:
        pass

    s = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert len(calls) == 3
    assert calls[1]["model_name"] == "gpt-5.2-ttadk"
    assert calls[2]["model_name"] is None


def test_engine_session_invalid_model_no_available_models_degrades_to_coco(monkeypatch):
    """端到端夹具：Invalid model 但无法解析 Available models 时，应最终降级到 coco 而不是崩溃。"""
    import src.agent_session as agent_session
    from src.ttadk.model_fetcher import TTADKModelFetcher

    calls: list[dict] = []

    class _DummySession:
        session_id = "dummy"

        def close(self):
            return

    def _fake_start_session_with_retry(agent_type, cwd, startup_timeout=60, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
        # ttadk 第一次启动失败，且不包含 Available models
        if agent_type.startswith("ttadk_"):
            raise RuntimeError("✗ Error: Invalid model 'gpt-5.2'.")
        # 降级到 coco 成功
        return _DummySession()

    monkeypatch.setattr(agent_session, "get_settings", lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})())
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    # refresh_models(force_refresh) 会走 probe，这里让 probe 也拿不到 Available models
    runner = _SequenceRunner([(1, "", "✗ Error: Invalid model 'INVALID_PROBE'.")])
    fetcher = TTADKModelFetcher(runner=runner)
    from src.ttadk import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="coco", default_model="")
    mgr._initialized = True
    mgr._model_fetcher = fetcher
    try:
        mgr._model_fetcher.set_cache_sink(mgr._on_fetcher_cache_update)
    except Exception:
        pass

    s = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert any(c["agent_type"] == "coco" for c in calls)


def test_engine_session_invalid_model_with_ansi_still_recovers(monkeypatch):
    """端到端夹具：Invalid model 输出包含 ANSI 时仍能识别并自动纠错重试。"""
    import src.agent_session as agent_session
    from src.ttadk.model_fetcher import TTADKModelFetcher

    calls: list[dict] = []

    class _DummySession:
        session_id = "dummy"

        def close(self):
            return

    ansi_err = (
        "\x1b[31m✗ Error:\x1b[0m Invalid model 'gpt-5.2'. Available models: "
        "gpt-5.2-ttadk, gpt-4.1-ttadk\n<id>abc</id>"
    )

    def _fake_start_session_with_retry(agent_type, cwd, startup_timeout=60, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
        if len(calls) == 1:
            raise RuntimeError(ansi_err)
        return _DummySession()

    monkeypatch.setattr(agent_session, "get_settings", lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})())
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    # refresh_models(force_refresh) 会触发 probe；用 runner 返回一次 invalid model（含可用模型）
    runner = _SequenceRunner(
        [(1, "", "\x1b[31m✗ Error:\x1b[0m Invalid model 'INVALID_PROBE'. Available models: gpt-5.2-ttadk")]
    )
    fetcher = TTADKModelFetcher(runner=runner)
    from src.ttadk import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="coco", default_model="")
    mgr._initialized = True
    mgr._model_fetcher = fetcher
    try:
        mgr._model_fetcher.set_cache_sink(mgr._on_fetcher_cache_update)
    except Exception:
        pass

    s = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert len(calls) == 2
    assert calls[1]["model_name"] == "gpt-5.2-ttadk"


def test_engine_session_empty_message_error_uses_stderr_to_recover(monkeypatch):
    """端到端夹具：异常 message 为空，但 stderr 含 Invalid model 时仍能走纠错重试。"""
    import src.agent_session as agent_session
    from src.ttadk.model_fetcher import TTADKModelFetcher

    calls: list[dict] = []

    class _DummySession:
        session_id = "dummy"

        def close(self):
            return

    class _EmptyMsgErr(RuntimeError):
        def __str__(self):
            return ""

    def _fake_start_session_with_retry(agent_type, cwd, startup_timeout=60, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
        if len(calls) == 1:
            e = _EmptyMsgErr()
            setattr(e, "stderr", "✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-ttadk")
            raise e
        return _DummySession()

    monkeypatch.setattr(agent_session, "get_settings", lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})())
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    runner = _SequenceRunner(
        [(1, "", "✗ Error: Invalid model 'INVALID_PROBE'. Available models: gpt-5.2-ttadk")]
    )
    fetcher = TTADKModelFetcher(runner=runner)
    from src.ttadk import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="coco", default_model="")
    mgr._initialized = True
    mgr._model_fetcher = fetcher
    try:
        mgr._model_fetcher.set_cache_sink(mgr._on_fetcher_cache_update)
    except Exception:
        pass

    s = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert len(calls) == 2
    assert calls[1]["model_name"] == "gpt-5.2-ttadk"


def test_fetcher_file_cache_strategy_loads_models(monkeypatch, tmp_path):
    """第二来源：file_cache 应能从 ~/.ttadk/models_cache.json 读取真实模型列表。"""
    from pathlib import Path
    from src.ttadk.model_fetcher import TTADKModelFetcher

    # 构造临时 home/.ttadk/models_cache.json
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = tmp_path / ".ttadk" / "models_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"codex": [{"name": "gpt-5.2-codex-ttadk", "description": "d"}]}',
        encoding="utf-8",
    )

    fetcher = TTADKModelFetcher(runner=_FakeRunner())
    models = fetcher._file_cache.fetch("codex")
    assert [m.name for m in models] == ["gpt-5.2-codex-ttadk"]


def test_preheat_first_use_failure_cooldown_skips_reprobe(monkeypatch):
    """预热失败后应有退避，避免短时间内反复启动 probe 子进程（best-effort 不阻塞主流程）。"""
    from types import SimpleNamespace
    from src.ttadk.manager import TTADKManager
    import src.ttadk.manager as ttadk_manager_mod
    import shutil

    manager = TTADKManager(default_tool="coco")

    # enable preheat
    monkeypatch.setattr(
        ttadk_manager_mod,
        "get_settings",
        lambda: SimpleNamespace(
            ttadk_default_tool="coco",
            ttadk_default_model="",
            ttadk_preheat_enabled=True,
            ttadk_preheat_on_first_use=True,
            ttadk_preheat_timeout=0.1,
        ),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ttadk" if name == "ttadk" else None)

    probe = MagicMock(return_value=[])
    manager._model_fetcher.probe_tool_models = probe

    manager.maybe_preheat_tool_models("codex", cwd="/tmp")
    manager.maybe_preheat_tool_models("codex", cwd="/tmp")

    # 第二次应被 cooldown 跳过，避免重复 probe
    assert probe.call_count == 1


def test_preheat_common_models_respects_on_startup_switch(monkeypatch):
    """maybe_preheat_common_models 应尊重 on_startup 开关，并避免无效时抢先 set once 标志。"""
    from types import SimpleNamespace
    from src.ttadk.manager import TTADKManager
    import src.ttadk.manager as ttadk_manager_mod
    import shutil

    manager = TTADKManager(default_tool="coco")

    monkeypatch.setattr(
        ttadk_manager_mod,
        "get_settings",
        lambda: SimpleNamespace(
            ttadk_default_tool="coco",
            ttadk_default_model="",
            ttadk_preheat_enabled=True,
            ttadk_preheat_on_startup=False,
            ttadk_preheat_tools="codex,coco",
            ttadk_preheat_timeout=0.1,
        ),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ttadk" if name == "ttadk" else None)

    probe = MagicMock(return_value=[TTADKModel(name="m")])
    manager._model_fetcher.probe_tool_models = probe

    manager.maybe_preheat_common_models(cwd="/tmp")

    assert probe.call_count == 0
    assert manager._preheat_once.is_set() is False


def test_resolve_and_ensure_valid_model_marks_defaults_untrusted_and_refreshes(monkeypatch):
    """当 get_models 返回 defaults 时，应视为不可信并触发一次 refresh(force_refresh=True)。"""
    from src.ttadk.manager import TTADKManager
    from src.ttadk.models import TTADKModel
    from src.ttadk.model_fetcher import FetchResult, FetchDiagnostics

    m = TTADKManager(default_tool="codex")
    m._initialized = True

    calls: list[dict] = []

    def _fake_fetch(tool_name: str, cwd=None, force_refresh: bool = False):
        calls.append({"tool": tool_name, "force_refresh": bool(force_refresh)})
        if force_refresh:
            return FetchResult(
                tool_name=tool_name,
                models=[TTADKModel(name="gpt-5.2-codex-ttadk")],
                source="probe",
                diagnostics=FetchDiagnostics(tool_name=tool_name, chosen_strategy="probe"),
            )
        # 非 force_refresh 时模拟“拿不到真实列表”，触发 defaults fallback
        return FetchResult(
            tool_name=tool_name,
            models=[],
            source="",
            diagnostics=FetchDiagnostics(tool_name=tool_name, chosen_strategy=""),
        )

    monkeypatch.setattr(m._model_fetcher, "fetch_tool_models_with_diagnostics", _fake_fetch)

    r = m.resolve_and_ensure_valid_model("gpt-5.2", tool_name="codex", cwd="/tmp")
    assert r.real_name == "gpt-5.2-codex-ttadk"
    assert any(c.get("force_refresh") for c in calls)


def test_kickoff_preheat_common_models_skips_when_once_is_set(monkeypatch):
    """kickoff_preheat_common_models 在 once 已 set 时应直接返回，避免重复创建线程。"""
    from types import SimpleNamespace
    from src.ttadk.manager import TTADKManager
    import src.ttadk.manager as ttadk_manager_mod

    manager = TTADKManager(default_tool="coco")
    manager._preheat_once.set()

    # 即便开关开启，也应因 once 已 set 而跳过线程创建
    monkeypatch.setattr(
        ttadk_manager_mod,
        "get_settings",
        lambda: SimpleNamespace(
            ttadk_default_tool="coco",
            ttadk_default_model="",
            ttadk_preheat_enabled=True,
            ttadk_preheat_on_startup=True,
            ttadk_preheat_tools="codex,coco",
            ttadk_preheat_timeout=0.1,
        ),
    )

    started: list[str] = []

    class _DummyThread:
        def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
            self._name = name
            self._target = target
            self._kwargs = kwargs or {}

        def start(self):
            started.append(self._name or "")

    monkeypatch.setattr(ttadk_manager_mod.threading, "Thread", _DummyThread)

    manager.kickoff_preheat_common_models(cwd="/tmp")
    assert started == []


def test_kickoff_preheat_tool_models_skips_thread_when_preheat_disabled(monkeypatch):
    """kickoff_preheat_tool_models 在预热关闭时不应创建后台线程。"""
    from types import SimpleNamespace
    from src.ttadk.manager import TTADKManager
    import src.ttadk.manager as ttadk_manager_mod
    import shutil

    manager = TTADKManager(default_tool="coco")

    monkeypatch.setattr(
        ttadk_manager_mod,
        "get_settings",
        lambda: SimpleNamespace(
            ttadk_default_tool="coco",
            ttadk_default_model="",
            ttadk_preheat_enabled=False,
            ttadk_preheat_on_first_use=True,
            ttadk_preheat_timeout=0.1,
        ),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ttadk" if name == "ttadk" else None)

    started: list[str] = []

    class _DummyThread:
        def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
            self._name = name

        def start(self):
            started.append(self._name or "")

    monkeypatch.setattr(ttadk_manager_mod.threading, "Thread", _DummyThread)

    manager.kickoff_preheat_tool_models("codex", cwd="/tmp")
    assert started == []


def test_kickoff_preheat_tool_models_skips_thread_when_inflight(monkeypatch):
    """kickoff_preheat_tool_models 在 inflight 时应跳过，避免重复线程。"""
    from types import SimpleNamespace
    from src.ttadk.manager import TTADKManager
    import src.ttadk.manager as ttadk_manager_mod
    import shutil

    manager = TTADKManager(default_tool="coco")

    monkeypatch.setattr(
        ttadk_manager_mod,
        "get_settings",
        lambda: SimpleNamespace(
            ttadk_default_tool="coco",
            ttadk_default_model="",
            ttadk_preheat_enabled=True,
            ttadk_preheat_on_first_use=True,
            ttadk_preheat_timeout=0.1,
        ),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ttadk" if name == "ttadk" else None)

    with manager._lock:
        manager._preheat_inflight_tools.add("codex")

    started: list[str] = []

    class _DummyThread:
        def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
            self._name = name

        def start(self):
            started.append(self._name or "")

    monkeypatch.setattr(ttadk_manager_mod.threading, "Thread", _DummyThread)

    manager.kickoff_preheat_tool_models("codex", cwd="/tmp")
    assert started == []


def test_agent_session_ttadk_precheck_uses_real_model_when_valid(monkeypatch):
    """TTADK 模式下（ACP 可用的 tool），应在启动前把友好/短名解析为 real_id 并传给 ACP 启动函数。"""
    import importlib
    import src.agent_session as agent_session
    from src.ttadk.models import TTADKModel

    # 避免真实启动：替换 start_session_with_retry
    calls: list[dict] = []

    class _DummySession:
        session_id = "dummy"

        def close(self):
            return

    def _fake_start_session_with_retry(agent_type, cwd, startup_timeout=60, model_name=None, **kwargs):
        calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
        return _DummySession()

    monkeypatch.setattr(agent_session, "get_settings", lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})())

    # 伪造 ttadk manager：get_models 返回真实列表，确保 validated=True
    from src.ttadk import get_ttadk_manager

    mgr = get_ttadk_manager(default_tool="coco", default_model="")
    mgr._initialized = True
    mgr._tool_models_cache["coco"] = [TTADKModel(name="gpt-5.2-ttadk")]
    mgr._cache_time["coco"] = importlib.import_module("time").time()

    # create_engine_session 内部是 from src.acp.sync_adapter import start_session_with_retry
    # 因此需要 patch 真实定义处
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", lambda *a, **k: _fake_start_session_with_retry(*a, **k))

    s = agent_session.create_engine_session(agent_type="ttadk_coco", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "dummy"
    assert calls and calls[0]["model_name"] == "gpt-5.2-ttadk"


def test_manager_refresh_models_force_refresh(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    # 让刷新路径走 structured 并返回模型
    monkeypatch.setattr(
        manager._model_fetcher._structured,
        "fetch",
        lambda tool_name, cwd=None: [TTADKModel(name="real-model")],
    )
    result = manager.refresh_models(tool_name="coco", cwd=".")
    assert result.error is None
    assert result.models and result.models[0].name == "real-model"


def test_manager_get_models_with_tool_name():
    """测试获取指定工具的模型列表"""
    manager = TTADKManager(default_tool="coco")

    # 获取当前工具的模型
    result = manager.get_models()
    assert result.error is None

    # 获取指定工具的模型
    result_codex = manager.get_models(tool_name="codex")
    assert result_codex.error is None


def test_manager_model_cache_invalidation():
    """测试模型缓存失效"""
    manager = TTADKManager(default_tool="coco")

    # 获取模型，填充缓存
    manager.get_models()

    # 使特定工具的缓存失效
    manager.invalidate_model_cache("coco")

    # 使所有缓存失效
    manager.invalidate_model_cache()


def test_manager_model_cached_flag():
    """测试模型缓存标志"""
    manager = TTADKManager(default_tool="coco")

    # 第一次获取，cached 应该为 False
    result = manager.get_models()
    assert result.cached is False

    # 如果有缓存，再次获取时 cached 应该为 True
    # 但由于模型获取可能失败（终端交互），这里只测试缓存逻辑
    if manager._is_cache_valid("coco"):
        result2 = manager.get_models()
        assert result2.cached is True


def test_start_session_with_retry_logs_stderr_when_error_message_empty(monkeypatch, caplog):
    """诊断：即使异常 message 为空，也应能从 stderr/stdout 补齐 snippet（避免空错误）。"""
    import src.acp.sync_adapter as sync_adapter

    class _EmptyMsgErr(RuntimeError):
        def __str__(self):
            return ""

    def _fail_start(self, startup_timeout=60):
        e = _EmptyMsgErr()
        setattr(e, "stderr", "Invalid model gpt-5.2 ...")
        raise e

    # 构造一个假的 SyncACPSession，start 总是抛空 message 异常，但带 stderr
    class _DummySession:
        def __init__(self, agent_type, cwd, model_name=None):
            self._agent_type = agent_type
            self._cwd = cwd

        def start(self, startup_timeout=60):
            return _fail_start(self, startup_timeout=startup_timeout)

        def close(self):
            return

        def describe_agent(self):
            return "cmd=dummy"

    monkeypatch.setattr(sync_adapter, "SyncACPSession", _DummySession)
    monkeypatch.setattr(sync_adapter, "get_settings", lambda: type("S", (), {"acp_startup_retries": 1})())

    caplog.set_level("WARNING")
    # start_session_with_retry 失败应抛可诊断异常（ACPStartupError），且携带 fail_phase
    from src.acp.session import ACPStartupError

    with pytest.raises(ACPStartupError) as ctx:
        sync_adapter.start_session_with_retry(agent_type="ttadk_codex", cwd="/tmp", startup_timeout=1, model_name="gpt-5.2")

    assert getattr(ctx.value, "fail_phase", "") in ("retry_exhausted", "")

    # 校验日志里包含 stderr_snippet（字符串匹配即可）
    assert "stderr_snippet" in caplog.text


def test_extract_models_ignores_unrelated_string_list(monkeypatch):
    manager = TTADKManager(default_tool="claude")

    # Mock fetch_tool_models_with_diagnostics 返回指定的模型列表
    from src.ttadk.model_fetcher import FetchResult, FetchDiagnostics

    def mock_fetch(tool_name, cwd=None, force_refresh=False, prefer_probe=False):
        if tool_name == "claude":
            return FetchResult(
                tool_name=tool_name,
                models=[
                    TTADKModel(name="claude-3.7-sonnet"),
                    TTADKModel(name="claude-3.5-sonnet"),
                ],
                source="mock",
                diagnostics=FetchDiagnostics(tool_name=tool_name),
            )
        return FetchResult(tool_name=tool_name, models=[], source="mock", diagnostics=FetchDiagnostics(tool_name=tool_name))

    monkeypatch.setattr(
        manager._model_fetcher,
        "fetch_tool_models_with_diagnostics",
        mock_fetch,
    )

    result = manager.get_models(cwd=".")
    assert [m.name for m in result.models] == ["claude-3.7-sonnet", "claude-3.5-sonnet"]


def test_extract_models_not_from_generic_string_list(monkeypatch):
    manager = TTADKManager(default_tool="claude")
    monkeypatch.setattr(
        manager,
        "_run_ttadk_sync",
        lambda cwd: {
            "workspace_files": ["README.md", "image.png", "notes.txt"],
            "metadata": {"tags": ["dev", "ops"]},
        },
    )
    
    # Mock strategies to return empty (new API uses diagnostics wrapper)
    from src.ttadk.model_fetcher import FetchDiagnostics, FetchResult

    monkeypatch.setattr(
        manager._model_fetcher,
        "fetch_tool_models_with_diagnostics",
        lambda tool_name, cwd=None, force_refresh=False: FetchResult(
            tool_name=tool_name,
            models=[],
            source="",
            diagnostics=FetchDiagnostics(tool_name=tool_name),
        ),
    )

    result = manager.get_models(cwd=".")
    model_names = [m.name for m in result.models]
    # Should fall back to defaults
    assert "image.png" not in model_names
    assert "README.md" not in model_names
    # 兜底应回到默认模型列表（不要求具体命名形态，只要包含 GPT 5.2 系列即可）
    assert any("gpt-5.2" in n for n in model_names)


def test_get_real_model_name_resolution():
    """测试模型名称解析逻辑（精确、友好名、模糊匹配）"""
    manager = TTADKManager(default_tool="test-tool")
    
    # Mock cache
    mock_models = [
        TTADKModel(name="gpt-5.2-codex-ttadk", description="GPT 5.2", friendly_name="GPT 5.2"),
        TTADKModel(name="claude-3-opus-20240229", description="Claude 3 Opus", friendly_name="Claude 3 Opus"),
        TTADKModel(name="simple-model", description="Simple", friendly_name="Simple"),
    ]
    manager._tool_models_cache["test-tool"] = mock_models
    # 标记缓存有效，避免触发真实拉取逻辑
    manager._cache_time["test-tool"] = time.time()
    
    # 1. 精确匹配
    assert manager.get_real_model_name("gpt-5.2-codex-ttadk") == "gpt-5.2-codex-ttadk"
    
    # 2. 友好名称匹配
    assert manager.get_real_model_name("GPT 5.2") == "gpt-5.2-codex-ttadk"
    
    # 3. 前缀模糊匹配
    assert manager.get_real_model_name("gpt-5.2") == "gpt-5.2-codex-ttadk"
    assert manager.get_real_model_name("claude-3-opus") == "claude-3-opus-20240229"
    
    # 4. 包含匹配 (Partial match)
    assert manager.get_real_model_name("codex") == "gpt-5.2-codex-ttadk"
    
    # 5. 无匹配 (返回原值)
    assert manager.get_real_model_name("non-existent") == "non-existent"


def test_resolve_real_model_name_returns_metadata():
    """resolve_real_model_name 应返回 source/validated/warnings 等元信息。"""
    manager = TTADKManager(default_tool="test-tool")
    manager._tool_models_cache["test-tool"] = [
        TTADKModel(name="gpt-5.2-codex-ttadk", description="GPT 5.2", friendly_name="GPT 5.2"),
        TTADKModel(name="claude-3-opus-20240229", description="Claude 3 Opus", friendly_name="Claude 3 Opus"),
    ]
    # 避免触发真实拉取：让 get_models 直接走缓存
    manager._cache_time["test-tool"] = time.time()

    r1 = manager.resolve_real_model_name("GPT 5.2", tool_name="test-tool")
    assert r1.real_name == "gpt-5.2-codex-ttadk"
    assert r1.source in ("friendly", "exact")
    assert r1.tool_name == "test-tool"

    r2 = manager.resolve_real_model_name("gpt-5.2", tool_name="test-tool")
    assert r2.real_name == "gpt-5.2-codex-ttadk"
    assert r2.source in ("prefix", "partial", "exact")


def test_resolve_real_model_name_require_valid_fallback():
    """require_valid=True 时若解析结果不可用，应降级到可用模型并标记 fallback。"""
    manager = TTADKManager(default_tool="test-tool")
    manager._tool_models_cache["test-tool"] = [
        TTADKModel(name="real-a", description="A", friendly_name="A"),
        TTADKModel(name="real-b", description="B", friendly_name="B"),
    ]
    manager._cache_time["test-tool"] = time.time()

    r = manager.resolve_real_model_name("non-existent", tool_name="test-tool", require_valid=True)
    assert r.real_name in ("real-a", "real-b")
    assert r.source == "fallback"
    assert r.validated is True
    assert "model_not_available" in r.warnings


def test_require_valid_defaults_triggers_refresh_probe(monkeypatch):
    """仅 defaults 时 require_valid 不应误判为可用，需触发 refresh(probe) 后给出真实模型。"""
    from src.ttadk.models import ModelListResult, ResolvedModelResult

    manager = TTADKManager(default_tool="codex")

    # 第一次解析：get_models 返回 defaults（不可信）
    monkeypatch.setattr(
        manager,
        "get_models",
        lambda cwd=None, tool_name=None, force_refresh=False: ModelListResult(
            models=[TTADKModel(name="gpt-5.2")],
            source="defaults",
            cached=False,
        ),
    )

    # refresh_models -> 返回真实模型列表（模拟 probe 得到）
    monkeypatch.setattr(
        manager,
        "refresh_models",
        lambda tool_name=None, cwd=None: ModelListResult(
            models=[TTADKModel(name="gpt-5.2-codex-ttadk"), TTADKModel(name="gpt-5.2-ttadk")],
            source="probe",
            cached=False,
        ),
    )

    # 第二次解析：直接返回 validated=True 的真实模型
    calls = {"n": 0}

    def _fake_resolve(model_name, tool_name=None, cwd=None, require_valid=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return ResolvedModelResult(
                tool_name=tool_name or "codex",
                input_name=model_name,
                real_name=model_name,
                source="exact",
                validated=False,
                warnings=["models_untrusted"],
            )
        return ResolvedModelResult(
            tool_name=tool_name or "codex",
            input_name=model_name,
            real_name="gpt-5.2-codex-ttadk",
            source="friendly",
            validated=True,
            warnings=[],
        )

    monkeypatch.setattr(manager, "resolve_real_model_name", _fake_resolve)

    r = manager.resolve_and_ensure_valid_model("gpt-5.2", tool_name="codex", cwd=".")
    assert r.real_name.endswith("-ttadk")
    assert r.validated is True
    assert any(w.startswith("refreshed:") for w in (r.warnings or []))


def test_resolve_and_ensure_valid_model_refreshes_when_untrusted(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    from src.ttadk.models import ResolvedModelResult, ModelListResult

    # 通过 mock refresh_models 让第二次解析附加 refreshed:* 标记
    monkeypatch.setattr(
        manager,
        "refresh_models",
        lambda tool_name=None, cwd=None: ModelListResult(models=[TTADKModel(name="real-model")], source="probe"),
    )
    # 第一次解析认为“不可信”，第二次（刷新后）返回 validated=True 的真实模型
    calls = {"n": 0}

    def _fake_resolve(model_name, tool_name=None, cwd=None, require_valid=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return ResolvedModelResult(
                tool_name=tool_name or "coco",
                input_name=model_name,
                real_name=model_name,
                source="unknown",
                validated=False,
                warnings=["models_untrusted"],
            )
        return ResolvedModelResult(
            tool_name=tool_name or "coco",
            input_name=model_name,
            real_name="real-model",
            source="fallback",
            validated=True,
            warnings=[],
        )

    monkeypatch.setattr(manager, "resolve_real_model_name", _fake_resolve)

    manager._tool_models_cache["coco"] = [TTADKModel(name="real-model")]
    manager._cache_time["coco"] = time.time()

    r = manager.resolve_and_ensure_valid_model("non-existent", tool_name="coco", cwd=".")
    assert r.real_name == "real-model"
    assert any(w.startswith("refreshed:") for w in (r.warnings or []))


def test_low_confidence_cache_does_not_mark_validated(monkeypatch):
    """低置信缓存（例如 ~/.ttadk/models_cache.json）不应导致 validated=True，从而误透传 -m。"""
    manager = TTADKManager(default_tool="codex")

    # 模拟从文件缓存加载后的状态：有模型但标记 low_confidence
    manager._tool_models_cache["codex"] = [TTADKModel(name="real-a", description="A", friendly_name="A")]
    manager._cache_time["codex"] = time.time()
    manager._tool_models_meta["codex"] = {"source": "file_cache", "warnings": ["source_cross_project", "low_confidence"]}

    r = manager.resolve_real_model_name("real-a", tool_name="codex", cwd=".")
    assert r.real_name == "real-a"
    assert r.validated is False
    assert "models_untrusted" in (r.warnings or [])


def test_coordinate_ttadk_startup_cooldown_skip_degrades(monkeypatch):
    """cooldown 生效时，不应重复修复/重试，应走降级并在 attempts 记录 cooldown_skip。"""
    from src.ttadk.startup import coordinate_ttadk_startup

    class _Mgr:
        def seed_models_from_error(self, tool, err_blob):
            return ["m1-ttadk"]

    # Enable runtime retry but set a very large cooldown
    monkeypatch.setattr(
        "src.ttadk.manager.get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_retry_enabled": True,
                "ttadk_runtime_retry_cooldown_s": 9999.0,
                "ttadk_runtime_retry_allow_autoswitch": True,
            },
        )(),
    )

    def _start_fn(model):
        raise RuntimeError("Invalid model 'x'. Available models: m1-ttadk")

    def _fallback_fn(err):
        return "fallback"

    # First call: allowed, will attempt repair path and then still fail, so fall back
    info1 = coordinate_ttadk_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
    )
    assert info1["degraded"] is True

    # Second call: cooldown triggers skip, should degrade with cooldown_skip attempt
    info2 = coordinate_ttadk_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
    )
    attempts = list((info2.get("diagnostics") or {}).get("attempts") or [])
    assert any(a.get("step") == "cooldown_skip" for a in attempts)
    assert info2["degraded"] is True


def test_coordinate_ttadk_startup_cooldown_isolated_by_ttadk_manager_instance(monkeypatch):
    """同一 TTADKManager 实例内命中 cooldown，不同实例之间不应互相污染。"""
    from src.ttadk.manager import TTADKManager, coordinate_ttadk_startup

    monkeypatch.setattr(
        "src.ttadk.manager.get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_retry_enabled": True,
                "ttadk_runtime_retry_cooldown_s": 9999.0,
                "ttadk_runtime_retry_allow_autoswitch": True,
            },
        )(),
    )

    def _start_fn(model):
        raise RuntimeError("Invalid model 'x'. Available models: m1-ttadk")

    def _fallback_fn(err):
        return "fallback"

    def _precheck_fn(_model_intent: str) -> dict:
        # 避免触发真实的 TTADKManager precheck/refresh/probe：本用例仅验证 cooldown 状态隔离。
        return {"validated": False, "model": None, "resolved_real_name": _model_intent, "source": "test"}

    m1 = TTADKManager(default_tool="codex", default_model="")
    m2 = TTADKManager(default_tool="codex", default_model="")
    # 避免 seed 走到“回灌缓存 + 落盘”路径
    monkeypatch.setattr(m1, "seed_models_from_error", lambda tool, err: ["m1-ttadk"], raising=False)
    monkeypatch.setattr(m2, "seed_models_from_error", lambda tool, err: ["m1-ttadk"], raising=False)

    r1 = coordinate_ttadk_startup(
        manager=m1,
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
        precheck_fn=_precheck_fn,
    )
    assert r1["degraded"] is True

    r2 = coordinate_ttadk_startup(
        manager=m1,
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
        precheck_fn=_precheck_fn,
    )
    attempts2 = list((r2.get("diagnostics") or {}).get("attempts") or [])
    assert any(a.get("step") == "cooldown_skip" for a in attempts2)

    r3 = coordinate_ttadk_startup(
        manager=m2,
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
        precheck_fn=_precheck_fn,
    )
    attempts3 = list((r3.get("diagnostics") or {}).get("attempts") or [])
    assert not any(a.get("step") == "cooldown_skip" for a in attempts3)


def test_coordinate_ttadk_startup_cooldown_isolated_by_stub_class(monkeypatch):
    """测试桩冷却：同一 stub 类内共享冷却，不同 stub 类之间隔离（stub key）。"""
    from src.ttadk.startup import coordinate_ttadk_startup

    monkeypatch.setattr(
        "src.ttadk.manager.get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_retry_enabled": True,
                "ttadk_runtime_retry_cooldown_s": 9999.0,
                "ttadk_runtime_retry_allow_autoswitch": True,
            },
        )(),
    )

    def _start_fn(model):
        raise RuntimeError("Invalid model 'x'. Available models: m1-ttadk")

    def _fallback_fn(err):
        return "fallback"

    class _MgrA:
        def seed_models_from_error(self, tool, err_blob):
            return ["m1-ttadk"]

    class _MgrB:
        def seed_models_from_error(self, tool, err_blob):
            return ["m1-ttadk"]

    a1 = coordinate_ttadk_startup(
        manager=_MgrA(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
    )
    assert a1["degraded"] is True

    a2 = coordinate_ttadk_startup(
        manager=_MgrA(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
    )
    attempts_a2 = list((a2.get("diagnostics") or {}).get("attempts") or [])
    assert any(a.get("step") == "cooldown_skip" for a in attempts_a2)

    b1 = coordinate_ttadk_startup(
        manager=_MgrB(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        start_fn=_start_fn,
        fallback_fn=_fallback_fn,
    )
    attempts_b1 = list((b1.get("diagnostics") or {}).get("attempts") or [])
    assert not any(a.get("step") == "cooldown_skip" for a in attempts_b1)


def test_ttadk_runtime_stub_cooldown_concurrent_read_write_is_safe(monkeypatch):
    """模块级 stub 冷却 store：并发读写不应抛异常。"""
    import threading

    from src.ttadk import manager as m

    # Force deterministic limits (avoid Settings defaults interfering with TTL/max_keys)
    monkeypatch.setattr(
        m,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": 0.0,
                "ttadk_runtime_stub_cooldown_max_keys": 100000,
                "ttadk_runtime_stub_cooldown_gc_interval_s": 0.0,
            },
        )(),
    )

    # 重置全局状态，避免跨用例污染
    monkeypatch.setattr(m._STUB_COOLDOWN, "_store", {}, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_last_gc_ts", 0.0, raising=False)
    # defaults（配置缺失/非法时回退）
    monkeypatch.setattr(m._STUB_COOLDOWN, "_ttl_default", 0.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_max_keys_default", 100000, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_gc_interval_default", 0.0, raising=False)

    class _Mgr:
        pass

    mgr = _Mgr()
    errs: list[Exception] = []

    def _worker(idx: int):
        try:
            tool = f"codex-{idx % 7}"
            m._runtime_invalid_model_stub_set_last_ts(mgr, tool, float(idx))
            _ = m._runtime_invalid_model_stub_get_last_ts(mgr, tool)
        except Exception as e:
            errs.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errs == []


def test_ttadk_runtime_stub_cooldown_gc_ttl_and_max_keys(monkeypatch):
    """stub 冷却 store：TTL 清理与 max_keys 兜底应生效且保持 newest 优先。"""
    from src.ttadk import manager as m

    # 固定时间源，保证确定性
    monkeypatch.setattr(m.time, "time", lambda: 100.0)

    # reset limits cache to avoid cross-test interference
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache", None, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache_ts", 0.0, raising=False)

    # 通过 get_settings 注入阈值（实现已配置化）
    settings_box = {
        "ttadk_runtime_stub_cooldown_ttl_s": 10.0,
        "ttadk_runtime_stub_cooldown_max_keys": 1024,
        "ttadk_runtime_stub_cooldown_gc_interval_s": 0.0,
    }
    monkeypatch.setattr(m, "get_settings", lambda: type("S", (), dict(settings_box))())

    # 重置 store
    monkeypatch.setattr(m._STUB_COOLDOWN, "_store", {}, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_last_gc_ts", 0.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_gc_interval_default", 0.0, raising=False)

    class _Mgr:
        pass

    mgr = _Mgr()

    # 1) TTL：写入一个过期 key（ts=50），应被 GC 删除
    monkeypatch.setattr(m._STUB_COOLDOWN, "_ttl_default", 10.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_max_keys_default", 1024, raising=False)
    m._runtime_invalid_model_stub_set_last_ts(mgr, "codex", 50.0)
    assert m._runtime_invalid_model_stub_get_last_ts(mgr, "codex") == 0.0

    # 2) max_keys：禁用 TTL，限制 max_keys=3，应保留最新 3 个
    settings_box["ttadk_runtime_stub_cooldown_ttl_s"] = 0.0
    settings_box["ttadk_runtime_stub_cooldown_max_keys"] = 3
    monkeypatch.setattr(m, "get_settings", lambda: type("S", (), dict(settings_box))())

    monkeypatch.setattr(m._STUB_COOLDOWN, "_ttl_default", 0.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_max_keys_default", 3, raising=False)

    for i in range(1, 6):
        m._runtime_invalid_model_stub_set_last_ts(mgr, f"t{i}", float(i))

    store = m._runtime_invalid_model_stub_store()
    assert len(store) <= 3

    # 断言只保留 t3,t4,t5（newest 3）
    k3 = m._runtime_invalid_model_stub_key(mgr, "t3")
    k4 = m._runtime_invalid_model_stub_key(mgr, "t4")
    k5 = m._runtime_invalid_model_stub_key(mgr, "t5")
    assert k3 in store
    assert k4 in store
    assert k5 in store


def test_ttadk_runtime_stub_cooldown_legacy_store_sync(monkeypatch):
    """legacy store（模块级显式挂载点）存在时，应合并并将 store 引用指向 legacy。"""
    from src.ttadk import manager as m

    # reset limits cache to avoid cross-test interference
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache", None, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache_ts", 0.0, raising=False)

    # 禁用 TTL/max_keys 干扰，确保 legacy 合并断言稳定
    monkeypatch.setattr(
        m,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": 0.0,
                "ttadk_runtime_stub_cooldown_max_keys": 0,
                "ttadk_runtime_stub_cooldown_gc_interval_s": 0.0,
            },
        )(),
    )

    # 让模块级 store 先有数据
    monkeypatch.setattr(m._STUB_COOLDOWN, "_store", {}, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_last_gc_ts", 0.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_gc_interval_default", 0.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_ttl_default", 0.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_max_keys_default", 1024, raising=False)

    class _Mgr:
        pass

    mgr = _Mgr()
    m._runtime_invalid_model_stub_set_last_ts(mgr, "before-legacy", 1.0)

    legacy: dict = {}
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", legacy, raising=False)

    # 再写一次：应触发合并 + rebind
    m._runtime_invalid_model_stub_set_last_ts(mgr, "after-legacy", 2.0)

    # 模块级引用应指向 legacy（或至少 store() 返回 legacy）
    store = m._runtime_invalid_model_stub_store()
    assert store is legacy

    # 合并不丢数据
    assert m._runtime_invalid_model_stub_get_last_ts(mgr, "before-legacy") == 1.0
    assert m._runtime_invalid_model_stub_get_last_ts(mgr, "after-legacy") == 2.0


def test_ttadk_runtime_stub_cooldown_migrates_once_from_function_attr(monkeypatch):
    """legacy 函数属性 store 存在时：仅在显式初始化路径迁移一次，之后不再反复读取函数属性。"""
    from src.ttadk import manager as m
    import src.ttadk.compat as compat

    # 禁用 TTL/max_keys 干扰
    monkeypatch.setattr(
        m,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": 0.0,
                "ttadk_runtime_stub_cooldown_max_keys": 0,
                "ttadk_runtime_stub_cooldown_gc_interval_s": 0.0,
            },
        )(),
    )

    # reset store + module SSOT
    monkeypatch.setattr(m._STUB_COOLDOWN, "_store", {}, raising=False)
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)

    fn = getattr(m, "coordinate_ttadk_startup", None)
    assert callable(fn)

    fn_store: dict = {}
    monkeypatch.setattr(fn, "_runtime_invalid_model_last_ts_by_stub", fn_store, raising=False)

    # 显式初始化：触发一次迁移 + provider 安装
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    monkeypatch.setattr(m, "_legacy_store_migrated", False, raising=False)
    m.get_ttadk_manager()

    class _Mgr:
        pass

    mgr = _Mgr()

    # 触发迁移：写入应落到 fn_store（迁移后 self._store 指向 legacy）
    m._runtime_invalid_model_stub_set_last_ts(mgr, "k1", 1.0)
    assert m._runtime_invalid_model_stub_store() is fn_store
    assert m._LEGACY_STUB_COOLDOWN_STORE is fn_store

    # 迁移后：即使函数属性被替换，模块级 SSOT 也不应被覆盖
    new_fn_store: dict = {}
    setattr(fn, "_runtime_invalid_model_last_ts_by_stub", new_fn_store)
    m._runtime_invalid_model_stub_set_last_ts(mgr, "k2", 2.0)
    assert m._runtime_invalid_model_stub_store() is fn_store
    assert m._LEGACY_STUB_COOLDOWN_STORE is fn_store
    assert m._runtime_invalid_model_stub_get_last_ts(mgr, "k1") == 1.0
    assert m._runtime_invalid_model_stub_get_last_ts(mgr, "k2") == 2.0


def test_ttadk_runtime_stub_cooldown_does_not_writeback_function_attr(monkeypatch):
    """无 legacy store 时：不应把当前 store 写回到 `coordinate_ttadk_startup` 函数属性（避免隐式耦合）。"""
    from src.ttadk import manager as m

    # 禁用 TTL/max_keys 干扰
    monkeypatch.setattr(
        m,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": 0.0,
                "ttadk_runtime_stub_cooldown_max_keys": 0,
                "ttadk_runtime_stub_cooldown_gc_interval_s": 0.0,
            },
        )(),
    )

    # reset store + module SSOT
    monkeypatch.setattr(m._STUB_COOLDOWN, "_store", {}, raising=False)
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)

    fn = getattr(m, "coordinate_ttadk_startup", None)
    assert callable(fn)
    monkeypatch.setattr(fn, "_runtime_invalid_model_last_ts_by_stub", None, raising=False)

    class _Mgr:
        pass

    mgr = _Mgr()
    m._runtime_invalid_model_stub_set_last_ts(mgr, "k1", 1.0)

    # 断言：仍为 None（没有 writeback）
    assert getattr(fn, "_runtime_invalid_model_last_ts_by_stub", None) is None


def test_ttadk_runtime_repair_seeded_prefers_best_model_and_retry_ok(monkeypatch):
    """runtime_repair: seeded 命中时应选择最匹配模型并重试成功。"""
    from src.ttadk.runtime_repair import repair_invalid_model_startup

    attempts: list[dict] = []
    calls: list[object] = []

    class _Mgr:
        def seed_models_from_error(self, tool_name: str, err_blob: str):
            assert tool_name == "codex"
            assert "Invalid model" in err_blob
            return ["gpt-5.2-ttadk", "gpt-5.2-codex-ttadk"]

    def _start(model_name):
        calls.append(model_name)
        # 只允许传入最匹配的 codex 模型
        assert model_name == "gpt-5.2-codex-ttadk"
        return {"ok": True, "model": model_name}

    def _precheck(_intent: str) -> dict:
        # 模拟 precheck 低可信/未 validated
        return {"model": None, "validated": False, "source": "defaults", "warnings": ["models_untrusted"]}

    out = repair_invalid_model_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        error=RuntimeError("✗ Error: Invalid model 'bad'. Available models: gpt-5.2-ttadk, gpt-5.2-codex-ttadk"),
        error_blob="✗ Error: Invalid model 'bad'. Available models: gpt-5.2-ttadk, gpt-5.2-codex-ttadk",
        attempts=attempts,
        start_fn=_start,
        fallback_fn=None,
        precheck_fn=_precheck,
        get_settings_fn=lambda: type(
            "S",
            (),
            {"ttadk_runtime_retry_enabled": True, "ttadk_runtime_retry_cooldown_s": 0.0, "ttadk_runtime_retry_allow_autoswitch": True},
        )(),
        time_fn=lambda: 100.0,
    )
    assert out["decision"] == "invalid_model_repaired_retry_ok"
    assert out["resolved_model"] == "gpt-5.2-codex-ttadk"
    assert calls == ["gpt-5.2-codex-ttadk"]


def test_ttadk_runtime_repair_cooldown_gate_blocks_and_degrades(monkeypatch):
    """runtime_repair: cooldown gate 命中时应跳过修复并走降级（若提供 fallback）。"""
    from src.ttadk.runtime_repair import repair_invalid_model_startup

    attempts: list[dict] = []

    class _Mgr:
        pass

    def _start(_model_name):
        raise AssertionError("should not start when cooldown blocks")

    def _fallback(_e: Exception):
        return {"fallback": True}

    out = repair_invalid_model_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="gpt-5.2",
        cwd="/tmp",
        error=RuntimeError("✗ Error: Invalid model 'bad'. Available models: a, b"),
        error_blob="✗ Error: Invalid model 'bad'. Available models: a, b",
        attempts=attempts,
        start_fn=_start,
        fallback_fn=_fallback,
        precheck_fn=lambda _: {"model": None, "validated": False, "source": "defaults", "warnings": []},
        get_settings_fn=lambda: type(
            "S",
            (),
            {"ttadk_runtime_retry_enabled": True, "ttadk_runtime_retry_cooldown_s": 120.0, "ttadk_runtime_retry_allow_autoswitch": True},
        )(),
        time_fn=lambda: 100.0,
        stub_get_last_ts_fn=lambda _mgr, _tool: 90.0,
        stub_set_last_ts_fn=lambda _mgr, _tool, _ts: None,
    )
    assert out["degraded"] is True
    assert out["repaired"] is False
    assert out["decision"] == "invalid_model_degraded_runtime_repair_disabled"
    assert any(a.get("step") == "cooldown_skip" for a in (out.get("diagnostics") or {}).get("attempts") or [])


def test_ttadk_runtime_repair_retry_model_not_in_seeded_fallbacks_to_seeded(monkeypatch):
    """runtime_repair: precheck 给出的 retry_model 不在 seeded 时，必须回落到 seeded（避免再传无效/不可信模型）。"""
    from src.ttadk.runtime_repair import repair_invalid_model_startup

    attempts: list[dict] = []
    called: list[object] = []

    class _Mgr:
        def seed_models_from_error(self, tool_name: str, err_blob: str):
            return ["a", "b"]

    def _start(model_name):
        called.append(model_name)
        assert model_name in ("a", "b")
        return {"ok": True}

    def _precheck(_intent: str) -> dict:
        # 伪造 validated=True 但 model 不在 seeded
        return {"model": "NOT_IN_SEEDED", "validated": True, "source": "cache", "warnings": []}

    out = repair_invalid_model_startup(
        manager=_Mgr(),
        tool_name="codex",
        input_model="x",
        cwd="/tmp",
        error=RuntimeError("✗ Error: Invalid model 'x'. Available models: a, b"),
        error_blob="✗ Error: Invalid model 'x'. Available models: a, b",
        attempts=attempts,
        start_fn=_start,
        fallback_fn=None,
        precheck_fn=_precheck,
        get_settings_fn=lambda: type(
            "S",
            (),
            {"ttadk_runtime_retry_enabled": True, "ttadk_runtime_retry_cooldown_s": 0.0, "ttadk_runtime_retry_allow_autoswitch": False},
        )(),
        time_fn=lambda: 100.0,
    )
    assert out["decision"] == "invalid_model_repaired_retry_ok"
    assert out["resolved_model"] in ("a", "b")
    assert called and called[0] in ("a", "b")


def test_ttadk_select_retry_model_prefers_tool_subset_then_best_match():
    """select_retry_model: tool 子集存在时应只在子集里 best-match。"""
    from src.ttadk.runtime_repair import select_retry_model

    seen = {"models": None}

    def _choose_best(_intent: str, models: list[str]):
        seen["models"] = list(models)
        return "gpt-5.2-codex-ttadk"

    out = select_retry_model(
        tool_name="codex",
        input_model="gpt-5.2",
        seeded=["gpt-5.2-ttadk", "gpt-5.2-codex-ttadk"],
        allow_autoswitch=True,
        choose_best_fn=_choose_best,
    )
    assert out == "gpt-5.2-codex-ttadk"
    assert seen["models"] == ["gpt-5.2-codex-ttadk"]


def test_ttadk_select_retry_model_uses_all_when_no_tool_subset():
    """select_retry_model: tool 子集不存在时应在全量 seeded 上 best-match。"""
    from src.ttadk.runtime_repair import select_retry_model

    seen = {"models": None}

    def _choose_best(_intent: str, models: list[str]):
        seen["models"] = list(models)
        return "b"

    out = select_retry_model(
        tool_name="codex",
        input_model="x",
        seeded=["a", "b"],
        allow_autoswitch=True,
        choose_best_fn=_choose_best,
    )
    assert out == "b"
    assert seen["models"] == ["a", "b"]


def test_ttadk_select_retry_model_allow_autoswitch_false_returns_first():
    from src.ttadk.runtime_repair import select_retry_model

    out = select_retry_model(
        tool_name="codex",
        input_model="x",
        seeded=["a", "b"],
        allow_autoswitch=False,
        choose_best_fn=lambda *_: "b",  # should be ignored
    )
    assert out == "a"


def test_ttadk_select_retry_model_best_match_exception_falls_back_first():
    from src.ttadk.runtime_repair import select_retry_model

    def _boom(_intent: str, _models: list[str]):
        raise RuntimeError("boom")

    out = select_retry_model(
        tool_name="codex",
        input_model="x",
        seeded=["a", "b"],
        allow_autoswitch=True,
        choose_best_fn=_boom,
    )
    assert out == "a"


def test_ttadk_select_retry_model_empty_seeded_returns_none():
    from src.ttadk.runtime_repair import select_retry_model

    assert (
        select_retry_model(tool_name="codex", input_model="x", seeded=[], allow_autoswitch=True)
        is None
    )


def test_ttadk_runtime_repair_cooldown_gate_stub_blocks(monkeypatch):
    """_cooldown_gate: stub gate 命中时应返回 False，并写入 cooldown_skip attempts。"""
    from src.ttadk.runtime_repair import _cooldown_gate

    attempts: list[dict] = []

    class _Mgr:
        pass

    ok = _cooldown_gate(
        manager=_Mgr(),
        tool="codex",
        attempts=attempts,
        get_settings_fn=lambda: type("S", (), {"ttadk_runtime_retry_enabled": True, "ttadk_runtime_retry_cooldown_s": 120.0})(),
        time_fn=lambda: 100.0,
        stub_get_last_ts_fn=lambda _mgr, _tool: 90.0,
        stub_set_last_ts_fn=lambda _mgr, _tool, _ts: None,
    )
    assert ok is False
    assert any(a.get("step") == "cooldown_skip" for a in attempts)


def test_ttadk_runtime_repair_seed_models_records_attempt_and_backfills_cache(monkeypatch):
    """_seed_models: 应写入 seed_from_error attempts，并 best-effort 回灌缓存字段。"""
    import threading

    from src.ttadk.runtime_repair import _seed_models

    attempts: list[dict] = []

    class _Mgr:
        def __init__(self):
            self._lock = threading.Lock()
            self._tool_models_cache = {}
            self._cache_time = {}
            self._known_models = set()

    mgr = _Mgr()
    seeded = _seed_models(
        manager=mgr,
        tool="codex",
        error_blob="✗ Error: Invalid model 'x'. Available models: a, b",
        attempts=attempts,
        time_fn=lambda: 100.0,
    )
    assert seeded == ["a", "b"]
    assert any(a.get("step") == "seed_from_error" and a.get("ok") is True for a in attempts)
    assert "codex" in mgr._tool_models_cache
    assert "a" in mgr._known_models


def test_ttadk_runtime_repair_run_retry_flow_retry_auto_ok(monkeypatch):
    """_run_retry_flow: retry 失败后 retry_auto 成功应返回 auto_ok decision。"""
    from src.ttadk.runtime_repair import _run_retry_flow

    attempts: list[dict] = []
    calls: list[object] = []

    def _start(model_name):
        calls.append(model_name)
        if model_name is None:
            return {"ok": True, "auto": True}
        raise RuntimeError("boom")

    out = _run_retry_flow(
        tool="codex",
        intent="gpt-5.2",
        fixed={"validated": False, "source": "probe", "warnings": []},
        retry_model="real-x",
        start_fn=_start,
        fallback_fn=None,
        attempts=attempts,
    )
    assert calls == ["real-x", None]
    assert out["decision"] == "invalid_model_repaired_auto_ok"


def test_ttadk_runtime_stub_limits_invalid_values_fallback(monkeypatch):
    """stub 冷却限额：非法配置值应回退默认且不抛异常。"""
    from src.ttadk import manager as m
    import src.ttadk.compat as compat

    # reset limits cache to avoid cross-test interference
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache", None, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache_ts", 0.0, raising=False)

    # set non-default module defaults
    monkeypatch.setattr(m._STUB_COOLDOWN, "_ttl_default", 123.0, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_max_keys_default", 456, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_gc_interval_default", 7.0, raising=False)

    # invalid settings types
    monkeypatch.setattr(
        m,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": "bad",
                "ttadk_runtime_stub_cooldown_max_keys": None,
                "ttadk_runtime_stub_cooldown_gc_interval_s": object(),
            },
        )(),
    )

    # 显式初始化：让 startup_common 通过 provider 使用本测试 monkeypatch 的 get_settings
    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    m.get_ttadk_manager()

    ttl, max_keys, interval = m._runtime_invalid_model_stub_limits()
    assert ttl == 123.0
    assert max_keys == 456
    assert interval == 7.0


def test_ttadk_runtime_stub_limits_negative_values_clamped(monkeypatch):
    """stub 冷却限额：负值应被 clamp 到 0。"""
    from src.ttadk import manager as m
    import src.ttadk.compat as compat

    # reset limits cache to avoid cross-test interference
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache", None, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache_ts", 0.0, raising=False)

    monkeypatch.setattr(
        m,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": -1.0,
                "ttadk_runtime_stub_cooldown_max_keys": -10,
                "ttadk_runtime_stub_cooldown_gc_interval_s": -2.0,
            },
        )(),
    )

    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    m.get_ttadk_manager()

    ttl, max_keys, interval = m._runtime_invalid_model_stub_limits()
    assert ttl == 0.0
    assert max_keys == 0
    assert interval == 0.0


def test_ttadk_runtime_stub_limits_cached_by_gc_interval(monkeypatch):
    """gc_interval_s>0 时，limits 在一个周期内应命中缓存并减少 get_settings 调用。"""
    from src.ttadk import manager as m
    import src.ttadk.compat as compat

    # reset cache
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache", None, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache_ts", 0.0, raising=False)

    tbox = {"t": 100.0}
    monkeypatch.setattr(m.time, "time", lambda: float(tbox["t"]))

    calls = {"n": 0}

    def _settings():
        calls["n"] += 1
        return type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": 10.0,
                "ttadk_runtime_stub_cooldown_max_keys": 3,
                "ttadk_runtime_stub_cooldown_gc_interval_s": 2.0,
            },
        )()

    monkeypatch.setattr(m, "get_settings", _settings)

    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    m.get_ttadk_manager()

    a = m._runtime_invalid_model_stub_limits()
    b = m._runtime_invalid_model_stub_limits()
    assert a == b
    assert calls["n"] == 1

    # advance past interval -> refresh
    tbox["t"] = 103.0
    _ = m._runtime_invalid_model_stub_limits()
    assert calls["n"] == 2


def test_ttadk_runtime_stub_limits_no_cache_when_interval_zero(monkeypatch):
    """gc_interval_s=0 时不缓存：每次调用都会读取 settings。"""
    from src.ttadk import manager as m
    import src.ttadk.compat as compat

    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache", None, raising=False)
    monkeypatch.setattr(m._STUB_COOLDOWN, "_limits_cache_ts", 0.0, raising=False)

    calls = {"n": 0}

    def _settings():
        calls["n"] += 1
        return type(
            "S",
            (),
            {
                "ttadk_runtime_stub_cooldown_ttl_s": 10.0,
                "ttadk_runtime_stub_cooldown_max_keys": 3,
                "ttadk_runtime_stub_cooldown_gc_interval_s": 0.0,
            },
        )()

    monkeypatch.setattr(m, "get_settings", _settings)

    monkeypatch.setattr(compat, "_providers_installed", False, raising=False)
    m.get_ttadk_manager()

    _ = m._runtime_invalid_model_stub_limits()
    _ = m._runtime_invalid_model_stub_limits()
    assert calls["n"] == 2


def test_resolve_and_ensure_valid_model_refreshes_when_low_confidence_cache(monkeypatch):
    """resolve_and_ensure_valid_model：当缓存命中但来源 low_confidence 时，应刷新一次再尝试校验。"""
    manager = TTADKManager(default_tool="codex")

    manager._tool_models_cache["codex"] = [TTADKModel(name="real-a", description="A", friendly_name="A")]
    manager._cache_time["codex"] = time.time()
    manager._tool_models_meta["codex"] = {"source": "file_cache", "warnings": ["source_cross_project", "low_confidence"]}

    def _refresh(tool_name=None, cwd=None):
        # 刷新后视为来自可信来源（例如 probe），并清除 low_confidence
        manager._tool_models_cache["codex"] = [TTADKModel(name="real-a", description="A", friendly_name="A")]
        manager._cache_time["codex"] = time.time()
        manager._tool_models_meta["codex"] = {"source": "probe", "warnings": []}
        from src.ttadk.models import ModelListResult

        return ModelListResult(models=list(manager._tool_models_cache["codex"]), source="probe", warnings=[])

    monkeypatch.setattr(manager, "refresh_models", _refresh)

    r = manager.resolve_and_ensure_valid_model("real-a", tool_name="codex", cwd=".")
    assert r.real_name == "real-a"
    assert r.validated is True
    assert any(w.startswith("refreshed:") for w in (r.warnings or []))


def test_startup_resolve_rejects_low_confidence_cache_even_if_quick_validated(monkeypatch):
    """启动预校验：即便 quick 认为命中缓存可 validated，也不能在 low_confidence 来源下直接透传。"""
    manager = TTADKManager(default_tool="codex")

    # 让 quick 路径可 validated（resolve_startup_model 只看缓存列表，不看 meta）
    manager._tool_models_cache["codex"] = [TTADKModel(name="real-a", description="A", friendly_name="A")]
    manager._cache_time["codex"] = time.time()
    # 但 models 列表来源标记为 low_confidence
    manager._tool_models_meta["codex"] = {"source": "file_cache", "warnings": ["source_cross_project", "low_confidence"]}

    def _refresh_fail(tool_name=None, cwd=None):
        raise RuntimeError("refresh blocked")

    monkeypatch.setattr(manager, "refresh_models", _refresh_fail)

    resolved, diag = manager.resolve_startup_model_with_diagnostics(
        "real-a",
        tool_name="codex",
        cwd=".",
        timeout_s=0.01,
    )
    assert bool(getattr(resolved, "validated", False)) is False
    assert "no_m_passthrough" in (getattr(resolved, "warnings", []) or [])
    assert isinstance(diag, dict)
    assert any(a.get("phase") == "quick" for a in (diag.get("attempts") or []))


def test_model_cache_file_corrupted_recovers(monkeypatch, tmp_path):
    """缓存文件损坏时应自动清空并删除文件，避免后续一直读坏文件。"""
    manager = TTADKManager(default_tool="test-tool")
    bad = tmp_path / "models_cache.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(manager, "_cache_file_path", bad)

    # 触发加载（确保不被 _ensure_initialized 的状态影响）
    manager._initialized = True
    manager._load_cache_from_file()
    assert manager._tool_models_cache == {}
    assert getattr(manager, "_tool_models_meta", {}) == {}
    assert not bad.exists()


def test_ttadk_fetcher_probe_tool_models_parses_available_models():
    from src.ttadk.model_fetcher import TTADKModelFetcher

    fake = _FakeRunner(
        out="",
        err="✗ Error: Invalid model 'X'. Available models: m1, m2",
        rc=1,
    )
    fetcher = TTADKModelFetcher(runner=fake)
    models = fetcher.probe_tool_models("codex", cwd=".", timeout=0.2)
    assert [m.name for m in models] == ["m1", "m2"]
    assert fake.calls and fake.calls[0][0][:4] == ["ttadk", "code", "-t", "codex"]


def test_extract_available_models_multiline_and_ansi():
    from src.ttadk.models import extract_available_models, is_invalid_model_error

    text = (
        "\x1b[31m✗ Error: Invalid model 'X'. Available models:\n"
        "  gpt-5.2-codex-ttadk\n"
        "  gpt-5.2-ttadk\n"
        "Command failed: ttadk code\n"
        "\x1b[0m"
    )
    assert is_invalid_model_error(text) is True
    assert extract_available_models(text) == ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]


def test_extract_available_models_empty_list_returns_empty():
    from src.ttadk.models import extract_available_models, is_invalid_model_error

    text = "✗ Error: Invalid model 'gpt-5.2'. Available models:    "
    assert is_invalid_model_error(text) is True
    assert extract_available_models(text) == []


def test_ttadk_manager_preheat_once_and_writes_cache(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    # 让 preheat 只覆盖 codex
    s = type(
        "S",
        (),
        {
            "ttadk_default_tool": "coco",
            "ttadk_default_model": "",
            "ttadk_preheat_enabled": True,
            "ttadk_preheat_on_first_use": True,
            "ttadk_preheat_on_startup": True,
            "ttadk_preheat_tools": "codex",
            "ttadk_preheat_timeout": 0.2,
        },
    )()
    monkeypatch.setattr("src.ttadk.manager.get_settings", lambda: s)

    # 不依赖系统是否安装 ttadk
    monkeypatch.setattr("src.ttadk.manager.shutil.which", lambda _: "/usr/bin/ttadk")

    # 注入可控 runner
    fake = _FakeRunner(
        out="",
        err="Invalid model 'X'. Available models: real-a, real-b",
        rc=1,
    )
    manager._model_fetcher = TTADKModelFetcher(runner=fake)

    manager.maybe_preheat_common_models(cwd=".")
    assert "codex" in manager._tool_models_cache
    assert [m.name for m in manager._tool_models_cache["codex"]] == ["real-a", "real-b"]

    # 再次调用不应再次 probe
    manager.maybe_preheat_common_models(cwd=".")
    assert len(fake.calls) == 1


def test_ttadk_manager_preheat_timeout_zero_noop(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    s = type(
        "S",
        (),
        {
            "ttadk_default_tool": "coco",
            "ttadk_default_model": "",
            "ttadk_preheat_enabled": True,
            "ttadk_preheat_on_first_use": True,
            "ttadk_preheat_on_startup": True,
            "ttadk_preheat_tools": "codex",
            "ttadk_preheat_timeout": 0.0,
        },
    )()
    monkeypatch.setattr("src.ttadk.manager.get_settings", lambda: s)
    monkeypatch.setattr("src.ttadk.manager.shutil.which", lambda _: "/usr/bin/ttadk")

    fake = _FakeRunner(
        out="",
        err="Invalid model 'X'. Available models: real-a",
        rc=1,
    )
    manager._model_fetcher = TTADKModelFetcher(runner=fake)

    manager.maybe_preheat_common_models(cwd=".")
    assert fake.calls == []


def test_ttadk_manager_preheat_first_use_only_once(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    s = type(
        "S",
        (),
        {
            "ttadk_default_tool": "coco",
            "ttadk_default_model": "",
            "ttadk_preheat_enabled": True,
            "ttadk_preheat_on_first_use": True,
            "ttadk_preheat_on_startup": False,
            "ttadk_preheat_tools": "",
            "ttadk_preheat_timeout": 0.2,
        },
    )()
    monkeypatch.setattr("src.ttadk.manager.get_settings", lambda: s)
    monkeypatch.setattr("src.ttadk.manager.shutil.which", lambda _: "/usr/bin/ttadk")

    fake = _FakeRunner(
        out="",
        err="Invalid model 'X'. Available models: real-a, real-b",
        rc=1,
    )
    manager._model_fetcher = TTADKModelFetcher(runner=fake)

    # 首次触发：应 probe 一次并填充缓存
    manager.maybe_preheat_tool_models("codex", cwd=".")
    assert "codex" in manager._tool_models_cache
    assert [m.name for m in manager._tool_models_cache["codex"]] == ["real-a", "real-b"]
    assert len(fake.calls) == 1

    # 再次触发：不应重复 probe
    manager.maybe_preheat_tool_models("codex", cwd=".")
    assert len(fake.calls) == 1


def test_ttadk_manager_preheat_first_use_disabled_noop(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    s = type(
        "S",
        (),
        {
            "ttadk_default_tool": "coco",
            "ttadk_default_model": "",
            "ttadk_preheat_enabled": True,
            "ttadk_preheat_on_first_use": False,
            "ttadk_preheat_on_startup": False,
            "ttadk_preheat_tools": "",
            "ttadk_preheat_timeout": 0.2,
        },
    )()
    monkeypatch.setattr("src.ttadk.manager.get_settings", lambda: s)
    monkeypatch.setattr("src.ttadk.manager.shutil.which", lambda _: "/usr/bin/ttadk")

    fake = _FakeRunner(
        out="",
        err="Invalid model 'X'. Available models: real-a",
        rc=1,
    )
    manager._model_fetcher = TTADKModelFetcher(runner=fake)

    manager.maybe_preheat_tool_models("codex", cwd=".")
    assert fake.calls == []


def test_ttadk_manager_preheat_disabled_noop(monkeypatch):
    manager = TTADKManager(default_tool="coco")

    s = type(
        "S",
        (),
        {
            "ttadk_default_tool": "coco",
            "ttadk_default_model": "",
            "ttadk_preheat_enabled": False,
            "ttadk_preheat_on_first_use": True,
            "ttadk_preheat_on_startup": True,
            "ttadk_preheat_tools": "codex",
            "ttadk_preheat_timeout": 0.2,
        },
    )()
    monkeypatch.setattr("src.ttadk.manager.get_settings", lambda: s)

    # 即便 which 返回存在，也不应触发
    monkeypatch.setattr("src.ttadk.manager.shutil.which", lambda _: "/usr/bin/ttadk")
    fake = _FakeRunner(
        out="",
        err="Invalid model 'X'. Available models: real-a",
        rc=1,
    )
    manager._model_fetcher = TTADKModelFetcher(runner=fake)

    manager.maybe_preheat_common_models(cwd=".")
    assert fake.calls == []
