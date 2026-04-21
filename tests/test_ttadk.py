import sys
import time
from unittest.mock import MagicMock

import pytest

from src.ttadk import TTADKManager, TTADKModel, TTADKModelFetcher, TTADKTool, get_ttadk_manager
from src.ttadk.models import is_stdin_not_tty_error
from src.ttadk.strategies import InteractiveStrategy

# Real-world ttadk outputs sampled from local environment (ttadk 0.3.8)
_SAMPLE_INVALID_MODEL_CODEX = (
    "✗ Error: Invalid model 'gpt-5.2'. Available models: gpt-5.2-codex-ttadk, gpt-5.2-ttadk, gpt-5.3-codex"
)
_SAMPLE_INVALID_MODEL_CLAUDE = "✗ Error: Invalid model 'gpt-5.2'. Available models: glm-5-ttadk, kimi-k2.5, glm-4.7-ttadk, gpt-5.2-codex-ttadk, gpt-5.2-ttadk"
_SAMPLE_INVALID_MODEL_COCO_EMPTY = "✗ Error: Invalid model 'gpt-5.2'. Available models:"

_SAMPLE_INVALID_MODEL_ANSI = (
    "\x1b[31m✗ Error:\x1b[0m Invalid model 'gpt-5.2'. Available models: gpt-5.2-codex-ttadk, gpt-5.2-ttadk\n"
    "<id>abc</id>"
)
_SAMPLE_INVALID_MODEL_MULTILINE = (
    "Error: Invalid model 'x'. Available models:\n  - gpt-5.2-codex-ttadk\n  - gpt-5.2-ttadk\n  - gpt-5.3-codex\n"
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
    from src.ttadk.manager import TTADKStartupError
    from src.ttadk.startup import coordinate_ttadk_startup

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

    目标：确认 src.agent_session.create_engine_session(ttadk_*) 必经
    src.ttadk.startup_common.precheck_ttadk_startup_model()，且返回 SyncTTADKCLISession。
    """
    import src.agent_session as agent_session
    from src.agent_session import SyncTTADKCLISession

    # 关闭 rate limit wrapper，避免影响“返回 session 是否为 SSOT result”的断言
    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 3, "rate_limit_retry_enabled": False})(),
    )

    # 让 ModelFailureAwareSession 变成 identity，避免包装影响断言
    monkeypatch.setattr(agent_session, "ModelFailureAwareSession", lambda **kw: kw["inner"], raising=False)

    precheck_called: list[dict] = []

    def _fake_precheck_ttadk_startup_model(**kwargs):
        precheck_called.append(dict(kwargs))
        return {
            "tool": "codex",
            "input_model": kwargs.get("model_intent") or "",
            "resolved_real_name": "",
            "passthrough_model": None,
            "model": "gpt-5.2-codex-ttadk",  # validated model
            "validated": True,
            "source": "defaults",
            "warnings": [],
            "decision": "precheck_validated",
            "diagnostics": {"attempts": []},
        }

    monkeypatch.setattr("src.ttadk.startup_common.precheck_ttadk_startup_model", _fake_precheck_ttadk_startup_model)

    sess = agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")

    assert isinstance(sess, SyncTTADKCLISession)
    assert len(precheck_called) == 1
    assert precheck_called[0]["agent_type"] == "ttadk_codex"
    assert precheck_called[0]["cwd"] == "/tmp"
    assert precheck_called[0]["model_intent"] == "gpt-5.2"
    # Check if model name was passed to session
    assert sess._model_name == "gpt-5.2-codex-ttadk"


def test_ttadk_startup_summary_log_fields_success(monkeypatch, caplog):
    """TTADK CLI 启动成功路径：应返回会话并记录 startup 摘要日志。"""
    import logging

    import src.agent_session as agent_session

    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 3, "rate_limit_retry_enabled": False})(),
    )
    monkeypatch.setattr(agent_session, "ModelFailureAwareSession", lambda **kw: kw["inner"], raising=False)

    monkeypatch.setattr(
        "src.ttadk.startup_common.precheck_ttadk_startup_model",
        lambda **kw: {
            "tool": "codex",
            "input_model": "gpt-5.2",
            "model": "gpt-5.2-codex-ttadk",
            "validated": True,
            "source": "probe",
            "warnings": [],
        },
    )

    class _FakeCLISession:
        def __init__(self, agent_type: str, cwd: str, model_name=None):
            self.session_id = ""
            self._agent_type = agent_type
            self._cwd = cwd
            self._model_name = model_name

        def start(self, startup_timeout: float = 60):
            self.session_id = "sid"
            return self.session_id

    monkeypatch.setattr(agent_session, "SyncTTADKCLISession", _FakeCLISession, raising=False)

    with caplog.at_level(logging.INFO):
        s = agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")
    assert getattr(s, "session_id", "") == "sid"
    assert getattr(s, "_model_name", "") == "gpt-5.2-codex-ttadk"

    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "ttadk cli startup" in logs


def test_ttadk_startup_summary_log_fields_degraded(monkeypatch, caplog):
    """TTADK CLI 启动失败时应直接报错（不降级到 ACP）。"""
    import logging

    import src.agent_session as agent_session

    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 3, "rate_limit_retry_enabled": False})(),
    )
    monkeypatch.setattr(agent_session, "ModelFailureAwareSession", lambda **kw: kw["inner"], raising=False)

    monkeypatch.setattr(
        "src.ttadk.startup_common.precheck_ttadk_startup_model",
        lambda **kw: {
            "tool": "codex",
            "input_model": "gpt-5.2",
            "model": None,
            "validated": False,
            "source": "probe",
            "warnings": ["no_m_passthrough"],
        },
    )

    class _FakeFailCLISession:
        def __init__(self, agent_type: str, cwd: str, model_name=None):
            self.session_id = ""

        def start(self, startup_timeout: float = 60):
            raise RuntimeError("cli_start_failed")

    monkeypatch.setattr(agent_session, "SyncTTADKCLISession", _FakeFailCLISession, raising=False)

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="cli_start_failed"):
            agent_session.create_engine_session(agent_type="ttadk_codex", cwd="/tmp", model_name="gpt-5.2")


def test_is_stdin_not_tty_error_basic():
    assert is_stdin_not_tty_error(_SAMPLE_STDIN_NOT_TTY) is True


def test_is_stdin_not_tty_error_strips_ansi():
    assert is_stdin_not_tty_error(_SAMPLE_STDIN_NOT_TTY_ANSI) is True

def test_is_stdin_not_tty_error_negative_cases():
    assert is_stdin_not_tty_error("") is False
    assert is_stdin_not_tty_error("some other error") is False


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
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run should not be called"))
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.Popen should not be called")),
    )

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




def test_ttadk_sandbox_env_does_not_write_real_home_setting_json(monkeypatch, tmp_path):
    """回归：启用 sandbox 后，ttadk 子进程 env 应指向项目隔离目录，不应改写真实 HOME 下的 ~/.ttadk/setting.json。"""
    import json

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
    from src.ttadk.command_exec import execute_ttadk_code_with_repair
    from src.ttadk.manager import TTADKCommandRunner, get_ttadk_manager
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

    r = execute_ttadk_code_with_repair(manager=mgr, tool_name="codex", cwd=str(tmp_path), input_model="gpt-5.2")
    assert r["ok"] is True
    assert r["decision"] == "ttadk_code_ok"
    assert r["model"] == "gpt-5.2-codex-ttadk"
    assert r["validated"] is True
    assert r["fail_reason"] == ""
    assert isinstance(r.get("attempts"), list) and r["attempts"]


def test_ttadk_code_execute_invalid_model_then_refresh_and_retry_ok(monkeypatch, tmp_path):
    """用户路径：首次 invalid_model → force_refresh + 重新选真名 → 重试成功。"""
    from src.ttadk.command_exec import execute_ttadk_code_with_repair
    from src.ttadk.manager import TTADKCommandRunner, get_ttadk_manager
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

    r = execute_ttadk_code_with_repair(manager=mgr, tool_name="codex", cwd=str(tmp_path), input_model="gpt-5.2")
    assert r["ok"] is True
    assert r["decision"] == "ttadk_code_ok_after_refresh"
    assert r["model"] == "gpt-5.2-codex-ttadk"
    assert called["refresh"] == 1
    phases = [a.get("phase") for a in (r.get("attempts") or [])]
    assert "force_refresh" in phases
    assert "retry_after_refresh" in phases



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
    import shutil
    from types import SimpleNamespace

    import src.ttadk.manager as ttadk_manager_mod
    from src.ttadk.manager import TTADKManager

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



def test_resolve_and_ensure_valid_model_marks_defaults_untrusted_and_refreshes(monkeypatch):
    """当 get_models 返回 defaults 时，应视为不可信并触发一次 refresh(force_refresh=True)。"""
    from src.ttadk.manager import TTADKManager
    from src.ttadk.model_fetcher import FetchDiagnostics, FetchResult
    from src.ttadk.models import TTADKModel

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

    import src.ttadk.manager as ttadk_manager_mod
    from src.ttadk.manager import TTADKManager

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




def test_agent_session_ttadk_precheck_uses_real_model_when_valid(monkeypatch):
    """TTADK 模式下，validated real model 应透传给 CLI 会话构造。"""
    import src.agent_session as agent_session

    calls: list[dict] = []

    monkeypatch.setattr(
        agent_session,
        "get_settings",
        lambda: type("S", (), {"acp_startup_timeout": 1, "rate_limit_retry_enabled": False})(),
    )

    monkeypatch.setattr(
        "src.ttadk.startup_common.precheck_ttadk_startup_model",
        lambda **kw: {
            "tool": "coco",
            "input_model": kw.get("model_intent") or "",
            "resolved_real_name": "gpt-5.2-ttadk",
            "model": "gpt-5.2-ttadk",
            "validated": True,
            "source": "probe",
            "decision": "precheck_validated",
            "fail_phase": "",
            "warnings": [],
            "diagnostics": {},
        },
    )

    class _DummyCLISession:
        def __init__(self, agent_type: str, cwd: str, model_name=None):
            calls.append({"agent_type": agent_type, "cwd": cwd, "model_name": model_name})
            self.session_id = ""

        def start(self, startup_timeout: float = 60):
            self.session_id = "dummy"
            return self.session_id

    monkeypatch.setattr(agent_session, "SyncTTADKCLISession", _DummyCLISession, raising=False)

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
    if manager._cache._is_cache_valid("coco"):
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
        e.stderr = "Invalid model gpt-5.2 ..."
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
        sync_adapter.start_session_with_retry(
            agent_type="ttadk_codex", cwd="/tmp", startup_timeout=1, model_name="gpt-5.2"
        )

    assert getattr(ctx.value, "fail_phase", "") in ("retry_exhausted", "")

    # 校验日志里包含 stderr_snippet（字符串匹配即可）
    assert "stderr_snippet" in caplog.text


def test_extract_models_ignores_unrelated_string_list(monkeypatch):
    manager = TTADKManager(default_tool="claude")

    # Mock fetch_tool_models_with_diagnostics 返回指定的模型列表
    from src.ttadk.model_fetcher import FetchDiagnostics, FetchResult

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
        return FetchResult(
            tool_name=tool_name, models=[], source="mock", diagnostics=FetchDiagnostics(tool_name=tool_name)
        )

    monkeypatch.setattr(
        manager._model_fetcher,
        "fetch_tool_models_with_diagnostics",
        mock_fetch,
    )

    result = manager.get_models(cwd=".")
    assert [m.name for m in result.models] == ["claude-3.7-sonnet", "claude-3.5-sonnet"]


def test_extract_models_not_from_generic_string_list(monkeypatch):
    manager = TTADKManager(default_tool="claude")

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

    from src.ttadk.models import ModelListResult, ResolvedModelResult

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
    manager._tool_models_meta["codex"] = {
        "source": "file_cache",
        "warnings": ["source_cross_project", "low_confidence"],
    }

    r = manager.resolve_real_model_name("real-a", tool_name="codex", cwd=".")
    assert r.real_name == "real-a"
    assert r.validated is False
    assert "models_untrusted" in (r.warnings or [])








def test_ttadk_runtime_stub_cooldown_migrates_once_from_function_attr(monkeypatch):
    """legacy 函数属性 store 存在时：仅在显式初始化路径迁移一次，之后不再反复读取函数属性。"""
    import src.ttadk.startup_common as sc
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

    fn_store: dict = {}
    monkeypatch.setattr(fn, "_runtime_invalid_model_last_ts_by_stub", fn_store, raising=False)

    # 显式初始化：触发一次迁移 + provider 安装
    monkeypatch.setattr(m, "_LEGACY_STUB_COOLDOWN_STORE", None, raising=False)
    monkeypatch.setattr(sc, "_compat_providers_installed", False, raising=False)
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
    fn._runtime_invalid_model_last_ts_by_stub = new_fn_store
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
            {
                "ttadk_runtime_retry_enabled": True,
                "ttadk_runtime_retry_cooldown_s": 0.0,
                "ttadk_runtime_retry_allow_autoswitch": True,
            },
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
            {
                "ttadk_runtime_retry_enabled": True,
                "ttadk_runtime_retry_cooldown_s": 120.0,
                "ttadk_runtime_retry_allow_autoswitch": True,
            },
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
            {
                "ttadk_runtime_retry_enabled": True,
                "ttadk_runtime_retry_cooldown_s": 0.0,
                "ttadk_runtime_retry_allow_autoswitch": False,
            },
        )(),
        time_fn=lambda: 100.0,
    )
    assert out["decision"] == "invalid_model_repaired_retry_ok"
    assert out["resolved_model"] in ("a", "b")
    assert called and called[0] in ("a", "b")



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




def test_ttadk_select_retry_model_empty_seeded_returns_none():
    from src.ttadk.runtime_repair import select_retry_model

    assert select_retry_model(tool_name="codex", input_model="x", seeded=[], allow_autoswitch=True) is None


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
        get_settings_fn=lambda: type(
            "S", (), {"ttadk_runtime_retry_enabled": True, "ttadk_runtime_retry_cooldown_s": 120.0}
        )(),
        time_fn=lambda: 100.0,
        stub_get_last_ts_fn=lambda _mgr, _tool: 90.0,
        stub_set_last_ts_fn=lambda _mgr, _tool, _ts: None,
    )
    assert ok is False
    assert any(a.get("step") == "cooldown_skip" for a in attempts)




def test_ttadk_runtime_stub_limits_invalid_values_fallback(monkeypatch):
    """stub 冷却限额：非法配置值应回退默认且不抛异常。"""
    import src.ttadk.startup_common as sc
    from src.ttadk import manager as m

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
    monkeypatch.setattr(sc, "_compat_providers_installed", False, raising=False)
    m.get_ttadk_manager()

    ttl, max_keys, interval = m._runtime_invalid_model_stub_limits()
    assert ttl == 123.0
    assert max_keys == 456
    assert interval == 7.0


def test_ttadk_runtime_stub_limits_negative_values_clamped(monkeypatch):
    """stub 冷却限额：负值应被 clamp 到 0。"""
    import src.ttadk.startup_common as sc
    from src.ttadk import manager as m

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

    monkeypatch.setattr(sc, "_compat_providers_installed", False, raising=False)
    m.get_ttadk_manager()

    ttl, max_keys, interval = m._runtime_invalid_model_stub_limits()
    assert ttl == 0.0
    assert max_keys == 0
    assert interval == 0.0




def test_resolve_and_ensure_valid_model_refreshes_when_low_confidence_cache(monkeypatch):
    """resolve_and_ensure_valid_model：当缓存命中但来源 low_confidence 时，应刷新一次再尝试校验。"""
    manager = TTADKManager(default_tool="codex")

    manager._tool_models_cache["codex"] = [TTADKModel(name="real-a", description="A", friendly_name="A")]
    manager._cache_time["codex"] = time.time()
    manager._tool_models_meta["codex"] = {
        "source": "file_cache",
        "warnings": ["source_cross_project", "low_confidence"],
    }

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
    manager._tool_models_meta["codex"] = {
        "source": "file_cache",
        "warnings": ["source_cross_project", "low_confidence"],
    }

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


class TestTTADKTitleSuffix:
    def test_with_tool_and_model(self):
        from src.card.shared import _build_ttadk_title_suffix

        assert _build_ttadk_title_suffix("claude", "glm-5") == " · claude(glm-5)"

    def test_with_tool_only(self):
        from src.card.shared import _build_ttadk_title_suffix

        assert _build_ttadk_title_suffix("gemini", None) == " · gemini"
        assert _build_ttadk_title_suffix("coco", "") == " · coco"

    def test_no_tool_no_model(self):
        from src.card.shared import _build_ttadk_title_suffix

        assert _build_ttadk_title_suffix(None, None) == ""
        assert _build_ttadk_title_suffix("", "") == ""


class TestResolveTitleTTADKWithToolModel:
    def test_with_project_tool_model(self):
        from src.card.shared import resolve_title_and_template
        from src.mode.manager import InteractionMode

        title, template = resolve_title_and_template(
            "myProject",
            mode=InteractionMode.TTADK, ttadk_tool_name="claude", ttadk_model_name="glm-5",
        )
        assert "TTADK" in title
        assert "claude" in title
        assert "glm-5" in title
        assert template == "orange"

    def test_with_project_tool_only(self):
        from src.card.shared import resolve_title_and_template
        from src.mode.manager import InteractionMode

        title, _ = resolve_title_and_template(
            "myProject",
            mode=InteractionMode.TTADK, ttadk_tool_name="gemini",
        )
        assert "TTADK · gemini" in title
        assert "myProject" in title

    def test_no_project_with_tool_model(self):
        from src.card.shared import resolve_title_and_template
        from src.mode.manager import InteractionMode

        title, _ = resolve_title_and_template(
            None,
            mode=InteractionMode.TTADK, ttadk_tool_name="codex", ttadk_model_name="gpt-5.2",
        )
        assert "TTADK" in title
        assert "codex" in title
        assert "gpt-5.2" in title

    def test_no_project_no_tool(self):
        from src.card.shared import resolve_title_and_template
        from src.mode.manager import InteractionMode

        title, _ = resolve_title_and_template(
            None, mode=InteractionMode.TTADK,
        )
        assert "TTADK" in title

    def test_backward_compatible_no_ttadk_params(self):
        from src.card.shared import resolve_title_and_template
        from src.mode.manager import InteractionMode

        title, _ = resolve_title_and_template(
            "ghostAp", mode=InteractionMode.TTADK,
        )
        assert title == "🎮 ghostAp · TTADK"


class TestPreambleAsciiArtSingleQuote:
    def test_ascii_art_line_with_quote_filtered(self):
        from src.agent_session import _is_ttadk_preamble_line

        assert _is_ttadk_preamble_line("  | |   | | / _ \\ | | | | ' /")

    def test_all_banner_lines_filtered(self):
        from src.agent_session import _is_ttadk_preamble_line

        lines = [
            "  _____ _____  _    ____  _  __",
            " |_   _|_   _|/ \\  |  _ \\| |/ /",
            "   | |   | | / _ \\ | | | | ' /",
            "   | |   | |/ ___ \\| |_| | . \\",
            "   |_|   |_/_/   \\_\\____/|_|\\_\\",
        ]
        for i, line in enumerate(lines):
            assert _is_ttadk_preamble_line(line), f"Banner line {i+1} should be filtered: {line!r}"


class TestBuildTTADKPassthroughPrompt:
    def test_coco_uses_print_mode(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("coco", "hello world")
        assert result.startswith("-p ")
        assert "hello world" in result

    def test_claude_uses_print_mode(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("claude", "fix the bug")
        assert result.startswith("-p ")
        assert "fix the bug" in result

    def test_gemini_uses_print_mode(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("gemini", "explain this code")
        assert result.startswith("-p ")

    def test_codex_uses_positional_only(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("codex", "write a test")
        assert not result.startswith("-p")
        assert "write a test" in result

    def test_unknown_tool_uses_positional(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("tmates", "do something")
        assert not result.startswith("-p")

    def test_special_chars_are_quoted(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("coco", 'fix "bug" in file.py')
        assert "fix" in result
        assert "bug" in result

    def test_case_insensitive_tool_name(self):
        from src.agent_session import _build_ttadk_passthrough_prompt

        result = _build_ttadk_passthrough_prompt("Coco", "test")
        assert result.startswith("-p ")


class TestSyncTTADKCLISessionCmdArgs:
    @pytest.fixture()
    def _patch_env(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent_session.build_ttadk_subprocess_env",
            lambda **kw: ({"PATH": "/usr/bin", "NO_COLOR": "1"}, {}),
        )

    @pytest.mark.usefixtures("_patch_env")
    def test_send_prompt_uses_passthrough_args(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from src.agent_session import SyncTTADKCLISession

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["Hello\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None

        with patch("src.agent_session.subprocess.Popen", return_value=mock_proc) as mock_popen:
            session = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
            session.send_prompt("say hello")

            cmd_args = mock_popen.call_args[0][0]
            assert cmd_args[:4] == ["ttadk", "code", "-t", "coco"]
            assert "-a" in cmd_args
            a_idx = cmd_args.index("-a")
            a_val = cmd_args[a_idx + 1]
            assert a_val.startswith("-p ")
            assert "say hello" in a_val

    @pytest.mark.usefixtures("_patch_env")
    def test_send_prompt_does_not_append_prompt_as_positional(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from src.agent_session import SyncTTADKCLISession

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["ok\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None

        with patch("src.agent_session.subprocess.Popen", return_value=mock_proc) as mock_popen:
            session = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
            session.send_prompt("my prompt text")

            cmd_args = mock_popen.call_args[0][0]
            assert cmd_args[-1] != "my prompt text", "prompt should not be a positional arg to 'ttadk code'"

    @pytest.mark.usefixtures("_patch_env")
    def test_send_prompt_codex_no_print_flag(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from src.agent_session import SyncTTADKCLISession

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["done\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None

        with patch("src.agent_session.subprocess.Popen", return_value=mock_proc) as mock_popen:
            session = SyncTTADKCLISession(agent_type="ttadk_codex", cwd="/tmp")
            session.send_prompt("write tests")

            cmd_args = mock_popen.call_args[0][0]
            a_idx = cmd_args.index("-a")
            a_val = cmd_args[a_idx + 1]
            assert not a_val.startswith("-p"), "codex should not use -p print mode"


class TestTTADKPreambleNewPatterns:
    def test_launching_tool_filtered(self):
        from src.agent_session import _is_ttadk_preamble_line

        assert _is_ttadk_preamble_line("🚀 Launching Trae CLI (Coco)...")
        assert _is_ttadk_preamble_line("🚀 Launching Claude Code...")

    def test_real_content_not_filtered(self):
        from src.agent_session import _is_ttadk_preamble_line

        assert not _is_ttadk_preamble_line("Hello, I can help you with that!")
        assert not _is_ttadk_preamble_line("The answer is 42.")
        assert not _is_ttadk_preamble_line("def foo(): return 1")




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
