"""TTADK 运行期 invalid-model 修复逻辑（提炼模块）。

目标：将 `coordinate_ttadk_startup()` 中的 invalid-model 自愈分支（seed/选模/冷却/重试）
提炼为可单测、可复用的独立模块，降低 `src/ttadk/manager.py` 的多职责耦合。

约束：
- 不引入 ACP/Engine 层依赖，避免循环依赖。
- 对外返回结构与 `coordinate_ttadk_startup()` 的稳定契约保持一致。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..config import get_settings
from ..utils.errors import get_error_detail
from .models import (
    TTADKModel,
    choose_best_available_model,
    extract_available_models,
    is_invalid_model_error,
    resolve_model_id,
)
from .models import build_invalid_model_context as _build_invalid_model_context_ssot
import logging

logger = logging.getLogger(__name__)



def build_invalid_model_context(
    err: Exception,
    *,
    get_settings_fn: Callable[[], object] = get_settings,
    limit: int = 1600,
) -> dict:
    """兼容入口：委托给 `src.ttadk.models.build_invalid_model_context`（SSOT）。"""
    try:
        return dict(_build_invalid_model_context_ssot(err, get_settings_fn=get_settings_fn, limit=limit) or {})
    except Exception:
        # SSOT contract: never raises; this is extra guard for extreme import/runtime issues.
        return {
            "err_blob": "(empty)",
            "stderr_snippet": "",
            "stdout_snippet": "",
            "available_models": [],
            "is_invalid_model": False,
        }


StartFn = Callable[[Optional[str]], Any]
FallbackFn = Callable[[Exception], Any]
PrecheckFn = Callable[[str], dict]


# ---------------------------------------------------------------------------
# Repair flow helpers (private)
#
# Stable flow contract (frozen for tests/logs):
# - attempts 写入点（唯一责任归属）：
#   - _cooldown_gate(): phase=repair step=cooldown_skip
#   - _seed_models(): phase=repair step=seed_from_error
#   - _force_refresh_with_cooldown(): phase=repair step=force_refresh
#   - _pick_retry_model(): phase=repair step=resolve_after_repair
#   - _run_retry_flow(): phase=retry / retry_auto / degrade
# - repair_invalid_model_startup() 仅编排：
#   校验 invalid-model → gate → seed/refresh → precheck → pick → run_retry_flow → finalize
# ---------------------------------------------------------------------------


def _finalize_diagnostics(attempts: list[dict]) -> dict:
    return {"attempts": attempts}


def _cooldown_gate(
    *,
    manager: object,
    tool: str,
    attempts: list[dict],
    get_settings_fn: Callable[[], object],
    time_fn: Callable[[], float],
    stub_get_last_ts_fn: Optional[Callable[[object, str], float]],
    stub_set_last_ts_fn: Optional[Callable[[object, str, float], None]],
) -> bool:
    """运行期 invalid-model 修复冷却 gate。

    返回 True 表示允许继续修复；False 表示被开关/冷却抑制。
    """
    try:
        if not bool(getattr(get_settings_fn(), "ttadk_runtime_retry_enabled", True)):
            return False
    except Exception:
        logger.debug("_cooldown_gate: evaluate condition", exc_info=True)

    cooldown_s = 120.0
    try:
        cooldown_s = float(getattr(get_settings_fn(), "ttadk_runtime_retry_cooldown_s", 120.0) or 120.0)
    except Exception:
        cooldown_s = 120.0
    cooldown_s = max(0.0, cooldown_s)
    if not cooldown_s:
        return True

    now = float(time_fn())

    # 优先走 manager gate
    if hasattr(manager, "check_and_mark_runtime_invalid_model_repair"):
        try:
            allowed, _last = manager.check_and_mark_runtime_invalid_model_repair(
                tool_name=tool,
                cooldown_s=cooldown_s,
                now_ts=now,
            )
            if not allowed:
                attempts.append(
                    {
                        "phase": "repair",
                        "step": "cooldown_skip",
                        "ok": False,
                        "cooldown_s": cooldown_s,
                        "error_type": "cooldown",
                        "error": "suppressed_by_manager_gate",
                    }
                )
            return bool(allowed)
        except Exception:
            logger.debug("unexpected error", exc_info=True)
            return True

    # 再走 stub gate（由调用方注入，避免循环依赖）
    if callable(stub_get_last_ts_fn) and callable(stub_set_last_ts_fn):
        try:
            last = float(stub_get_last_ts_fn(manager, tool) or 0.0)
        except Exception:
            last = 0.0
        if last and (now - last) < cooldown_s:
            attempts.append(
                {
                    "phase": "repair",
                    "step": "cooldown_skip",
                    "ok": False,
                    "cooldown_s": cooldown_s,
                    "error_type": "cooldown",
                    "error": "suppressed_by_stub_gate",
                }
            )
            return False
        try:
            stub_set_last_ts_fn(manager, tool, now)
        except Exception:
            logger.debug("stub_set_last_ts_fn(manager, tool, now)", exc_info=True)
        return True

    # 无 gate 能力时：不阻塞修复
    return True


def _seed_models(
    *,
    manager: object,
    tool: str,
    error_blob: str,
    attempts: list[dict],
    time_fn: Callable[[], float],
) -> list[str]:
    """从 invalid-model 错误输出中提取并回灌可用模型列表（best-effort）。"""
    names: list[str] = []

    # 优先走 manager seed
    if hasattr(manager, "seed_models_from_error"):
        try:
            names = list(manager.seed_models_from_error(tool, error_blob) or [])
        except Exception:
            names = []

    if not names:
        try:
            names = extract_available_models(error_blob or "")
        except Exception:
            names = []

    if names:
        attempts.append({"phase": "repair", "step": "seed_from_error", "ok": True, "count": len(names)})
    else:
        attempts.append(
            {
                "phase": "repair",
                "step": "seed_from_error",
                "ok": False,
                "count": 0,
                "error_type": "seed_empty",
                "error": "no_models_extracted_from_error_blob",
            }
        )
    if not names:
        return []

    # best-effort 回灌到常见缓存字段（兼容测试桩）
    try:
        lock = getattr(manager, "_lock", None)
        if lock is None:
            raise RuntimeError("missing_lock")
        with lock:
            cache = getattr(manager, "_tool_models_cache", None)
            cache_time = getattr(manager, "_cache_time", None)
            known = getattr(manager, "_known_models", None)
            if isinstance(cache, dict):
                cache[tool] = [TTADKModel(name=n, description=n, friendly_name=n) for n in names]
            if isinstance(cache_time, dict):
                cache_time[tool] = float(time_fn())
            if isinstance(known, set):
                known.update(names)
            try:
                if hasattr(manager, "_save_cache_to_file"):
                    manager._save_cache_to_file()
            except Exception:
                logger.debug("evaluate condition", exc_info=True)
    except Exception:
        logger.debug("evaluate condition", exc_info=True)

    return names


def _force_refresh_with_cooldown(
    *,
    manager: object,
    tool: str,
    cwd: Optional[str],
    attempts: list[dict],
    get_settings_fn: Callable[[], object],
    time_fn: Callable[[], float],
) -> bool:
    """启动期强刷新（带冷却与失败记忆），避免频繁 refresh 造成抖动。"""
    ok = False

    now = float(time_fn())
    try:
        last_attempt = float(getattr(manager, "_startup_refresh_last_attempt", {}).get(tool, 0.0) or 0.0)
    except Exception:
        last_attempt = 0.0
    try:
        last_fail = float(getattr(manager, "_startup_refresh_last_failure", {}).get(tool, 0.0) or 0.0)
    except Exception:
        last_fail = 0.0

    cooldown_s = 60.0
    fail_cooldown_s = 120.0
    try:
        s = get_settings_fn()
        cooldown_s = float(getattr(s, "ttadk_startup_refresh_cooldown_s", 60.0) or 60.0)
        fail_cooldown_s = float(getattr(s, "ttadk_startup_refresh_fail_cooldown_s", 120.0) or 120.0)
    except Exception:
        logger.debug("_force_refresh_with_cooldown: get_settings_fn()", exc_info=True)
    cooldown_s = max(0.0, cooldown_s)
    fail_cooldown_s = max(0.0, fail_cooldown_s)

    if cooldown_s and last_attempt and (now - last_attempt) < cooldown_s:
        attempts.append(
            {
                "phase": "repair",
                "step": "force_refresh",
                "ok": False,
                "error_type": "cooldown",
                "error": "suppressed_by_startup_refresh_cooldown",
                "cooldown_s": cooldown_s,
            }
        )
        return False
    if fail_cooldown_s and last_fail and (now - last_fail) < fail_cooldown_s:
        attempts.append(
            {
                "phase": "repair",
                "step": "force_refresh",
                "ok": False,
                "error_type": "cooldown",
                "error": "suppressed_by_startup_refresh_fail_cooldown",
                "cooldown_s": fail_cooldown_s,
            }
        )
        return False

    try:
        getattr(manager, "_startup_refresh_last_attempt", {})[tool] = now
    except Exception:
        logger.debug("now", exc_info=True)

    if hasattr(manager, "refresh_models"):
        try:
            manager.refresh_models(tool_name=tool, cwd=cwd)
            ok = True
        except Exception:
            ok = False

    if not ok:
        try:
            getattr(manager, "_startup_refresh_last_failure", {})[tool] = float(time_fn())
        except Exception:
            logger.debug("convert to float", exc_info=True)

    if ok:
        attempts.append({"phase": "repair", "step": "force_refresh", "ok": True})
    else:
        attempts.append(
            {
                "phase": "repair",
                "step": "force_refresh",
                "ok": False,
                "error_type": "refresh_failed",
                "error": "refresh_models_failed_or_unavailable",
            }
        )
    return bool(ok)


def _resolve_after_repair(*, precheck_fn: PrecheckFn, intent: str) -> dict:
    """统一调用 precheck 并返回 dict（best-effort）。"""
    try:
        return dict(precheck_fn(intent) or {})
    except Exception:
        logger.debug("_resolve_after_repair: return dict(precheck_fn(intent) or...", exc_info=True)
        return {}


def _pick_retry_model(
    *,
    tool: str,
    intent: str,
    fixed: dict,
    seeded: list[str],
    attempts: list[dict],
    get_settings_fn: Callable[[], object],
) -> Optional[str]:
    """选择要透传给 start_fn 的 retry_model，并写入 resolve_after_repair attempts。"""
    retry_model: Optional[str] = fixed.get("model") if bool(fixed.get("validated")) else None
    try:
        retry_model_norm = str(retry_model or "").strip() if retry_model is not None else ""
    except Exception:
        retry_model_norm = ""

    if seeded:
        try:
            seed_set = {str(x).strip() for x in list(seeded) if str(x).strip()}
        except Exception:
            seed_set = set()
        if (not retry_model_norm) or (retry_model_norm == intent) or (seed_set and retry_model_norm not in seed_set):
            allow_autoswitch = True
            try:
                allow_autoswitch = bool(getattr(get_settings_fn(), "ttadk_runtime_retry_allow_autoswitch", True))
            except Exception:
                allow_autoswitch = True
            # 规则优先级（保持历史语义，避免行为漂移）：
            # 1) 先走既有 tool-aware 选模规则（select_retry_model），确保同前缀时优先命中 tool token 子集
            # 2) 仅当选模失败（None）时，再用 resolver 做 display/alias → model_id 的映射兜底
            retry_model = select_retry_model(
                tool_name=tool, input_model=intent, seeded=list(seeded), allow_autoswitch=allow_autoswitch
            )
            if retry_model is None:
                try:
                    desc = [
                        TTADKModel(name=str(x), description=str(x), friendly_name=str(x))
                        for x in (seeded or [])
                        if str(x).strip()
                    ]
                    r, _diag = resolve_model_id(
                        tool_name=tool, input_name=str(intent or ""), descriptors=desc, allow_unknown_passthrough=False
                    )
                    cand = str(getattr(r, "real_name", "") or "").strip()
                    if cand and str(getattr(r, "source", "") or "") != "unknown":
                        retry_model = cand
                except Exception:
                    logger.debug("_pick_retry_model: [", exc_info=True)

    attempts.append(
        {
            "phase": "repair",
            "step": "resolve_after_repair",
            "resolved_model": fixed.get("resolved_real_name") or fixed.get("model") or intent,
            "validated": bool(fixed.get("validated")),
            "source": fixed.get("source") or "unknown",
            "warnings": list(fixed.get("warnings") or []),
            "passthrough_model": retry_model,
            # SSOT：透传模型列表诊断（来自 precheck_fn 的 diagnostics）
            "models_source": (
                dict(fixed.get("diagnostics") or {}).get("source")
                if isinstance(fixed.get("diagnostics"), dict)
                else None
            ),
            "models_raw_cmd": (
                dict(fixed.get("diagnostics") or {}).get("raw_cmd")
                if isinstance(fixed.get("diagnostics"), dict)
                else None
            ),
            "models_exit_code": (
                dict(fixed.get("diagnostics") or {}).get("exit_code")
                if isinstance(fixed.get("diagnostics"), dict)
                else None
            ),
            "models_stderr_snippet": (
                dict(fixed.get("diagnostics") or {}).get("stderr_snippet")
                if isinstance(fixed.get("diagnostics"), dict)
                else None
            ),
            "models_freshness": (
                dict(fixed.get("diagnostics") or {}).get("freshness")
                if isinstance(fixed.get("diagnostics"), dict)
                else None
            ),
        }
    )

    return retry_model


def _run_retry_flow(
    *,
    tool: str,
    intent: str,
    fixed: dict,
    retry_model: Optional[str],
    start_fn: StartFn,
    fallback_fn: Optional[FallbackFn],
    attempts: list[dict],
) -> dict:
    """执行 retry→retry_auto→fallback，并返回最终结果 dict。"""

    # 1) 重试：带 real model
    try:
        r2 = start_fn(retry_model)
        attempts.append({"phase": "retry", "ok": True, "passthrough_model": retry_model})
        resolved_real_name = retry_model or fixed.get("resolved_real_name") or fixed.get("model") or intent
        return {
            "result": r2,
            "tool": tool,
            "input_model": intent,
            "resolved_real_name": resolved_real_name,
            "passthrough_model": retry_model,
            "resolved_model": retry_model or "(auto)",
            "validated": bool(fixed.get("validated")),
            "source": fixed.get("source") or "unknown",
            "warnings": list(fixed.get("warnings") or []),
            "degraded": False,
            "repaired": True,
            "fail_phase": "invalid_model",
            "decision": "invalid_model_repaired_retry_ok",
            "diagnostics": _finalize_diagnostics(attempts),
        }
    except Exception as e2:
        attempts.append({"phase": "retry", "ok": False, "error_type": type(e2).__name__, "error": get_error_detail(e2)})

        # 2) 若带 real model 仍失败，再尝试 auto
        if retry_model is not None:
            try:
                r3 = start_fn(None)
                attempts.append({"phase": "retry_auto", "ok": True})
                resolved_real_name = fixed.get("resolved_real_name") or fixed.get("model") or intent
                return {
                    "result": r3,
                    "tool": tool,
                    "input_model": intent,
                    "resolved_real_name": resolved_real_name,
                    "passthrough_model": None,
                    "resolved_model": "(auto)",
                    "validated": False,
                    "source": "auto",
                    "warnings": list((fixed.get("warnings") or []) + ["auto_model_retry"]),
                    "degraded": False,
                    "repaired": True,
                    "fail_phase": "invalid_model",
                    "decision": "invalid_model_repaired_auto_ok",
                    "diagnostics": _finalize_diagnostics(attempts),
                }
            except Exception as e3:
                attempts.append({"phase": "retry_auto", "ok": False, "error_type": type(e3).__name__})
                if callable(fallback_fn):
                    r_fb = fallback_fn(e3)
                    attempts.append({"phase": "degrade", "ok": True})
                    resolved_real_name = fixed.get("resolved_real_name") or fixed.get("model") or intent
                    return {
                        "result": r_fb,
                        "tool": tool,
                        "input_model": intent,
                        "resolved_real_name": resolved_real_name,
                        "passthrough_model": None,
                        "resolved_model": "(fallback)",
                        "validated": False,
                        "source": "fallback",
                        "warnings": ["degraded"],
                        "degraded": True,
                        "repaired": True,
                        "fail_phase": "invalid_model",
                        "decision": "invalid_model_repaired_degraded",
                        "diagnostics": _finalize_diagnostics(attempts),
                    }
                raise

        if callable(fallback_fn):
            r_fb = fallback_fn(e2)
            attempts.append({"phase": "degrade", "ok": True})
            resolved_real_name = fixed.get("resolved_real_name") or fixed.get("model") or intent
            return {
                "result": r_fb,
                "tool": tool,
                "input_model": intent,
                "resolved_real_name": resolved_real_name,
                "passthrough_model": None,
                "resolved_model": "(fallback)",
                "validated": False,
                "source": "fallback",
                "warnings": ["degraded"],
                "degraded": True,
                "repaired": True,
                "fail_phase": "invalid_model",
                "decision": "invalid_model_retry_failed_degraded",
                "diagnostics": _finalize_diagnostics(attempts),
            }
        raise


def select_retry_model(
    *,
    tool_name: str,
    input_model: str,
    seeded: list[str],
    allow_autoswitch: bool,
    choose_best_fn: Callable[[str, list[str]], Optional[str]] = lambda input_model,
    available_models: choose_best_available_model(input_model=input_model, available_models=available_models),
) -> Optional[str]:
    """从 seeded（Invalid model 输出解析出的可用模型列表）中选择要重试的真实模型名。

    规则（稳定契约，必须用单测冻结）：
    1) seeded 为空：返回 None
    2) allow_autoswitch=False：返回 seeded[0]
    3) allow_autoswitch=True：
       3.1) 若存在包含 tool token（例如 codex/claude/coco）的子集，优先在子集里 best-match
       3.2) 否则在全量 seeded 上 best-match
       3.3) best-match 失败则回退 seeded[0]
    4) 任意异常：best-effort，回退 seeded[0] 或 None，不抛异常
    """

    try:
        cand = [str(x) for x in (seeded or []) if str(x).strip()]
    except Exception:
        cand = []
    if not cand:
        return None

    if not bool(allow_autoswitch):
        return cand[0]

    tool_token = (tool_name or "").strip().lower()
    try:
        tool_cands = [m for m in cand if tool_token and (tool_token in m.lower())]
    except Exception:
        tool_cands = []

    # 先在 tool 子集里挑
    if tool_cands:
        try:
            best = choose_best_fn(str(input_model or "").strip(), list(tool_cands))
            best = str(best).strip() if best is not None else ""
            return best or tool_cands[0]
        except Exception:
            return tool_cands[0]

    # 再在全量里挑
    try:
        best = choose_best_fn(str(input_model or "").strip(), list(cand))
        best = str(best).strip() if best is not None else ""
        return best or cand[0]
    except Exception:
        return cand[0]


def repair_invalid_model_startup(
    *,
    manager: object,
    tool_name: str,
    input_model: str,
    cwd: Optional[str],
    error: Exception,
    error_blob: str,
    attempts: list[dict],
    start_fn: StartFn,
    fallback_fn: Optional[FallbackFn],
    precheck_fn: PrecheckFn,
    get_settings_fn: Callable[[], object] = get_settings,
    time_fn: Callable[[], float] = time.time,
    stub_get_last_ts_fn: Optional[Callable[[object, str], float]] = None,
    stub_set_last_ts_fn: Optional[Callable[[object, str, float], None]] = None,
) -> dict:
    """处理启动阶段命中的 Invalid model，自愈并返回稳定结果。

    调用方约束：
    - 仅当确认 `error_blob` 命中 invalid-model 时调用本函数。
    - `attempts` 为可变 list，本函数会追加 repair/retry/degrade 记录。
    """

    tool = (tool_name or "").strip().lower()
    intent = (input_model or "").strip()

    # 防御式：若调用方误调用（非 invalid-model），保持可诊断并回退抛出。
    try:
        if not is_invalid_model_error(error_blob or ""):
            raise error
    except Exception:
        # 若检测逻辑自身失败，仍按 invalid-model 继续执行（best-effort）。
        pass

    if not _cooldown_gate(
        manager=manager,
        tool=tool,
        attempts=attempts,
        get_settings_fn=get_settings_fn,
        time_fn=time_fn,
        stub_get_last_ts_fn=stub_get_last_ts_fn,
        stub_set_last_ts_fn=stub_set_last_ts_fn,
    ):
        if callable(fallback_fn):
            r_fb = fallback_fn(error)
            attempts.append({"phase": "degrade", "ok": True})
            return {
                "result": r_fb,
                "tool": tool,
                "input_model": intent,
                "resolved_real_name": intent,
                "passthrough_model": None,
                "resolved_model": "(fallback)",
                "validated": False,
                "source": "fallback",
                "warnings": ["degraded", "runtime_repair_disabled"],
                "degraded": True,
                "repaired": False,
                "fail_phase": "invalid_model",
                "decision": "invalid_model_degraded_runtime_repair_disabled",
                "diagnostics": _finalize_diagnostics(attempts),
            }
        raise error

    seeded = _seed_models(manager=manager, tool=tool, error_blob=error_blob, attempts=attempts, time_fn=time_fn)
    if not seeded:
        _ = _force_refresh_with_cooldown(
            manager=manager,
            tool=tool,
            cwd=cwd,
            attempts=attempts,
            get_settings_fn=get_settings_fn,
            time_fn=time_fn,
        )

    fixed = _resolve_after_repair(precheck_fn=precheck_fn, intent=intent)
    retry_model = _pick_retry_model(
        tool=tool, intent=intent, fixed=fixed, seeded=list(seeded), attempts=attempts, get_settings_fn=get_settings_fn
    )
    return _run_retry_flow(
        tool=tool,
        intent=intent,
        fixed=fixed,
        retry_model=retry_model,
        start_fn=start_fn,
        fallback_fn=fallback_fn,
        attempts=attempts,
    )
