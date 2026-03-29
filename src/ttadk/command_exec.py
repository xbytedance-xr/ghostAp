"""TTADK 命令执行闭环（SSOT）。

职责：
- 执行 `ttadk code`（可注入 runner）并采集 rc/stdout/stderr
- 对失败进行稳定分型（fail_reason）
- 针对 invalid_model 做最小自愈（force_refresh + re-resolve + retry → auto → fail）
- 输出稳定的结果 dict（供日志/卡片/回归断言）

依赖边界（重要）：
- 允许依赖 `src.ttadk.models` 与 `src.acp.diagnostics`（仅用于脱敏与截断）
- 禁止 import `src.agent_session` / `src.acp.manager` / `src.ttadk.startup*`，避免循环依赖与职责漂移
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import get_settings
from .models import (
    extract_invalid_model_diagnostics,
    is_invalid_model_error,
    is_stdin_not_tty_error,
    redact_and_truncate,
    strict_truncate,
)


@dataclass
class TTADKCommandRunResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    fail_reason: str
    cmd: str
    args: list[str]
    stdout_snippet: str
    stderr_snippet: str


class TTADKCommandRunner:
    """可注入/可 mock 的 ttadk 命令执行器。

    runner 需提供：run_simple(args, cwd, timeout) -> (rc, out, err)
    """

    def __init__(self, *, runner: Optional[object] = None, get_settings_fn=get_settings) -> None:
        self._runner = runner
        self._get_settings_fn = get_settings_fn

    def run(self, *, args: list[str], cwd: Optional[str], timeout_s: float) -> TTADKCommandRunResult:
        cmd = str(args[0] if args else "")
        xs = [str(x) for x in (args or [])]
        try:
            timeout = float(timeout_s or 0.0)
        except Exception:
            timeout = 0.0
        timeout = max(0.1, timeout)

        r = self._runner
        if r is None:
            from .model_fetcher import TTADKRunner

            r = TTADKRunner()

        try:
            rc, out, err = r.run_simple(xs, cwd, timeout)  # type: ignore[attr-defined]
        except Exception as e:
            et = type(e).__name__
            msg = str(e) or "(empty)"
            err = f"{et}: {msg}"
            rc, out = 1, ""

        try:
            rc_i = int(rc or 0)
        except Exception:
            rc_i = 1

        out_s = str(out or "")
        err_s = str(err or "")
        blob = (out_s + "\n" + err_s).strip()

        fail_reason = ""
        if rc_i == 0:
            ok = True
        else:
            ok = False
            try:
                if is_stdin_not_tty_error(blob):
                    fail_reason = "stdin_not_tty"
                elif is_invalid_model_error(blob):
                    fail_reason = "invalid_model"
            except Exception:
                fail_reason = ""
            if not fail_reason:
                lower = blob.lower()
                if "permission denied" in lower:
                    fail_reason = "permission"
                elif "not initialized" in lower or "ttadk init" in lower or "initialize the project" in lower:
                    fail_reason = "not_initialized"
                elif "not found" in lower and "ttadk" in lower:
                    fail_reason = "binary_missing"
                elif "timed out" in lower or "timeout" in lower:
                    fail_reason = "timeout"
                else:
                    fail_reason = "unknown"

        return TTADKCommandRunResult(
            ok=bool(ok),
            returncode=rc_i,
            stdout=out_s,
            stderr=err_s,
            fail_reason=str(fail_reason or ""),
            cmd=cmd,
            args=list(xs),
            stdout_snippet=redact_and_truncate(out_s, hard_limit=240, get_settings_fn=self._get_settings_fn),
            stderr_snippet=redact_and_truncate(err_s, hard_limit=240, get_settings_fn=self._get_settings_fn),
        )


def _build_ttadk_code_args(
    *, tool_name: str, model_name: Optional[str], extra_args: Optional[list[str]] = None
) -> list[str]:
    tool = (tool_name or "").strip().lower()
    xs: list[str] = ["ttadk", "code", "-t", tool]
    m = (model_name or "").strip()
    if m:
        xs.extend(["-m", m])
    for a in list(extra_args or []):
        aa = str(a or "").strip()
        if aa:
            xs.append(aa)
    return xs


def _next_steps_for_fail_reason(fail_reason: str) -> list[str]:
    r = (fail_reason or "").strip()
    if r == "not_initialized":
        return ["在项目目录执行 ttadk init", "执行 ttadk sync（如需要）", "重试命令"]
    if r == "binary_missing":
        return ["确认机器已安装 ttadk 并在 PATH 中可用", "或切换到 coco/claude 模式"]
    if r == "permission":
        return ["检查当前目录/二进制执行权限", "检查网络/登录态（如需要）", "重试命令"]
    if r == "stdin_not_tty":
        return ["尝试开启 PTY 模式或在终端环境执行", "或降级到 (auto) 重试"]
    if r == "invalid_model":
        return ["强制刷新模型列表", "选择真实模型名重试", "必要时降级为 (auto) 或 coco"]
    return ["查看日志中的 stderr_snippet/attempts", "必要时切换工具或降级到 coco"]


def execute_ttadk_code_with_repair(
    *,
    manager: object,
    tool_name: str,
    cwd: Optional[str],
    input_model: str,
    timeout_s: float = 10.0,
    force_refresh_timeout_s: float = 12.0,
    extra_args: Optional[list[str]] = None,
    runner: Optional[TTADKCommandRunner] = None,
) -> dict:
    """执行 `ttadk code` 并实现最小自愈闭环。

    返回结构（稳定字段，用于用户提示/回归断言）：
    - ok/bool, decision/str, fail_reason/str
    - tool/input_model/model(actual passthrough or (auto))
    - validated/source/warnings
    - attempts: list[dict]
    - next_steps: list[str]
    """
    tool = (tool_name or "").strip().lower()
    intent = (input_model or "").strip()
    attempts: list[dict] = []

    # manager methods (best-effort typing)
    resolve_startup = manager.resolve_startup_model_with_diagnostics
    seed_runtime = getattr(manager, "seed_models_from_invalid_model_runtime", None)
    get_models = manager.get_models
    resolve_real = manager.resolve_real_model_name

    resolved, diag = resolve_startup(intent, tool_name=tool, cwd=cwd)
    passthrough = getattr(resolved, "real_name", "") if bool(getattr(resolved, "validated", False)) else ""
    source = getattr(resolved, "source", "") or "unknown"
    warnings = list(getattr(resolved, "warnings", []) or [])
    attempts.append(
        {
            "phase": "precheck",
            "ok": True,
            "validated": bool(getattr(resolved, "validated", False)),
            "passthrough_model": passthrough or "(auto)",
            "source": source,
            "warnings": list(warnings),
        }
    )
    try:
        if diag and isinstance(diag, dict) and diag.get("attempts"):
            attempts.append({"phase": "precheck_attempts", "ok": True, "detail": list(diag.get("attempts") or [])[:6]})
    except Exception:
        pass

    cmd_runner = runner or getattr(manager, "_command_runner", None) or TTADKCommandRunner(get_settings_fn=get_settings)

    def _run_once(model_name: Optional[str], *, phase: str) -> TTADKCommandRunResult:
        args = _build_ttadk_code_args(tool_name=tool, model_name=model_name, extra_args=extra_args)
        r = cmd_runner.run(args=args, cwd=cwd, timeout_s=float(timeout_s or 10.0))
        attempts.append(
            {
                "phase": phase,
                "ok": bool(r.ok),
                "model": (model_name or "(auto)") if model_name else "(auto)",
                "returncode": int(r.returncode),
                "fail_reason": str(r.fail_reason or ""),
                "stdout_snippet": r.stdout_snippet,
                "stderr_snippet": r.stderr_snippet,
            }
        )
        return r

    r1 = _run_once(passthrough or None, phase="run")
    if r1.ok:
        return {
            "ok": True,
            "decision": "ttadk_code_ok",
            "fail_reason": "",
            "tool": tool,
            "input_model": intent,
            "model": passthrough or "(auto)",
            "validated": bool(getattr(resolved, "validated", False)),
            "source": source,
            "warnings": list(warnings),
            "attempts": attempts,
            "next_steps": [],
        }

    if str(r1.fail_reason or "") == "invalid_model":
        d = extract_invalid_model_diagnostics(stdout=r1.stdout, stderr=r1.stderr, snippet_limit=240)
        avail = list(d.get("available_models") or [])
        attempts.append({"phase": "invalid_model", "ok": True, "available_models": avail[:20]})
        try:
            if avail and callable(seed_runtime):
                seed_runtime(tool_name=tool, input_model=intent, available_models=avail)
        except Exception:
            pass

        try:
            _ = get_models(tool_name=tool, cwd=cwd, force_refresh=True)
            attempts.append({"phase": "force_refresh", "ok": True, "timeout_s": float(force_refresh_timeout_s or 0.0)})
        except Exception as e:
            attempts.append(
                {
                    "phase": "force_refresh",
                    "ok": False,
                    "timeout_s": float(force_refresh_timeout_s or 0.0),
                    "error_type": type(e).__name__,
                    "error": str(e) or "(empty)",
                }
            )

        try:
            r2_res = resolve_real(model_name=intent, tool_name=tool, cwd=cwd, require_valid=True)
            m2 = getattr(r2_res, "real_name", "") or ""
        except Exception:
            m2 = ""
        attempts.append({"phase": "re_resolve", "ok": bool(m2), "passthrough_model": m2 or "(auto)"})
        if m2:
            r2 = _run_once(m2, phase="retry_after_refresh")
            if r2.ok:
                return {
                    "ok": True,
                    "decision": "ttadk_code_ok_after_refresh",
                    "fail_reason": "",
                    "tool": tool,
                    "input_model": intent,
                    "model": m2,
                    "validated": True,
                    "source": "force_refresh",
                    "warnings": list(warnings),
                    "attempts": attempts,
                    "next_steps": [],
                }

    r3 = _run_once(None, phase="retry_auto")
    if r3.ok:
        return {
            "ok": True,
            "decision": "ttadk_code_ok_auto",
            "fail_reason": "",
            "tool": tool,
            "input_model": intent,
            "model": "(auto)",
            "validated": False,
            "source": source,
            "warnings": list(set(list(warnings) + ["no_m_passthrough"])),
            "attempts": attempts,
            "next_steps": [],
        }

    fr = str(r3.fail_reason or r1.fail_reason or "unknown")
    return {
        "ok": False,
        "decision": "ttadk_code_failed",
        "fail_reason": fr,
        "tool": tool,
        "input_model": intent,
        "model": "(auto)",
        "validated": False,
        "source": source,
        "warnings": list(set(list(warnings) + ["ttadk_code_failed"])),
        "attempts": attempts,
        "next_steps": _next_steps_for_fail_reason(fr),
    }


def format_ttadk_code_user_message(result: dict) -> str:
    """将 execute_ttadk_code_with_repair 的结果格式化为用户可读提示（用于 handler/卡片）。"""
    r = dict(result or {})
    ok = bool(r.get("ok"))
    tool = str(r.get("tool") or "")
    input_model = str(r.get("input_model") or "")
    model = str(r.get("model") or "(auto)")
    validated = bool(r.get("validated"))
    source = str(r.get("source") or "")
    decision = str(r.get("decision") or "")
    warnings = [str(w) for w in (r.get("warnings") or []) if w]
    fail_reason = str(r.get("fail_reason") or "")
    next_steps = [str(x) for x in (r.get("next_steps") or []) if x]

    head = "TTADK 命令执行成功" if ok else "TTADK 命令执行失败"
    lines = [
        head,
        f"tool={tool} input_model={input_model}",
        f"model={model} validated={validated} source={source}",
    ]
    if decision:
        lines.append(f"decision={decision}")
    if warnings:
        lines.append(f"warnings={warnings}")
    if (not ok) and fail_reason:
        lines.append(f"fail_reason={fail_reason}")
    if (not ok) and next_steps:
        lines.append("下一步建议：" + "；".join(next_steps))

    try:
        atts = list(r.get("attempts") or [])
        for a in reversed(atts):
            s = str(a.get("stderr_snippet") or "").strip()
            if s:
                lines.append("stderr_snippet=" + s)
                break
    except Exception:
        pass

    return "\n".join([x for x in lines if x])
