"""TTADK 工具模型列表动态获取模块。

核心目标：尽量拿到“真实可用的 model id”（例如 `gpt-5.2-codex-ttadk`），用于启动前校验与日志可观测。
策略链由 `TTADKModelFetcher._select_strategies()` 统一决定，允许通过配置覆盖顺序。
"""

import json
import logging
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import re

from ..config import get_settings
from ..acp.diagnostics import get_diagnostics_config, redact_text, truncate_text

from .env_sandbox import build_ttadk_subprocess_env

from .models import TTADKModel, truncate_snippet, parse_models_cache_json
from .models import is_invalid_model_error
from .strategies import (
    ModelFetchStrategy,
    ProbeStrategy,
    InteractiveStrategy,
    OfficialCLIModelsStrategy,
    LocalConfigModelsStrategy,
    ProjectMetaModelsStrategy,
    TTADKProbeError,
    TTADKOfficialCLIError,
    TTADKLocalConfigError,
    TTADKProjectMetaError,
)

logger = logging.getLogger(__name__)


def _redacted_snippet(text: object, *, limit: int = 240) -> str:
    """统一对 stdout/stderr 片段做脱敏与截断（复用 ACP diagnostics 配置）。

    约束：best-effort，不抛异常。
    """
    try:
        cfg = get_diagnostics_config(get_settings_fn=get_settings)
        enabled = bool(cfg.redact_enabled)
        patterns = list(cfg.redact_patterns or [])
        repl = str(cfg.redact_replacement or "***REDACTED***")
        lim = int(cfg.snippet_limit or 0) or int(limit or 240)
    except Exception:
        enabled, patterns, repl, lim = True, [], "***REDACTED***", int(limit or 240)

    try:
        s = str(text or "")
    except Exception:
        s = ""
    s = truncate_text(s, int(lim or 240))
    if enabled:
        try:
            s = redact_text(s, patterns, repl)
        except Exception:
            pass
    return s


class TTADKCommandError(RuntimeError):
    """Attach stderr/stdout/returncode for diagnostics."""

    def __init__(self, message: str, returncode: Optional[int] = None, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _is_ttadk_config_missing_error(e: Exception) -> bool:
    """识别 structured_sync 在未 init/无配置时的可预期失败。

    该类失败不应导致直接回退 defaults（会造成 model 校验误判），而应降级到 probe。
    """
    msg = ""
    try:
        msg = str(e) or ""
    except Exception:
        msg = ""
    stderr = ""
    try:
        stderr = str(getattr(e, "stderr", "") or "")
    except Exception:
        stderr = ""
    hay = (msg + "\n" + stderr).lower()
    needles = [
        "config file not found",
        "please initialize the project first",
        "ttadk init",
        "not initialized",
    ]
    return any(n in hay for n in needles)


@dataclass
class FetchDiagnostics:
    tool_name: str
    attempts: list[dict] = field(default_factory=list)
    chosen_strategy: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    tool_name: str
    models: list[TTADKModel] = field(default_factory=list)
    source: str = ""
    diagnostics: FetchDiagnostics = field(default_factory=lambda: FetchDiagnostics(tool_name=""))

    def __post_init__(self) -> None:
        # 确保 diagnostics.tool_name 与 tool_name 一致，便于上层直接记录
        try:
            if not getattr(self.diagnostics, "tool_name", None):
                self.diagnostics.tool_name = self.tool_name
        except Exception:
            pass

class TTADKModelFetcher:
    """通过多种策略获取 TTADK 工具的模型列表"""

    # 缓存 TTL（秒）
    CACHE_TTL = 300

    MODEL_KEYS = ("models", "model_list", "available_models", "ai_models", "llm_models", "llms")
    TOOL_KEYS = ("tools", "ai_tools", "providers", "toolkits")

    def __init__(self, runner: Optional["TTADKRunner"] = None):
        self._cache: dict[str, list[TTADKModel]] = {}
        self._cache_time: dict[str, float] = {}
        self._runner = runner or TTADKRunner()
        settings = get_settings()
        probe_timeout = float(getattr(settings, "ttadk_probe_timeout", 10.0) or 10.0)
        structured_timeout = float(getattr(settings, "ttadk_structured_timeout", 8.0) or 8.0)
        interactive_enabled = bool(getattr(settings, "ttadk_interactive_enabled", False))

        # 官方命令策略：较短超时（以免阻塞），并允许通过 env/config 后续扩展
        try:
            official_timeout = float(getattr(settings, "ttadk_official_timeout", 4.0) or 4.0)
        except Exception:
            official_timeout = 4.0

        self._structured = StructuredSyncStrategy(self._runner, self, timeout_s=structured_timeout)
        self._official_cli = OfficialCLIModelsStrategy(runner=self._runner.run_simple, timeout_s=official_timeout)
        self._project_meta = ProjectMetaModelsStrategy(runner=self._runner.run_simple, timeout_s=structured_timeout)
        self._local_config = LocalConfigModelsStrategy()
        self._probe = ProbeStrategy(runner=self._runner.run_simple, timeout_s=probe_timeout)
        self._file_cache = FileCacheStrategy(self)
        self._interactive = InteractiveStrategy()
        # 默认策略链（SSOT，面向“拿到真实可用 model_id”）：
        # - official_cli：官方子命令（若存在），通常最接近真实可用模型列表
        # - probe：通过 Invalid model 输出反推（高可信/短超时）
        # - structured_sync：依赖项目 init/config；成功时可提供结构化的真实模型列表
        # - project_meta：从项目元数据 best-effort 提取（仅在检测到 init 迹象时启用）
        # - local_config：项目/用户侧配置文件 token 提取（项目侧相对可信；用户侧为低可信）
        # - file_cache：历史 HOME 写盘缓存（跨项目/低可信，仅兜底）
        # - interactive：高风险慢路径（默认关闭）
        self._strategies: list[ModelFetchStrategy] = [
            self._official_cli,
            self._probe,
            self._structured,
            self._project_meta,
            self._local_config,
            self._file_cache,
        ]
        if interactive_enabled:
            self._strategies.append(self._interactive)

        # Optional external cache sink (set by TTADKManager) to keep single source of truth.
        # Signature: Callable[[tool_name, models, source, diagnostics], None]
        self._cache_sink = None

        # ttadk CLI 能力探测缓存（避免频繁执行 `ttadk --help`）
        # 结构：(ts, commands_set, version_str)
        self._ttadk_cli_cap_cache: tuple[float, set[str], str] | None = None
        self._ttadk_cli_cap_lock = threading.Lock()
        self._ttadk_cli_cap_inflight: threading.Event | None = None

    def _get_ttadk_cli_capabilities(
        self,
        *,
        diag: Optional[FetchDiagnostics] = None,
        force_refresh: bool = False,
    ) -> tuple[set[str], str]:
        """探测 ttadk CLI 子命令集合与版本号（best-effort）。

        目的：在某些版本（例如 0.3.8）不存在 `models/model` 子命令时，避免 official_cli 策略的永久失败噪声。

        约束：
        - 支持 TTL 缓存
        - 支持并发去重（同一时刻最多一个线程执行 `ttadk --help`）
        - 若提供 diag，将把探测记录写入 diag.attempts（用于验收/排障）
        """
        # Settings: allow override
        ttl_s = 300.0
        timeout_s = 2.0
        try:
            s = get_settings()
            ttl_s = float(getattr(s, "ttadk_cli_capabilities_ttl_s", 300.0) or 300.0)
            timeout_s = float(getattr(s, "ttadk_cli_capabilities_timeout_s", 2.0) or 2.0)
        except Exception:
            ttl_s, timeout_s = 300.0, 2.0
        ttl_s = max(0.0, ttl_s)
        timeout_s = max(0.1, timeout_s)

        now = time.time()
        # Fast path: cache hit
        with self._ttadk_cli_cap_lock:
            cached = self._ttadk_cli_cap_cache
            if (not force_refresh) and cached and ttl_s > 0 and (now - float(cached[0] or 0.0)) < ttl_s:
                try:
                    if diag is not None:
                        diag.attempts.append(
                            {
                                "strategy": "ttadk_cli_capabilities",
                                "ok": True,
                                "cached": True,
                                "count": len(set(cached[1] or set())),
                                "exit_code": 0,
                                "raw_cmd": ["ttadk", "--help"],
                                "duration_ms": 0,
                                "timeout_ms": int(timeout_s * 1000),
                            }
                        )
                except Exception:
                    pass
                return set(cached[1] or set()), str(cached[2] or "")

            inflight = self._ttadk_cli_cap_inflight
            if inflight is None:
                inflight = threading.Event()
                self._ttadk_cli_cap_inflight = inflight
                do_run = True
            else:
                do_run = False

        if not do_run:
            # Wait for the in-flight probe; bounded wait to avoid deadlock.
            inflight.wait(timeout=float(timeout_s) + 1.0)
            with self._ttadk_cli_cap_lock:
                cached2 = self._ttadk_cli_cap_cache
                if cached2:
                    try:
                        if diag is not None:
                            diag.attempts.append(
                                {
                                    "strategy": "ttadk_cli_capabilities",
                                    "ok": True,
                                    "cached": True,
                                    "inflight_wait": True,
                                    "count": len(set(cached2[1] or set())),
                                    "exit_code": 0,
                                    "raw_cmd": ["ttadk", "--help"],
                                    "duration_ms": 0,
                                    "timeout_ms": int(timeout_s * 1000),
                                }
                            )
                    except Exception:
                        pass
                    return set(cached2[1] or set()), str(cached2[2] or "")
            # If still no cache, fall through to run ourselves (best-effort).

        start = time.time()
        commands: set[str] = set()
        version = ""
        rc_i: Optional[int] = None
        out_s = ""
        err_s = ""
        try:
            rc, out, err = self._runner.run_simple(["ttadk", "--help"], cwd=None, timeout=float(timeout_s))
            rc_i = int(rc or 0)
            out_s = str(out or "")
            err_s = str(err or "")
            blob = out_s + "\n" + err_s

            # version
            m = re.search(r"\bVersion\s+([0-9]+\.[0-9]+\.[0-9]+)\b", blob)
            if m:
                version = m.group(1)

            lines = (blob or "").splitlines()
            in_cmds = False
            for line in lines:
                s = (line or "").rstrip("\n")
                if not in_cmds:
                    if s.strip().lower().startswith("commands:"):
                        in_cmds = True
                    continue
                # 退出条件：到下一节
                if s.strip().lower().startswith("options:"):
                    break
                if not s.strip():
                    continue
                mm = re.match(r"\s{2,}([A-Za-z0-9_\-]+)\b", s)
                if mm:
                    commands.add(mm.group(1).strip().lower())

            # 若帮助输出不包含 Commands 段，保守返回空集合
            if rc_i != 0:
                commands = set()
        except Exception as e:
            rc_i = None
            out_s = out_s or ""
            err_s = err_s or (str(e) or "")
            commands = set()
            version = ""
        finally:
            duration_ms = int((time.time() - start) * 1000)
            with self._ttadk_cli_cap_lock:
                self._ttadk_cli_cap_cache = (time.time(), set(commands), str(version))
                try:
                    if self._ttadk_cli_cap_inflight is not None:
                        self._ttadk_cli_cap_inflight.set()
                finally:
                    self._ttadk_cli_cap_inflight = None

            # diagnostics attempt
            try:
                if diag is not None:
                    diag.attempts.append(
                        {
                            "strategy": "ttadk_cli_capabilities",
                            "ok": bool(commands),
                            "cached": False,
                            "count": len(commands),
                            "error_type": None if bool(commands) else ("NonZeroExit" if (rc_i not in (None, 0)) else None),
                            "exit_code": rc_i,
                            "raw_cmd": ["ttadk", "--help"],
                            "stdout_snippet": _redacted_snippet(out_s),
                            "stderr_snippet": _redacted_snippet(err_s),
                            "duration_ms": duration_ms,
                            "timeout_ms": int(timeout_s * 1000),
                        }
                    )
            except Exception:
                pass

        return set(commands), str(version)

    def _is_official_cli_enabled(
        self,
        tool_name: Optional[str] = None,
        cwd: Optional[str] = None,
        *,
        diag: Optional[FetchDiagnostics] = None,
        force_refresh: bool = False,
    ) -> bool:
        """是否启用 official_cli 策略（按版本/能力显式启用）。

        规则：仅当 `ttadk --help` 明确暴露 `models` 或 `model` 子命令时启用。
        若 `ttadk --help` 无法解析 Commands 段（commands 为空），允许降级到 official_cli 自身的 `--help` 探测。
        解析失败或缺失时一律禁用（宁可少用，避免误判）。
        """
        # 用户显式开关：允许在某些环境下直接禁用该策略（避免额外子进程/噪声）
        try:
            if not bool(getattr(get_settings(), "ttadk_official_cli_enabled", True)):
                return False
        except Exception:
            pass
        # 先用 official_cli 自身的 help 探测：避免在 force_refresh 路径引入额外的 `ttadk --help` 调用。
        # 注意：OfficialCLIModelsStrategy._probe 已包含“usage 前缀校验”，不会把顶层 help 误判为 capability。
        try:
            if hasattr(self, "_official_cli") and self._official_cli is not None:
                ok, cmd_prefix, probe_warnings, probe_last = self._official_cli._probe((tool_name or ""), cwd=cwd)

                # 记录 capability probe（best-effort）
                # 说明：
                # - _probe 命中缓存时 probe_last 为空 dict
                # - 失败时 probe_last 包含 cmd/rc/stdout/stderr
                try:
                    if diag is not None:
                        raw_cmd = None
                        try:
                            if isinstance(probe_last, dict) and probe_last.get("cmd"):
                                raw_cmd = list(probe_last.get("cmd") or [])
                        except Exception:
                            raw_cmd = None
                        diag.attempts.append(
                            {
                                "strategy": "official_cli_probe",
                                "ok": bool(ok),
                                "cached": bool(ok) and (not bool(probe_last)),
                                "count": 1 if bool(ok) else 0,
                                "exit_code": (probe_last or {}).get("rc") if isinstance(probe_last, dict) else None,
                                "raw_cmd": raw_cmd or ["ttadk", "models", "--help"],
                                "stderr_snippet": _redacted_snippet((probe_last or {}).get("stderr") if isinstance(probe_last, dict) else ""),
                                "stdout_snippet": _redacted_snippet((probe_last or {}).get("stdout") if isinstance(probe_last, dict) else ""),
                                "warnings": list(probe_warnings or []),
                                "detail": {"cmd_prefix": list(cmd_prefix or [])},
                                "duration_ms": 0,
                                "timeout_ms": int(min(2.0, float(getattr(self._official_cli, "timeout_s", 4.0) or 4.0)) * 1000),
                            }
                        )
                except Exception:
                    pass
                if ok:
                    return True
        except Exception:
            pass

        # 回退到 `ttadk --help` Commands 列表（更稳定的“显式命令能力”判断）。
        commands, _ = self._get_ttadk_cli_capabilities(diag=diag, force_refresh=bool(force_refresh))
        return bool(commands) and (("models" in commands) or ("model" in commands))

    def _select_strategies(
        self,
        *,
        tool_name: str,
        cwd: Optional[str],
        force_refresh: bool,
        prefer_probe: bool,
        diag: FetchDiagnostics,
    ) -> list[ModelFetchStrategy]:
        """根据 SSOT 规则选择并排序策略。

        重要：只从 `self._strategies` 中选择，不注入额外策略。
        这样测试可以通过覆盖 `_strategies` 来构造可控环境。
        """
        by_name: dict[str, ModelFetchStrategy] = {}
        for s in list(self._strategies or []):
            try:
                name = str(getattr(s, "name", "") or "").strip()
            except Exception:
                name = ""
            if not name:
                continue
            if name not in by_name:
                by_name[name] = s

        # 特殊：若调用方显式只提供了 official_cli（或只剩极少策略），按其配置运行
        core = {"structured_sync", "probe", "file_cache", "interactive"}
        if ("official_cli" in by_name) and not (set(by_name.keys()) & core):
            return [by_name["official_cli"]]

        ordered: list[ModelFetchStrategy] = []

        def _parse_order(raw: str) -> list[str]:
            raw = (raw or "").strip()
            if not raw:
                return []
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

        # 可配置策略顺序：不改变策略集合，只决定排序
        cfg_order: list[str] = []
        try:
            cfg_order = _parse_order(getattr(get_settings(), "ttadk_models_strategy_order", ""))
        except Exception:
            cfg_order = []

        if cfg_order:
            # 说明：
            # - official_cli 仍需通过 _is_official_cli_enabled 判定是否可用
            # - structured_sync 只有 cwd 存在才会生效（与默认逻辑一致）
            for name in cfg_order:
                s = by_name.get(name)
                if not s:
                    continue
                if name == "official_cli":
                    if self._is_official_cli_enabled(tool_name=tool_name, cwd=cwd, diag=diag, force_refresh=force_refresh):
                        ordered.append(s)
                    else:
                        try:
                            if "official_cli_disabled" not in diag.warnings:
                                diag.warnings.append("official_cli_disabled")
                        except Exception:
                            pass
                    continue
                if name == "structured_sync" and not cwd:
                    continue
                ordered.append(s)

            # 补齐未出现在 cfg_order 的策略（保底：避免用户配置遗漏导致完全不可用）。
            # SSOT 默认优先级（高可信→低可信）：
            # official_cli → probe → structured_sync → project_meta → local_config → file_cache → interactive
            fallback_order = [
                "official_cli",
                "probe",
                "structured_sync",
                "project_meta",
                "local_config",
                "file_cache",
                "interactive",
            ]
            for name in fallback_order:
                if name in cfg_order:
                    continue
                s = by_name.get(name)
                if not s:
                    continue
                if name == "official_cli":
                    if self._is_official_cli_enabled(tool_name=tool_name, cwd=cwd, diag=diag, force_refresh=force_refresh):
                        ordered.append(s)
                    else:
                        try:
                            if "official_cli_disabled" not in diag.warnings:
                                diag.warnings.append("official_cli_disabled")
                        except Exception:
                            pass
                    continue
                if name == "structured_sync" and not cwd:
                    continue
                ordered.append(s)
            return ordered

        def _maybe_add_official_cli() -> None:
            """将 official_cli 作为“高可信来源”优先加入策略链（若启用且 capability 可用）。"""
            if "official_cli" not in by_name:
                return
            try:
                enabled = self._is_official_cli_enabled(tool_name=tool_name, cwd=cwd, diag=diag, force_refresh=force_refresh)
            except Exception:
                enabled = False
            if enabled:
                ordered.append(by_name["official_cli"])
                return

            # 不可用：记录禁用信号（best-effort）。
            try:
                if "official_cli_disabled" not in diag.warnings:
                    diag.warnings.append("official_cli_disabled")
            except Exception:
                pass

            # force_refresh 路径：额外记录版本/缺失命令，便于排障与验收。
            if not force_refresh:
                return
            try:
                cmds, ver = self._get_ttadk_cli_capabilities(diag=diag, force_refresh=True)
                if ver:
                    diag.warnings.append(f"ttadk_version:{ver}")
                if cmds and ("models" not in cmds) and ("model" not in cmds):
                    diag.warnings.append("missing_commands:models,model")
            except Exception:
                return

        # 默认链路（SSOT）：优先高可信来源，避免低置信来源阻断。
        # official_cli → probe → structured_sync → project_meta → local_config → file_cache → interactive
        _maybe_add_official_cli()

        if "probe" in by_name:
            ordered.append(by_name["probe"])
        if cwd and ("structured_sync" in by_name):
            ordered.append(by_name["structured_sync"])
        if cwd and ("project_meta" in by_name):
            ordered.append(by_name["project_meta"])
        if "local_config" in by_name:
            ordered.append(by_name["local_config"])
        if "file_cache" in by_name:
            ordered.append(by_name["file_cache"])
        if "interactive" in by_name:
            ordered.append(by_name["interactive"])
        return ordered

    def set_cache_sink(self, sink) -> None:
        """Set an external cache sink to persist fetched models (best-effort)."""
        self._cache_sink = sink

    def fetch_tool_models(self, tool_name: str, force_refresh: bool = False) -> list[TTADKModel]:
        """兼容旧接口：仅返回模型列表，不返回诊断信息。"""
        return self.fetch_tool_models_with_diagnostics(
            tool_name=tool_name,
            cwd=None,
            force_refresh=force_refresh,
        ).models

    def probe_tool_models(
        self,
        tool_name: str,
        cwd: Optional[str] = None,
        timeout: float = 2.5,
    ) -> list[TTADKModel]:
        """轻量 probe：用无效 model 触发 Invalid model 输出，解析 Available models。

        设计目标：
        - 不依赖 interactive/structured
        - 可配置超时，适合启动/首次使用预热
        - best-effort：失败返回空列表，不抛异常
        """
        timeout = float(timeout or 0)
        if timeout <= 0:
            return []

        # 统一复用 ProbeStrategy 的实现，避免重复解析/超时/runner 行为漂移
        try:
            probe = ProbeStrategy(runner=self._runner.run_simple, timeout_s=timeout)
            return probe.fetch(tool_name, cwd=cwd)
        except Exception as e:
            logger.debug("TTADK probe_tool_models failed: tool=%s err=%s", tool_name, e)
            return []

    def fetch_tool_models_with_diagnostics(
        self,
        tool_name: str,
        cwd: Optional[str] = None,
        force_refresh: bool = False,
        prefer_probe: bool = False,
    ) -> FetchResult:
        """获取模型列表，并返回诊断信息（用于上层记录 source/失败原因）。

        structured 目前以 manager 的 ttadk sync 为基线，因此这里的 cwd 预留给后续可插拔策略。
        """
        diag = FetchDiagnostics(tool_name=tool_name)

        # 缓存命中
        if not force_refresh and self._is_cache_valid(tool_name):
            diag.chosen_strategy = "memory_cache"
            return FetchResult(
                tool_name=tool_name,
                models=list(self._cache.get(tool_name, [])),
                source="memory_cache",
                diagnostics=diag,
            )

        strategies = self._select_strategies(
            tool_name=tool_name,
            cwd=cwd,
            force_refresh=force_refresh,
            prefer_probe=bool(prefer_probe),
            diag=diag,
        )

        for strategy in strategies:
            start = time.time()
            try:
                models = strategy.fetch(tool_name, cwd=cwd)
                duration_ms = int((time.time() - start) * 1000)
                timeout_ms = None
                try:
                    t = float(getattr(strategy, "timeout_s", 0) or 0)
                    timeout_ms = int(t * 1000) if t > 0 else None
                except Exception:
                    timeout_ms = None
                attempt: dict = {
                    "strategy": strategy.name,
                    "ok": bool(models),
                    "count": len(models) if models else 0,
                    "error_type": None,
                    "fail_reason": None,
                    "rc": None,
                    "exit_code": None,
                    "stderr_snippet": None,
                    "stdout_snippet": None,
                    "raw_cmd": None,
                    "duration_ms": duration_ms,
                    "timeout_ms": timeout_ms,
                }

                # best-effort：策略可提供附加 warnings/detail。
                # 重要：warnings 只应在“该策略命中返回 models”时提升到 diag.warnings；
                # 否则会污染最终 chosen_strategy 的可信度判定（例如 file_cache 空返回却标记 low_confidence）。
                try:
                    if hasattr(strategy, "get_warnings"):
                        ws = [str(w) for w in (strategy.get_warnings() or []) if w]
                        if ws:
                            attempt["warnings"] = ws
                            if models:
                                for w in ws:
                                    if w not in diag.warnings:
                                        diag.warnings.append(w)
                except Exception:
                    pass
                try:
                    if hasattr(strategy, "get_attempt_detail"):
                        detail = dict(strategy.get_attempt_detail() or {})
                        if detail:
                            attempt["detail"] = detail
                            # Promote common fields for SSOT consumption
                            try:
                                if attempt.get("raw_cmd") is None and detail.get("raw_cmd") is not None:
                                    attempt["raw_cmd"] = detail.get("raw_cmd")
                                if attempt.get("exit_code") is None and detail.get("exit_code") is not None:
                                    attempt["exit_code"] = detail.get("exit_code")
                                if attempt.get("exit_code") is None and detail.get("rc") is not None:
                                    attempt["exit_code"] = detail.get("rc")
                            except Exception:
                                pass
                except Exception:
                    pass

                diag.attempts.append(attempt)
                if models:
                    # Keep a small in-memory cache for fetcher-level fast path.
                    self._cache[tool_name] = models
                    self._cache_time[tool_name] = time.time()
                    diag.chosen_strategy = strategy.name

                    # Best-effort: propagate to external cache sink (manager owns disk cache).
                    try:
                        if callable(self._cache_sink):
                            self._cache_sink(tool_name, list(models), strategy.name, diag)
                    except Exception:
                        pass

                    return FetchResult(
                        tool_name=tool_name,
                        models=list(models),
                        source=strategy.name,
                        diagnostics=diag,
                    )
            except Exception as e:
                duration_ms = int((time.time() - start) * 1000)
                timeout_ms = None
                try:
                    # subprocess.TimeoutExpired carries .timeout seconds
                    if getattr(e, "timeout", None) is not None:
                        timeout_ms = int(float(getattr(e, "timeout", 0) or 0) * 1000)
                    else:
                        t = float(getattr(strategy, "timeout_s", 0) or 0)
                        timeout_ms = int(t * 1000) if t > 0 else None
                except Exception:
                    timeout_ms = None
                stderr_snip = _redacted_snippet(getattr(e, "stderr", "") or "")
                stdout_snip = _redacted_snippet(getattr(e, "stdout", "") or "")
                if isinstance(e, (TTADKProbeError, TTADKOfficialCLIError, TTADKLocalConfigError, TTADKProjectMetaError)):
                    # 可诊断失败：携带 stdout/stderr/rc
                    stderr_snip = _redacted_snippet(getattr(e, "stderr", "") or "")
                    stdout_snip = _redacted_snippet(getattr(e, "stdout", "") or "")

                # fail_reason: 优先从异常 message 前缀推断（例如 "official_cli_probe_failed: ..."）
                fail_reason = None
                try:
                    msg = str(e) or ""
                    head = msg.split(":", 1)[0].strip() if msg else ""
                    if head and len(head) <= 64:
                        fail_reason = head
                except Exception:
                    fail_reason = None

                diag.attempts.append(
                    {
                        "strategy": strategy.name,
                        "ok": False,
                        "count": 0,
                        "error_type": type(e).__name__,
                        "fail_reason": fail_reason,
                        "rc": getattr(e, "returncode", None),
                        "exit_code": getattr(e, "returncode", None),
                        "file_path": getattr(e, "file_path", None),
                        "stderr_snippet": stderr_snip,
                        "stdout_snippet": stdout_snip,
                        "raw_cmd": getattr(e, "cmd", None),
                        "duration_ms": duration_ms,
                        "timeout_ms": timeout_ms,
                    }
                )

                # best-effort：策略 detail 提升（例如 raw_cmd/cwd/pty 等）
                try:
                    if hasattr(strategy, "get_attempt_detail"):
                        detail = dict(strategy.get_attempt_detail() or {})
                        if detail:
                            diag.attempts[-1]["detail"] = detail
                            if diag.attempts[-1].get("raw_cmd") is None and detail.get("raw_cmd") is not None:
                                diag.attempts[-1]["raw_cmd"] = detail.get("raw_cmd")
                            if diag.attempts[-1].get("exit_code") is None and detail.get("exit_code") is not None:
                                diag.attempts[-1]["exit_code"] = detail.get("exit_code")
                            if diag.attempts[-1].get("exit_code") is None and detail.get("rc") is not None:
                                diag.attempts[-1]["exit_code"] = detail.get("rc")
                except Exception:
                    pass

                # official_cli 的额外可观测信息（best-effort：phase/cmd）
                if getattr(strategy, "name", "") == "official_cli":
                    try:
                        detail = {}
                        if hasattr(strategy, "get_attempt_detail"):
                            detail = dict(strategy.get_attempt_detail() or {})
                        if not detail:
                            detail = {
                                "phase": getattr(e, "phase", ""),
                                "cmd": getattr(e, "cmd", None),
                            }
                        if detail:
                            diag.attempts[-1]["detail"] = detail
                    except Exception:
                        pass

                # local_config 的额外可观测信息（best-effort）
                if getattr(strategy, "name", "") == "local_config":
                    try:
                        if hasattr(strategy, "get_warnings"):
                            for w in list(strategy.get_warnings() or []):
                                if w and w not in diag.warnings:
                                    diag.warnings.append(str(w))
                        if hasattr(strategy, "get_attempt_detail"):
                            detail = dict(strategy.get_attempt_detail() or {})
                            if detail:
                                diag.attempts[-1]["detail"] = detail
                    except Exception:
                        pass

                # structured_sync 在未 init/无配置时属于可预期失败：标记并继续降级 probe
                # 但考虑到策略链可能在 structured_sync 之前就已成功（例如 probe），因此这里对所有策略统一标记。
                # 目的：让上层能够知道“当前 cwd 未 init”，便于提示/排障。
                if _is_ttadk_config_missing_error(e):
                    if "ttadk_config_missing" not in diag.warnings:
                        diag.warnings.append("ttadk_config_missing")

                # 关键：若发现 invalid/unknown model，明确标记以便上层触发自愈（降级到 auto）
                try:
                    blob = (stderr_snip or "") + "\n" + (stdout_snip or "")
                    if is_invalid_model_error(blob):
                        if "invalid_model" not in diag.warnings:
                            diag.warnings.append("invalid_model")
                except Exception:
                    pass

        diag.warnings.append("all_strategies_failed")
        return FetchResult(tool_name=tool_name, models=[], source="", diagnostics=diag)

    def _is_cache_valid(self, tool_name: str) -> bool:
        """检查缓存是否有效。"""
        if tool_name not in self._cache:
            return False
        cache_time = self._cache_time.get(tool_name, 0)
        return (time.time() - cache_time) < self.CACHE_TTL

    def invalidate_cache(self, tool_name: Optional[str] = None) -> None:
        """使缓存失效。"""
        if tool_name:
            self._cache.pop(tool_name, None)
            self._cache_time.pop(tool_name, None)
        else:
            self._cache.clear()
            self._cache_time.clear()

    # ---- sync json parsing helpers ----
    def _extract_models_from_sync(self, data: object, tool_name: Optional[str], current_model: Optional[str] = None) -> list[TTADKModel]:
        if isinstance(data, dict) and tool_name:
            for key in self.TOOL_KEYS:
                container = data.get(key)
                models = self._extract_models_from_tool_container(container, tool_name, current_model)
                if models:
                    return models
        return self._extract_models_from_container(data, current_model, under_model_key=False)

    def _extract_models_from_tool_container(self, container: object, tool_name: str, current_model: Optional[str]) -> list[TTADKModel]:
        if isinstance(container, dict):
            if tool_name in container:
                return self._extract_models_from_container(container.get(tool_name), current_model, under_model_key=False)
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tool") or item.get("id")
                    if name == tool_name:
                        return self._extract_models_from_container(item, current_model, under_model_key=False)
        return []

    def _extract_models_from_container(self, container: object, current_model: Optional[str], under_model_key: bool) -> list[TTADKModel]:
        if isinstance(container, dict):
            for key in self.MODEL_KEYS:
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
                    friendly = item.get("friendly_name") or item.get("display_name") or item.get("title") or ""
                    models.append(
                        TTADKModel(
                            name=str(name),
                            description=str(desc),
                            is_default=(str(name) == current_model),
                            friendly_name=str(friendly),
                        )
                    )
            return models
        return []


class FileCacheStrategy(ModelFetchStrategy):
    """从本地文件缓存加载模型列表。

    作为“第二真实模型来源”：当 ttadk sync 不可用/未 init，且 probe 不稳定时，
    可以依赖历史成功探测/写盘的真实模型列表。
    """

    def __init__(self, fetcher: "TTADKModelFetcher"):
        self._fetcher = fetcher
        # 注意：服务侧不应写入真实 HOME（~/.ttadk）。
        # file_cache 策略仅作为“legacy 低置信来源”读取；路径仍指向 HOME 以兼容历史文件。
        self._path = Path.home() / ".ttadk" / "models_cache.json"

    @property
    def name(self) -> str:
        return "file_cache"

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        try:
            if not self._path.exists():
                return []
            payload = self._path.read_text(encoding="utf-8")
            if not payload.strip():
                return []
            data = json.loads(payload)
            tool = (tool_name or "").strip().lower()
            names, exact = parse_models_cache_json(data, tool_name=tool, allow_cross_tool_fallback=False)
            if not names:
                return []
            # 标注：若未命中 tool（理论上不会发生，因为 allow_cross_tool_fallback=False），则降级为低可信
            out: list[TTADKModel] = [TTADKModel(name=n, description=n, friendly_name=n) for n in names]
            return out
        except Exception:
            return []

    # best-effort diagnostics hooks (read by fetcher)
    def get_warnings(self) -> list[str]:
        # ~/.ttadk/models_cache.json 属于跨项目缓存来源
        # 约定：该来源不可用于 validated 判定，因此显式标记 models_untrusted。
        return ["source_cross_project", "low_confidence", "models_untrusted"]

    def get_attempt_detail(self) -> dict:
        try:
            return {"file_hit": str(getattr(self._path, "name", "models_cache.json") or "models_cache.json"), "scope": "home"}
        except Exception:
            return {"file_hit": "models_cache.json", "scope": "home"}


@dataclass
class TTADKRunResult:
    returncode: int
    stdout: str
    stderr: str


class TTADKRunner:
    """可注入 runner：用于测试与端到端夹具。"""

    def run(self, args: list[str], cwd: Optional[str] = None, timeout: float = 8.0) -> TTADKRunResult:
        import subprocess

        env, _ = build_ttadk_subprocess_env(cwd=cwd or ".", agent_type="ttadk", tool_name="")
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return TTADKRunResult(returncode=p.returncode, stdout=p.stdout or "", stderr=p.stderr or "")

    def run_simple(self, args: list[str], cwd: Optional[str], timeout: float) -> tuple[int, str, str]:
        r = self.run(args, cwd=cwd, timeout=timeout)
        return r.returncode, r.stdout, r.stderr


class StructuredSyncStrategy(ModelFetchStrategy):
    def __init__(self, runner: TTADKRunner, fetcher: "TTADKModelFetcher", timeout_s: float = 8.0):
        self._runner = runner
        self._fetcher = fetcher
        try:
            self.timeout_s = float(timeout_s or 0) or 8.0
        except Exception:
            self.timeout_s = 8.0

    @property
    def name(self) -> str:
        return "structured_sync"

    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        if not cwd:
            return []
        r = self._runner.run(["ttadk", "sync", "-d", "-f", "json"], cwd=cwd, timeout=self.timeout_s)
        if r.returncode != 0:
            raise TTADKCommandError(
                "structured_sync non-zero exit",
                returncode=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
            )
        payload = (r.stdout or "").strip()
        if not payload:
            raise TTADKCommandError(
                "structured_sync empty stdout",
                returncode=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
            )
        try:
            data = json.loads(payload)
        except Exception:
            start = payload.find("{")
            end = payload.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(payload[start : end + 1])
            else:
                raise TTADKCommandError(
                    "structured_sync invalid json",
                    returncode=r.returncode,
                    stdout=r.stdout,
                    stderr=r.stderr,
                )
        return self._fetcher._extract_models_from_sync(data, tool_name, current_model=None)

    # best-effort diagnostics hooks (read by fetcher)
    def get_warnings(self) -> list[str]:
        # structured_sync 属于“项目侧”来源；但可能因为未 init 失败（由 fetcher 统一标记 ttadk_config_missing）
        return ["source_project"]

    def get_attempt_detail(self) -> dict:
        try:
            return {
                "raw_cmd": ["ttadk", "sync", "-d", "-f", "json"],
                "scope": "project",
            }
        except Exception:
            return {"scope": "project"}
