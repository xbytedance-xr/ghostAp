"""ACP-driven Spec Engine — structured methodology with iterative review.

Follows spec-kit methodology: each cycle progresses through
spec → plan → task → build → review. Review suggestions feed back
as input for the next cycle. Terminates when all criteria are satisfied
and all review perspectives pass.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from ..acp import ACPEvent, ACPEventType, ACPEventRenderer
from ..agent_session import SyncSession, close_session_safely, create_engine_session
from ..config import get_settings
from ..deep_engine.models import EngineRunState
from ..loop_engine.models import (
    ReviewPerspective,
    PerspectiveReview,
    ReviewResult,
)
from ..utils.spec_utils import (
    CRITERIA_PATTERNS as _CRITERIA_PATTERNS,
    PERSPECTIVE_TAG_MAP as _PERSPECTIVE_TAG_MAP,
    extract_json_blob,
    normalize_list,
    parse_review_output_strict_tolerant,
    parse_review_output_loose,
    validate_plan_artifact_dict,
    validate_spec_artifact_dict,
)

from .models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    SpecCycle,
    SpecTask,
    SpecArtifact,
    PlanArtifact,
    SpecWorkItem,
    SpecWorkItemStatus,
    SpecCycleMetrics,
)
from .retry import RetryPolicy, should_retry, get_retry_delay
from .task_persistence import SpecTaskState, save_task_state, delete_task_state, generate_task_id
from .tracker import PhaseTracker
from ..coco_model import get_coco_model_manager

logger = logging.getLogger(__name__)

# Pre-compiled regex for task parsing
_TASK_LINE_PATTERN = re.compile(
    r"^\s*(\d+)\s*[.、)]\s*(.+?)(?:\s*\(\s*(?:依赖|depends?)\s*[:：]?\s*(.*?)\s*\))?\s*$",
    re.IGNORECASE,
)


@dataclass
class SpecEngineCallbacks:
    """Spec Engine event callbacks."""

    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[SpecProject], None]] = None
    on_cycle_start: Optional[Callable[[int, int], None]] = None  # (current, max)
    on_phase_start: Optional[Callable[[int, SpecPhase], None]] = None
    on_phase_event: Optional[Callable[[int, SpecPhase, ACPEvent], None]] = None
    on_phase_done: Optional[Callable[[int, SpecPhase, str], None]] = None
    on_review_done: Optional[Callable[[int, ReviewResult], None]] = None
    on_cycle_done: Optional[Callable[[int, SpecCycle], None]] = None
    on_project_done: Optional[Callable[[SpecProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_retry: Optional[Callable[[int, str], None]] = None
    on_model_switch: Optional[Callable[[str, str], None]] = None
    on_task_saved: Optional[Callable[[str], None]] = None


@dataclass
class ContinuationPolicy:
    """Decides whether to continue the next spec-kit optimization cycle."""

    max_cycles: int
    infinite_mode: bool = False
    disable_convergence: bool = False
    disable_early_stop: bool = False
    min_cycles: int = 1

    def should_stop(
        self,
        cycle_num: int,
        all_satisfied: bool,
        review_passed: bool,
        converged: bool,
        metrics: SpecCycleMetrics,
    ) -> Optional[str]:
        # Force at least min_cycles if we haven't hit max_cycles
        if cycle_num < self.min_cycles and cycle_num < self.max_cycles:
            return None

        # Success always stops
        if all_satisfied and review_passed:
            # Bug fix: don't stop if we have pending work items (backlog)
            # This allows spec-kit discovery mechanism to drive further iterations.
            if metrics.backlog_pending > 0:
                return None
            return "success"

        # Infinite mode: never stop due to convergence/early stop
        if self.infinite_mode:
            return None

        if (not self.disable_convergence) and converged:
            return "converged"

        if self.disable_early_stop:
            return None

        # Secondary guard: low improvement space + empty backlog for several cycles
        if (
            (not all_satisfied)
            and metrics.improvement_space <= 0.2
            and metrics.backlog_pending == 0
            and metrics.new_satisfied == 0
            and cycle_num >= 3
        ):
            return "converged"
        return None


class SpecEngine:
    """ACP-driven structured development engine with iterative review cycles."""

    def __init__(self, chat_id: str, root_path: str,
                 agent_type: str = "coco", engine_name: str = "Coco",
                 model_name: Optional[str] = None):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name
        self._agent_type = agent_type
        self._model_name = model_name

        self._session: Optional[SyncSession] = None
        self._project: Optional[SpecProject] = None
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._user_guidance: list[str] = []
        self._last_review: Optional[ReviewResult] = None
        # success / paused / converged / max_cycles / stopped
        self._termination_reason: Optional[str] = None
        self._resume_meta: Optional[dict] = None
        self._retry_policy = RetryPolicy()
        self._models_tried: list[str] = []
        self._current_model: Optional[str] = None
        self._saved_task_id: Optional[str] = None
        # best-effort: carry review exception diagnostics to cycle/metrics
        self._last_review_failure_diag: Optional[dict] = None
        # review failure circuit breaker (best-effort)
        self._review_failure_consecutive: int = 0
        self._review_circuit_open_until_cycle: int = 0

    @property
    def project(self) -> Optional[SpecProject]:
        return self._project

    @property
    def run_state(self) -> EngineRunState:
        return self._run_state

    @property
    def is_running(self) -> bool:
        return self._run_state != EngineRunState.IDLE

    def _close_session_safely(self) -> None:
        close_session_safely(self._session)
        self._session = None

    def _get_bool_setting(self, name: str, default: bool = False) -> bool:
        """Safely read bool settings.

        注意：测试里经常用 MagicMock 作为 settings，缺失字段会返回新的 MagicMock（truthy），
        这里强制要求类型为 bool，否则回退到 default。
        """
        try:
            v = getattr(self.settings, name, default)
            return v if isinstance(v, bool) else default
        except Exception:
            return default

    def _get_int_setting(self, name: str, default: int) -> int:
        """Safely read int settings (avoid MagicMock truthiness / type drift)."""
        try:
            v = getattr(self.settings, name, default)
            if isinstance(v, bool):
                return default
            if isinstance(v, int):
                return v
            # Only accept numeric-ish primitives; avoid unittest.mock (MagicMock is int-castable to 1).
            if isinstance(v, float):
                return int(v)
            if isinstance(v, str):
                try:
                    return int(v.strip())
                except Exception:
                    return default
            return default
        except Exception:
            return default

    # ------------------------------------------------------------------
    # Review failure diagnostics (SSOT)
    # ------------------------------------------------------------------

    # 审查异常诊断/日志稳定字段契约（SSOT）
    #
    # 目标：避免出现线上日志 `[Spec] 多视角审查异常: , 将继续循环` 这种“异常信息为空”的不可观测情况。
    # 约定：
    # - stable 字段为唯一“对外契约”（日志/metrics/持久化 state 消费方应优先使用 stable）。
    # - compat 字段仅用于迁移窗口期的写入与历史状态读取（读取时由 normalize 统一兜底映射）。
    #
    # 迁移窗口期（退场计划）：
    # - 当前：写入 stable，并可选保留 compat 写入（降低线上回归风险）。
    # - 退场条件：连续 3 次全量回归稳定（或 1 个发布窗口）后，停止写入 compat；读取 compat 保持长期兼容。
    #
    # 所有 review 异常日志（包括熔断跳过）必须输出以下 stable 字段，且 `err_repr/error_text` 永不为空：
    # - phase: 发生阶段（固定为 review）
    # - role: 发生子阶段/角色（当前为 multi_perspective）
    # - cycle: 当前循环号
    # - decision: continue/skip/circuit_open 等决策
    # - err_type: 异常类型（或 ReviewCircuitOpen）
    # - err_repr: repr(exception)（空则回退为 <ExceptionType>）
    # - error_text: 面向人类的摘要（空则回退为 err_repr）

    _REVIEW_DIAG_STABLE_KEYS = (
        "phase",
        "role",
        "cycle",
        "decision",
        "fail_reason",
        "err_type",
        "err_repr",
        "error_text",
        "traceback_snippet",
    )

    _REVIEW_DIAG_COMPAT_KEYS = (
        "cycle_number",
        "exception_type",
        "review_role",
    )

    _REVIEW_EXCEPTION_LOG_FIELDS = (
        "phase",
        "role",
        "cycle",
        "decision",
        "fail_reason",
        "err_type",
        "err_repr",
        "error_text",
        "diag_json",
    )
    @staticmethod
    def _safe_str(x: object) -> str:
        try:
            return str(x)
        except Exception:
            return ""

    @staticmethod
    def _truncate_text(text: str, max_len: int, suffix: str = "...") -> str:
        try:
            s = str(text or "")
        except Exception:
            return ""
        if max_len <= 0:
            return ""
        if len(s) <= max_len:
            return s
        if len(suffix) >= max_len:
            return s[:max_len]
        return s[: max_len - len(suffix)] + suffix

    def _build_review_exception_diagnostics(self, e: Exception, *, cycle: int) -> dict:
        """构造多视角审查异常诊断信息（保证可序列化、error_text 非空）。"""

        # Reuse ACP diagnostics config for redaction/truncation limits.
        try:
            from ..acp.diagnostics import get_diagnostics_config, redact_text

            cfg = get_diagnostics_config(get_settings_fn=get_settings)
            redact_enabled = bool(getattr(cfg, "redact_enabled", True))
            redact_patterns = list(getattr(cfg, "redact_patterns", []) or [])
            redact_repl = str(getattr(cfg, "redact_replacement", "***REDACTED***") or "***REDACTED***")
            cfg_snip = int(getattr(cfg, "snippet_limit", 240) or 240)
            cfg_total = int(getattr(cfg, "total_limit", 2000) or 2000)
        except Exception:
            redact_text = None  # type: ignore[assignment]
            redact_enabled, redact_patterns, redact_repl = True, [], "***REDACTED***"
            cfg_snip, cfg_total = 240, 2000

        def _truncate_strict(s: str, lim: int) -> str:
            try:
                lim = int(lim or 0)
            except Exception:
                lim = 0
            if lim <= 0:
                return ""
            ss = self._safe_str(s)
            if not ss:
                return ""
            if len(ss) <= lim:
                return ss
            suffix = "…(truncated)"
            if lim <= len(suffix):
                return ss[:lim]
            return ss[: max(0, lim - len(suffix))] + suffix

        def _redact_and_truncate(text: str, *, hard_limit: int, cfg_limit: int) -> str:
            lim = hard_limit
            try:
                lim = int(hard_limit or 0)
            except Exception:
                lim = 0
            lim = max(1, lim)
            try:
                cfg_lim = int(cfg_limit or 0)
            except Exception:
                cfg_lim = 0
            if cfg_lim > 0:
                lim = min(lim, cfg_lim)

            s = self._safe_str(text)
            if redact_enabled and callable(redact_text):
                try:
                    s = redact_text(s, redact_patterns, redact_repl)  # type: ignore[misc]
                except Exception:
                    pass
            return _truncate_strict(s, lim)

        def _extract_error_text(err: Exception) -> str:
            base = (self._safe_str(err) or "").strip()
            if base:
                return base
            # 兼容：上游异常可能 message 为空，但携带 stderr/stdout 片段
            for k in ("stderr_snippet", "stdout_snippet", "stderr", "stdout", "message", "detail"):
                try:
                    v = (self._safe_str(getattr(err, k, "")) or "").strip()
                    if v:
                        return v
                except Exception:
                    continue
            return ""

        def _infer_fail_reason(err: Exception) -> str:
            """Best-effort, stable-ish failure reason string for logs/metrics."""
            et = "Exception"
            try:
                et = type(err).__name__
            except Exception:
                et = "Exception"
            # Timeout-ish
            if isinstance(err, TimeoutError):
                return "timeout"
            if et in ("TimeoutExpired", "ReadTimeout", "ConnectTimeout"):
                return "timeout"
            # Parsing-ish
            if et in ("JSONDecodeError",):
                return "parse_json"
            if et in ("ValueError", "TypeError"):
                return "parse_error"
            return "exception"

        def _extract_err_repr(err: Exception) -> str:
            # repr 也可能为空/抛异常（极端 mock / 自定义异常），必须兜底
            err_type = "Exception"
            try:
                err_type = type(err).__name__
            except Exception:
                err_type = "Exception"
            try:
                s = repr(err)
            except Exception:
                s = ""
            s = (self._safe_str(s) or "").strip()
            if not s:
                s = f"<{err_type}>"
            return s

        err_repr = _extract_err_repr(e)
        err_type = "Exception"
        try:
            err_type = type(e).__name__
        except Exception:
            err_type = "Exception"

        error_text = _extract_error_text(e)
        if not (error_text or "").strip():
            # 若 message/snippet 都为空，至少输出明确的“空 message”提示
            error_text = f"{err_type} (empty message)"

        fail_reason = _infer_fail_reason(e)
        tb = ""
        try:
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        except Exception:
            tb = ""
        tb = (tb or "").strip()

        # 脱敏+截断：遵循 diagnostics 配置上限（hard_limit 只允许更严格）
        err_repr_rt = _redact_and_truncate(err_repr, hard_limit=600, cfg_limit=cfg_snip)
        if not (err_repr_rt or "").strip():
            err_repr_rt = f"<{err_type}>"
        error_text_rt = _redact_and_truncate(error_text, hard_limit=600, cfg_limit=cfg_snip)
        if not (error_text_rt or "").strip():
            error_text_rt = err_repr_rt

        # 兼容字段（旧字段名） + 新字段名（稳定字段契约）并存，降低回归风险
        diag = {
            # Stable contract
            "phase": "review",
            "role": "multi_perspective",
            "cycle": int(cycle or 0),
            "decision": "review_failed_continue",
            "fail_reason": str(fail_reason or "exception"),
            "err_type": err_type,
            "err_repr": err_repr_rt,
            "error_text": error_text_rt,

            # Backward-compatible keys (do not remove)
            "cycle_number": int(cycle or 0),
            "exception_type": err_type,
            "review_role": "multi_perspective",

            # traceback_snippet 用 total_limit 作为上限（避免写入过大）
            "traceback_snippet": _redact_and_truncate(tb, hard_limit=1600, cfg_limit=cfg_total),
            "project": (getattr(self._project, "name", "") or "").strip() if self._project else "",
            "chat_id": (self.chat_id or ""),
            "root_path": (self.root_path or ""),
            "agent_type": (self._agent_type or ""),
        }
        # 尽量补充 session_id（不强依赖）
        try:
            diag["session_id"] = str(getattr(self._session, "session_id", "") or "")
        except Exception:
            diag["session_id"] = ""

        return diag


    @classmethod
    def _normalize_review_diagnostics(cls, diag: object) -> dict:
        """将 review diagnostics 规范化为 stable 字段（SSOT）。

        语义：
        - 输入可能来自：本版本产出的 diag、历史 state/artifacts（仅含 compat 字段）、或测试桩。
        - 输出必须只包含 stable 字段（见 `_REVIEW_DIAG_STABLE_KEYS`），且关键字段永不为空：
          `err_type/err_repr/error_text/cycle/decision/phase/role`。

        注意：
        - 本函数不做重型操作，且 best-effort 不抛异常。
        - 脱敏/截断：沿用上游 `_build_review_exception_diagnostics` 已做的处理；若输入未脱敏，
          此处只做“兜底截断”，不做额外 regex redact（避免重复成本与不一致）。
        """

        d = diag if isinstance(diag, dict) else {}

        def _s(x: object) -> str:
            try:
                return str(x) if x is not None else ""
            except Exception:
                try:
                    return repr(x)
                except Exception:
                    return ""

        # phase/role
        phase = (_s(d.get("phase")) or "review").strip() or "review"
        role = (_s(d.get("role")) or _s(d.get("review_role")) or "multi_perspective").strip() or "multi_perspective"

        # cycle: stable=cycle, compat=cycle_number
        cycle_val: int = 0
        try:
            if "cycle" in d and d.get("cycle") is not None:
                cycle_val = int(d.get("cycle") or 0)
            else:
                cycle_val = int(d.get("cycle_number") or 0)
        except Exception:
            cycle_val = 0

        # decision
        decision = (_s(d.get("decision")) or "review_failed_continue").strip() or "review_failed_continue"

        # fail_reason: stable=fail_reason (best-effort, optional)
        fail_reason = (_s(d.get("fail_reason")) or "").strip()
        if not fail_reason:
            fail_reason = "exception" if decision.startswith("review_failed") else ""

        # err_type: stable=err_type, compat=exception_type
        err_type = (_s(d.get("err_type")) or _s(d.get("exception_type")) or "Exception").strip() or "Exception"

        # err_repr: stable=err_repr, fallback <err_type>
        err_repr = (_s(d.get("err_repr")) or "").strip()
        if not err_repr:
            err_repr = f"<{err_type}>"

        # error_text: stable=error_text, fallback err_repr
        error_text = (_s(d.get("error_text")) or "").strip()
        if not error_text:
            error_text = err_repr

        tb = (_s(d.get("traceback_snippet")) or "").strip()

        out = {
            "phase": phase,
            "role": role,
            "cycle": int(cycle_val),
            "decision": decision,
            "fail_reason": fail_reason,
            "err_type": err_type,
            "err_repr": err_repr,
            "error_text": error_text,
            "traceback_snippet": tb,
        }

        # Ensure output only includes stable keys
        try:
            return {k: out.get(k) for k in cls._REVIEW_DIAG_STABLE_KEYS}
        except Exception:
            return out


    @classmethod
    def _format_review_exception_log_line(cls, diag: dict, *, diag_json: str) -> str:
        """格式化 review 异常日志单行（SSOT）。

        约束：
        - 输入 diag 应为 stable（可先调用 `_normalize_review_diagnostics`）。
        - 输出必须包含 stable 字段，并保证关键字段永不为空。
        - diag_json 可能较长：这里做“兜底截断”，脱敏/截断优先依赖上游 diagnostics。
        """

        d = cls._normalize_review_diagnostics(diag)

        def _s(x: object) -> str:
            try:
                return str(x) if x is not None else ""
            except Exception:
                try:
                    return repr(x)
                except Exception:
                    return ""

        phase = (_s(d.get("phase")) or "review").strip() or "review"
        role = (_s(d.get("role")) or "multi_perspective").strip() or "multi_perspective"
        decision = (_s(d.get("decision")) or "review_failed_continue").strip() or "review_failed_continue"
        fail_reason = (_s(d.get("fail_reason")) or "").strip()
        err_type = (_s(d.get("err_type")) or "Exception").strip() or "Exception"
        err_repr = (_s(d.get("err_repr")) or "").strip() or f"<{err_type}>"
        error_text = (_s(d.get("error_text")) or "").strip() or err_repr

        cycle_val = 0
        try:
            cycle_val = int(d.get("cycle") or 0)
        except Exception:
            cycle_val = 0

        # best-effort truncate for diag_json (avoid overly long log lines)
        dj = _s(diag_json)
        try:
            if len(dj) > 2400:
                dj = dj[:2400] + "…(truncated)"
        except Exception:
            pass

        return (
            f"[Spec] review_exception: phase={phase} role={role} cycle={cycle_val} decision={decision} fail_reason={fail_reason} "
            f"err_type={err_type} err_repr={err_repr} error_text={error_text} diag={dj}, 将继续循环"
        )

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------
    def execute(
        self,
        requirement_text: str,
        callbacks: Optional[SpecEngineCallbacks] = None,
        task_id: Optional[str] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> SpecProject:
        """Run the spec engine: analyze → cycle(spec→plan→task→build→review) → repeat."""
        callbacks = callbacks or SpecEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._on_rate_limit = on_rate_limit
        max_cycles = self._resolve_max_cycles(self.settings.spec_max_cycles)
        self._termination_reason = None

        project_name = os.path.basename(self.root_path) or "spec_project"
        self._project = SpecProject.create(name=project_name, root_path=self.root_path)
        self._project.task_id = task_id
        self._project.status = SpecProjectStatus.ANALYZING
        self._project.requirement = requirement_text

        if callbacks.on_analyzing_start:
            callbacks.on_analyzing_start(requirement_text)

        logger.info("[Spec:%s] 启动, 需求长度=%d, 路径=%s, agent=%s",
                    project_name, len(requirement_text), self.root_path, self._agent_type)

        try:
            self._current_model = get_coco_model_manager().get_current_model()
            self._models_tried = [self._current_model] if self._current_model else []

            # Parse requirement — extract acceptance criteria
            criteria = self._parse_acceptance_criteria(requirement_text)
            self._project.acceptance_criteria = criteria
            self._project.criteria_tracker.init_criteria(criteria)
            self._project.status = SpecProjectStatus.RUNNING
            self._project.started_at = time.time()

            if callbacks.on_analyzing_done:
                callbacks.on_analyzing_done(self._project)

            # Create ACP session
            self._session = create_engine_session(
                agent_type=self._agent_type, cwd=self.root_path,
                on_rate_limit=on_rate_limit,
                model_name=self._model_name,
            )

            self._last_review = None

            self._termination_reason = self._run_cycle_loop(
                start_cycle=1,
                max_cycles=max_cycles,
                callbacks=callbacks,
                timeout=self.settings.spec_execution_timeout,
                first_raw_input=requirement_text,
            )

            # Determine final status
            reason = self._termination_reason or "max_cycles"
            if self._run_state == EngineRunState.STOPPING or reason == "paused":
                self._project.status = SpecProjectStatus.PAUSED
            elif reason == "success":
                self._project.status = SpecProjectStatus.COMPLETED
                self._project.completed_at = time.time()
            else:
                # Converged/max-cycles without satisfying requirements
                if reason == "converged":
                    msg = (
                        f"收敛终止：连续{self.settings.spec_convergence_window}轮无有效改进，"
                        "仍有未满足验收标准或审查未通过"
                    )
                    self._project.abort(msg)
                elif reason == "max_cycles":
                    # Check criteria via project tracker
                    is_all_satisfied = self._project.is_all_satisfied
                    
                    # Check review status via last review result
                    last_review_passed = True
                    if self.settings.spec_review_enabled:
                         if self._last_review:
                             last_review_passed = self._last_review.all_passed
                         else:
                             # 只有当 review enabled 但没有 review result 时（比如第一轮就失败），算未通过
                             last_review_passed = False 

                    if is_all_satisfied and last_review_passed:
                        # 核心目标达成，但因为 backlog 没跑完导致次数耗尽
                        msg = f"达到最大循环次数({max_cycles})。核心验收标准已满足，但仍有待办优化项（Backlog）。"
                        self._project.status = SpecProjectStatus.PAUSED
                        self._project.error = msg + "（已暂停，可使用 /spec_resume 继续执行优化项）"
                        self._project.completed_at = time.time()
                    else:
                        msg = f"达到最大循环次数({max_cycles})仍未满足验收标准或审查未通过"
                        # Infinite mode: treat max-cycles as a pause point instead of abort
                        if self._get_bool_setting("spec_infinite_mode", False):
                            self._project.status = SpecProjectStatus.PAUSED
                            self._project.error = msg + "（已暂停，可继续 /spec_resume 或提升 SPEC_MAX_CYCLES）"
                            self._project.completed_at = time.time()
                        else:
                            self._project.abort(msg)
                else:
                    msg = f"终止：{reason}"
                    self._project.abort(msg)

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"Spec执行异常: {str(e)}"
            logger.error("[Spec:%s] %s", project_name, error_msg)
            if self._project:
                self._project.status = SpecProjectStatus.ABORTED
                self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE
            if (
                self._project
                and self._project.status == SpecProjectStatus.COMPLETED
                and self._saved_task_id
            ):
                delete_task_state(self._saved_task_id)
                self._saved_task_id = None

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------
    def _run_phase(
        self,
        cycle_num: int,
        phase: SpecPhase,
        prompt: str,
        callbacks: SpecEngineCallbacks,
        timeout: int,
    ) -> str:
        """Execute a single phase: send prompt, collect output, return text."""
        if callbacks.on_phase_start:
            callbacks.on_phase_start(cycle_num, phase)

        max_retries = int(getattr(self.settings, "spec_max_retries", 3) or 3)
        attempt = 0
        last_error: Optional[str] = None

        while True:
            try:
                tracker = PhaseTracker()

                def on_event(event: ACPEvent):
                    tracker.process(event)
                    self._renderer.process_event(event)
                    if callbacks.on_phase_event:
                        callbacks.on_phase_event(cycle_num, phase, event)

                self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)
                output = tracker.text_buffer

                if callbacks.on_phase_done:
                    callbacks.on_phase_done(cycle_num, phase, output)

                return output

            except Exception as e:
                last_error = str(e)
                attempt += 1

                if not should_retry(e) or attempt > max_retries:
                    if self._try_switch_model(callbacks):
                        attempt = 0
                        continue

                    task_id = self._save_failed_task(last_error, cycle_num, phase, callbacks)
                    raise RuntimeError(
                        f"Phase {phase.value} 失败，任务已保存(task_id={task_id}): {last_error}"
                    ) from e

                if callbacks.on_retry:
                    callbacks.on_retry(attempt, last_error)

                delay = get_retry_delay(attempt - 1, self._retry_policy)
                logger.info("[Spec] Phase %s 失败，%ds 后重试 (attempt=%d/%d): %s",
                           phase.value, delay, attempt, max_retries, last_error)
                time.sleep(delay)

    def _try_switch_model(self, callbacks: SpecEngineCallbacks) -> bool:
        # TTADK: 使用 TTADKManager 的可用模型 + 真实名解析
        if (self._agent_type or "").startswith("ttadk"):
            from ..ttadk import get_ttadk_manager
            from ..utils.path import normalize_ttadk_cwd

            ttadk_manager = get_ttadk_manager()
            tool_name = (self._agent_type or "").replace("ttadk_", "")
            result = ttadk_manager.get_models(cwd=normalize_ttadk_cwd(self.root_path), tool_name=tool_name)
            all_models = [m.name for m in result.models]
        else:
            model_manager = get_coco_model_manager()
            result = model_manager.get_models()
            all_models = [m.name for m in result.models]

        available = [m for m in all_models if m not in self._models_tried]
        if not available:
            return False

        old_model = self._current_model or "(unknown)"
        new_model = available[0]

        if not model_manager.set_model(new_model):
            return False

        self._models_tried.append(new_model)
        self._current_model = new_model

        self._close_session_safely()
        self._session = create_engine_session(
            agent_type=self._agent_type,
            cwd=self.root_path,
            on_rate_limit=getattr(self, "_on_rate_limit", None),
            model_name=self._current_model or self._model_name,
        )

        logger.info("[Spec] 模型切换: %s -> %s", old_model, new_model)
        if callbacks.on_model_switch:
            callbacks.on_model_switch(old_model, new_model)

        return True

    def _save_failed_task(
        self,
        error: str,
        cycle_num: int,
        phase: SpecPhase,
        callbacks: SpecEngineCallbacks,
    ) -> str:
        task_id = generate_task_id()
        state = SpecTaskState(
            task_id=task_id,
            created_at=time.time(),
            requirement=self._project.requirement if self._project else "",
            project_path=self.root_path,
            chat_id=self.chat_id,
            agent_type=self._agent_type,
            current_cycle=cycle_num,
            current_phase=phase.value,
            last_error=error,
            retry_count=int(getattr(self.settings, "spec_max_retries", 3) or 3),
            models_tried=list(self._models_tried),
            project_snapshot=self._project_to_compact_dict() if self._project else None,
        )
        save_task_state(state)
        self._saved_task_id = task_id

        logger.info("[Spec] 任务已保存, task_id=%s, phase=%s, error=%s",
                   task_id, phase.value, error[:100])
        if callbacks.on_task_saved:
            callbacks.on_task_saved(task_id)

        return task_id

    # ------------------------------------------------------------------
    # Cycle loop (shared by execute / resume)
    # ------------------------------------------------------------------
    def _run_cycle_loop(
        self,
        start_cycle: int,
        max_cycles: int,
        callbacks: SpecEngineCallbacks,
        timeout: int,
        first_raw_input: Optional[str] = None,
    ) -> str:
        """Execute spec cycles. Modifies ``self._project`` in-place.

        Returns a termination reason:
        - success: all criteria satisfied and (review disabled or all PASS)
        - paused: user stopped/pause requested
        - converged: no measurable progress in window
        - max_cycles: hit max_cycles without success
        - stopped: engine stopped without a cycle (edge)
        """
        requirement = self._project.requirement
        termination: str = "max_cycles"

        policy = ContinuationPolicy(
            max_cycles=max_cycles,
            infinite_mode=self._get_bool_setting("spec_infinite_mode", False),
            disable_convergence=self._get_bool_setting("spec_disable_convergence", False),
            disable_early_stop=self._get_bool_setting("spec_disable_early_stop", False),
            # Spec mode defaults to at least 2 cycles to ensure discovery;
            # allow overriding via settings for single-cycle tasks/tests.
            min_cycles=max(1, self._get_int_setting("spec_min_cycles", 2)),
        )

        for cycle_num in range(start_cycle, max_cycles + 1):
            if self._run_state != EngineRunState.RUNNING:
                termination = "paused" if self._run_state == EngineRunState.STOPPING else "stopped"
                break

            cycle = SpecCycle(cycle_number=cycle_num)

            if callbacks.on_cycle_start:
                callbacks.on_cycle_start(cycle_num, max_cycles)

            # Pick next work item (spec file) if any; otherwise refine from requirement.
            work_item = self._pick_next_work_item(cycle_num)

            # First cycle of a fresh execute uses raw requirement
            if first_raw_input is not None and cycle_num == start_cycle:
                spec_input = first_raw_input
            elif work_item and work_item.spec_path:
                spec_input = self._build_input_from_spec_file(requirement, work_item)
            else:
                spec_input = self._build_refinement_input(requirement)

            # --- SPEC PHASE ---
            cycle.phase = SpecPhase.SPEC
            if work_item and work_item.spec_path and self._should_load_spec_directly(work_item):
                # spec 文件本身就是 spec-kit 规格产物：直接加载进入下一阶段
                spec_output = self._read_text_file_best_effort(work_item.spec_path)
            else:
                spec_output = self._run_phase(
                    cycle_num, SpecPhase.SPEC,
                    self._build_spec_prompt(spec_input),
                    callbacks, timeout,
                )
            cycle.spec_content = self._truncate_output(spec_output)
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.spec_path = self._persist_cycle_artifact(cycle_num, "spec", spec_output, ext="json")
            cycle.spec_artifact, cycle.spec_artifact_errors = self._parse_spec_artifact(spec_output)

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()

            if work_item:
                work_item.status = SpecWorkItemStatus.DONE
                work_item.used_in_cycle = cycle_num
                self._append_history_event(
                    "work_item_consumed",
                    {
                        "cycle": cycle_num,
                        "item_id": work_item.item_id,
                        "question": work_item.question,
                        "spec_path": work_item.spec_path,
                    },
                )

            # If the spec provides better acceptance criteria, merge into project.
            if cycle_num == 1 and cycle.spec_artifact and cycle.spec_artifact.acceptance_criteria:
                self._merge_acceptance_criteria(cycle.spec_artifact.acceptance_criteria)

            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- PLAN PHASE ---
            cycle.phase = SpecPhase.PLAN
            plan_output = self._run_phase(
                cycle_num, SpecPhase.PLAN,
                self._build_plan_prompt(spec_output),
                callbacks, timeout,
            )
            cycle.plan_content = self._truncate_output(plan_output)
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.plan_path = self._persist_cycle_artifact(cycle_num, "plan", plan_output, ext="json")
            cycle.plan_artifact, cycle.plan_artifact_errors = self._parse_plan_artifact(plan_output)

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()
            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- TASK PHASE ---
            cycle.phase = SpecPhase.TASK
            task_output = self._run_phase(
                cycle_num, SpecPhase.TASK,
                self._build_task_prompt(plan_output),
                callbacks, timeout,
            )
            parsed_tasks = self._parse_tasks(task_output)
            cycle.tasks_total = len(parsed_tasks)
            cycle.tasks = parsed_tasks[: self.settings.spec_cycle_tasks_max]
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.tasks_path = self._persist_cycle_artifact(
                    cycle_num, "tasks", json.dumps([t.to_dict() for t in parsed_tasks], ensure_ascii=False, indent=2), ext="json"
                )

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()
            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- BUILD PHASE ---
            cycle.phase = SpecPhase.BUILD
            build_output = self._run_phase(
                cycle_num, SpecPhase.BUILD,
                self._build_build_prompt(cycle.tasks, plan_output),
                callbacks, timeout,
            )
            cycle.build_output = self._truncate_output(build_output)
            if self._get_bool_setting("spec_persist_phase_artifacts", True):
                cycle.build_path = self._persist_cycle_artifact(cycle_num, "build", build_output, ext="txt")

            if self._get_bool_setting("spec_persist_every_phase", True):
                self._persist_state_best_effort()
            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- REVIEW PHASE (conditional, like loop engine) ---
            review_passed = True
            if self.settings.spec_review_enabled:
                cycle.phase = SpecPhase.REVIEW
                review_result = self._conduct_review(cycle_num, callbacks)
                cycle.review_result = review_result
                # best-effort: persist review failure decision/diagnostics for traceability
                diag = self._last_review_failure_diag
                if isinstance(diag, dict) and diag:
                    cycle.review_decision = str(diag.get("decision") or "review_failed_continue")
                    cycle.review_diagnostics = dict(diag)
                if self._get_bool_setting("spec_persist_phase_artifacts", True):
                    cycle.review_path = self._persist_cycle_artifact(cycle_num, "review", self._review_result_to_text(review_result), ext="txt")
                self._last_review = review_result
                review_passed = review_result.all_passed

                if self._get_bool_setting("spec_persist_every_phase", True):
                    self._persist_state_best_effort()

            cycle.complete()

            self._project.cycles.append(cycle)
            self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)

            if callbacks.on_cycle_done:
                callbacks.on_cycle_done(cycle_num, cycle)

            logger.info("[Spec:%s] 循环 %d/%d 完成, 审查=%s",
                        self._project.name, cycle_num, max_cycles,
                        f"{cycle.review_result.total_suggestions}条建议" if cycle.review_result else "跳过")

            # --- CRITERIA EVALUATION ---
            criteria_result = self._evaluate_criteria(
                self._project.acceptance_criteria, cycle_num)
            all_satisfied = criteria_result.get("all_satisfied", False)

            # --- POST-CYCLE PROBLEM DISCOVERY + SPEC GENERATION ---
            if self._get_bool_setting("spec_discovery_enabled", True) and self._run_state == EngineRunState.RUNNING:
                discovery = self._discover_optimization_questions(cycle_num)
                cycle.discovery_path = self._persist_cycle_artifact(cycle_num, "discovery", json.dumps(discovery, ensure_ascii=False, indent=2), ext="json")
                new_items = self._generate_specs_from_discovery(cycle_num, discovery)
                # 防止 backlog 无限制膨胀：只保留最近 N 条（长期任务可配合外部清理）
                if new_items:
                    self._project.work_items.extend(new_items)
                    self._project.work_items_total = max(self._project.work_items_total, len(self._project.work_items))
                    for wi in new_items:
                        self._append_history_event(
                            "work_item_generated",
                            {
                                "cycle": cycle_num,
                                "item_id": wi.item_id,
                                "question": wi.question,
                                "spec_path": wi.spec_path,
                            },
                        )

            # --- METRICS SNAPSHOT (monitoring) ---
            metrics = self._compute_cycle_metrics(cycle)
            self._project.metrics_history.append(metrics)
            cycle.metrics_path = self._persist_cycle_artifact(cycle_num, "metrics", json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2), ext="json")

            self._append_history_event(
                "cycle_done",
                {
                    "cycle": cycle_num,
                    "status": cycle.status,
                    "satisfied": metrics.satisfied_count,
                    "total": metrics.total_criteria,
                    "new_satisfied": metrics.new_satisfied,
                    "review_suggestions": metrics.review_suggestions,
                    "goal_attainment": metrics.goal_attainment,
                    "improvement_space": metrics.improvement_space,
                    "backlog_pending": metrics.backlog_pending,
                },
            )

            self._persist_state_best_effort()
            self._cleanup_old_cycle_artifacts(cycle_num)
            self._cleanup_generated_specs()

            # --- TERMINATION CHECK (ContinuationPolicy) ---
            converged = False if policy.disable_convergence else self._detect_convergence()
            if converged:
                logger.info("[Spec:%s] 收敛检测触发, 循环 %d 轮",
                            self._project.name, cycle_num)

            decision = policy.should_stop(
                cycle_num=cycle_num,
                all_satisfied=all_satisfied,
                review_passed=review_passed,
                converged=converged,
                metrics=metrics,
            )
            if decision == "success":
                logger.info("[Spec:%s] 所有标准+审查通过, 循环 %d 轮",
                            self._project.name, cycle_num)
                termination = "success"
                break
            if decision == "converged":
                termination = "converged"
                break

        return termination

    # ------------------------------------------------------------------
    # Long-range: work items, discovery, spec generation
    # ------------------------------------------------------------------
    def _resolve_max_cycles(self, requested: int) -> int:
        try:
            requested = int(requested)
        except Exception:
            requested = 10
        
        limit = 5000
        try:
            val = getattr(self.settings, "spec_max_cycles_limit", 5000)
            if val is not None:
                limit = int(val)
        except Exception:
            limit = 5000
            
        if limit <= 0:
            limit = 5000
        if requested <= 0:
            requested = 1
        return min(requested, limit)

    def _pick_next_work_item(self, cycle_num: int) -> Optional[SpecWorkItem]:
        if not self._project:
            return None
        for wi in self._project.work_items:
            if wi.status == SpecWorkItemStatus.PENDING and wi.spec_path:
                wi.status = SpecWorkItemStatus.IN_PROGRESS
                wi.used_in_cycle = cycle_num
                return wi
        return None

    def _should_load_spec_directly(self, work_item: SpecWorkItem) -> bool:
        # 仅当 spec_path 指向已生成的 spec-kit JSON 产物时才直读。
        path = (work_item.spec_path or "").lower()
        return path.endswith(".json")

    def _build_input_from_spec_file(self, original_requirement: str, work_item: SpecWorkItem) -> str:
        spec_text = self._read_text_file_best_effort(work_item.spec_path)
        return (
            f"## 长程任务目标\n{original_requirement}\n\n"
            f"## 本轮优化关注点（由问题发现机制生成）\n{work_item.question}\n\n"
            f"## 已生成的 Spec 产物（供参考，可修正）\n{spec_text}\n"
        )

    def _discover_optimization_questions(self, cycle_num: int) -> list[dict]:
        """在每轮 spec-kit 循环后触发“自我提问”。"""
        if not self._session or not self._project:
            return []

        tracker = self._project.criteria_tracker
        unsatisfied = tracker.unsatisfied_criteria
        pending_suggestions: list[str] = []
        if self._last_review:
            for pr in self._last_review.failed_perspectives:
                for s in pr.suggestions:
                    if s:
                        pending_suggestions.append(str(s))
        unsat_text = "\n".join(f"- {c}" for c in unsatisfied[:12]) if unsatisfied else "(无)"
        sugg_text = "\n".join(f"- {s}" for s in pending_suggestions[:12]) if pending_suggestions else "(无)"

        prompt = f"""你是一个长期任务的自我改进系统（Spec-kit 驱动）。

请在本轮 spec-kit 实现后，自动发现与目标相关的“可优化问题”，并提出下一步要解决的关键问题。

## 长程目标
{self._project.requirement}

## 当前未满足的验收标准（Top）
{unsat_text}

## 上轮审查未通过的建议（Top）
{sugg_text}

## 输出要求（必须严格遵守）
仅输出一个 JSON 数组，放在 ```json fenced code block``` 中，不要输出任何其他文字。

每个元素 schema：
{{
  "id": "Q-...",
  "question": "与目标相关的具体可优化问题（可落到代码/测试/鲁棒性/性能/体验/可维护性）",
  "why": "为什么重要（1-2句）",
  "priority": "P0|P1|P2"
}}

约束：
- 每个 question 必须“可行动”（能转成下一轮 spec-kit 的 Spec 任务单元）
- 优先覆盖未满足验收标准与审查建议
- 数量 1~{int(self.settings.spec_discovery_max_questions or 5)}
"""

        chunks: list[str] = []

        def on_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                chunks.append(event.text)

        try:
            self._session.send_prompt(prompt, on_event=on_event, timeout=120)
            raw = "".join(chunks)
            blob = extract_json_blob(raw)
            data = json.loads(blob) if blob else None
            if isinstance(data, list):
                cleaned: list[dict] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    q = str(item.get("question", "")).strip()
                    if not q:
                        continue
                    cleaned.append({
                        "id": str(item.get("id") or f"Q-{cycle_num}-{len(cleaned)+1}"),
                        "question": q,
                        "why": str(item.get("why", "")).strip(),
                        "priority": str(item.get("priority", "P1")).strip().upper(),
                    })
                if cleaned:
                    return cleaned[: int(self.settings.spec_discovery_max_questions or 5)]
        except Exception as e:
            logger.debug("[Spec] 问题发现机制失败: %s", e)

        # 强制非空：保证每轮都能产出可优化问题
        if self._get_bool_setting("spec_discovery_force_nonempty", True) and self._project:
            fallback_q = None
            if unsatisfied:
                fallback_q = f"如何满足验收标准：{unsatisfied[0]}？"
            elif pending_suggestions:
                fallback_q = f"如何落实改进建议：{pending_suggestions[0]}？"
            else:
                fallback_q = "当前实现还有哪些可测试性/可维护性/鲁棒性方面的改进空间？"
            return [{
                "id": f"Q-{cycle_num}-1",
                "question": fallback_q,
                "why": "兜底：保证长程任务每轮都有明确的下一步优化方向",
                "priority": "P1",
            }]

        return []

    def _generate_specs_from_discovery(self, cycle_num: int, discovery: list[dict]) -> list[SpecWorkItem]:
        if not self._project:
            return []
        if not discovery:
            return []

        max_specs = int(getattr(self.settings, "spec_generated_specs_per_cycle", 3) or 3)
        selected = discovery[:max_specs]

        if not self._session:
            return []

        prompt = f"""你是一个 spec-kit 规格生成器。

请把下面这些“可优化问题”拆解为符合 spec-kit 的 Spec 任务单元，并为每个问题生成一个 Spec JSON 产物。

## 长程目标
{self._project.requirement}

## 可优化问题列表
{json.dumps(selected, ensure_ascii=False, indent=2)}

## 输出要求（必须严格遵守）
仅输出一个 JSON 数组，放在 ```json fenced code block``` 中，不要输出任何其他文字。

数组元素 schema：
{{
  "id": "Q-...", 
  "spec": {{
    "goals": ["..."],
    "functional_spec": ["..."],
    "non_functional_requirements": ["..."],
    "acceptance_criteria": ["可验证条件..."],
    "out_of_scope": ["..."],
    "risks": ["..."],
    "clarification_questions": ["..."],
    "decisions": ["..."],
    "version": "1.0"
  }}
}}
"""

        chunks: list[str] = []

        def on_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                chunks.append(event.text)

        items: list[SpecWorkItem] = []
        try:
            self._session.send_prompt(prompt, on_event=on_event, timeout=180)
            raw = "".join(chunks)
            blob = extract_json_blob(raw)
            data = json.loads(blob) if blob else None
            if not isinstance(data, list):
                data = []

            # Map id -> question
            id_to_question = {str(d.get("id")): str(d.get("question", "")).strip() for d in selected}

            for entry in data:
                if not isinstance(entry, dict):
                    continue
                qid = str(entry.get("id") or "").strip()
                spec = entry.get("spec")
                if not qid or not isinstance(spec, dict):
                    continue
                # validate and persist
                errors = validate_spec_artifact_dict(spec)
                if errors:
                    # still persist for traceability
                    logger.debug("[Spec] 生成的 spec 产物不合规(qid=%s): %s", qid, errors[:3])
                question = id_to_question.get(qid) or qid
                spec_text = json.dumps(spec, ensure_ascii=False, indent=2)
                spec_path = self._persist_generated_spec_file(cycle_num, qid, spec_text)
                items.append(SpecWorkItem(
                    item_id=qid,
                    question=question,
                    created_in_cycle=cycle_num,
                    spec_path=spec_path,
                    status=SpecWorkItemStatus.PENDING,
                ))

        except Exception as e:
            logger.debug("[Spec] spec 生成失败: %s", e)

        # Fallback: 如果生成失败，至少把问题落盘为 minimal spec 文件
        if not items and self._get_bool_setting("spec_discovery_force_nonempty", True):
            for d in selected[:max_specs]:
                qid = str(d.get("id") or f"Q-{cycle_num}-{uuid.uuid4().hex[:4]}")
                question = str(d.get("question") or "").strip() or qid
                minimal = {
                    "goals": [f"解决问题：{question}"],
                    "functional_spec": ["实现必要的改动以满足问题要求"],
                    "non_functional_requirements": ["不引入回归，保持可测试性"],
                    "acceptance_criteria": [f"问题被解决：{question}"],
                    "out_of_scope": [],
                    "risks": [],
                    "clarification_questions": [],
                    "decisions": ["假设：允许基于现有实现做增量改进"],
                    "version": "1.0",
                }
                spec_path = self._persist_generated_spec_file(cycle_num, qid, json.dumps(minimal, ensure_ascii=False, indent=2))
                items.append(SpecWorkItem(
                    item_id=qid,
                    question=question,
                    created_in_cycle=cycle_num,
                    spec_path=spec_path,
                    status=SpecWorkItemStatus.PENDING,
                ))

        return items

    # ------------------------------------------------------------------
    # Persistence helpers (state + artifacts)
    # ------------------------------------------------------------------
    def _persist_state_best_effort(self) -> None:
        if not self._project:
            return
        try:
            self.save_state(self._get_state_path())
        except Exception as e:
            logger.debug("[Spec] 保存状态失败: %s", e)

    def _get_state_path(self) -> str:
        filename = getattr(self.settings, "spec_state_filename", ".spec_engine_state.json")
        return os.path.join(self.root_path, filename)

    def _artifact_root_dir(self) -> str:
        dirname = getattr(self.settings, "spec_artifacts_dirname", ".spec_engine")
        pid = self._project.project_id if self._project else "unknown"
        return os.path.join(self.root_path, dirname, pid)

    def _history_log_path(self) -> str:
        root = self._artifact_root_dir()
        os.makedirs(root, exist_ok=True)
        filename = getattr(self.settings, "spec_history_log_filename", "history.jsonl")
        return os.path.join(root, filename)

    def _append_history_event(self, event_type: str, payload: dict) -> None:
        """Append an event to the per-project history log (JSONL)."""
        try:
            path = self._history_log_path()
            record = {
                "ts": time.time(),
                "type": event_type,
                "project_id": self._project.project_id if self._project else "",
                "cycle": int(payload.get("cycle") or 0),
                "payload": payload,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _persist_cycle_artifact(self, cycle_num: int, name: str, content: str, ext: str = "txt") -> Optional[str]:
        if not content and name not in ("metrics", "discovery"):
            return None
        try:
            root = self._artifact_root_dir()
            cycle_dir = os.path.join(root, f"cycle_{cycle_num:04d}")
            os.makedirs(cycle_dir, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)
            path = os.path.join(cycle_dir, f"{safe_name}.{ext}")
            tmp = path + ".tmp"

            # Bound disk artifact size as well (keep the system stable for 5k+ cycles)
            persist_max = int(getattr(self.settings, "spec_phase_output_persist_max_chars", 20000) or 20000)
            to_write = content or ""
            if persist_max > 0 and len(to_write) > persist_max:
                to_write = to_write[:persist_max] + "\n...\n(已截断，超长输出未全部落盘)"

            with open(tmp, "w", encoding="utf-8") as f:
                f.write(to_write)
            os.replace(tmp, path)
            return path
        except Exception as e:
            logger.debug("[Spec] 落盘产物失败(%s): %s", name, e)
            return None

    def _cleanup_old_cycle_artifacts(self, current_cycle: int) -> None:
        """Keep only the latest N cycle directories (generated_specs are kept forever)."""
        try:
            retention = int(getattr(self.settings, "spec_cycle_artifact_retention", 50) or 0)
            if retention <= 0:
                return
            root = self._artifact_root_dir()
            cutoff = current_cycle - retention
            if cutoff <= 0:
                return
            old_dir = os.path.join(root, f"cycle_{cutoff:04d}")
            if os.path.isdir(old_dir):
                shutil.rmtree(old_dir, ignore_errors=True)

            # Avoid stale paths in persisted state
            if self._project and self._project.cycles:
                for c in self._project.cycles:
                    if c.cycle_number == cutoff:
                        c.spec_path = None
                        c.plan_path = None
                        c.tasks_path = None
                        c.build_path = None
                        c.review_path = None
                        c.discovery_path = None
                        c.metrics_path = None
                        break
        except Exception:
            return

    def _cleanup_generated_specs(self) -> None:
        """Retention for generated spec files.

        规则：
        - 永远保留 PENDING/IN_PROGRESS 的 work_item spec 文件
        - 对已 DONE 的 work_item，仅保留最近 N 个（按 created_at）
        """
        if not self._project:
            return
        try:
            retention = int(getattr(self.settings, "spec_generated_specs_retention", 1000) or 0)
            if retention <= 0:
                return

            keep_paths: set[str] = set()
            for w in self._project.work_items:
                if w.status in (SpecWorkItemStatus.PENDING, SpecWorkItemStatus.IN_PROGRESS):
                    if w.spec_path:
                        keep_paths.add(w.spec_path)

            done = [w for w in self._project.work_items if w.status == SpecWorkItemStatus.DONE and w.spec_path]
            done.sort(key=lambda x: x.created_at)
            keep_done = done[-retention:]
            for w in keep_done:
                keep_paths.add(w.spec_path)

            # Delete extra DONE spec files
            for w in done[:-retention]:
                p = w.spec_path
                if not p or p in keep_paths:
                    continue
                try:
                    if os.path.exists(p):
                        os.remove(p)
                    w.spec_deleted = True
                except Exception:
                    continue
        except Exception:
            return

    def _persist_generated_spec_file(self, cycle_num: int, qid: str, spec_text: str) -> str:
        root = self._artifact_root_dir()
        spec_dir = os.path.join(root, "generated_specs")
        os.makedirs(spec_dir, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", qid)[:80] or f"Q_{cycle_num}"
        path = os.path.join(spec_dir, f"cycle_{cycle_num:04d}_{safe_id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(spec_text or "{}")
        os.replace(tmp, path)
        return path

    @staticmethod
    def _read_text_file_best_effort(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _truncate_output(self, text: str) -> str:
        max_chars = int(getattr(self.settings, "spec_cycle_output_max_chars", 4000) or 4000)
        if max_chars <= 0:
            return text or ""
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...\n(已截断，完整内容见落盘产物)"

    # ------------------------------------------------------------------
    # Monitoring metrics
    # ------------------------------------------------------------------
    def _compute_cycle_metrics(self, cycle: SpecCycle) -> SpecCycleMetrics:
        if not self._project:
            return SpecCycleMetrics(
                cycle_number=cycle.cycle_number,
                satisfied_count=0,
                total_criteria=0,
                new_satisfied=0,
                review_suggestions=0,
                backlog_pending=0,
                goal_attainment=0.0,
                improvement_space=0.0,
            )

        tracker = self._project.criteria_tracker
        satisfied = tracker.satisfied_count
        total = tracker.total_count

        prev_satisfied = 0
        if self._project.metrics_history:
            prev_satisfied = self._project.metrics_history[-1].satisfied_count
        new_satisfied = max(0, satisfied - prev_satisfied)

        review_suggestions = 0
        if cycle.review_result:
            review_suggestions = int(cycle.review_result.total_suggestions)

        # Review failure observability (best-effort)
        review_decision = str(getattr(cycle, "review_decision", "") or "")
        # 约定：review_failed* 表示审查执行异常（含开启熔断）；review_circuit_open_skip 仅表示跳过。
        review_failed = bool(review_decision) and review_decision.startswith("review_failed")
        review_exception_type = ""
        review_error_text = ""
        try:
            diag = getattr(cycle, "review_diagnostics", None)
            if isinstance(diag, dict):
                d = self._normalize_review_diagnostics(diag)
                review_exception_type = str(d.get("err_type") or "")
                review_error_text = str(d.get("error_text") or "")
        except Exception:
            pass

        backlog_pending = sum(1 for w in self._project.work_items if w.status == SpecWorkItemStatus.PENDING)

        # 目标达成度：以验收标准为主（0~1），审查可作为次要信号
        criteria_ratio = (satisfied / total) if total else 0.0
        review_ratio = 1.0 if (cycle.review_result and cycle.review_result.all_passed) else 0.0
        goal_attainment = min(1.0, max(0.0, criteria_ratio * 0.8 + review_ratio * 0.2))

        # 优化空间评估：若本轮有增量进展或 backlog 尚有待办，则认为仍有空间
        improvement_space = 0.0
        if new_satisfied > 0:
            improvement_space = 1.0
        elif backlog_pending > 0:
            improvement_space = 0.7
        elif review_suggestions > 0:
            improvement_space = 0.5
        else:
            improvement_space = 0.2

        termination_hint = ""
        if goal_attainment >= 0.999 and improvement_space <= 0.2:
            termination_hint = "可终止：目标达成度高且优化空间小"

        return SpecCycleMetrics(
            cycle_number=cycle.cycle_number,
            satisfied_count=satisfied,
            total_criteria=total,
            new_satisfied=new_satisfied,
            review_suggestions=review_suggestions,
            backlog_pending=backlog_pending,
            goal_attainment=goal_attainment,
            improvement_space=improvement_space,
            termination_hint=termination_hint,
            review_failed=review_failed,
            review_decision=review_decision,
            review_exception_type=review_exception_type,
            review_error_text=review_error_text,
        )

    @staticmethod
    def _review_result_to_text(review: ReviewResult) -> str:
        if not review:
            return ""
        lines: list[str] = []
        for pr in review.reviews:
            verdict = "PASS" if pr.passed else "FAIL"
            lines.append(f"[{pr.perspective.name}] {verdict}")
            for s in pr.suggestions:
                lines.append(f"- {s}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------
    def _build_spec_prompt(self, requirement: str) -> str:
        """Phase 1: Analyze requirement into structured spec (WHAT & WHY, not HOW)."""
        guidance = self._consume_guidance()
        criteria_status = self._format_criteria_status()
        return f"""你是一个专业的软件架构师。请使用 spec-kit 风格产出“规格（Spec）”。

目标：只描述 **做什么/为什么/范围与约束**，不讨论具体怎么实现。

## 需求
{requirement}

## 工作目录
{self.root_path}
{guidance}{criteria_status}
## 输出要求（必须严格遵守）
仅输出一个 JSON 对象，放在 ```json fenced code block``` 中，不要输出任何其他文字。

Schema（字段必须存在；数组元素为字符串）：
{{
  "goals": ["..."],
  "functional_spec": ["..."],
  "non_functional_requirements": ["..."],
  "acceptance_criteria": ["可验证条件..."],
  "out_of_scope": ["明确不做什么..."],
  "risks": ["风险/约束..."],
  "clarification_questions": ["已识别的模糊点（仅记录，不等待用户回答）..."],
  "decisions": ["已确认/可接受的假设（必须显式标注为假设）..."],
  "version": "1.0"
}}

约束：
- acceptance_criteria 必须可被判定 PASS/FAIL
- 遇到信息不足时，不要停下等待用户——基于项目上下文和行业最佳实践自主选择最优方案
- 将模糊点记录在 clarification_questions 中（仅供参考），将你的决策记录在 decisions 中
- 如果用户引导中提供了相关信息，优先使用用户的指示
"""

    def _build_plan_prompt(self, spec: str) -> str:
        """Phase 2: Generate technical plan from spec."""
        return f"""你是一个资深工程师。基于下述 Spec（规格），产出 Plan（规划），强调可执行、可验证。

## Spec 输入
{spec}

## 工作目录
{self.root_path}

## 输出要求（必须严格遵守）
仅输出一个 JSON 对象，放在 ```json fenced code block``` 中，不要输出任何其他文字。

Schema（字段必须存在；数组元素为字符串）：
{{
  "architecture": "总体架构与关键决策（文本）",
  "tech_stack": ["语言/框架/库..."],
  "steps": ["按优先级的一句话步骤..."],
  "file_changes": ["新增/修改文件路径..."],
  "test_plan": ["将新增/更新的测试与验证方式..."],
  "risks": ["风险与应对..."],
  "version": "1.0"
}}
"""

    def _build_task_prompt(self, plan: str) -> str:
        """Phase 3: Break plan into granular tasks."""
        return f"""将以下实现方案分解为可执行的具体任务。

## 实现方案
{plan}

## 输出要求
请输出结构化的任务列表，每个任务包含：
- 任务编号（1, 2, 3, ...）
- 任务描述（一句话）
- 依赖的任务编号（如果有）

格式（严格遵循）：
1. [任务描述] (依赖: 无)
2. [任务描述] (依赖: 1)
3. [任务描述] (依赖: 1, 2)
...

要求：
- 每个任务应可独立测试
- 任务粒度适中，不要过大或过小
- 标注依赖关系以确定执行顺序
"""

    def _build_build_prompt(self, tasks: list[SpecTask], plan: str) -> str:
        """Phase 4: Execute tasks (agent writes actual code)."""
        task_list = "\n".join(
            f"{t.task_id}. {t.description}"
            for t in tasks
        )
        guidance = self._consume_guidance()
        return f"""按以下任务列表逐步执行实现。

## 实现方案
{plan}

## 任务列表
{task_list}

## 工作目录
{self.root_path}
{guidance}
## 要求
1. 严格按照任务顺序执行
2. 每个任务完成后进行自检
3. 确保代码质量：无安全漏洞、有适当的错误处理
4. 完成所有任务后输出总结
"""

    def _build_review_prompt(self) -> str:
        """Build the multi-perspective review prompt (same as loop engine)."""
        perspective_sections = []
        for p in ReviewPerspective:
            # Spec mode: make PRODUCT perspective more "Apple-like" (tasteful, high bar).
            if p == ReviewPerspective.PRODUCT:
                perspective_sections.append(
                    "- **PRODUCT**: Apple 风格产品审查（高审美/高标准/完美主义）。关注：信息架构与心智模型、关键路径是否一气呵成、默认行为是否聪明、边界与异常是否体面、文案是否克制清晰、细节一致性与打磨程度。"
                )
            else:
                perspective_sections.append(f"- **{p.value.upper()}**: {p.review_focus}")
        perspectives_desc = "\n".join(perspective_sections)

        goal = self._project.requirement if self._project else ""

        return f"""请从以下五个视角审查当前的实现质量，并给出结构化的审查结果。

## 项目目标
{goal}

## 审查视角
{perspectives_desc}

## PRODUCT 视角加严要求（Apple 风格）
- 以“少即是多”的审美判断：删繁就简，不为功能堆砌找理由。
- 以默认体验为王：默认路径必须顺滑、可预期、可解释；拒绝把复杂度转嫁给用户。
- 以细节一致性为底线：命名/状态/交互/错误提示/边界行为必须统一。
- 以体面为标准：失败与异常也要有尊严（清晰提示、可恢复、不给用户添堵）。
- 建议要具体可落地：每条建议最好能对应到 1 个明确改动点（文案/交互/流程/边界/信息层级）。

<output_format>
严格按照以下格式输出每个视角的审查结果（每个视角占一个区块）。
不要使用 markdown 表格、JSON、编号列表等任何其他格式。
必须使用 [TAG] 作为区块分隔符。

[ARCHITECT]
PASS 或 FAIL
- 改进建议1（如果FAIL）
- 改进建议2（如果FAIL）

[PRODUCT]
PASS 或 FAIL
- 改进建议1（如果FAIL）

[USER]
PASS 或 FAIL
- 改进建议1（如果FAIL）

[TESTER]
PASS 或 FAIL
- 改进建议1（如果FAIL）

[DESIGNER]
PASS 或 FAIL
- 改进建议1（如果FAIL）
- 改进建议2（如果FAIL）
- (请重点关注: UI视觉、交互体验、移动端适配)
</output_format>

<example>
[ARCHITECT]
PASS

[PRODUCT]
FAIL
- 缺少错误提示文案
- 搜索结果无分页

[USER]
PASS

[TESTER]
FAIL
- 缺少边界条件测试

[DESIGNER]
FAIL
- 按钮间距过小，容易误触
- 错误提示颜色对比度不足
</example>

## 审查标准
- PASS: 该视角认为当前实现质量良好，无需改进
- FAIL: 该视角发现可改进之处，请列出具体建议
- 建议应具体、可操作，而非泛泛而谈
- 如果某视角为 PASS，不需要列出建议
"""

    def _build_refinement_input(self, original_requirement: str) -> str:
        """Build refined input from review suggestions for next cycle."""
        lines = [f"## 原始需求\n{original_requirement}\n"]

        if self._last_review:
            failed = self._last_review.failed_perspectives
            if failed:
                lines.append("## 上轮审查改进建议\n以下建议需要在本轮 Spec 循环中解决：\n")
                for pr in failed:
                    lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**:")
                    for s in pr.suggestions:
                        lines.append(f"  - {s}")
                    lines.append("")

        # Include criteria progress
        if self._project:
            tracker = self._project.criteria_tracker
            unsatisfied = tracker.unsatisfied_criteria
            if unsatisfied:
                lines.append("## 未满足的验收标准\n")
                for c in unsatisfied:
                    lines.append(f"- [ ] {c}")
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Artifact parsing (spec-kit inspired)
    # ------------------------------------------------------------------
    def _parse_spec_artifact(self, text: str) -> tuple[Optional[SpecArtifact], list[str]]:
        """Parse spec JSON artifact.

        Returns: (artifact|None, validation_errors)
        """
        blob = extract_json_blob(text)
        if not blob:
            return None, ["未找到 ```json``` 规格产物；已降级为纯文本"]
        try:
            data = json.loads(blob)
        except Exception as e:
            return None, [f"规格 JSON 解析失败：{e}"]
        if not isinstance(data, dict):
            return None, ["规格 JSON 不是对象；已降级为纯文本"]

        errors = validate_spec_artifact_dict(data)
        artifact = SpecArtifact.from_dict({
            "goals": normalize_list(data.get("goals")),
            "functional_spec": normalize_list(data.get("functional_spec")),
            "non_functional_requirements": normalize_list(data.get("non_functional_requirements")),
            "acceptance_criteria": normalize_list(data.get("acceptance_criteria")),
            "out_of_scope": normalize_list(data.get("out_of_scope")),
            "risks": normalize_list(data.get("risks")),
            "clarification_questions": normalize_list(data.get("clarification_questions")),
            "decisions": normalize_list(data.get("decisions")),
        })
        return artifact, errors

    def _parse_plan_artifact(self, text: str) -> tuple[Optional[PlanArtifact], list[str]]:
        """Parse plan JSON artifact.

        Returns: (artifact|None, validation_errors)
        """
        blob = extract_json_blob(text)
        if not blob:
            return None, ["未找到 ```json``` 规划产物；已降级为纯文本"]
        try:
            data = json.loads(blob)
        except Exception as e:
            return None, [f"规划 JSON 解析失败：{e}"]
        if not isinstance(data, dict):
            return None, ["规划 JSON 不是对象；已降级为纯文本"]

        errors = validate_plan_artifact_dict(data)
        artifact = PlanArtifact.from_dict({
            "architecture": data.get("architecture", ""),
            "tech_stack": normalize_list(data.get("tech_stack")),
            "steps": normalize_list(data.get("steps")),
            "file_changes": normalize_list(data.get("file_changes")),
            "test_plan": normalize_list(data.get("test_plan")),
            "risks": normalize_list(data.get("risks")),
        })
        return artifact, errors

    def _merge_acceptance_criteria(self, new_criteria: list[str]) -> None:
        """Apply criteria from the spec artifact.

        spec-kit 的验收标准应来自 Spec 产物本身，因此在第一轮我们倾向于
        **用 Spec 的 acceptance_criteria 替换** 之前从用户输入中提取的
        fallback 列表（避免形成“双重标准”导致永远无法满足）。

        仅在以下情况下生效：
        - 还未有任何标准被满足（satisfied_count == 0）
        - 新标准不是明显的占位符（如 "CRITERIA_1" 这种标签）
        """
        if not self._project:
            return

        incoming = [c.strip() for c in (new_criteria or []) if c and c.strip()]
        if not incoming:
            return

        # Filter obvious placeholders
        placeholder_pat = re.compile(r"^CRITERIA_\d+$", re.IGNORECASE)
        non_placeholder = [c for c in incoming if not placeholder_pat.match(c)]
        if not non_placeholder:
            return

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for c in non_placeholder:
            if c in seen:
                continue
            seen.add(c)
            deduped.append(c)

        if self._project.criteria_tracker.satisfied_count == 0:
            self._project.acceptance_criteria = deduped
            self._project.criteria_tracker.init_criteria(deduped)

    # ------------------------------------------------------------------
    # Requirement parsing (reuses loop pattern)
    # ------------------------------------------------------------------
    def _parse_acceptance_criteria(self, text: str) -> list[str]:
        """Extract acceptance criteria from user input."""
        lines = text.strip().split("\n")
        criteria = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                criteria.append(stripped[2:])
            elif stripped.startswith("[ ] ") or stripped.startswith("[x] "):
                criteria.append(stripped[4:])

        if not criteria:
            criteria = self._decompose_criteria_with_llm(text)

        if not criteria:
            criteria = [f"完成需求: {text[:100]}"]

        return criteria

    def _decompose_criteria_with_llm(self, text: str) -> list[str]:
        """Use LLM to decompose colloquial input into acceptance criteria."""
        settings = self.settings
        # 测试里常用最小 settings stub；这里必须容忍缺失字段。
        if not getattr(settings, "ark_api_key", "") or not getattr(settings, "ark_model", ""):
            return []

        prompt = f"""请分析以下用户需求，提取并拆解为明确的验收标准。

用户需求（口语化描述）：
{text}

要求：
1. 先理解用户的核心诉求
2. 将需求拆解为 3-8 条具体、可验证的验收标准
3. 每条标准应该是独立可验证的（能明确判断 PASS/FAIL）
4. 标准应覆盖用户提到的所有功能点
5. 用简洁的技术语言描述，不要过于笼统

输出格式（严格按此格式，每行一条，以 "- " 开头）：
- 验收标准1
- 验收标准2
- 验收标准3
..."""

        try:
            llm = ChatOpenAI(
                base_url=getattr(settings, "ark_base_url", None),
                api_key=getattr(settings, "ark_api_key", ""),
                model=getattr(settings, "ark_model", ""),
                temperature=0.1,
            )
            response = llm.invoke([
                SystemMessage(content="你是一个需求分析助手，擅长将口语化的产品需求拆解为结构化的验收标准。"),
                HumanMessage(content=prompt),
            ])
            return self._extract_criteria_from_llm_response(response.content)
        except Exception as e:
            logger.warning("[Spec] LLM 需求拆解失败: %s, 将使用原始文本", e)
            return []

    @staticmethod
    def _extract_criteria_from_llm_response(text: str) -> list[str]:
        """Extract criteria lines from LLM response."""
        criteria = []
        for line in text.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                criterion = stripped[2:].strip()
            elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".、):） ":
                criterion = stripped[2:].strip()
            elif len(stripped) > 3 and stripped[:2].isdigit() and stripped[2] in ".、):） ":
                criterion = stripped[3:].strip()
            else:
                continue
            if criterion:
                criteria.append(criterion)
        return criteria

    # ------------------------------------------------------------------
    # Task parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_tasks(text: str) -> list[SpecTask]:
        """Parse numbered task list from agent output."""
        tasks = []
        for line in text.strip().split("\n"):
            m = _TASK_LINE_PATTERN.match(line)
            if not m:
                continue
            task_id = int(m.group(1))
            description = m.group(2).strip().strip("[]")
            deps_str = (m.group(3) or "").strip()
            dependencies = []
            if deps_str and deps_str.lower() not in ("无", "none", ""):
                for dep in re.split(r"[,，\s]+", deps_str):
                    dep = dep.strip()
                    if dep.isdigit():
                        dependencies.append(int(dep))
            tasks.append(SpecTask(
                task_id=task_id,
                description=description,
                dependencies=dependencies,
            ))
        return tasks

    # ------------------------------------------------------------------
    # Criteria evaluation (reuses loop pattern)
    # ------------------------------------------------------------------
    def _evaluate_criteria(self, criteria: list[str], cycle: int) -> dict:
        """Evaluate acceptance criteria by asking the agent in the same session."""
        if not self._session:
            return {"all_satisfied": False}

        criteria_list = "\n".join(f"CRITERIA_{i+1}: {c}" for i, c in enumerate(criteria))
        eval_prompt = f"""请评估以下验收标准是否已满足：
{criteria_list}

对每个标准回答 PASS 或 FAIL，严格按照以下格式回复（每行一个）：
CRITERIA_1: PASS
CRITERIA_2: FAIL
...
"""
        try:
            eval_text: list[str] = []

            def on_eval_event(event: ACPEvent):
                if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                    eval_text.append(event.text)

            self._session.send_prompt(eval_prompt, on_event=on_eval_event, timeout=60)
            full_text = "".join(eval_text).upper()

            per_criteria: dict[int, bool] = {}
            for i in range(len(criteria)):
                pat = _CRITERIA_PATTERNS[i] if i < len(_CRITERIA_PATTERNS) else re.compile(rf"CRITERIA_{i+1}\s*:\s*(PASS|FAIL)")
                match = pat.search(full_text)
                if match:
                    per_criteria[i] = (match.group(1) == "PASS")

            if self._project:
                self._project.criteria_tracker.batch_update(per_criteria, cycle)

            all_satisfied = self._project.criteria_tracker.is_all_satisfied if self._project else False
            return {"all_satisfied": all_satisfied}

        except Exception as e:
            logger.debug("[Spec] 验收标准评估失败: %s", e)
            return {"all_satisfied": False}

    # ------------------------------------------------------------------
    # Review (reuses loop engine's parsing infrastructure)
    # ------------------------------------------------------------------
    def _conduct_review(self, cycle: int, callbacks: SpecEngineCallbacks) -> ReviewResult:
        """Conduct multi-perspective review in the same ACP session."""
        # Optional circuit breaker: suppress repeated review failures.
        enabled = self._get_bool_setting("spec_review_failure_circuit_enabled", False)
        max_consecutive = max(1, self._get_int_setting("spec_review_failure_max_consecutive", 3))
        cooldown_cycles = max(0, self._get_int_setting("spec_review_failure_cooldown_cycles", 3))

        if enabled and int(self._review_circuit_open_until_cycle or 0) and int(cycle or 0) <= int(self._review_circuit_open_until_cycle or 0):
            diag_raw = {
                # Stable contract
                "phase": "review",
                "role": "multi_perspective",
                "cycle": int(cycle or 0),
                "decision": "review_circuit_open_skip",
                "fail_reason": "circuit_open",
                "err_type": "ReviewCircuitOpen",
                "err_repr": "<ReviewCircuitOpen>",
                "error_text": "review_circuit_open",

                # Backward-compatible keys
                "cycle_number": int(cycle or 0),
                "exception_type": "ReviewCircuitOpen",
                "review_role": "multi_perspective",

                "traceback_snippet": "",
                "consecutive_failures": int(self._review_failure_consecutive or 0),
                "open_until_cycle": int(self._review_circuit_open_until_cycle or 0),
            }

            diag = self._normalize_review_diagnostics(diag_raw)
            self._last_review_failure_diag = dict(diag)
            logger.warning(
                "[Spec] review_circuit_open: phase=review role=multi_perspective cycle=%s decision=review_circuit_open_skip open_until=%s consecutive=%s, 将跳过本轮审查",
                diag_raw.get("cycle_number"),
                diag_raw.get("open_until_cycle"),
                diag_raw.get("consecutive_failures"),
            )
            return ReviewResult(
                reviews=[
                    PerspectiveReview(
                        perspective=p,
                        passed=False,
                        suggestions=[f"审查熔断：连续{int(self._review_failure_consecutive or 0)}次异常，跳过本轮审查"],
                        summary="熔断",
                    )
                    for p in ReviewPerspective
                ],
                iteration=cycle,
            )

        # 没有 session 时无法发送审查 prompt；但熔断判定已在上面完成。
        if not self._session:
            return ReviewResult(iteration=cycle)

        review_prompt = self._build_review_prompt()
        review_text: list[str] = []
        thought_text: list[str] = []

        def on_review_event(event: ACPEvent):
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                review_text.append(event.text)
            elif event.event_type == ACPEventType.THOUGHT_CHUNK and event.text:
                thought_text.append(event.text)

        # Reset failure diag for this review attempt
        self._last_review_failure_diag = None

        try:
            self._session.send_prompt(review_prompt, on_event=on_review_event, timeout=120)
            full_text = "".join(review_text)
            # Combine text + thought for parsing (some agents put verdicts in thinking)
            combined_text = full_text
            if thought_text:
                combined_text = full_text + "\n" + "".join(thought_text)
            review_result = self._parse_review_output(combined_text, cycle)
            # success: reset circuit breaker
            self._review_failure_consecutive = 0
            self._review_circuit_open_until_cycle = 0
        except Exception as e:
            diag_raw = self._build_review_exception_diagnostics(e, cycle=cycle)
            diag = self._normalize_review_diagnostics(diag_raw)
            self._last_review_failure_diag = dict(diag)

            # Update circuit breaker state (best-effort)
            try:
                self._review_failure_consecutive = int(self._review_failure_consecutive or 0) + 1
            except Exception:
                self._review_failure_consecutive = 1
            if enabled and self._review_failure_consecutive >= max_consecutive and cooldown_cycles > 0:
                try:
                    self._review_circuit_open_until_cycle = int(cycle or 0) + int(cooldown_cycles)
                except Exception:
                    self._review_circuit_open_until_cycle = int(cycle or 0)
                try:
                    self._last_review_failure_diag["review_circuit_open"] = True
                    self._last_review_failure_diag["open_until_cycle"] = int(self._review_circuit_open_until_cycle or 0)
                    self._last_review_failure_diag["consecutive_failures"] = int(self._review_failure_consecutive or 0)
                    # 标记“本次失败已触发熔断开启”（但仍按保守策略继续循环）。
                    # 注意：metrics.review_failed 不能只依赖 == review_failed_continue，因此后续会统一判断。
                    self._last_review_failure_diag["decision"] = "review_failed_open_circuit"
                except Exception:
                    pass
            # 稳定单行字段契约：关键字段可 grep；diagnostics 使用 JSON 附加
            diag_json = ""
            try:
                diag_json = json.dumps(diag, ensure_ascii=False, sort_keys=True)
            except Exception:
                diag_json = "{\"phase\":\"review\",\"decision\":\"review_failed_continue\"}"

            # 关键：日志必须永不为空。
            # 即便格式化 helper 意外抛异常，也要降级输出稳定字段（err_type/err_repr/error_text）。
            try:
                logger.warning(self._format_review_exception_log_line(diag, diag_json=diag_json))
            except Exception as log_e:
                d = self._normalize_review_diagnostics(diag)
                err_type = str(d.get("err_type") or "Exception")
                err_repr = str(d.get("err_repr") or "").strip() or f"<{err_type}>"
                error_text = str(d.get("error_text") or "").strip() or err_repr
                logger.warning(
                    "[Spec] 多视角审查异常: phase=review role=multi_perspective cycle=%s decision=%s "
                    "err_type=%s err_repr=%s error_text=%s (log_format_failed=%s), 将继续循环",
                    d.get("cycle"),
                    d.get("decision") or "review_failed_continue",
                    err_type,
                    err_repr,
                    error_text,
                    type(log_e).__name__,
                )
            review_result = ReviewResult(reviews=[
                PerspectiveReview(
                    perspective=p, passed=False,
                    suggestions=[f"审查执行异常: {str(diag.get('error_text') or '').strip() or str(diag.get('err_repr') or '(empty)')}"],
                    summary="异常",
                ) for p in ReviewPerspective
            ], iteration=cycle)

        if callbacks.on_review_done:
            callbacks.on_review_done(cycle, review_result)

        return review_result

    def _parse_review_output(self, text: str, cycle: int) -> ReviewResult:
        """Parse structured review output into ReviewResult (same logic as loop engine)."""
        raw = (text or "").replace("\r\n", "\n")

        # 1) strict/tolerant parsing (shared utils)
        reviews = parse_review_output_strict_tolerant(raw, cycle)

        # 2.5) loose parsing: keyword-pair / JSON / table formats
        if not reviews:
            reviews = parse_review_output_loose(raw, cycle)

        # 3) LLM fallback
        if not reviews:
            preview = raw[:500] if raw else "(empty)"
            logger.warning("[Spec] 正则+loose解析全部失败, 尝试LLM兜底解析. 原文预览: %s", preview)
            reviews = self._parse_review_with_llm(raw)

        # 4) Final fallback
        if not reviews:
            logger.warning("[Spec] 审查输出解析失败, 将视为有改进建议继续循环")
            for p in ReviewPerspective:
                reviews.append(PerspectiveReview(
                    perspective=p, passed=False,
                    suggestions=["审查输出解析失败，请检查实现质量"],
                    summary="解析失败",
                ))

        return ReviewResult(reviews=reviews, iteration=cycle)

    def _parse_review_with_llm(self, raw_text: str) -> list[PerspectiveReview]:
        """LLM fallback: extract review verdicts from free-form text."""
        settings = self.settings
        # 测试里常用最小 settings stub；这里必须容忍缺失字段。
        if not getattr(settings, "ark_api_key", "") or not getattr(settings, "ark_model", ""):
            return []
        if not raw_text or len(raw_text.strip()) < 10:
            return []

        prompt = f"""请从以下文本中提取四个视角的审查结果。

文本内容：
{raw_text[:3000]}

请严格按以下 JSON 格式输出（不要输出其他内容）：
[
  {{"perspective": "ARCHITECT", "verdict": "PASS或FAIL", "suggestions": ["建议1", "建议2"]}},
  {{"perspective": "PRODUCT", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "USER", "verdict": "PASS或FAIL", "suggestions": []}},
  {{"perspective": "TESTER", "verdict": "PASS或FAIL", "suggestions": []}}
]"""

        try:
            llm = ChatOpenAI(
                base_url=getattr(settings, "ark_base_url", None),
                api_key=getattr(settings, "ark_api_key", ""),
                model=getattr(settings, "ark_model", ""),
                temperature=0.0,
            )
            response = llm.invoke([
                SystemMessage(content="你是一个文本解析助手。从审查文本中提取结构化的审查结果，只输出JSON。"),
                HumanMessage(content=prompt),
            ])
            return self._extract_reviews_from_llm_response(response.content)
        except Exception as e:
            logger.warning("[Spec] LLM 兜底审查解析失败: %s", e)
            return []

    @staticmethod
    def _extract_reviews_from_llm_response(text: str) -> list[PerspectiveReview]:
        """Parse LLM JSON response into PerspectiveReview list."""
        cleaned = text.strip()
        if "```" in cleaned:
            parts = cleaned.split("```")
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                if stripped.startswith("["):
                    cleaned = stripped
                    break

        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []

        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return []

        if not isinstance(data, list):
            return []

        reviews: list[PerspectiveReview] = []
        found: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("perspective", "")).upper()
            perspective = _PERSPECTIVE_TAG_MAP.get(tag)
            if not perspective or tag in found:
                continue
            found.add(tag)
            verdict = str(item.get("verdict", "")).upper()
            passed = verdict == "PASS"
            suggestions = item.get("suggestions", [])
            if not isinstance(suggestions, list):
                suggestions = []
            suggestions = [str(s) for s in suggestions if s]
            if passed:
                suggestions = []
            reviews.append(PerspectiveReview(
                perspective=perspective, passed=passed,
                suggestions=suggestions,
                summary=f"{'通过' if passed else f'{len(suggestions)}条建议'}",
            ))
        return reviews

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _format_criteria_status(self) -> str:
        """Format current criteria status for prompt inclusion."""
        if not self._project or not self._project.criteria_tracker.criteria:
            return ""
        tracker = self._project.criteria_tracker
        lines = ["\n## 验收标准进度"]
        for i, c in enumerate(tracker.criteria):
            if tracker.satisfied.get(i, False):
                lines.append(f"- [x] {c} ✅ (已满足)")
            else:
                lines.append(f"- [ ] {c}")
        return "\n".join(lines) + "\n"

    def _consume_guidance(self) -> str:
        """Consume and format pending user guidance."""
        if not self._user_guidance:
            return ""
        combined = "\n\n".join(self._user_guidance)
        self._user_guidance.clear()
        return f"\n## 用户引导\n{combined}\n"

    def _detect_convergence(self) -> bool:
        """Detect if recent cycles made no progress.

        Spec 模式的收敛定义：在最近的窗口期内
        - 验收标准满足数量没有任何增加
        - 且多视角审查建议数量也没有任何变化（一直重复同一批建议/一直无建议）

        注意：收敛仅表示“卡住”，不代表需求达成；最终是否成功由
        “验收标准全部满足 + 审查通过”决定。
        """

        if not self._project:
            return False

        window = int(self.settings.spec_convergence_window or 0)
        # Convergence needs at least 2 cycles to compare progress.
        if window < 2:
            return False
        if len(self._project.cycles) < window:
            return False

        # When already all satisfied, never treat as converged.
        if self._project.criteria_tracker.is_all_satisfied:
            return False

        recent = self._project.cycles[-window:]

        # --- Criteria progress ---
        tracker = self._project.criteria_tracker
        counts: list[int] = []
        for c in recent:
            satisfied_now = 0
            for idx in range(len(tracker.criteria)):
                at = tracker.satisfied_at_iteration.get(idx)
                if at is not None and at <= c.cycle_number:
                    satisfied_now += 1
            counts.append(satisfied_now)

        if len(set(counts)) != 1:
            return False  # criteria improved in window

        # --- Review suggestion progress (content-aware, not just counts) ---
        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").strip().lower())

        suggestion_sets: list[frozenset[str]] = []
        if not self.settings.spec_review_enabled:
            suggestion_sets = [frozenset()] * window
        else:
            for c in recent:
                if c.review_result is None:
                    return False
                ss: set[str] = set()
                for pr in c.review_result.failed_perspectives:
                    for s in pr.suggestions:
                        ns = _norm(str(s))
                        if ns:
                            ss.add(ns)
                suggestion_sets.append(frozenset(ss))

        return len(set(suggestion_sets)) == 1

    def inject_guidance(self, message: str):
        """Inject user guidance — will be included in the next phase prompt."""
        self._user_guidance.append(message)
        logger.info("[Spec] 用户引导已注入(队列=%d): %s...", len(self._user_guidance), message[:100])

    def stop(self):
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def pause(self):
        if self._project:
            self._project.status = SpecProjectStatus.PAUSED
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def resume(self, callbacks: Optional[SpecEngineCallbacks] = None) -> Optional[SpecProject]:
        """Resume a paused spec execution."""
        if not self._project or self._project.status not in (SpecProjectStatus.PAUSED, SpecProjectStatus.CLARIFYING):
            return self._project

        callbacks = callbacks or SpecEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._project.status = SpecProjectStatus.RUNNING
        max_cycles = self._resolve_max_cycles(self.settings.spec_max_cycles)
        
        # Resume from the last known cycle number
        last_cycle_num = 0
        if self._project.cycles:
            last_cycle_num = self._project.cycles[-1].cycle_number
        start_cycle = max(last_cycle_num, self._project.cycle_count_total) + 1
        
        self._termination_reason = None

        try:
            self._close_session_safely()

            # Resolve TTADK startup model (resume)
            self._session = create_engine_session(
                agent_type=self._agent_type, cwd=self.root_path,
                on_rate_limit=getattr(self, "_on_rate_limit", None),
                model_name=self._model_name,
            )

            self._termination_reason = self._run_cycle_loop(
                start_cycle=start_cycle,
                max_cycles=max_cycles,
                callbacks=callbacks,
                timeout=self.settings.spec_execution_timeout,
            )

            reason = self._termination_reason or "max_cycles"
            if self._run_state == EngineRunState.STOPPING or reason == "paused":
                self._project.status = SpecProjectStatus.PAUSED
            elif reason == "success":
                self._project.status = SpecProjectStatus.COMPLETED
                self._project.completed_at = time.time()
            else:
                if reason == "converged":
                    msg = (
                        f"收敛终止：连续{self.settings.spec_convergence_window}轮无有效改进，"
                        "仍有未满足验收标准或审查未通过"
                    )
                elif reason == "max_cycles":
                    msg = f"达到最大循环次数({max_cycles})仍未满足验收标准或审查未通过"
                else:
                    msg = f"终止：{reason}"
                self._project.abort(msg)

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

        except Exception as e:
            error_msg = f"Spec恢复异常: {str(e)}"
            logger.error("[Spec:%s] %s", self._project.name, error_msg)
            self._project.status = SpecProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE

        return self._project

    def get_rendered_content(self) -> str:
        return self._renderer.get_final_content()

    def _project_to_compact_dict(self) -> dict:
        """Build a compact project dict for state persistence.

        关键点：避免在 5000+ 循环下每轮把全部 cycles/work_items/metrics 全量写回，
        否则会出现 O(n^2) 的 IO 开销导致不稳定。
        """
        if not self._project:
            return {}

        tail_cycles = int(getattr(self.settings, "spec_state_cycles_tail", 50) or 50)
        tail_items = int(getattr(self.settings, "spec_state_work_items_tail", 200) or 200)
        tail_metrics = int(getattr(self.settings, "spec_state_metrics_tail", 200) or 200)

        cycle_count_total = max(int(getattr(self._project, "cycle_count_total", 0) or 0), len(self._project.cycles))
        work_items_total = max(int(getattr(self._project, "work_items_total", 0) or 0), len(self._project.work_items))

        pd = {
            "project_id": self._project.project_id,
            "name": self._project.name,
            "root_path": self._project.root_path,
            "requirement": self._project.requirement,
            "acceptance_criteria": list(self._project.acceptance_criteria),
            "criteria_tracker": self._project.criteria_tracker.to_dict(),
            "status": self._project.status.value,
            "created_at": self._project.created_at,
            "started_at": self._project.started_at,
            "completed_at": self._project.completed_at,
            "error": self._project.error,
            "cycle_count_total": cycle_count_total,
            "work_items_total": work_items_total,
            "cycles": [c.to_dict() for c in (self._project.cycles[-tail_cycles:] if tail_cycles > 0 else [])],
            "metrics_history": [m.to_dict() for m in (self._project.metrics_history[-tail_metrics:] if tail_metrics > 0 else [])],
        }

        # Work items: keep pending + last N for traceability
        pending = [w for w in self._project.work_items if w.status == SpecWorkItemStatus.PENDING]
        recent = self._project.work_items[-tail_items:] if tail_items > 0 else []
        merged: list[SpecWorkItem] = []
        seen: set[str] = set()
        for w in pending + list(recent):
            if not w or not w.item_id or w.item_id in seen:
                continue
            seen.add(w.item_id)
            merged.append(w)
        pd["work_items"] = [w.to_dict() for w in merged]
        pd["_compact"] = {
            "cycles_tail": tail_cycles,
            "work_items_tail": tail_items,
            "metrics_tail": tail_metrics,
            "cycles_truncated_before": max(0, cycle_count_total - len(pd.get("cycles") or [])),
        }
        # For discoverability
        pd["artifacts_root"] = self._artifact_root_dir()
        pd["history_log_path"] = self._history_log_path()
        return pd

    def save_state(self, filepath: Optional[str] = None) -> str:
        if not self._project:
            raise ValueError("没有项目状态可保存")
        if not filepath:
            filepath = self._get_state_path()
        state = {
            "chat_id": self.chat_id,
            "root_path": self.root_path,
            "project": self._project_to_compact_dict(),
            "saved_at": time.time(),
        }
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        return filepath

    @classmethod
    def load_state(cls, filepath: str) -> Optional[SpecProject]:
        """Load a SpecProject from a state file written by save_state()."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            proj = data.get("project")
            if not isinstance(proj, dict):
                return None
            return SpecProject.from_dict(proj)
        except Exception:
            return None

    def cleanup(self):
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("关闭ACP session失败: %s", e)
            self._session = None
        self._project = None
        self._run_state = EngineRunState.IDLE


class SpecEngineManager:
    """Manages SpecEngine instances per chat.

    Uses a secondary index (_chat_keys) to avoid O(n) full-table scans.
    """

    def __init__(self):
        self._engines: dict[str, SpecEngine] = {}
        self._chat_keys: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def _add_index(self, chat_id: str, key: str) -> None:
        self._chat_keys.setdefault(chat_id, set()).add(key)

    def _iter_chat_engines(self, chat_id: str):
        for key in self._chat_keys.get(chat_id, ()):
            engine = self._engines.get(key)
            if engine:
                yield engine

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> SpecEngine:
        key = f"{chat_id}:{root_path}"
        
        from ..ttadk import get_ttadk_manager
        if engine_name.lower() == "ttadk":
            ttadk_manager = get_ttadk_manager()
            current_tool = ttadk_manager.get_current_tool()
            current_model = ttadk_manager.get_current_model()
            agent_type = f"ttadk_{current_tool}" if current_tool else "ttadk_coco"
            model_name = current_model
        else:
            agent_type = "claude" if engine_name.lower().startswith("claude") else "coco"
            model_name = None

        with self._lock:
            if key not in self._engines:
                self._engines[key] = SpecEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=agent_type,
                    engine_name=engine_name,
                    model_name=model_name,
                )
                self._add_index(chat_id, key)
            else:
                existing = self._engines[key]
                if existing.engine_name.lower() != engine_name.lower() and not existing.is_running:
                    existing.cleanup()
                    self._engines[key] = SpecEngine(
                        chat_id=chat_id,
                        root_path=root_path,
                        agent_type=agent_type,
                        engine_name=engine_name,
                        model_name=model_name,
                    )
            return self._engines[key]

    def load_or_create_from_disk(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> SpecEngine:
        """Create engine and hydrate project state from disk if present.

        用于进程重启后的断点续传：handler 在 `/spec_status`/`/spec_resume` 时可调用。
        """
        engine = self.get_or_create(chat_id, root_path, engine_name=engine_name)
        if not engine._get_bool_setting("spec_allow_resume_from_disk", True):
            return engine
        state_path = os.path.join(root_path, getattr(engine.settings, "spec_state_filename", ".spec_engine_state.json"))
        if os.path.exists(state_path) and (engine.project is None):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                proj = data.get("project")
                if isinstance(proj, dict):
                    engine._project = SpecProject.from_dict(proj)
                    engine._resume_meta = {
                        "state_path": state_path,
                        "saved_at": data.get("saved_at"),
                        "compact": proj.get("_compact"),
                    }
            except Exception:
                pass
        return engine

    def get(self, chat_id: str, root_path: str) -> Optional[SpecEngine]:
        key = f"{chat_id}:{root_path}"
        return self._engines.get(key)

    def get_active_engine(self, chat_id: str) -> Optional[SpecEngine]:
        for engine in self._iter_chat_engines(chat_id):
            if engine.is_running:
                return engine
        return None

    def get_active_engines(self, chat_id: str) -> list[SpecEngine]:
        return [e for e in self._iter_chat_engines(chat_id) if e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[SpecEngine]:
        if chat_id is None:
            return list(self._engines.values())
        return list(self._iter_chat_engines(chat_id))

    def cleanup_all(self):
        with self._lock:
            for engine in self._engines.values():
                engine.cleanup()
            self._engines.clear()
            self._chat_keys.clear()
