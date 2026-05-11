"""TTADK 引擎会话启动编排。

从 `manager.py` 提取，保持向后兼容（manager.py 会 re-export 所有符号）。
"""

import logging
import time
from typing import Any, Callable, Optional

from ..config import get_settings
from ..utils.errors import get_error_detail
from .startup_errors import TTADKStartupError

logger = logging.getLogger(__name__)


def start_ttadk_engine_session(
    *,
    agent_type: str,
    cwd: str,
    model_intent: Optional[str],
    startup_timeout: float,
    manager: Optional["_TTADKManager"] = None,
    # Injectable deps for testing / decoupling
    start_ttadk_session_fn: Optional[Callable[..., Any]] = None,
    resolve_agent_spec_fn: Optional[Callable[..., tuple[str, list[str]]]] = None,
    precheck_fn: Optional[Callable[[str], dict]] = None,
    fallback_fn: Optional[Callable[[Exception], Any]] = None,
    get_settings_fn: Callable[[], object] = get_settings,
    time_fn: Callable[[], float] = time.time,
) -> dict:
    """Deep/Spec 引擎 TTADK 启动编排 SSOT（start/precheck/repair/degrade）。

    返回值契约：与 `coordinate_ttadk_startup()` 一致（result/tool/input_model/resolved_model/.../diagnostics）。

    说明：
    - 该函数只编排"启动期"逻辑，不处理 send_prompt 阶段的模型错误（compaction/loop/failover）。
    - 所有外部依赖默认使用局部 import，以避免在模块 import 时触发重依赖/循环依赖。

    启动期模型决策 SSOT（重要约束）：
    - "是否允许透传 -m 以及透传的真实模型名"的决策必须收敛到
      `TTADKManager.resolve_startup_model_with_diagnostics()` →
      `src.ttadk.startup_common.precheck_ttadk_startup_model()` →
      `src.ttadk.startup.coordinate_ttadk_startup()`。
    - 上层（agent_session/acp/engine/handler）不得旁路调用
      `resolve_real_model_name()` / `resolve_and_ensure_valid_model()` 来决定启动透传，
      否则会出现"各处探测/各处兜底"导致的语义漂移与回归风险。
    """
    from .manager import get_ttadk_manager

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
                get_error_detail(err)[:200],
            )
            s = _start_coco(
                agent_type="coco",
                cwd=cwd,
                startup_timeout=float(startup_timeout or 60),
                model_name=fallback_model,
            )
            try:
                s._degraded_to = "coco"
                s._degraded_reason = get_error_detail(err)[:200]
            except Exception:
                logger.debug("fallback_fn: 'coco'", exc_info=True)
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

