"""TTADK 模型/启动管理（manager）。

注意：stub 冷却（runtime invalid-model cooldown）的 SSOT 已迁移到 `src.ttadk.startup_common`。
本文件仅保留“兼容入口/符号”以避免历史导入与单测 monkeypatch 回归。

⚠️ 重要约束：
- 任何运行逻辑不得在本文件新增 stub 冷却的“第二套实现”。
- `src.ttadk.startup_common._STUB_COOLDOWN` 是唯一权威对象。
"""

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import get_settings
from .cache import TTADKModelCache
from .command_exec import (
    TTADKCommandRunner,
)

# ---------------------------------------------------------------------------
# deprecated_* Runtime invalid-model cooldown (compat only)
# ---------------------------------------------------------------------------
#
from .startup_common import _LEGACY_STUB_COOLDOWN_STORE as _LEGACY_STUB_COOLDOWN_STORE
from .startup_common import _STUB_COOLDOWN as _STUB_COOLDOWN
from .startup_common import _runtime_invalid_model_stub_get_last_ts as _runtime_invalid_model_stub_get_last_ts
from .startup_common import _runtime_invalid_model_stub_key as _runtime_invalid_model_stub_key
from .startup_common import _runtime_invalid_model_stub_limits as _runtime_invalid_model_stub_limits
from .startup_common import _runtime_invalid_model_stub_set_last_ts as _runtime_invalid_model_stub_set_last_ts
from .startup_common import _runtime_invalid_model_stub_store as _runtime_invalid_model_stub_store
from .startup_common import _runtime_invalid_model_stub_store_unlocked as _runtime_invalid_model_stub_store_unlocked
from .startup_common import _StubCooldownStore as _StubCooldownStore
from .model_fetcher import TTADKModelFetcher
from .models import (
    ModelListResult,
    ResolvedModelResult,
    ToolListResult,
    TTADKModel,
    TTADKTool,
    build_model_list_diagnostics,
    resolve_model_id,
)

logger = logging.getLogger(__name__)


class TTADKStartupError(RuntimeError):
    """TTADK 启动失败的可降级异常（携带上下文供上层做 fallback）。

    说明：该异常类型定义在 TTADK 层，避免在启动编排 SSOT 中依赖 `src.agent_session`。
    调用方可通过 `type(err).__name__ == "TTADKStartupError"` 进行稳定分类。
    """

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        input_model: str = "",
        real_model: str = "",
        cause: Exception | None = None,
        # Startup failure diagnostics (best-effort; keep field names aligned with ACP diagnostics)
        agent_cmd: str = "",
        agent_args: Optional[list[str]] = None,
        returncode: Optional[int] = None,
        stdout_snippet: str = "",
        stderr_snippet: str = "",
        fail_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.input_model = input_model
        self.real_model = real_model
        self.__cause__ = cause

        # Keep names compatible with `src.acp.sync_adapter.build_startup_diagnostics`
        # so upper layers can extract cmd/args/rc/snippets without special-casing.
        try:
            self.agent_cmd = str(agent_cmd or "")
        except Exception:
            self.agent_cmd = ""
        try:
            self.agent_args = [str(x) for x in (agent_args or [])]
        except Exception:
            self.agent_args = []
        try:
            self.returncode = int(returncode) if returncode is not None else None
        except Exception:
            self.returncode = None
        try:
            self.stdout_snippet = str(stdout_snippet or "")
        except Exception:
            self.stdout_snippet = ""
        try:
            self.stderr_snippet = str(stderr_snippet or "")
        except Exception:
            self.stderr_snippet = ""
        try:
            self.fail_reason = str(fail_reason or "")
        except Exception:
            self.fail_reason = ""


def start_ttadk_engine_session(
    *,
    agent_type: str,
    cwd: str,
    model_intent: Optional[str],
    startup_timeout: float,
    manager: Optional["TTADKManager"] = None,
    # Injectable deps for testing / decoupling
    start_ttadk_session_fn: Optional[Callable[..., Any]] = None,
    resolve_agent_spec_fn: Optional[Callable[..., tuple[str, list[str]]]] = None,
    precheck_fn: Optional[Callable[[str], dict]] = None,
    fallback_fn: Optional[Callable[[Exception], Any]] = None,
    get_settings_fn: Callable[[], object] = get_settings,
    time_fn: Callable[[], float] = time.time,
) -> dict:
    """Deep/Loop/Spec 引擎 TTADK 启动编排 SSOT（start/precheck/repair/degrade）。

    返回值契约：与 `coordinate_ttadk_startup()` 一致（result/tool/input_model/resolved_model/.../diagnostics）。

    说明：
    - 该函数只编排“启动期”逻辑，不处理 send_prompt 阶段的模型错误（compaction/loop/failover）。
    - 所有外部依赖默认使用局部 import，以避免在模块 import 时触发重依赖/循环依赖。

    启动期模型决策 SSOT（重要约束）：
    - “是否允许透传 -m 以及透传的真实模型名”的决策必须收敛到
      `TTADKManager.resolve_startup_model_with_diagnostics()` →
      `src.ttadk.startup_common.precheck_ttadk_startup_model()` →
      `src.ttadk.startup.coordinate_ttadk_startup()`。
    - 上层（agent_session/acp/engine/handler）不得旁路调用
      `resolve_real_model_name()` / `resolve_and_ensure_valid_model()` 来决定启动透传，
      否则会出现“各处探测/各处兜底”导致的语义漂移与回归风险。
    """
    agent_type = (agent_type or "").strip().lower()
    if not agent_type.startswith("ttadk_"):
        raise ValueError(f"not_ttadk_agent_type: {agent_type}")

    tool_name = agent_type.replace("ttadk_", "", 1)
    intent = (model_intent or "").strip()

    mgr = manager
    if mgr is None:
        mgr = get_ttadk_manager()

    # Defaults: local import to avoid import-time cycles
    if resolve_agent_spec_fn is None:
        from ..acp.sync_adapter import resolve_agent_spec as _resolve

        resolve_agent_spec_fn = _resolve

    if start_ttadk_session_fn is None:
        from ..acp.sync_adapter import start_ttadk_session_with_pty_retry as _start

        start_ttadk_session_fn = _start

    if fallback_fn is None:
        # Default deterministic degrade: fall back to coco ACP
        from ..acp.sync_adapter import start_session_with_retry as _start_coco
        from ..coco_model import get_coco_model_manager as _get_coco_model_manager

        def fallback_fn(err: Exception):
            fallback_model = _get_coco_model_manager().get_current_model()
            logger.warning(
                "[TTADK:Startup] degrade_to_coco: tool=%s input_model=%s err_type=%s err=%s",
                tool_name,
                intent,
                type(err).__name__,
                str(err) or "(empty)",
            )
            s = _start_coco(
                agent_type="coco",
                cwd=cwd,
                startup_timeout=float(startup_timeout or 60),
                model_name=fallback_model,
            )
            try:
                s._degraded_to = "coco"
                s._degraded_reason = str(err) or ""
            except Exception:
                pass
            return s

    def _start_fn(passthrough_model: Optional[str]):
        # 协议适配快速失败：若无法解析 cmd/args，直接触发可控降级（避免长时间等待）。
        try:
            resolve_agent_spec_fn(agent_type, model_name=passthrough_model)
        except Exception as e:
            # Best-effort diagnostics extraction from spec resolve error.
            agent_cmd = ""
            agent_args: list[str] = []
            rc = None
            out_snip = ""
            err_snip = ""
            try:
                agent_cmd = str(getattr(e, "agent_cmd", "") or getattr(e, "cmd", "") or "")
            except Exception:
                agent_cmd = ""
            try:
                agent_args = [str(x) for x in (getattr(e, "agent_args", None) or getattr(e, "args", None) or [])]
            except Exception:
                agent_args = []
            try:
                _rc = getattr(e, "returncode", None)
                rc = int(_rc) if _rc is not None else None
            except Exception:
                rc = None
            try:
                out_snip = str(getattr(e, "stdout_snippet", "") or "")
            except Exception:
                out_snip = ""
            try:
                err_snip = str(getattr(e, "stderr_snippet", "") or "")
            except Exception:
                err_snip = ""
            raise TTADKStartupError(
                "ttadk_protocol_adapter_failed",
                tool_name=tool_name,
                input_model=intent,
                real_model=str(passthrough_model or ""),
                cause=e,
                agent_cmd=agent_cmd,
                agent_args=agent_args,
                returncode=rc,
                stdout_snippet=out_snip,
                stderr_snippet=err_snip,
                fail_reason="protocol_adapter",
            )

        # claude：短探测发现 wrapper 不产出 JSON-RPC，则直接降级，避免 ACP handshake 超时。
        if tool_name == "claude":
            enabled = True
            quick_timeout_s = 2.0
            try:
                s = get_settings_fn()
                enabled = bool(getattr(s, "ttadk_claude_acp_ready_check_enabled", True))
                quick_timeout_s = float(getattr(s, "ttadk_claude_acp_ready_check_timeout_s", 2.0) or 2.0)
            except Exception:
                enabled = True
                quick_timeout_s = 2.0
            quick_timeout_s = max(0.1, quick_timeout_s)

            if not enabled:
                raise TTADKStartupError(
                    "ttadk_claude_acp_ready_check_disabled", tool_name=tool_name, input_model=intent
                )
            try:
                from .startup_probe import ttadk_acp_ready_quickcheck

                if not ttadk_acp_ready_quickcheck(
                    agent_type=agent_type,
                    cwd=cwd,
                    model_name=passthrough_model,
                    resolve_agent_spec_fn=resolve_agent_spec_fn,
                    time_fn=time_fn,
                    timeout_s=quick_timeout_s,
                ):
                    raise TTADKStartupError(
                        "ttadk_claude_acp_not_ready",
                        tool_name=tool_name,
                        input_model=intent,
                        real_model=str(passthrough_model or ""),
                        fail_reason="protocol_not_ready",
                    )
            except TTADKStartupError:
                raise
            except Exception as e:
                raise TTADKStartupError(
                    "ttadk_claude_acp_ready_check_failed",
                    tool_name=tool_name,
                    input_model=intent,
                    real_model=str(passthrough_model or ""),
                    cause=e,
                    fail_reason="protocol_ready_check_failed",
                )

        # 启动（可能内部执行 PTY 重试）
        try:
            return start_ttadk_session_fn(
                agent_type=agent_type,
                cwd=cwd,
                startup_timeout=float(startup_timeout or 60),
                model_name=passthrough_model,
            )
        except TypeError:
            # 兼容旧签名/测试桩
            return start_ttadk_session_fn(
                agent_type=agent_type,
                cwd=cwd,
                startup_timeout=float(startup_timeout or 60),
                model_name=passthrough_model,
            )  # type: ignore[misc]

    if precheck_fn is None:

        def precheck_fn(x):
            return precheck_ttadk_startup_model(
                agent_type=agent_type,
                cwd=cwd,
                model_intent=x,
                manager=mgr,
            )

    # 统一：fail_phase/decision/diagnostics 的 SSOT 由 startup.coordinator 输出。
    # 这里确保 protocol_adapter/timeout/invalid_model/start_failed 的分类输入信息充分。
    from .startup import coordinate_ttadk_startup as _coordinate

    return _coordinate(
        manager=mgr,
        tool_name=tool_name,
        input_model=intent,
        cwd=cwd,
        start_fn=_start_fn,
        fallback_fn=fallback_fn,
        precheck_fn=precheck_fn,
        startup_probe_timeout_s=None,
    )


# ---------------------------------------------------------------------------
# TTADK startup precheck contract
# ---------------------------------------------------------------------------
#
# 目标：收敛“启动阶段预校验/透传 model”的单一入口，避免 agent_session/acp/engine 多处实现漂移。
#
# 启动链路（SSOT）：
# - `precheck_ttadk_startup_model()`：决定“是否透传 -m”以及记录解析来源/告警
# - `coordinate_ttadk_startup()`：负责 start→invalid_model 闭环修复→retry→degrade，并产出 attempts
# - `src/acp/manager.py`：仅消费 coordinator 的稳定字段记录日志/诊断
#
# source 语义（稳定约定）：
# - 模型列表来源（ModelListResult.source）：cache / structured_sync / official_cli / probe / file_cache / local_config / defaults
# - 名称匹配来源（ResolvedModelResult.source）：exact / friendly / prefix / partial / unknown / fallback
#
# warnings 语义（稳定约定）：
# - models_untrusted：模型列表不可信，不允许用于 validated 透传 -m（例如 defaults 兜底、跨项目缓存、拉取失败等）
# - models_empty / models_error：模型列表为空/拉取失败，必须走 (auto)
# - low_confidence / source_cross_project：跨项目来源（例如 ~/.ttadk/models_cache.json），必须经更可信来源验证后才可透传
# - no_m_passthrough：显式标记“不应透传 -m”（用于上层日志/验收与 UI 提示）
#
# 输出字段（稳定契约）：
# - tool: str                 # ttadk tool 名（例如 codex）
# - input_model: str          # 用户输入/当前选择的 model 意图（可能是友好名/短名）
# - model: Optional[str]      # validated=True 时透传给 ttadk 的真实 model id；否则 None（表示 (auto)）
# - validated: bool           # 是否能确定 model 为真实可用 id
# - source: str               # 解析来源（cache/probe/structured/.../unknown/error）
# - decision: str             # precheck_validated / precheck_auto / precheck_error / non_ttadk
# - fail_phase: str           # precheck_error 或空字符串
# - warnings: list[str]       # 诊断提示

TTADK_PRECHECK_DECISIONS = {
    "precheck_validated",
    "precheck_auto",
    "precheck_error",
    "non_ttadk",
}

TTADK_PRECHECK_FAIL_PHASES = {
    "",
    "precheck_error",
}


# 统一日志模板：Engine/ACP 复用（保留关键前缀 `ttadk startup model` 以兼容 grep/监控）
TTADK_STARTUP_LOG_FMT = (
    "ttadk startup model: tool=%s input_model=%s model=%s validated=%s source=%s fail_phase=%s decision=%s warnings=%s"
)
TTADK_STARTUP_LOG_RESUME_FMT = "ttadk startup model(resume): tool=%s input_model=%s model=%s validated=%s source=%s fail_phase=%s decision=%s warnings=%s"


def precheck_ttadk_startup_model(
    *,
    agent_type: str,
    cwd: str,
    model_intent: Optional[str],
    manager=None,
    startup_probe_timeout_s: Optional[float] = None,
) -> dict:
    """兼容入口（DEPRECATED）：请改用 `src.ttadk.startup_common.precheck_ttadk_startup_model`。"""
    from .startup_common import precheck_ttadk_startup_model as _pre

    return _pre(
        agent_type=agent_type,
        cwd=cwd,
        model_intent=model_intent,
        manager=manager,
        startup_probe_timeout_s=startup_probe_timeout_s,
    )


DEFAULT_TOOLS = [
    TTADKTool(name="claude", description="Claude AI Assistant"),
    TTADKTool(name="cursor", description="Cursor AI Editor"),
    TTADKTool(name="gemini", description="Google Gemini AI"),
    TTADKTool(name="codex", description="OpenAI Codex"),
    TTADKTool(name="coco", description="Coco AI Assistant"),
    TTADKTool(name="tmates", description="Tmates AI"),
    TTADKTool(name="trae", description="Trae IDE AI"),
    TTADKTool(name="opencode", description="OpenCode AI"),
]

DEFAULT_MODELS = [
    TTADKModel(name="gpt-5.2", description="GPT-5.2"),
    TTADKModel(name="gpt-4.1", description="GPT-4.1"),
    TTADKModel(name="claude-3-opus", description="Claude 3 Opus"),
    TTADKModel(name="claude-3.5-sonnet", description="Claude 3.5 Sonnet"),
    TTADKModel(name="claude-3.7-sonnet", description="Claude 3.7 Sonnet"),
    TTADKModel(name="doubao-1.5-pro", description="Doubao 1.5 Pro"),
    TTADKModel(name="gemini-2.0-pro", description="Gemini 2.0 Pro"),
    TTADKModel(name="gemini-2.5-pro", description="Gemini 2.5 Pro"),
]

TOOL_DESCRIPTIONS = {
    "claude": "Claude AI Assistant",
    "cursor": "Cursor AI Editor",
    "gemini": "Google Gemini AI",
    "codex": "OpenAI Codex",
    "coco": "Coco AI Assistant",
    "tmates": "Tmates AI",
    "trae": "Trae IDE AI",
    "opencode": "OpenCode AI",
}

MODEL_KEYS = ("models", "model_list", "available_models", "ai_models", "llm_models", "llms")
TOOL_KEYS = ("tools", "ai_tools", "providers", "toolkits")


class TTADKManager:
    def __init__(self, default_tool: Optional[str] = None, default_model: Optional[str] = None):
        self._lock = threading.Lock()
        self._current_tool: Optional[str] = default_tool
        self._current_model: Optional[str] = default_model
        self._known_tools: set[str] = set()
        self._initialized = False
        # 模型获取器（供 cache 使用；在本类中保留以兼容旧测试桩注入/spy）
        self._model_fetcher = TTADKModelFetcher()
        # 模型列表缓存（单独模块承载落盘/TTL/预热等逻辑）
        self._cache = TTADKModelCache(
            default_models=list(DEFAULT_MODELS),
            # 重要：服务侧不得写入真实 HOME。
            # cache 落盘路径由 TTADKModelCache 通过 (cwd + Settings) 统一决定。
            cache_file_path=None,
            cache_ttl_s=300.0,
            model_fetcher=self._model_fetcher,
            # 兼容测试对 `src.ttadk.manager.get_settings` 的 monkeypatch
            # 注意：测试里往往在实例创建后 monkeypatch 本模块的 `get_settings`，
            # 因此这里用 lambda 以便调用时动态解析全局名。
            get_settings_fn=lambda: get_settings(),
        )
        # 将持久化落点交给 manager 的方法（测试可 monkeypatch 该方法以禁用 HOME 写入）
        try:
            self._cache.set_persist_hook(lambda cwd=None: self._save_cache_to_file(cwd=cwd))
        except Exception:
            pass

        # 兼容字段：历史测试/外部脚本可能直接访问这些内部字段
        self._tool_models_cache = self._cache._tool_models_cache
        self._tool_models_meta = self._cache._tool_models_meta
        self._cache_time = self._cache._cache_time
        self._known_models = self._cache._known_models
        self._cache_ttl = 300
        self._cache_file_path = self._cache._cache_file_path

        # 兼容预热字段
        self._preheat_once = self._cache._preheat_once
        self._preheated_tools = self._cache._preheated_tools
        self._preheat_inflight_tools = self._cache._preheat_inflight_tools
        self._preheat_last_attempt = self._cache._preheat_last_attempt
        self._preheat_cooldown_s = self._cache._preheat_cooldown_s

        # 启动期 force_refresh 退避：避免“每次启动都 refresh→启动→失败”的抖动
        self._startup_refresh_last_attempt: dict[str, float] = {}
        self._startup_refresh_last_failure: dict[str, float] = {}

        # 运行期 invalid-model 修复冷却（每 tool 一条时间戳；实例级存储，避免跨测试/跨进程污染）
        self._runtime_invalid_model_last_ts: dict[str, float] = {}

        # 运行期命令执行器（可注入测试 runner，默认使用 TTADKRunner）
        self._command_runner = TTADKCommandRunner(get_settings_fn=lambda: get_settings())

    def set_command_runner(self, runner: TTADKCommandRunner) -> None:
        """注入命令执行器（用于测试/可控环境）。"""
        if runner is None:
            return
        try:
            self._command_runner = runner
        except Exception:
            return

    def _get_runtime_invalid_model_last_ts(self, tool_name: str) -> float:
        tool = (tool_name or "").strip().lower()
        if not tool:
            return 0.0
        with self._lock:
            try:
                return float(self._runtime_invalid_model_last_ts.get(tool, 0.0) or 0.0)
            except Exception:
                return 0.0

    def _set_runtime_invalid_model_last_ts(self, tool_name: str, ts: float) -> None:
        tool = (tool_name or "").strip().lower()
        if not tool:
            return
        with self._lock:
            try:
                self._runtime_invalid_model_last_ts[tool] = float(ts)
            except Exception:
                return

    def check_and_mark_runtime_invalid_model_repair(
        self,
        *,
        tool_name: str,
        cooldown_s: float,
        now_ts: Optional[float] = None,
    ) -> tuple[bool, float]:
        """运行期 invalid-model 自愈冷却判断。

        返回 (allowed, last_ts)。
        - allowed=True：已写入最新时间戳，可继续执行 repair/retry
        - allowed=False：命中冷却，不应再次 repair/retry
        """
        tool = (tool_name or "").strip().lower()
        if not tool:
            return True, 0.0
        try:
            cooldown = max(0.0, float(cooldown_s or 0.0))
        except Exception:
            cooldown = 0.0
        if not cooldown:
            return True, 0.0
        now = float(now_ts) if now_ts is not None else time.time()
        with self._lock:
            try:
                last = float(self._runtime_invalid_model_last_ts.get(tool, 0.0) or 0.0)
            except Exception:
                last = 0.0
            if last and (now - last) < cooldown:
                return False, last
            try:
                self._runtime_invalid_model_last_ts[tool] = now
            except Exception:
                pass
            return True, last

    def __setattr__(self, name, value):
        # 兼容测试：允许通过 `manager._model_fetcher = TTADKModelFetcher(runner=fake)` 注入可控 fetcher。
        # 由于 cache 现在持有 fetcher 的引用，这里需要同步更新 cache 内部引用。
        if name == "_model_fetcher":
            try:
                object.__setattr__(self, name, value)
            except Exception:
                return
            try:
                cache = object.__getattribute__(self, "_cache")
                if cache is not None and hasattr(cache, "set_model_fetcher"):
                    cache.set_model_fetcher(value)
            except Exception:
                pass
            return
        return object.__setattr__(self, name, value)

    def _resolve_in_models_list(self, models: list[TTADKModel], model_name: str) -> tuple[str, str]:
        """在给定 models 列表中解析 model_name，返回 (resolved_real_name, source)。

        仅做字符串匹配，不做可用性校验。
        """
        resolved = model_name
        source = "unknown"

        # 1) 精确匹配 real_name
        for m in models:
            if getattr(m, "name", "") == model_name:
                return m.name, "exact"

        # 2) 精确匹配 friendly_name
        for m in models:
            if getattr(m, "friendly_name", "") and m.friendly_name == model_name:
                return m.name, "friendly"

        # 3) 前缀匹配（real 或 friendly）
        for m in models:
            n = getattr(m, "name", "") or ""
            fn = getattr(m, "friendly_name", "") or ""
            if n.startswith(model_name) or (fn and fn.startswith(model_name)):
                return m.name, "prefix"

        # 4) 包含匹配
        for m in models:
            n = getattr(m, "name", "") or ""
            fn = getattr(m, "friendly_name", "") or ""
            if (model_name and model_name in n) or (fn and model_name and model_name in fn):
                return m.name, "partial"

        return resolved, source

    def seed_models_from_error(self, tool_name: str, error_text: str) -> list[str]:
        """从错误文本中提取 Available models 并回灌缓存/落盘。"""
        self._ensure_initialized()
        return self._cache.seed_models_from_error(tool_name, error_text)

    def seed_models_from_invalid_model_runtime(
        self,
        *,
        tool_name: str,
        input_model: str,
        available_models: list[str],
        source: str = "runtime_invalid_model_seed",
    ) -> list[str]:
        """运行期 Invalid model 闭环：将 available_models 回灌到缓存/落盘。

        约束：
        - 仅写入“像真实模型 token”的 name（由 TTADKModel 直接承载）
        - 标记 meta.source=runtime_invalid_model_seed，便于诊断
        """
        self._ensure_initialized()
        return self._cache.seed_models_from_invalid_model_runtime(
            tool_name=tool_name, available_models=available_models, source=source
        )

    def resolve_startup_model(
        self,
        model_name: str,
        *,
        tool_name: str,
        cwd: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> ResolvedModelResult:
        """启动前的“快速解析”入口（兼容保留，较保守）。

        重要：策略选择/外部命令探测的 SSOT 在 `src.ttadk.model_fetcher`，因此该“快路径”不再直接执行 probe。

        当前语义：
        - 优先使用内存/项目落盘缓存（若有效）
        - 缓存未命中则返回 validated=False（并提示 no_m_passthrough），由上层/慢路径按需触发 fetcher 策略链
        """
        self._ensure_initialized()
        tool = (tool_name or "").strip().lower()
        if not tool:
            return ResolvedModelResult(
                tool_name="",
                input_name=model_name,
                real_name=model_name,
                source="unknown",
                validated=False,
                warnings=["missing_tool"],
            )

        # best-effort: load project-level persisted cache before quick-path decisions.
        # Motivation: allow previously discovered *real* model IDs (project cache) to
        # participate in startup validation without touching real HOME.
        try:
            self._cache.load_from_file_for_project(cwd=cwd)
        except Exception:
            pass

        # 允许用 settings.ttadk_preheat_timeout 作为默认快速探测超时
        if timeout_s is None:
            try:
                timeout_s = float(getattr(get_settings(), "ttadk_preheat_timeout", 2.5) or 2.5)
            except Exception:
                timeout_s = 2.5

        models: list[TTADKModel] = []
        cache_ok = False
        cache_untrusted = False
        with self._lock:
            cache_ok = self._cache._is_cache_valid(tool) and bool(self._tool_models_cache.get(tool))
            if cache_ok:
                models = list(self._tool_models_cache.get(tool, []) or [])
                # 启动期安全策略：cache 命中也必须考虑可信度（避免跨项目缓存误判 validated=True 并透传 -m）。
                meta = dict(self._tool_models_meta.get(tool, {}) or {})
                ws = list(meta.get("warnings") or [])
                cache_untrusted = any(w in ("low_confidence", "source_cross_project", "models_untrusted") for w in ws)

        # NOTE: 旧实现会在 cache miss 时执行一次 probe。
        # 该探测已收敛到 fetcher/cache 的慢路径（SSOT），此处不再旁路执行外部命令。

        available_set = {m.name for m in models if m and getattr(m, "name", None)}
        if cache_untrusted:
            # 不可信 cache：不参与 validated 判定（只允许解析 real_name，但禁止透传 -m）
            available_set = set()
        real, source = self._resolve_in_models_list(models, model_name)
        validated = bool(available_set) and real in available_set
        warnings: list[str] = []
        if not models:
            warnings.append("models_empty")
        if cache_untrusted and "models_untrusted" not in warnings:
            warnings.append("models_untrusted")
        if not validated:
            warnings.append("no_m_passthrough")

        return ResolvedModelResult(
            tool_name=tool,
            input_name=model_name,
            real_name=real,
            source=source,
            validated=validated,
            warnings=warnings,
        )

    def resolve_startup_model_with_diagnostics(
        self,
        model_name: str,
        *,
        tool_name: str,
        cwd: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> tuple[ResolvedModelResult, dict]:
        """启动前解析真实 model id，并返回可诊断信息。

        设计目标：
        - 固化解析链路：official_cli → probe → file_cache（必要时再由上层触发 force_refresh）
        - validated=True 才允许透传 -m；否则必须走 (auto)
        - diagnostics.attempts 记录每一步的来源与 warnings，便于排障与验收

        稳定约定（用于启动期是否允许透传 -m）：
        - 仅当 `resolved.validated=True` 时，上层才允许将 `resolved.real_name` 透传给 ttadk。
        - 若模型列表来源不可信（defaults / low_confidence / source_cross_project / models_empty / models_error），
          必须强制 `validated=False` 并附加 `no_m_passthrough`，由上层走 `(auto)`。
        """
        tool = (tool_name or "").strip().lower()
        intent = (model_name or "").strip()
        attempts: list[dict] = []

        def _is_untrusted_models_result(src: str, ws: list[str], count: int) -> bool:
            # 可信度约定（用于启动期是否允许透传 -m）：
            # - models_untrusted: 上层“模型列表不可用于校验”的统一信号（例如 defaults 兜底、空列表、拉取失败）。
            # - source_cross_project: 数据来自跨项目共享位置（典型为 ~/.ttadk/models_cache.json）。
            # - low_confidence: 低置信来源（通常与 source_cross_project 绑定），必须由更可信来源（official_cli/probe/structured）验证后才允许透传。
            if (src or "") == "defaults":
                return True
            if any(
                w in ("models_untrusted", "models_empty", "models_error") or str(w).startswith("models_error")
                for w in (ws or [])
            ):
                return True
            # 低置信来源：跨项目缓存（例如 ~/.ttadk/models_cache.json）必须先通过更可信来源验证
            if any(w in ("low_confidence", "source_cross_project") for w in (ws or [])):
                return True
            if count <= 0:
                return True
            return False

        def _mark_untrusted(r: ResolvedModelResult, extra_warn: list[str]) -> ResolvedModelResult:
            # 启动安全策略：不可信时禁止透传 -m（即 validated 必须为 False）
            try:
                r.validated = False
            except Exception:
                pass
            try:
                r.warnings = list(getattr(r, "warnings", []) or []) + list(extra_warn or [])
            except Exception:
                pass
            # 兜底：提示上层不要透传 -m
            try:
                if "no_m_passthrough" not in (getattr(r, "warnings", []) or []):
                    r.warnings = list(getattr(r, "warnings", []) or []) + ["no_m_passthrough"]
            except Exception:
                pass
            return r

        # 先做一次“快路径解析”（内部可能 probe 一次，但不会走完整策略链）
        quick = self.resolve_startup_model(intent, tool_name=tool, cwd=cwd, timeout_s=timeout_s)
        attempts.append(
            {
                "phase": "quick",
                "ok": True,
                "validated": bool(getattr(quick, "validated", False)),
                "real_name": getattr(quick, "real_name", "") or intent,
                "source": getattr(quick, "source", "") or "unknown",
                "warnings": list(getattr(quick, "warnings", []) or []),
            }
        )

        # 单测/极小超时预算：避免在启动预检里执行任何外部命令（probe/official_cli/structured_sync）。
        # 语义：在极小 timeout_s 下，仅允许使用 quick 结果做保守决策（validated=False ⇒ 走 (auto)）。
        try:
            if timeout_s is not None and float(timeout_s or 0.0) <= 0.05:
                attempts.append({"phase": "models", "ok": False, "reason": "timeout_budget_skip"})
                # quick 可能来自不可信 cache；此时必须保持 validated=False（见 resolve_startup_model 的 cache_untrusted 逻辑）
                return quick, {"attempts": attempts}
        except Exception:
            pass

        # 慢路径：获取“真实模型列表”（默认顺序 official_cli→probe→file_cache→structured）
        models_result = None
        try:
            # 启动期：优先尝试 probe（高可信、直出 available models），避免 file_cache/local_config 的低置信来源误导。
            # get_models 内部会通过 model_fetcher 执行策略链。
            try:
                fetch_result = self._model_fetcher.fetch_tool_models_with_diagnostics(
                    tool,
                    cwd=cwd,
                    force_refresh=False,
                    prefer_probe=True,
                )
                if fetch_result.models:
                    # 复用 get_models 的缓存落点逻辑（meta/warnings/落盘）
                    with self._lock:
                        self._tool_models_cache[tool] = list(fetch_result.models)
                        self._cache_time[tool] = time.time()
                        self._known_models.update(m.name for m in fetch_result.models)
                        self._tool_models_meta[tool] = {
                            "source": str(fetch_result.source or ""),
                            "warnings": list(fetch_result.diagnostics.warnings or []),
                        }
                        try:
                            self._save_cache_to_file(cwd=cwd)
                        except Exception:
                            pass
                    models_result = ModelListResult(
                        models=list(fetch_result.models),
                        cached=False,
                        source=fetch_result.source or "unknown",
                        warnings=list(fetch_result.diagnostics.warnings or []),
                        diagnostics={
                            "chosen_strategy": fetch_result.diagnostics.chosen_strategy,
                            "attempts": list(fetch_result.diagnostics.attempts),
                        },
                    )
                else:
                    models_result = self.get_models(cwd=cwd, tool_name=tool, force_refresh=False)
            except Exception:
                models_result = self.get_models(cwd=cwd, tool_name=tool, force_refresh=False)
        except Exception as e:
            models_result = None
            attempts.append({"phase": "models", "ok": False, "error_type": type(e).__name__})

        models = list(getattr(models_result, "models", []) or []) if models_result else []
        src = (getattr(models_result, "source", "") or "unknown") if models_result else "unknown"
        ws = list(getattr(models_result, "warnings", []) or []) if models_result else []
        attempts.append(
            {
                "phase": "models",
                "ok": True if models_result is not None else False,
                "source": src,
                "count": len(models),
                "warnings": ws,
            }
        )

        untrusted = _is_untrusted_models_result(src, ws, len(models))

        # 若 quick 已 validated 且模型列表可信，则直接返回 quick
        if bool(getattr(quick, "validated", False)) and not untrusted:
            return quick, {"attempts": attempts}

        # 若不可信：尝试一次带冷却的 force_refresh
        refreshed = None
        now = time.time()
        cooldown_s = 0.0
        fail_cooldown_s = 0.0
        try:
            settings = get_settings()
            cooldown_s = float(getattr(settings, "ttadk_startup_refresh_cooldown_s", 60.0) or 60.0)
            fail_cooldown_s = float(getattr(settings, "ttadk_startup_refresh_fail_cooldown_s", 120.0) or 120.0)
        except Exception:
            cooldown_s, fail_cooldown_s = 60.0, 120.0
        cooldown_s = max(0.0, cooldown_s)
        fail_cooldown_s = max(0.0, fail_cooldown_s)

        last_attempt = float(self._startup_refresh_last_attempt.get(tool, 0.0) or 0.0)
        last_fail = float(self._startup_refresh_last_failure.get(tool, 0.0) or 0.0)

        allow_refresh = True
        if cooldown_s and last_attempt and (now - last_attempt) < cooldown_s:
            allow_refresh = False
            attempts.append({"phase": "force_refresh", "ok": False, "reason": "cooldown", "cooldown_s": cooldown_s})
        if fail_cooldown_s and last_fail and (now - last_fail) < fail_cooldown_s:
            allow_refresh = False
            attempts.append(
                {"phase": "force_refresh", "ok": False, "reason": "fail_cooldown", "cooldown_s": fail_cooldown_s}
            )

        if untrusted and allow_refresh:
            self._startup_refresh_last_attempt[tool] = now
            try:
                refreshed = self.refresh_models(tool_name=tool, cwd=cwd)
                attempts.append(
                    {"phase": "force_refresh", "ok": True, "source": getattr(refreshed, "source", "") or "unknown"}
                )
            except Exception as e:
                self._startup_refresh_last_failure[tool] = time.time()
                attempts.append({"phase": "force_refresh", "ok": False, "error_type": type(e).__name__})

        # 最终解析：强校验 require_valid=True
        resolved = self.resolve_real_model_name(model_name=intent, tool_name=tool, cwd=cwd, require_valid=True)
        attempts.append(
            {
                "phase": "resolve",
                "ok": True,
                "validated": bool(getattr(resolved, "validated", False)),
                "real_name": getattr(resolved, "real_name", "") or intent,
                "source": getattr(resolved, "source", "") or "unknown",
                "warnings": list(getattr(resolved, "warnings", []) or []),
            }
        )

        # 如果模型列表仍不可信，则强制不透传 -m
        if untrusted:
            extra = ["models_untrusted"]
            if ws:
                extra.extend([f"models_warn:{w}" for w in ws])
            if refreshed is not None:
                extra.append(f"refreshed:{getattr(refreshed, 'source', '') or 'unknown'}")
                try:
                    extra.extend([f"refresh_warn:{w}" for w in (getattr(refreshed, "warnings", []) or [])])
                except Exception:
                    pass
            resolved = _mark_untrusted(resolved, extra)

        return resolved, {"attempts": attempts}

    def _preheat_probe_and_cache(self, tool: str, *, cwd: Optional[str], timeout: float, reason: str) -> bool:
        """兼容入口：预热实现已迁移到 `src.ttadk.cache`。"""
        self._ensure_initialized()
        try:
            return bool(self._cache._preheat_probe_and_cache(tool, cwd=cwd, timeout=timeout, reason=reason))
        except Exception:
            return False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            settings = get_settings()
            if not self._current_tool:
                self._current_tool = settings.ttadk_default_tool or "coco"
            if not self._current_model:
                self._current_model = settings.ttadk_default_model or None
            # 重要：避免在 init 阶段触发磁盘读/写（尤其是 legacy HOME 路径）。
            # 磁盘缓存加载下沉到 TTADKModelCache.get_models(cwd=...) 的按需路径。
            self._initialized = True

    def _parse_preheat_tools(self, raw: str) -> list[str]:
        raw = (raw or "").strip()
        if not raw:
            return []
        # 支持逗号/空白分隔
        parts: list[str] = []
        for chunk in raw.replace(",", " ").split():
            name = (chunk or "").strip().lower()
            if name:
                parts.append(name)
        # 去重保序
        seen = set()
        out: list[str] = []
        for x in parts:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def maybe_preheat_common_models(self, cwd: Optional[str] = None) -> None:
        """常用工具最小可用性预热：probe 一次模型列表并写入缓存（best-effort）。

        - 可通过 Settings 开关控制
        - 只执行一次（进程生命周期内）
        - 不阻塞主流程：内部用短超时 + 异常吞掉
        """
        self._ensure_initialized()
        return self._cache.maybe_preheat_common_models(cwd=cwd)

    def maybe_preheat_tool_models(self, tool_name: str, cwd: Optional[str] = None) -> None:
        """首次使用某 tool 时触发单工具预热（best-effort）。"""
        self._ensure_initialized()
        return self._cache.maybe_preheat_tool_models(tool_name, cwd=cwd)

    def kickoff_preheat_common_models(self, cwd: Optional[str] = None) -> None:
        """后台触发 common tools 预热（不阻塞主流程）。"""
        self._ensure_initialized()
        return self._cache.kickoff_preheat_common_models(cwd=cwd)

    def kickoff_preheat_tool_models(self, tool_name: str, cwd: Optional[str] = None) -> None:
        """后台触发单工具预热（不阻塞主流程）。"""
        self._ensure_initialized()
        return self._cache.kickoff_preheat_tool_models(tool_name, cwd=cwd)

    def _load_cache_from_file(self, *, cwd: Optional[str] = None) -> None:
        """兼容入口：历史测试会 monkeypatch 该方法以避免触碰 HOME。"""
        # 兼容测试 monkeypatch `manager._cache_file_path`：同步到 cache 实例。
        try:
            p = getattr(self, "_cache_file_path", None)
            if p is not None and hasattr(self._cache, "_cache_file_path"):
                self._cache._cache_file_path = p
        except Exception:
            pass
        try:
            if hasattr(self._cache, "load_from_file_for_project"):
                return self._cache.load_from_file_for_project(cwd=cwd)
        except Exception:
            pass
        return self._cache.load_from_file()

    def _save_cache_to_file(self, *, cwd: Optional[str] = None) -> None:
        """兼容入口：历史测试会 monkeypatch 该方法以避免触碰 HOME。"""
        # 兼容测试 monkeypatch `manager._cache_file_path`：同步到 cache 实例。
        try:
            p = getattr(self, "_cache_file_path", None)
            if p is not None and hasattr(self._cache, "_cache_file_path"):
                self._cache._cache_file_path = p
        except Exception:
            pass
        try:
            return self._cache.save_to_file(cwd=cwd)
        except TypeError:
            return self._cache.save_to_file()

    def get_tools(self, cwd: Optional[str] = None, filter_available: bool = True) -> ToolListResult:
        """获取工具列表。

        Args:
            cwd: 工作目录（保留参数，兼容性）
            filter_available: 是否过滤掉不可用的工具（默认 True）
        """
        self._ensure_initialized()
        with self._lock:
            try:
                # 先尝试过滤
                tools = self._load_tools(filter_available=filter_available)
                # 如果过滤后没有可用工具，回退到未过滤列表
                if not tools and filter_available:
                    logger.warning("No tools available after filtering, falling back to full list")
                    tools = self._load_tools(filter_available=False)
                return ToolListResult(tools=list(tools), cached=False)
            except Exception as e:
                logger.error("Failed to load TTADK tools: %s", e)
                return ToolListResult(
                    tools=list(DEFAULT_TOOLS),
                    cached=False,
                    error=str(e),
                )

    def _check_tool_available(self, tool_name: str) -> bool:
        """检查工具是否在系统中可用（使用 which 命令）。

        对于某些工具（如 coco、claude），使用特殊的可用性检查逻辑。
        """
        # 工具名到可执行文件的映射
        tool_executables = {
            "claude": "claude",
            "cursor": "cursor",
            "gemini": "gemini",
            "codex": "codex",
            "coco": "coco",
            "tmates": "tmates",
            "trae": "trae",
            "opencode": "opencode",
        }

        executable = tool_executables.get(tool_name, tool_name)
        try:
            result = shutil.which(executable)
            return result is not None
        except Exception:
            return False

    def _load_tools(self, filter_available: bool = True) -> list[TTADKTool]:
        """加载工具列表。

        Args:
            filter_available: 是否过滤掉不可用的工具（默认 True）
        """
        tools = []
        tool_names = self._load_tool_names_from_settings()
        if not tool_names:
            tool_names = [t.name for t in DEFAULT_TOOLS]
        self._known_tools = {str(name) for name in tool_names}
        for name in tool_names:
            # 检查工具是否可用
            if filter_available and not self._check_tool_available(name):
                logger.debug("Tool %s is not available on this system, skipping", name)
                continue
            tools.append(
                TTADKTool(
                    name=name,
                    description=TOOL_DESCRIPTIONS.get(name, "AI Tool"),
                    is_default=(name == self._current_tool),
                )
            )
        return tools

    def get_current_tool(self) -> Optional[str]:
        self._ensure_initialized()
        with self._lock:
            return self._current_tool

    def set_tool(self, tool_name: str) -> bool:
        self._ensure_initialized()
        with self._lock:
            configured = set(self._load_tool_names_from_settings())
            known_names = {t.name for t in DEFAULT_TOOLS} | self._known_tools | configured
            if tool_name not in known_names:
                logger.warning("Unknown tool: %s", tool_name)
                return False
            self._current_tool = tool_name
            logger.info("Switched TTADK tool to: %s", tool_name)
            return True

    def get_models(
        self,
        cwd: Optional[str] = None,
        tool_name: Optional[str] = None,
        force_refresh: bool = False,
    ) -> ModelListResult:
        """获取当前工具或指定工具的模型列表"""
        self._ensure_initialized()
        current_tool = tool_name or self._current_tool

        if not current_tool:
            return ModelListResult(
                models=list(DEFAULT_MODELS), source="defaults", warnings=["models_untrusted", "missing_tool"]
            )

        result = self._cache.get_models(tool_name=current_tool, cwd=cwd, force_refresh=force_refresh)
        # 统一：当模型列表降级为 defaults 时，输出结构化摘要（便于排障与验收）。
        try:
            if (getattr(result, "source", "") or "") == "defaults":
                diag = result.diagnostics if isinstance(getattr(result, "diagnostics", None), dict) else {}
                logger.warning(
                    "[TTADK] models_fallback: tool=%s source=%s exit_code=%s stderr_snippet=%s freshness=%s",
                    str(current_tool),
                    str(getattr(result, "source", "") or ""),
                    (diag or {}).get("exit_code"),
                    str((diag or {}).get("stderr_snippet") or "")[:240],
                    (diag or {}).get("freshness"),
                )
        except Exception:
            pass
        return result

    def resolve_model_intent_ssot(
        self,
        *,
        tool_name: str,
        model_intent: str,
        cwd: Optional[str] = None,
        force_refresh: bool = False,
        require_valid: bool = False,
    ) -> tuple[ResolvedModelResult, ModelListResult]:
        """SSOT：模型列表获取 + 真名解析 + 校验/降级（对外统一入口）。

        返回 (resolved, models_result)。
        - models_result.diagnostics：保证包含 source/raw_cmd/exit_code/stderr_snippet/freshness
        - resolved.validated：仅当模型列表可信且 real_name 在可用集合中时为 True
        """
        self._ensure_initialized()
        tool = (tool_name or "").strip().lower()
        intent = (model_intent or "").strip()
        if not tool:
            mr = ModelListResult(
                models=list(DEFAULT_MODELS), source="defaults", warnings=["models_untrusted", "missing_tool"]
            )
            # 兜底：补齐 diagnostics（不依赖 cache 内部状态）
            try:
                mr.diagnostics = build_model_list_diagnostics(
                    source=str(getattr(mr, "source", "") or "defaults"),
                    cached=bool(getattr(mr, "cached", False)),
                    cache_ts=None,
                    ttl_s=0.0,
                    chosen_strategy=str(
                        (
                            (getattr(mr, "diagnostics", None) or {})
                            if isinstance(getattr(mr, "diagnostics", None), dict)
                            else {}
                        ).get("chosen_strategy")
                        or ""
                    ),
                    attempts=list(
                        (
                            (getattr(mr, "diagnostics", None) or {})
                            if isinstance(getattr(mr, "diagnostics", None), dict)
                            else {}
                        ).get("attempts")
                        or []
                    ),
                )
            except Exception:
                pass
            return (
                ResolvedModelResult(
                    tool_name=tool,
                    input_name=intent,
                    real_name=intent,
                    source="unknown",
                    validated=False,
                    warnings=["missing_tool", "models_untrusted", "no_m_passthrough"],
                ),
                mr,
            )

        # 1) 获取模型列表（唯一入口）
        mr = self.get_models(cwd=cwd, tool_name=tool, force_refresh=force_refresh)

        # 2) 统一补齐 diagnostics（兼容历史返回的 diagnostics={chosen_strategy,attempts}）
        try:
            diag = mr.diagnostics if isinstance(getattr(mr, "diagnostics", None), dict) else {}
            chosen = str((diag or {}).get("chosen_strategy") or "")
            atts = list((diag or {}).get("attempts") or [])
            # cache.get_models 已尽量生成完整 diagnostics；这里再兜底一次保证契约
            mr.diagnostics = build_model_list_diagnostics(
                source=str(getattr(mr, "source", "") or ""),
                cached=bool(getattr(mr, "cached", False)),
                cache_ts=None,
                ttl_s=float(getattr(self._cache, "_cache_ttl_s", 0.0) or 0.0),
                chosen_strategy=chosen,
                attempts=atts,
            )
        except Exception:
            pass

        # 3) 解析真实名（复用现有匹配逻辑）
        resolved = self.resolve_real_model_name(
            model_name=intent,
            tool_name=tool,
            cwd=cwd,
            require_valid=False,
        )

        # 4) 基于可信度决定 validated/透传策略
        warnings: list[str] = list(getattr(resolved, "warnings", []) or [])
        src = str(getattr(mr, "source", "") or "")
        ws = list(getattr(mr, "warnings", []) or [])
        low_conf = any(w in ("low_confidence", "source_cross_project") for w in ws)

        # defaults/低置信来源不允许 validated=True
        if src == "defaults" or any(w in ("models_untrusted", "models_empty", "models_error") for w in ws) or low_conf:
            try:
                resolved.validated = False
            except Exception:
                pass
            if "models_untrusted" not in warnings:
                warnings.append("models_untrusted")
            if "no_m_passthrough" not in warnings:
                warnings.append("no_m_passthrough")

        # require_valid：在可信列表中强制选一个可用模型（仍遵守“不可信不透传”）
        if require_valid and bool(getattr(resolved, "validated", False)) is False:
            try:
                available = {m.name for m in (mr.models or []) if m and getattr(m, "name", None)}
            except Exception:
                available = set()
            if available and (src != "defaults") and (not low_conf) and ("models_untrusted" not in ws):
                if resolved.real_name not in available:
                    warnings.append("model_not_available")
                    try:
                        fallback = None
                        if self._current_model and self._current_model in available:
                            fallback = self._current_model
                        elif mr.models:
                            fallback = mr.models[0].name
                        if fallback:
                            resolved.real_name = fallback
                            resolved.source = "fallback"
                            resolved.validated = True
                    except Exception:
                        pass

        try:
            resolved.warnings = warnings
        except Exception:
            pass

        return resolved, mr

    def resolve_real_model_name(
        self,
        model_name: str,
        tool_name: Optional[str] = None,
        cwd: Optional[str] = None,
        require_valid: bool = False,
    ) -> ResolvedModelResult:
        """将用户输入的模型名（可能是友好名/短名）解析为真实模型 ID，并可选做可用性校验。

        - require_valid=False：尽量解析；找不到则原样返回（兼容旧行为）
        - require_valid=True：若解析结果不在可用集合中，则降级到一个确定可用的模型并输出 warnings
        """
        self._ensure_initialized()
        target_tool = tool_name or self._current_tool
        if not target_tool:
            return ResolvedModelResult(
                tool_name="",
                input_name=model_name,
                real_name=model_name,
                source="unknown",
                validated=False,
                warnings=["missing_tool"],
            )

        # 触发加载模型列表（优先使用 cwd 的结构化 sync，如果调用方提供）
        models_result = self.get_models(cwd=cwd, tool_name=target_tool)
        models = list(models_result.models or [])
        available_set = {m.name for m in models if m and getattr(m, "name", None)}

        # defaults 仅作为 UI 兜底：不参与 require_valid 的“可用集合”判断。
        # 否则会把并不存在于 TTADK 的友好名/短名误判为可用，导致后续 ttadk code -m 仍报 Invalid model。
        low_conf = any(w in ("low_confidence", "source_cross_project") for w in (models_result.warnings or []))

        if (models_result.source or "") == "defaults" or low_conf:
            # defaults / 低置信来源都不参与“可用集合”判断：避免误判 validated=True 从而透传 -m
            available_set = set()

        warnings: list[str] = []
        if models_result.error:
            warnings.append(f"models_error:{models_result.error}")
        if not models:
            warnings.append("models_empty")
        if (models_result.source or "") == "defaults" or low_conf:
            if "models_untrusted" not in warnings:
                warnings.append("models_untrusted")

        resolved = model_name
        source = "unknown"

        # 1) 精确匹配 real_name
        for m in models:
            if m.name == model_name:
                resolved = m.name
                source = "exact"
                break
        else:
            # 2) 精确匹配 friendly_name
            for m in models:
                if m.friendly_name and m.friendly_name == model_name:
                    resolved = m.name
                    source = "friendly"
                    break
            else:
                # 3) 前缀匹配（real 或 friendly）
                for m in models:
                    if m.name.startswith(model_name) or (m.friendly_name and m.friendly_name.startswith(model_name)):
                        resolved = m.name
                        source = "prefix"
                        break
                else:
                    # 4) 包含匹配
                    for m in models:
                        if model_name in m.name or (m.friendly_name and model_name in m.friendly_name):
                            resolved = m.name
                            source = "partial"
                            break

        validated = bool(available_set) and resolved in available_set

        # 可选：强制可用性
        if require_valid and available_set and not validated:
            warnings.append("model_not_available")
            # 优先：若当前模型在列表中则用当前；否则取列表第一个
            fallback = None
            if self._current_model and self._current_model in available_set:
                fallback = self._current_model
            elif models:
                fallback = models[0].name
            if fallback:
                resolved = fallback
                source = "fallback"
                validated = True

        return ResolvedModelResult(
            tool_name=target_tool,
            input_name=model_name,
            real_name=resolved,
            source=source,
            validated=validated,
            warnings=warnings,
        )

    def refresh_models(self, tool_name: Optional[str] = None, cwd: Optional[str] = None) -> ModelListResult:
        """强制刷新指定工具的模型列表（优先 structured/probe，必要时 interactive）。"""
        self._ensure_initialized()
        target_tool = tool_name or self._current_tool
        if not target_tool:
            return ModelListResult(models=list(DEFAULT_MODELS), source="defaults", warnings=["missing_tool"])

        # 直接走 get_models(force_refresh=True)
        return self.get_models(cwd=cwd, tool_name=target_tool, force_refresh=True)

    def get_acceptance_baseline(self, tool_name: Optional[str] = None, friendly_model: str = "gpt-5.2") -> dict:
        """返回可执行且可验证的“验收基线命令集”（用于诊断/人工验收）。

        判定规则：stdout/stderr 不得包含 `Invalid model`（忽略大小写）。
        注意：该方法不执行外部命令，仅输出命令与规则。
        """
        tool = (tool_name or self._current_tool or "").strip().lower() or "coco"

        return {
            "tool": tool,
            "commands": [
                f"ttadk code -t {tool} -m {friendly_model}",
                f"ttadk code -t {tool}",
            ],
            "criteria": {
                "forbid_patterns": ["invalid model"],
                "description": "stdout/stderr 不包含 'Invalid model'（忽略大小写）",
            },
        }

    def resolve_and_ensure_valid_model(
        self,
        model_name: str,
        tool_name: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> ResolvedModelResult:
        """执行前强校验：解析真实模型名；若不可用/列表不可信则强制刷新一次再解析。

        注意：该方法属于“执行阶段强校验/纠错”能力。
        启动阶段“是否透传 -m”的决策必须走 `resolve_startup_model_with_diagnostics()`，
        避免不同调用方各自做 refresh/兜底造成行为漂移。
        """
        target_tool = tool_name or self._current_tool
        if not target_tool:
            return ResolvedModelResult(
                tool_name="",
                input_name=model_name,
                real_name=model_name,
                source="unknown",
                validated=False,
                warnings=["missing_tool"],
            )

        first = self.resolve_real_model_name(
            model_name=model_name,
            tool_name=target_tool,
            cwd=cwd,
            require_valid=True,
        )

        # 若模型列表来自 defaults，则认为“不可信”，优先强制刷新一次。
        # 这是为了避免：UI 兜底列表误导为可用模型，从而在 ttadk code -m 时触发 Invalid model。
        try:
            models_result = self.get_models(cwd=cwd, tool_name=target_tool)
            if (models_result.source or "") == "defaults" or any(
                w in ("low_confidence", "source_cross_project") for w in (models_result.warnings or [])
            ):
                if "models_untrusted" not in (first.warnings or []):
                    first.warnings = list(first.warnings or []) + ["models_untrusted"]
        except Exception:
            pass

        # 触发条件：未校验通过 或 明显不可信（models_empty/models_error/models_untrusted）
        need_refresh = (not first.validated) or any(
            w in ("models_empty", "models_error", "models_untrusted") or w.startswith("models_error:")
            for w in (first.warnings or [])
        )

        if not need_refresh:
            return first

        refreshed = self.refresh_models(tool_name=target_tool, cwd=cwd)
        second = self.resolve_real_model_name(
            model_name=model_name,
            tool_name=target_tool,
            cwd=cwd,
            require_valid=True,
        )
        # 将刷新信息附加到 warnings
        extra = []
        if refreshed.source:
            extra.append(f"refreshed:{refreshed.source}")
        if refreshed.warnings:
            extra.extend([f"refresh_warn:{w}" for w in refreshed.warnings])
        second.warnings = list(second.warnings or []) + extra
        return second

    def get_real_model_name(self, friendly_name: str, tool_name: Optional[str] = None) -> str:
        """兼容旧接口：仅返回解析后的 real_name（找不到则原样返回）。"""
        result = self.resolve_real_model_name(
            model_name=friendly_name,
            tool_name=tool_name,
            cwd=None,
            require_valid=False,
        )
        return result.real_name

    def invalidate_model_cache(self, tool_name: Optional[str] = None) -> None:
        """使模型缓存失效"""
        self._ensure_initialized()
        self._cache.invalidate(tool_name)
        try:
            self._model_fetcher.invalidate_cache(tool_name)
        except Exception:
            try:
                self._model_fetcher.invalidate_cache()
            except Exception:
                pass
        # 重要：服务侧不得触碰真实 HOME，因此这里不做磁盘删除。
        # 磁盘缓存路径由 (cwd + Settings) 决定，调用方若需清理某项目缓存应显式传入目标路径并删除。
        return

    def _load_tool_names_from_settings(self) -> list[str]:
        path = Path.home() / ".ttadk" / "setting.json"
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            commands = data.get("ai_tool_commands")
            if isinstance(commands, dict):
                return [str(k) for k in commands.keys()]
        except Exception as e:
            logger.debug("Failed to read ttadk setting.json: %s", e)
        return []

    def _extract_models_from_sync(
        self,
        data: object,
        tool_name: Optional[str],
        current_model: Optional[str],
    ) -> list[TTADKModel]:
        # Tool-specific lookup
        if isinstance(data, dict) and tool_name:
            for key in TOOL_KEYS:
                container = data.get(key)
                models = self._extract_models_from_tool_container(container, tool_name, current_model)
                if models:
                    return models

        # Fallback: search anywhere
        models = self._extract_models_from_container(data, current_model, under_model_key=False)
        return models

    def _extract_models_from_tool_container(
        self,
        container: object,
        tool_name: str,
        current_model: Optional[str],
    ) -> list[TTADKModel]:
        if isinstance(container, dict):
            if tool_name in container:
                return self._extract_models_from_container(
                    container.get(tool_name),
                    current_model,
                    under_model_key=False,
                )
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tool") or item.get("id")
                    if name == tool_name:
                        return self._extract_models_from_container(
                            item,
                            current_model,
                            under_model_key=False,
                        )
        return []

    def _extract_models_from_container(
        self,
        container: object,
        current_model: Optional[str],
        under_model_key: bool,
    ) -> list[TTADKModel]:
        if isinstance(container, dict):
            for key in MODEL_KEYS:
                if key in container:
                    models = self._normalize_models(container.get(key), current_model)
                    if models:
                        return models
            for value in container.values():
                models = self._extract_models_from_container(value, current_model, under_model_key=False)
                if models:
                    return models
        elif isinstance(container, list):
            if under_model_key:
                models = self._normalize_models(container, current_model)
                if models:
                    return models
            for item in container:
                models = self._extract_models_from_container(item, current_model, under_model_key=False)
                if models:
                    return models
        return []

    def _normalize_models(self, raw: object, current_model: Optional[str]) -> list[TTADKModel]:
        if isinstance(raw, list):
            if raw and all(isinstance(x, str) for x in raw):
                return [TTADKModel(name=name, description=name, is_default=(name == current_model)) for name in raw]
            models: list[TTADKModel] = []
            for item in raw:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("id") or item.get("model") or item.get("model_name")
                    if not name:
                        continue
                    desc = item.get("description") or item.get("label") or str(name)
                    models.append(
                        TTADKModel(
                            name=str(name),
                            description=str(desc),
                            is_default=(str(name) == current_model),
                        )
                    )
            return models
        if isinstance(raw, dict):
            # Map of name -> details
            models: list[TTADKModel] = []
            for name, item in raw.items():
                if isinstance(item, dict):
                    desc = item.get("description") or item.get("label") or str(name)
                else:
                    desc = str(item)
                models.append(
                    TTADKModel(
                        name=str(name),
                        description=str(desc),
                        is_default=(str(name) == current_model),
                    )
                )
            return models
        return []

    def get_current_model(self) -> Optional[str]:
        self._ensure_initialized()
        with self._lock:
            return self._current_model

    def set_model(self, model_name: str) -> bool:
        self._ensure_initialized()
        with self._lock:
            # 兼容：允许传入 display/alias，但内部始终保存真实 model_id
            target_tool = (self._current_tool or "").strip().lower()
            if not target_tool:
                logger.warning("Unknown model (missing tool): %s", model_name)
                return False

            # 从当前 tool 的模型列表构造 descriptors（不强制刷新，best-effort）
            try:
                mr = self.get_models(cwd=None, tool_name=target_tool, force_refresh=False)
                descriptors = list(getattr(mr, "models", []) or [])
            except Exception:
                descriptors = []

            resolved, diag = resolve_model_id(
                tool_name=target_tool, input_name=str(model_name or ""), descriptors=descriptors
            )
            mid = str(getattr(resolved, "real_name", "") or "").strip()
            if not mid:
                logger.warning("Unknown model: %s", model_name)
                return False

            # 若解析是 unknown 且没有候选，则拒绝（避免把 display 误保存为 model_id）
            if str(getattr(resolved, "source", "") or "") == "unknown":
                cands = (diag or {}).get("candidates") if isinstance(diag, dict) else None
                if not cands:
                    logger.warning("Unknown model: %s", model_name)
                    return False

            self._current_model = mid

            # best-effort：记录 display（不作为 SSOT，仅用于提示/诊断）
            try:
                self._current_model_display = str((diag or {}).get("model_display") or model_name)
            except Exception:
                pass

            try:
                logger.info(
                    "Switched TTADK model to: %s (display=%s source=%s reason=%s)",
                    mid,
                    str((diag or {}).get("model_display") or ""),
                    str(getattr(resolved, "source", "") or ""),
                    str((diag or {}).get("resolution_reason") or ""),
                )
            except Exception:
                logger.info("Switched TTADK model to: %s", mid)
            return True


_manager: Optional[TTADKManager] = None
_manager_lock = threading.Lock()

_ttadk_update_attempted: bool = False
_ttadk_update_lock = threading.Lock()


def auto_update_ttadk() -> None:
    global _ttadk_update_attempted
    with _ttadk_update_lock:
        if _ttadk_update_attempted:
            return
        _ttadk_update_attempted = True

    settings = get_settings()
    if not settings.ttadk_auto_update:
        return

    def _do_upgrade() -> None:
        try:
            p = subprocess.run(
                ["ttadk", "upgrade"],
                capture_output=True,
                text=True,
                timeout=settings.ttadk_update_timeout,
            )
            if p.returncode == 0:
                logger.info("[TTADK] auto-update succeeded")
            else:
                logger.warning(
                    "[TTADK] auto-update failed (rc=%d) stderr=%s",
                    p.returncode,
                    ((p.stderr or "").strip())[-200:] or "(empty)",
                )
        except Exception as e:
            logger.warning("[TTADK] auto-update error: %s", e)

    threading.Thread(target=_do_upgrade, daemon=True, name="ttadk-auto-upgrade").start()


# 显式迁移标记：避免反复从函数属性迁移 legacy store（保持 best-effort 且不引入 import-time 副作用）。
_legacy_store_migrated: bool = False


def _maybe_migrate_legacy_store() -> None:
    """best-effort：从历史函数属性挂载点迁移 legacy store。

    说明：历史实现可能将 store 挂在 `coordinate_ttadk_startup._runtime_invalid_model_last_ts_by_stub` 上。
    该迁移仅在初始化路径中执行一次，避免任何 import-time 侧效与隐式 sys.modules 扫描。
    """
    global _legacy_store_migrated
    if _legacy_store_migrated:
        return
    _legacy_store_migrated = True
    try:
        from . import startup_common as _sc

        migrated = _sc.migrate_legacy_store_from_fn_attr(coordinate_ttadk_startup)
        if isinstance(migrated, dict):
            try:
                if not isinstance(globals().get("_LEGACY_STUB_COOLDOWN_STORE", None), dict):
                    globals()["_LEGACY_STUB_COOLDOWN_STORE"] = migrated
            except Exception:
                pass
    except Exception:
        return


def _build_stub_providers():
    """构造注入到 startup_common 的 provider。

    语义（稳定契约）：
    - legacy_store_provider 返回 dict → 使用该 store
    - 返回 None/非 dict → 视为“显式解绑 legacy store”，返回 `startup_common.LEGACY_STORE_CLEARED`
    """
    from . import startup_common as _startup_common

    def _provider_time() -> float:
        # 兼容测试 monkeypatch `src.ttadk.manager.time.time`
        try:
            return float(time.time())
        except Exception:
            return 0.0

    def _provider_get_settings() -> object:
        # 兼容测试 monkeypatch `src.ttadk.manager.get_settings`
        return get_settings()

    def _provider_legacy_store() -> object:
        # 兼容挂载点（唯一权威入口）：允许历史测试/脚本 monkeypatch 本模块变量。
        # 语义：
        # - dict：使用该 store
        # - None：视为“显式清空 legacy store 绑定”（解绑旧 dict 引用，避免 monkeypatch 清空无效）
        try:
            cand = globals().get("_LEGACY_STUB_COOLDOWN_STORE", None)
        except Exception:
            cand = None
        if isinstance(cand, dict):
            return cand
        return _startup_common.LEGACY_STORE_CLEARED

    return _provider_time, _provider_get_settings, _provider_legacy_store


def _install_compat_providers() -> None:
    try:
        from . import startup_common as _sc

        time_fn, get_settings_fn, legacy_store_provider = _build_stub_providers()
        _sc.install_compat_providers(
            time_fn=time_fn,
            get_settings_fn=get_settings_fn,
            legacy_store_provider=legacy_store_provider,
        )
    except Exception:
        return


def _get_or_create_singleton(default_tool: Optional[str], default_model: Optional[str]) -> TTADKManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TTADKManager(default_tool=default_tool, default_model=default_model)
    return _manager


def get_ttadk_manager(default_tool: Optional[str] = None, default_model: Optional[str] = None) -> TTADKManager:
    # 显式一次性初始化点：将 compat provider 的安装从“import-time 副作用”迁移到
    # 稳定入口 `get_ttadk_manager()`。
    # - best-effort：任何异常都吞掉，避免影响主流程与兼容性
    # - 幂等：由 `compat.install_compat_providers()` 内部的 lock + 标记保证
    _maybe_migrate_legacy_store()
    _install_compat_providers()
    return _get_or_create_singleton(default_tool, default_model)


def coordinate_ttadk_startup(
    *,
    manager,
    tool_name: str,
    input_model: str,
    cwd: Optional[str],
    start_fn,
    fallback_fn=None,
    startup_probe_timeout_s: Optional[float] = None,
    precheck_fn=None,
) -> dict:
    """兼容入口（DEPRECATED）：请改用 `src.ttadk.startup.coordinate_ttadk_startup`。

    说明：历史版本在此处实现了完整的启动编排主流程。
    为消除“双 SSOT”，该函数现在仅做薄封装转调到 startup 模块的权威实现，
    保持签名与返回契约不变，以避免外部脚本/测试/旧导入路径回归。
    """

    # 延迟 import：避免 import-time 循环依赖
    from .startup import coordinate_ttadk_startup as _ssot

    return _ssot(
        manager=manager,
        tool_name=tool_name,
        input_model=input_model,
        cwd=cwd,
        start_fn=start_fn,
        fallback_fn=fallback_fn,
        startup_probe_timeout_s=startup_probe_timeout_s,
        precheck_fn=precheck_fn,
    )
