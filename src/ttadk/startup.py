"""TTADK 启动编排（startup）。

该模块承载“启动期 SSOT 编排”主流程：precheck → start → invalid-model repair → retry/auto → degrade。

设计约束：
- 不依赖 ACP/Engine/Feishu/agent_session 层，避免循环依赖
- 返回结构遵循稳定契约（被 tests 冻结）：result/tool/input_model/resolved_model/validated/source/warnings/degraded/repaired/fail_phase/decision/diagnostics
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypedDict

from .models import is_invalid_model_error
from .runtime_repair import repair_invalid_model_startup

__all__ = [
    "coordinate_ttadk_startup",
    "start_agent_session",
]


logger = logging.getLogger(__name__)


class AgentSessionStartInfo(TypedDict, total=False):
    """稳定返回契约（由 tests 冻结）。

    约定：
    - `session`/`session_id`：启动成功后必有；失败时可缺省。
    - `diagnostics`：失败/重试时用于日志与验收的结构化证据。
    - 其余字段用于 TTADK 启动期可观测性与决策解释。
    """

    session: Any
    session_id: str
    tool: str
    input_model: str
    resolved_model: str
    resolved_real_name: str
    passthrough_model: Optional[str]
    validated: bool
    source: str
    warnings: list[str]
    degraded: bool
    repaired: bool
    fail_phase: str
    decision: str
    diagnostics: dict


def start_agent_session(
    *,
    agent_type: str,
    cwd: Optional[str],
    startup_timeout: float = 60,
    model_name: Optional[str] = None,
    session_cls: Optional[type] = None,
    log_failures: bool = True,
    get_settings_fn: Optional[Callable[[], object]] = None,
) -> AgentSessionStartInfo:
    """TTADK 启动稳定入口（kw-only）。

    目标：让上层（ACP/Engine/Handler）只依赖该稳定接口，而不再内嵌 TTADK 专属分支。

    注意：该函数会在后续任务中实现完整 SSOT 启动流程（协调/纠错/降级/PTY 重试/attempts 记录）。
    当前阶段仅冻结签名与返回结构的字段集合，避免后续重构引发接口漂移。
    """
    at = (agent_type or "").strip().lower()
    if not at.startswith("ttadk_"):
        raise ValueError("start_agent_session only supports ttadk_* agent_type")

    # 延迟 import：避免 module import-time 引入 ACP 层导致循环依赖
    from . import get_ttadk_manager

    # ACP 启动依赖（延迟导入）
    from src.acp.sync_adapter import start_ttadk_session_with_pty_retry
    from src.acp.sync_adapter import _call_start_session_with_retry_compat
    from src.acp.sync_adapter import SyncACPSession as _DefaultSession

    # 可选：用于日志格式兼容（避免 acp.manager 持有该格式）
    try:
        from .manager import TTADK_STARTUP_LOG_FMT
    except Exception:
        TTADK_STARTUP_LOG_FMT = "tool=%s input_model=%s model=%s validated=%s source=%s fail_phase=%s decision=%s warnings=%s"

    tool_name = at.replace("ttadk_", "", 1)
    ttadk_manager = get_ttadk_manager()
    model_intent = (model_name or (ttadk_manager.get_current_model() or "")).strip()

    if session_cls is None:
        session_cls = _DefaultSession

    last_spec = ""

    def _start_ttadk(passthrough_model: Optional[str]):
        nonlocal last_spec
        # 兼容：测试桩可能不接受 session_cls/log_failures 等新参数，需要“降参”调用。
        # 注意：这里必须优先调用 `start_ttadk_session_with_pty_retry`（而不是 start_session_with_retry），
        # 否则会绕过单测注入点并触发真实子进程。
        s = None
        try:
            s = start_ttadk_session_with_pty_retry(
                agent_type=at,
                cwd=(cwd or "."),
                startup_timeout=float(startup_timeout or 60),
                model_name=passthrough_model,
                session_cls=session_cls,
                log_failures=bool(log_failures),
            )
        except TypeError:
            try:
                s = start_ttadk_session_with_pty_retry(
                    agent_type=at,
                    cwd=(cwd or "."),
                    startup_timeout=float(startup_timeout or 60),
                    model_name=passthrough_model,
                    session_cls=session_cls,
                )
            except TypeError:
                # 最后：最小参数集
                s = start_ttadk_session_with_pty_retry(
                    agent_type=at,
                    cwd=(cwd or "."),
                    startup_timeout=float(startup_timeout or 60),
                    model_name=passthrough_model,
                )
        try:
            last_spec = s.describe_agent() if s is not None else ""
        except Exception:
            last_spec = ""
        return (s, str(getattr(s, "session_id", "") or ""))

    def _coco_acp_args(model_name: Optional[str]) -> list[str]:
        args: list[str] = ["acp", "serve"]
        if model_name:
            args.extend(["-c", f"model.name={model_name}"])
        return args

    def _fallback_to_coco(err: Exception):
        # TTADK tool 不可用时的确定性降级：切到 coco ACP。
        # 关键约束：保留 session 的 `_agent_type=ttadk_*`，避免 ACPSessionManager 的 agent_type mismatch 触发抖动重启。
        from src.acp.sync_adapter import start_session_with_retry
        from src.coco_model import get_coco_model_manager

        fallback_model = get_coco_model_manager().get_current_model()
        # 兼容：部分单测会 monkeypatch `start_session_with_retry` 为旧签名，不接受 session_cls/log_failures。
        s = _call_start_session_with_retry_compat(
            agent_type="coco",
            cwd=(cwd or "."),
            startup_timeout=float(startup_timeout or 60),
            model_name=fallback_model,
            # 允许上层注入 session_cls（尤其是单测），避免 fallback 路径绕过注入点而触发真实子进程。
            session_cls=session_cls or _DefaultSession,
            ttadk_use_pty=False,
            # 降级路径避免重复刷屏；详细诊断通过 diagnostics 附着在异常/汇总日志中。
            log_failures=False,
        )
        sid = str(getattr(s, "session_id", "") or "")
        # best-effort: 标记降级，并将 agent_type 伪装回 ttadk_*（避免上层抖动）
        try:
            setattr(s, "_degraded_to", "coco")
        except Exception:
            pass
        try:
            setattr(s, "_agent_type", at)
        except Exception:
            pass
        # Best-effort: keep a non-empty, user-facing reason summary.
        try:
            from src.acp.sync_adapter import build_startup_diagnostics

            d = build_startup_diagnostics(
                agent_type=at,
                cwd=(cwd or "."),
                model_name=None,
                session=None,
                error=err,
                timeout_s=float(startup_timeout or 0),
            )
            fr = str((d or {}).get("fail_reason") or (d or {}).get("fail_phase") or "start_failed").strip() or "start_failed"
            et = str((d or {}).get("error_text") or (d or {}).get("stderr_snippet") or (d or {}).get("error") or "").strip()
            if not et:
                et = (repr(err) if err is not None else "<Exception> (empty)")
            try:
                setattr(s, "_degraded_reason", f"{fr}: {et}")
            except Exception:
                pass
        except Exception:
            pass
        return (s, sid)

    info = coordinate_ttadk_startup(
        manager=ttadk_manager,
        tool_name=tool_name,
        input_model=model_intent,
        cwd=(cwd or "."),
        start_fn=_start_ttadk,
        fallback_fn=_fallback_to_coco,
        get_settings_fn=get_settings_fn,
    )

    # 统一日志口径：在 TTADK 侧输出结构化“启动决策摘要”，避免上层持有 TTADK 格式/字段。
    try:
        logger.info(
            "[TTADK:%s] " + TTADK_STARTUP_LOG_FMT + " degraded=%s repaired=%s",
            tool_name.upper(),
            tool_name,
            info.get("input_model") or "",
            (
                info.get("passthrough_model")
                or (
                    (str(info.get("resolved_model") or "").strip())
                    if bool(info.get("validated")) and str(info.get("resolved_model") or "").strip() and not str(info.get("resolved_model") or "").strip().startswith("(")
                    else None
                )
                or "(auto)"
            ),
            bool(info.get("validated")),
            info.get("source") or "(unknown)",
            info.get("fail_phase") or "",
            info.get("decision") or "",
            list(info.get("warnings") or []),
            bool(info.get("degraded")),
            bool(info.get("repaired")),
        )
    except Exception:
        pass

    session, sid = info.get("result") if isinstance(info, dict) else (None, "")
    session_id = str(sid or "").strip()
    if not session or not session_id:
        detail = str(info) if info else "unknown"
        spec = f" ({last_spec})" if last_spec else ""
        raise RuntimeError(f"启动 {at} ACP Server 失败{spec}: {detail}")

    out: dict = dict(info or {})
    out["session"] = session
    out["session_id"] = session_id
    # 对外契约：resolved_model 语义保持为“实际透传值”或“(fallback)/(auto)”
    return out  # type: ignore[return-value]


def coordinate_ttadk_startup(
    *,
    manager: Any,
    tool_name: str,
    input_model: str,
    cwd: Optional[str],
    start_fn: Callable[[Optional[str]], Any],
    fallback_fn: Optional[Callable[[Exception], Any]] = None,
    startup_probe_timeout_s: Optional[float] = None,
    precheck_fn: Optional[Callable[[str], dict]] = None,
    get_settings_fn: Optional[Callable[[], object]] = None,
    time_fn: Optional[Callable[[], float]] = None,
) -> dict:
    """统一 TTADK 启动协调入口（precheck→start→invalid_model_repair→retry→degrade）。

    说明：
    - 该函数是“启动期 SSOT”核心编排，供 TTADK façade 与 ACP/Engine 入口复用。
    - invalid-model 修复逻辑委托给 `src/ttadk/runtime_repair.py`。

    参数约束：
    - start_fn: Callable[[Optional[str]], Any]，参数为要透传给 ttadk 的 model_name（validated 才传）。
    - fallback_fn: Callable[[Exception], Any]，用于确定性降级（例如 coco）。

    稳定返回契约（tests 冻结，调用方不得自行分叉实现）：
    - result/tool/input_model/resolved_model/validated/source/warnings/degraded/repaired/fail_phase/decision/diagnostics
    - diagnostics.attempts 为 list[dict]，记录 precheck/start/repair/retry/degrade 等阶段（best-effort）。
    """
    # 延迟 import，避免在模块 import 时引入重依赖
    import time as _time
    from ..config import get_settings as _get_settings
    # Avoid importing `src.ttadk.manager` at module import time to prevent cycles.
    from .startup_common import precheck_ttadk_startup_model
    from .startup_common import _runtime_invalid_model_stub_get_last_ts, _runtime_invalid_model_stub_set_last_ts

    if get_settings_fn is None:
        get_settings_fn = _get_settings
    if time_fn is None:
        time_fn = _time.time

    tool = (tool_name or "").strip().lower()
    intent = (input_model or "").strip()
    attempts: list[dict] = []

    def _safe_str(x: object) -> str:
        try:
            return str(x)
        except Exception:
            return ""

    def _truncate_text(text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        s = _safe_str(text)
        if len(s) <= limit:
            return s
        if limit <= 3:
            return s[:limit]
        return s[: limit - 3] + "..."

    def _error_blob(err: Exception) -> str:
        """构造用于 fail_phase 分类/invalid-model 判断的错误文本（best-effort，非空，限长）。"""
        parts: list[str] = []
        msg = (_safe_str(err) or "").strip()
        if msg:
            parts.append(msg)
        for k in ("stderr_snippet", "stdout_snippet", "stderr", "stdout"):
            try:
                v = (_safe_str(getattr(err, k, "")) or "").strip()
                if v:
                    parts.append(v)
            except Exception:
                continue
        blob = "\n".join([p for p in parts if p]).strip()
        if not blob:
            blob = "(empty)"
        return _truncate_text(blob, 1600)

    def _error_text(err: Exception) -> str:
        """构造用于 attempts/error_text 的错误文案（保证非空、可读、限长）。"""
        msg = (_safe_str(err) or "").strip()
        if msg:
            return _truncate_text(msg, 600)
        for k in ("stderr_snippet", "stdout_snippet"):
            try:
                v = (_safe_str(getattr(err, k, "")) or "").strip()
                if v:
                    return _truncate_text(v, 600)
            except Exception:
                continue
        # 兜底：不让日志/attempts 出现空 error
        return _truncate_text("(empty)", 600)

    def _precheck(model_intent: str) -> dict:
        if callable(precheck_fn):
            try:
                return dict(precheck_fn(model_intent) or {})
            except Exception:
                pass
        try:
            return precheck_ttadk_startup_model(
                agent_type=f"ttadk_{tool}",
                cwd=cwd or ".",
                model_intent=model_intent,
                manager=manager,
                startup_probe_timeout_s=startup_probe_timeout_s,
            )
        except Exception:
            return {
                "tool": tool,
                "input_model": model_intent,
                "resolved_real_name": model_intent,
                "passthrough_model": None,
                # compat: resolved_model 语义为“实际透传值”（validated 才有真实名），此处为 None 表示 (auto)
                "resolved_model": None,
                "model": None,
                "validated": False,
                "source": "unknown",
                "decision": "precheck_error",
                "fail_phase": "precheck_error",
                "warnings": ["precheck_error"],
                "diagnostics": {},
            }

    def _fail_phase_from_error(err: Exception, err_blob: str) -> str:
        if is_invalid_model_error(err_blob or ""):
            return "invalid_model"
        if isinstance(err, TimeoutError):
            return "timeout"
        if getattr(err, "timeout", None) is not None and type(err).__name__ == "TimeoutExpired":
            return "timeout"
        if type(err).__name__ in ("AgentSpecResolveError", "TTADKStartupError"):
            return "protocol_adapter"
        return "start_failed"

    pre = _precheck(intent)
    # 护栏：coordinator 只消费 precheck 的稳定输出字段来决定是否透传 -m。
    # - validated=False：必须传 None（走 (auto)）
    # - validated=True：仅当 pre['model'] 为非空真实名时才允许透传
    pre_validated_raw = bool(pre.get("validated"))
    passthrough_model = pre.get("model") if pre_validated_raw else None
    if not passthrough_model:
        passthrough_model = None

    # 额外一致性护栏：若 precheck 标记 validated=True 但未给出可透传的 model（空/None），
    # 则在 coordinator 侧将 validated 视为 False（避免日志/attempts 语义漂移）。
    validated = bool(pre_validated_raw and bool(passthrough_model))

    # 安全策略：若 precheck 明确标记模型列表不可信/为空/错误，则禁止透传 -m（即强制走 (auto)）。
    try:
        ws = list(pre.get("warnings") or [])
        if any(w in ("models_untrusted", "models_empty", "models_error") or str(w).startswith("models_error") for w in ws):
            passthrough_model = None
            pre["validated"] = False
            pre["decision"] = "precheck_auto"
            if "no_m_passthrough" not in ws:
                ws.append("no_m_passthrough")
            pre["warnings"] = ws
    except Exception:
        pass

    attempts.append(
        {
            "phase": "precheck",
            "ok": True,
            "tool": tool,
            "input_model": intent,
            "resolved_model": passthrough_model or "(auto)",
            "resolved_real_name": pre.get("resolved_real_name") or pre.get("model") or intent,
            "validated": validated,
            "source": pre.get("source") or "unknown",
            "warnings": list(pre.get("warnings") or []),
            "passthrough_model": passthrough_model,
            "precheck_diagnostics": dict(pre.get("diagnostics") or {}),
            # SSOT：将模型列表诊断关键字段提升到启动 attempts，便于日志与验收
            "models_source": (dict(pre.get("diagnostics") or {}).get("source") if isinstance(pre.get("diagnostics"), dict) else None),
            "models_raw_cmd": (dict(pre.get("diagnostics") or {}).get("raw_cmd") if isinstance(pre.get("diagnostics"), dict) else None),
            "models_exit_code": (dict(pre.get("diagnostics") or {}).get("exit_code") if isinstance(pre.get("diagnostics"), dict) else None),
            "models_stderr_snippet": (dict(pre.get("diagnostics") or {}).get("stderr_snippet") if isinstance(pre.get("diagnostics"), dict) else None),
            "models_freshness": (dict(pre.get("diagnostics") or {}).get("freshness") if isinstance(pre.get("diagnostics"), dict) else None),
        }
    )

    # 防漂移护栏：attempts[precheck].resolved_model 语义只能是“真实透传名”或 “(auto)”。
    # best-effort：仅在本地 attempts 结构上纠偏，不抛异常影响主流程。
    try:
        if attempts and attempts[-1].get("phase") == "precheck":
            if passthrough_model:
                attempts[-1]["resolved_model"] = passthrough_model
            else:
                attempts[-1]["resolved_model"] = "(auto)"
    except Exception:
        pass

    try:
        r = start_fn(passthrough_model)
        attempts.append(
            {
                "phase": "start",
                "ok": True,
                "tool": tool,
                "input_model": intent,
                "resolved_model": passthrough_model or "(auto)",
                "passthrough_model": passthrough_model,
                "validated": validated,
                "source": pre.get("source") or "unknown",
                "warnings": list(pre.get("warnings") or []),
            }
        )
        return {
            "result": r,
            "tool": tool,
            "input_model": intent,
            # Best-known real model name resolved from input (even if not passed through).
            # Note: this is for observability; the actual passthrough is controlled by `validated`.
            "resolved_real_name": pre.get("resolved_real_name") or pre.get("model") or intent,
            # Explicit field for the actual model passed to ttadk (validated-only).
            "passthrough_model": passthrough_model,
            "resolved_model": passthrough_model or "(auto)",
            "validated": validated,
            "source": pre.get("source") or "unknown",
            "warnings": list(pre.get("warnings") or []),
            "degraded": False,
            "repaired": False,
            "fail_phase": "",
            "decision": "start_ok",
            "diagnostics": {"attempts": attempts},
        }
    except Exception as e:
        err_blob = _error_blob(e)
        fail_phase = _fail_phase_from_error(e, err_blob)
        err_text = _error_text(e)

        # Best-effort structured evidence for diagnostics.attempts (kept minimal + serializable)
        def _redact_evidence(text: str) -> str:
            """对 evidence 做最小脱敏（不依赖 ACP 层，避免循环依赖）。"""
            try:
                import re as _re

                s = _safe_str(text or "")
                # Cover common secret patterns (keep small; best-effort)
                patterns = [
                    r"(?i)authorization\s*:\s*[^\s]+",
                    r"(?i)bearer\s+[^\s]+",
                    r"sk-[A-Za-z0-9]{10,}",
                    r"AKIA[0-9A-Z]{16}",
                    r"(?i)api[_-]?key\s*[:=]\s*[^\s]+",
                    r"(?i)token\s*[:=]\s*[^\s]+",
                ]
                for p in patterns:
                    try:
                        s = _re.sub(p, "***REDACTED***", s)
                    except Exception:
                        continue
                return s
            except Exception:
                return _safe_str(text or "")

        exit_code = None
        try:
            exit_code = getattr(e, "returncode", None)
        except Exception:
            exit_code = None
        if exit_code is None:
            try:
                exit_code = getattr(e, "rc", None)
            except Exception:
                exit_code = None

        stderr_snip = ""
        stdout_snip = ""
        try:
            stderr_snip = (_safe_str(getattr(e, "stderr_snippet", "")) or "").strip()
        except Exception:
            stderr_snip = ""
        try:
            stdout_snip = (_safe_str(getattr(e, "stdout_snippet", "")) or "").strip()
        except Exception:
            stdout_snip = ""

        cmd = ""
        args: list[str] = []
        try:
            cmd = (_safe_str(getattr(e, "agent_cmd", "")) or "").strip()
        except Exception:
            cmd = ""
        if not cmd:
            try:
                cmd = (_safe_str(getattr(e, "cmd", "")) or "").strip()
            except Exception:
                cmd = ""
        try:
            raw_args = getattr(e, "agent_args", None)
            if raw_args is None:
                raw_args = getattr(e, "args", None)
            if raw_args:
                args = [str(x) for x in list(raw_args or [])]
        except Exception:
            args = []

        # Apply redaction + truncation on evidence fields to avoid leaking secrets.
        try:
            cmd = _truncate_text(_redact_evidence(cmd), 240)
        except Exception:
            pass
        try:
            args = [_truncate_text(_redact_evidence(str(x)), 200) for x in (args or [])][:80]
        except Exception:
            args = [str(x) for x in (args or [])][:80]
        try:
            stderr_snip = _truncate_text(_redact_evidence(stderr_snip), 240)
        except Exception:
            pass
        try:
            stdout_snip = _truncate_text(_redact_evidence(stdout_snip), 240)
        except Exception:
            pass

        attempts.append(
            {
                "phase": "start",
                "ok": False,
                "tool": tool,
                "input_model": intent,
                "resolved_model": passthrough_model or "(auto)",
                "passthrough_model": passthrough_model,
                "validated": bool(validated),
                "source": pre.get("source") or "unknown",
                "warnings": list(pre.get("warnings") or []),
                "error_type": type(e).__name__,
                # alias for external consumers/tests
                "exception_type": type(e).__name__,
                "error": err_text,
                "error_text": err_text,
                "error_blob": err_blob,
                "fail_phase": fail_phase,
                # evidence fields (best-effort)
                "exit_code": exit_code,
                "stderr_snippet": stderr_snip,
                "stdout_snippet": stdout_snip,
                "cmd": cmd,
                "args": args,
            }
        )

        if is_invalid_model_error(err_blob):
            return repair_invalid_model_startup(
                manager=manager,
                tool_name=tool,
                input_model=intent,
                cwd=cwd,
                error=e,
                error_blob=err_blob,
                attempts=attempts,
                start_fn=start_fn,
                fallback_fn=fallback_fn,
                precheck_fn=_precheck,
                get_settings_fn=get_settings_fn,
                time_fn=time_fn,
                stub_get_last_ts_fn=_runtime_invalid_model_stub_get_last_ts,
                stub_set_last_ts_fn=_runtime_invalid_model_stub_set_last_ts,
            )

        if callable(fallback_fn):
            r_fb = fallback_fn(e)
            attempts.append(
                {
                    "phase": "degrade",
                    "ok": True,
                    "tool": tool,
                    "input_model": intent,
                    "resolved_model": "(fallback)",
                    "passthrough_model": None,
                    "validated": False,
                    "source": "fallback",
                }
            )
            return {
                "result": r_fb,
                "tool": tool,
                "input_model": intent,
                "resolved_real_name": pre.get("resolved_real_name") or pre.get("model") or intent,
                "passthrough_model": None,
                "resolved_model": "(fallback)",
                "validated": False,
                "source": "fallback",
                "warnings": ["degraded"],
                "degraded": True,
                "repaired": False,
                "fail_phase": fail_phase,
                "decision": "start_failed_degraded",
                "diagnostics": {"attempts": attempts},
            }
        raise
