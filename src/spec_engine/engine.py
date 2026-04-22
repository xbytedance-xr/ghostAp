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
import time
from collections import namedtuple
from dataclasses import dataclass
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..acp import ACPEvent, ACPEventType
from ..agent_session import create_engine_session
from ..engine_base import (
    BaseEngine,
    EngineRunState,
    PerspectiveReview,
    ReviewPerspective,
    ReviewResult,
)
from ..utils.errors import get_error_detail
from ..utils.llm import ChatOpenAICacheKey, get_cached_chat_openai
from ..utils.trace import TraceContext
from .validation import SpecInput
from pydantic import ValidationError
from ..utils.spec_utils import (
    extract_json_blob,
)
from .models import (
    SpecCycle,
    SpecPhase,
    SpecProject,
    SpecProjectStatus,
    SpecWorkItem,
    SpecWorkItemStatus,
)
from .prompts import (
    build_build_prompt,
    build_goal_rewrite_prompt,
    build_plan_prompt,
    build_refinement_input,
    build_spec_prompt,
    build_task_prompt,
    format_criteria_status,
)
from .artifacts import (
    merge_acceptance_criteria,
    parse_acceptance_criteria,
    parse_plan_artifact,
    parse_spec_artifact,
    parse_tasks,
)
from ..utils.retry import RetryPolicy
from .task_persistence import SpecTaskState, delete_task_state
from .tracker import PhaseTracker
from .review import (
    ReviewCircuitState,
    build_review_exception_diagnostics,
    conduct_review as _conduct_review_impl,
    extract_reviews_from_llm_response,
    format_review_exception_log_line,
    normalize_review_diagnostics,
    parse_review_output as _parse_review_output_impl,
    parse_review_with_llm as _parse_review_with_llm_impl,
    review_result_to_text,
)
from .convergence import (
    ContinuationPolicy,
    compute_cycle_metrics,
    detect_convergence,
)
from .criteria import (
    decompose_criteria_with_llm as _decompose_criteria_with_llm_impl,
    evaluate_criteria as _evaluate_criteria_impl,
)
from .persistence import (
    append_history_event as _append_history_event,
    cleanup_generated_specs as _cleanup_generated_specs,
    cleanup_old_cycle_artifacts as _cleanup_old_cycle_artifacts,
    get_state_path as _get_state_path,
    load_engine_state as _load_engine_state,
    persist_cycle_artifact as _persist_cycle_artifact,
    persist_state_best_effort as _persist_state_best_effort,
    project_to_compact_dict as _project_to_compact_dict_impl,
    read_text_file_best_effort as _read_text_file_best_effort,
    save_engine_state as _save_engine_state,
    save_failed_task as _save_failed_task_impl,
    truncate_output as _truncate_output,
)
from .discovery import (
    build_input_from_spec_file as _build_input_from_spec_file,
    discover_optimization_questions as _discover_optimization_questions,
    generate_specs_from_discovery as _generate_specs_from_discovery,
    pick_next_work_item as _pick_next_work_item,
    should_load_spec_directly as _should_load_spec_directly,
)
from .session_utils import (
    build_runtime_context as _build_runtime_context,
    initialize_model_context as _initialize_model_context,
    recreate_session_best_effort as _recreate_session_best_effort,
    restore_runtime_context as _restore_runtime_context,
    send_prompt_with_retry as _send_prompt_with_retry,
    try_switch_model as _try_switch_model,
)

logger = logging.getLogger(__name__)

VerifyResult = namedtuple("VerifyResult", ["passed", "output"])


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


class SpecEngine(BaseEngine):
    """ACP-driven structured development engine with iterative review cycles."""

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str = "coco",
        engine_name: str = "Coco",
        model_name: Optional[str] = None,
        *,
        retry_policy: Optional[RetryPolicy] = None,
        get_llm_fn: Optional[Callable] = None,
        create_session_fn: Optional[Callable] = None,
    ):
        super().__init__(chat_id, root_path, agent_type, engine_name, model_name)
        self._project: Optional[SpecProject] = None
        self._user_guidance: list[str] = []
        self._last_review: Optional[ReviewResult] = None
        # success / paused / converged / max_cycles / stopped
        self._termination_reason: Optional[str] = None
        self._resume_meta: Optional[dict] = None
        self._retry_policy = retry_policy or RetryPolicy()
        self._get_llm_fn = get_llm_fn or get_cached_chat_openai
        self._create_session_fn = create_session_fn or create_engine_session
        self._models_tried: list[str] = []
        self._current_model: Optional[str] = None
        self._on_rate_limit: Optional[Callable[[int], None]] = None
        self._saved_task_id: Optional[str] = None
        # Idempotency guard for failed task persistence: avoid saving the same failure multiple times.
        # Format: (cycle_num, phase.value, task_id)
        self._saved_task_signature: Optional[tuple[int, str, str]] = None
        self._review_circuit = ReviewCircuitState()
        self._last_cycle_num: int = 0
        self._last_phase: SpecPhase = SpecPhase.SPEC
        self._llm_cache: dict[ChatOpenAICacheKey, ChatOpenAI] = {}

    @property
    def _last_review_failure_diag(self) -> Optional[dict]:
        return self._review_circuit.last_review_failure_diag

    @_last_review_failure_diag.setter
    def _last_review_failure_diag(self, value: Optional[dict]):
        self._review_circuit.last_review_failure_diag = value

    @property
    def _review_failure_consecutive(self) -> int:
        return self._review_circuit.review_failure_consecutive

    @_review_failure_consecutive.setter
    def _review_failure_consecutive(self, value: int):
        self._review_circuit.review_failure_consecutive = value

    @property
    def _review_circuit_open_until_cycle(self) -> int:
        return self._review_circuit.review_circuit_open_until_cycle

    @_review_circuit_open_until_cycle.setter
    def _review_circuit_open_until_cycle(self, value: int):
        self._review_circuit.review_circuit_open_until_cycle = value

    def _get_llm(self, temperature: float) -> ChatOpenAI:
        return self._get_llm_fn(self.settings, temperature, cache=self._llm_cache, llm_cls=ChatOpenAI)

    def _wrap_callbacks(self, callbacks: SpecEngineCallbacks) -> SpecEngineCallbacks:
        def _wrap(fn: Optional[Callable[..., None]], name: str) -> Optional[Callable[..., None]]:
            if not fn:
                return None

            def _inner(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    logger.warning("[Spec] callback %s 失败: %s", name, e, exc_info=True)
                    return None

            return _inner

        return SpecEngineCallbacks(
            on_analyzing_start=_wrap(callbacks.on_analyzing_start, "on_analyzing_start"),
            on_analyzing_done=_wrap(callbacks.on_analyzing_done, "on_analyzing_done"),
            on_cycle_start=_wrap(callbacks.on_cycle_start, "on_cycle_start"),
            on_phase_start=_wrap(callbacks.on_phase_start, "on_phase_start"),
            on_phase_event=_wrap(callbacks.on_phase_event, "on_phase_event"),
            on_phase_done=_wrap(callbacks.on_phase_done, "on_phase_done"),
            on_review_done=_wrap(callbacks.on_review_done, "on_review_done"),
            on_cycle_done=_wrap(callbacks.on_cycle_done, "on_cycle_done"),
            on_project_done=_wrap(callbacks.on_project_done, "on_project_done"),
            on_error=_wrap(callbacks.on_error, "on_error"),
            on_retry=_wrap(callbacks.on_retry, "on_retry"),
            on_model_switch=_wrap(callbacks.on_model_switch, "on_model_switch"),
            on_task_saved=_wrap(callbacks.on_task_saved, "on_task_saved"),
        )

    def _initialize_model_context(self) -> None:
        self._current_model, self._models_tried = _initialize_model_context(self._agent_type)

    @staticmethod
    def _infer_engine_name(agent_type: Optional[str]) -> str:
        normalized = str(agent_type or "").strip().lower()
        if normalized.startswith("ttadk_"):
            return "TTADK"
        if normalized == "claude":
            return "Claude"
        return "Coco"

    def _build_runtime_context(self) -> dict:
        return _build_runtime_context(
            agent_type=str(self._agent_type or ""),
            engine_name=self.engine_name,
            model_name=self._model_name,
            current_model=self._current_model,
            models_tried=self._models_tried,
            infer_engine_name_fn=self._infer_engine_name,
        )

    def _restore_runtime_context(
        self,
        runtime_context: Optional[dict],
        *,
        saved_task_id: Optional[str] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> None:
        runtime = dict(runtime_context or {})
        tentative_agent = str(runtime.get("agent_type") or self._agent_type or "coco").strip().lower() or "coco"
        result = _restore_runtime_context(
            runtime_context,
            agent_type=str(self._agent_type or ""),
            engine_name=self.engine_name,
            model_name=self._model_name,
            current_model=self._current_model,
            models_tried=self._models_tried,
            infer_engine_name_fn=self._infer_engine_name,
            initialize_model_context_fn=lambda: _initialize_model_context(tentative_agent),
            saved_task_id=saved_task_id,
            on_rate_limit=on_rate_limit,
            existing_saved_task_id=self._saved_task_id,
            project=self._project,
        )
        self._agent_type = result["agent_type"]
        self.engine_name = result["engine_name"]
        self._model_name = result["model_name"]
        self._current_model = result["current_model"]
        self._models_tried = result["models_tried"]
        self._on_rate_limit = result["on_rate_limit"]
        self._saved_task_id = result["saved_task_id"]
        self._saved_task_signature = None

    def restore_from_task_state(
        self,
        state: SpecTaskState,
        *,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> SpecProject:
        if not state.project_snapshot:
            raise ValueError("恢复失败：缺少 project_snapshot")

        project = SpecProject.from_dict(state.project_snapshot)
        project.status = SpecProjectStatus.PAUSED
        project.task_id = state.task_id
        if project.cycles:
            project.cycle_count_total = max(project.cycle_count_total, project.cycles[-1].cycle_number)

        self._project = project
        self._restore_runtime_context(
            state.resolved_runtime_context(),
            saved_task_id=state.task_id,
            on_rate_limit=on_rate_limit,
        )
        return project

    def _send_prompt_with_retry(
        self,
        prompt: str,
        *,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
        retry_policy: Optional[RetryPolicy] = None,
        before_retry: Optional[Callable[[int, Exception], None]] = None,
        total_timeout: Optional[float] = None,
    ):
        return _send_prompt_with_retry(
            self._session, prompt,
            on_event=on_event, timeout=timeout,
            retry_policy=retry_policy, before_retry=before_retry,
            total_timeout=total_timeout,
        )

    def _build_review_exception_diagnostics(self, e: Exception, *, cycle: int) -> dict:
        session_id = ""
        try:
            session_id = str(getattr(self._session, "session_id", "") or "")
        except Exception:
            session_id = ""
        return build_review_exception_diagnostics(
            e,
            cycle=cycle,
            project_name=(self._project.name or "").strip() if self._project else "",
            chat_id=self.chat_id or "",
            root_path=self.root_path or "",
            agent_type=self._agent_type or "",
            session_id=session_id,
        )

    @classmethod
    def _normalize_review_diagnostics(cls, diag: object) -> dict:
        return normalize_review_diagnostics(diag)

    @classmethod
    def _format_review_exception_log_line(cls, diag: dict, *, diag_json: str) -> str:
        return format_review_exception_log_line(diag, diag_json=diag_json)


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
        callbacks = self._wrap_callbacks(callbacks or SpecEngineCallbacks())
        with self._lock:
            self._run_state = EngineRunState.RUNNING
            self._on_rate_limit = on_rate_limit
            self._saved_task_id = None
            self._saved_task_signature = None
            self._termination_reason = None
        max_cycles = self._resolve_max_cycles(self.settings.spec_max_cycles)

        project_name = os.path.basename(self.root_path) or "spec_project"
        self._project = SpecProject.create(name=project_name, root_path=self.root_path)

        # Initialize TraceContext
        trace_ctx = TraceContext(request_id=task_id or f"spec-{int(time.time())}")
        trace_ctx.__enter__()

        # Validation Gateway
        try:
            SpecInput(requirement_text=requirement_text, task_id=task_id)
        except ValidationError as e:
            # Flatten validation errors to a readable string
            errors = "; ".join([f"{err['loc'][0]}: {err['msg']}" for err in e.errors()])
            error_msg = f"非法配置参数: {errors}"
            self._project.status = SpecProjectStatus.ABORTED
            self._project.error = error_msg
            self._project.completed_at = time.time()
            logger.error("[Spec:%s] %s", project_name, error_msg)
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            trace_ctx.__exit__(None, None, None)
            return self._project

        self._project.task_id = task_id
        self._project.status = SpecProjectStatus.ANALYZING
        self._project.requirement = requirement_text
        self._last_cycle_num = 0
        self._last_phase = SpecPhase.SPEC

        if callbacks.on_analyzing_start:
            callbacks.on_analyzing_start(requirement_text)

        logger.info(
            "[Spec:%s] 启动, 需求长度=%d, 路径=%s, agent=%s",
            project_name,
            len(requirement_text),
            self.root_path,
            self._agent_type,
        )

        try:
            self._initialize_model_context()

            # Parse requirement — extract acceptance criteria
            criteria = parse_acceptance_criteria(requirement_text, self._decompose_criteria_with_llm)
            self._project.acceptance_criteria = criteria
            self._project.criteria_tracker.init_criteria(criteria)
            self._project.status = SpecProjectStatus.RUNNING
            self._project.started_at = time.time()

            if callbacks.on_analyzing_done:
                callbacks.on_analyzing_done(self._project)

            # Create ACP session
            self._session = self._create_session_fn(
                agent_type=self._agent_type,
                cwd=self.root_path,
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
                    self._handle_max_cycles_termination(max_cycles)
                else:
                    msg = f"终止：{reason}"
                    self._project.abort(msg)

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

            return self._project

        except TimeoutError as e:
            error_msg = self._format_engine_error(e, "Spec执行", is_timeout=True, callbacks=callbacks)
            if self._project:
                self._project.status = SpecProjectStatus.ABORTED
                self._project.completed_at = time.time()
                if not self._saved_task_id:
                    try:
                        self._save_failed_task(error_msg, self._last_cycle_num, self._last_phase, callbacks)
                    except Exception as save_err:
                        logger.warning("[Spec] 异常任务保存失败: %s", save_err)
            return self._project

        except Exception as e:
            error_msg = self._format_engine_error(e, "Spec执行", is_timeout=False, callbacks=callbacks)
            if self._project:
                self._project.status = SpecProjectStatus.ABORTED
                self._project.completed_at = time.time()
                if not self._saved_task_id:
                    try:
                        self._save_failed_task(error_msg, self._last_cycle_num, self._last_phase, callbacks)
                    except Exception as save_err:
                        logger.warning("[Spec] 异常任务保存失败: %s", save_err)
            return self._project

        finally:
            trace_ctx.__exit__(None, None, None)
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE

            if self._project and self._project.status == SpecProjectStatus.COMPLETED and self._saved_task_id:
                delete_task_state(self._saved_task_id)
                self._saved_task_id = None
                self._saved_task_signature = None

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
        _depth: int = 0,
    ) -> str:
        """Execute a single phase: send prompt, collect output, return text."""
        project_name = self._project.name if self._project else "unknown"
        logger.info("[Spec:%s] 循环 %d 阶段 %s 开始", project_name, cycle_num, phase.value)

        if not self._session:
            raise RuntimeError(f"Spec session is None before phase {phase.value} (cycle={cycle_num}), session may have failed to initialize or rebuild")

        if callbacks.on_phase_start:
            callbacks.on_phase_start(cycle_num, phase)

        def _before_retry(attempt: int, error: Exception):
            if attempt > 0:
                self._recreate_session_best_effort()
            if callbacks.on_retry:
                callbacks.on_retry(attempt, get_error_detail(error))

        from ..utils.retry import RetryPolicy
        retry_policy = RetryPolicy(
            max_retries=self.settings.spec_max_retries,
            retry_delay=self._retry_policy.retry_delay,
            backoff_multiplier=self._retry_policy.backoff_multiplier
        )

        try:
            tracker = PhaseTracker()

            def on_event(event: ACPEvent):
                try:
                    tracker.process(event)
                    renderer = self._renderer
                    if renderer is not None:
                        renderer.process_event(event)
                    if callbacks.on_phase_event:
                        try:
                            callbacks.on_phase_event(cycle_num, phase, event)
                        except Exception as cb_exc:
                            logger.debug("[Spec] on_phase_event callback failed: %s", get_error_detail(cb_exc))
                except Exception as exc:
                    logger.debug("[Spec] on_event handler error: %s", get_error_detail(exc))

            self._send_prompt_with_retry(
                prompt,
                on_event=on_event,
                timeout=timeout,
                retry_policy=retry_policy,
                before_retry=_before_retry,
            )
            output = tracker.text_buffer
            logger.info(
                "[Spec:%s] 循环 %d 阶段 %s 完成, 输出长度=%d",
                project_name, cycle_num, phase.value, len(output),
            )

            if callbacks.on_phase_done:
                callbacks.on_phase_done(cycle_num, phase, output)

            return output

        except Exception as e:
            from ..utils.errors import get_error_detail as _ged
            last_error = _ged(e)

            try:
                override_hint = (self.settings.spec_failed_task_id_override or "").strip()
                if (
                    override_hint
                    and phase == SpecPhase.BUILD
                    and "internal error" not in (last_error or "").lower()
                ):
                    last_error = "Internal error"
            except Exception:
                logger.debug("override hint processing failed", exc_info=True)

            # 停止态下（例如服务关闭触发 cancel），phase 异常通常是 session cancel 或进程退出导致，
            # 不应继续触发模型切换或失败任务持久化。
            if self._run_state == EngineRunState.STOPPING:
                reason = last_error or type(e).__name__
                try:
                    if len(reason) > 200:
                        reason = reason[:200] + "…(truncated)"
                except Exception:
                    reason = last_error or type(e).__name__
                logger.info("[Spec] Phase %s 中断（引擎停止中）: %s", phase.value, reason)
                return ""

            if self._try_switch_model(callbacks):
                if _depth >= 3:
                    raise RuntimeError(
                        f"Phase {phase.value} 模型切换递归超限 (depth={_depth})，停止重试"
                    ) from e
                return self._run_phase(cycle_num, phase, prompt, callbacks, timeout, _depth=_depth + 1)

            task_id = self._save_failed_task(last_error, cycle_num, phase, callbacks)
            try:
                err_preview = last_error or ""
                if len(err_preview) > 500:
                    err_preview = err_preview[:500] + "…(truncated)"
            except Exception:
                err_preview = last_error or ""
            logger.error("[Spec] Phase %s 失败 (task_id=%s): %s", phase.value, task_id, err_preview)
            raise RuntimeError(f"Phase {phase.value} 失败，任务已保存(task_id={task_id}): {last_error}") from e

    def _verify_build_result(self) -> VerifyResult:
        if not self._project or not self._project.verify_command:
            return VerifyResult(passed=True, output="")
        from ..sandbox.executor import SandboxExecutor
        executor = SandboxExecutor()
        result = executor.execute(
            self._project.verify_command,
            cwd=self.root_path,
            interactive=False,
        )
        output = result.stdout
        if result.stderr:
            output = f"{output}\n{result.stderr}".strip()
        return VerifyResult(passed=result.success, output=output)

    def _try_switch_model(self, callbacks) -> bool:
        switched, new_current, _, self._models_tried, new_session = _try_switch_model(
            agent_type=self._agent_type,
            run_state=self._run_state,
            models_tried=self._models_tried,
            current_model=self._current_model,
            root_path=self.root_path,
            model_name=self._model_name,
            on_rate_limit=getattr(self, "_on_rate_limit", None),
            close_session_fn=self._close_session_safely,
            callbacks=callbacks,
        )
        if switched:
            self._current_model = new_current
            self._session = new_session
        return switched

    def _recreate_session_best_effort(self) -> None:
        new_session = _recreate_session_best_effort(
            agent_type=self._agent_type,
            root_path=self.root_path,
            on_rate_limit=getattr(self, "_on_rate_limit", None),
            current_model=self._current_model,
            model_name=self._model_name,
            close_session_fn=self._close_session_safely,
        )
        if new_session is not None:
            self._session = new_session

    def _save_failed_task(
        self,
        error: str,
        cycle_num: int,
        phase: SpecPhase,
        callbacks,
    ) -> str:
        task_id, new_saved_id, new_sig = _save_failed_task_impl(
            project=self._project,
            root_path=self.root_path,
            chat_id=self.chat_id,
            agent_type=self._agent_type,
            settings=self.settings,
            models_tried=self._models_tried,
            build_runtime_context_fn=self._build_runtime_context,
            project_to_compact_dict_fn=self._project_to_compact_dict,
            saved_task_id=self._saved_task_id,
            saved_task_signature=self._saved_task_signature,
            error=error,
            cycle_num=cycle_num,
            phase=phase,
            callbacks=callbacks,
        )
        if new_saved_id is not None:
            self._saved_task_id = new_saved_id
        if new_sig is not None:
            self._saved_task_signature = new_sig
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
            infinite_mode=self.settings.spec_infinite_mode,
            disable_convergence=self.settings.spec_disable_convergence,
            disable_early_stop=self.settings.spec_disable_early_stop,
            # Spec mode defaults to at least 2 cycles to ensure discovery;
            # allow overriding via settings for single-cycle tasks/tests.
            min_cycles=max(1, self.settings.spec_min_cycles),
        )

        for cycle_num in range(start_cycle, max_cycles + 1):
            if self._run_state != EngineRunState.RUNNING:
                termination = "paused" if self._run_state == EngineRunState.STOPPING else "stopped"
                break

            cycle = SpecCycle(cycle_number=cycle_num)
            self._last_cycle_num = cycle_num

            if callbacks.on_cycle_start:
                callbacks.on_cycle_start(cycle_num, max_cycles)

            # Pick next work item (spec file) if any; otherwise refine from requirement.
            work_item = _pick_next_work_item(self._project, cycle_num)

            # First cycle of a fresh execute uses raw requirement
            if first_raw_input is not None and cycle_num == start_cycle:
                spec_input = first_raw_input
            elif work_item and work_item.spec_path:
                spec_input = _build_input_from_spec_file(requirement, work_item)
            else:
                spec_input = build_refinement_input(requirement, self._last_review, self._project)

            # --- SPEC PHASE ---
            cycle.phase = SpecPhase.SPEC
            self._last_phase = cycle.phase
            if work_item and work_item.spec_path and _should_load_spec_directly(work_item):
                # spec 文件本身就是 spec-kit 规格产物：直接加载进入下一阶段
                spec_output = _read_text_file_best_effort(work_item.spec_path)
            else:
                spec_output = self._run_phase(
                    cycle_num,
                    SpecPhase.SPEC,
                    build_spec_prompt(spec_input, self.root_path, self._consume_guidance(), format_criteria_status(self._project)),
                    callbacks,
                    timeout,
                )
            cycle.spec_content = _truncate_output(spec_output, self.settings)
            if self.settings.spec_persist_phase_artifacts:
                cycle.spec_path = _persist_cycle_artifact(self.root_path, self.settings, self._project, cycle_num, "spec", spec_output, "json")
            cycle.spec_artifact, cycle.spec_artifact_errors = parse_spec_artifact(spec_output)

            if self.settings.spec_persist_every_phase:
                self._persist_state_best_effort()

            if work_item:
                work_item.status = SpecWorkItemStatus.DONE
                work_item.used_in_cycle = cycle_num
                _append_history_event(
                    self.root_path, self.settings, self._project,
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
                merge_acceptance_criteria(self._project, cycle.spec_artifact.acceptance_criteria)

            if self._run_state != EngineRunState.RUNNING:
                cycle.fail()
                self._project.cycles.append(cycle)
                self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)
                self._persist_state_best_effort()
                termination = "paused"
                break

            # --- PLAN PHASE ---
            cycle.phase = SpecPhase.PLAN
            self._last_phase = cycle.phase
            plan_output = self._run_phase(
                cycle_num,
                SpecPhase.PLAN,
                build_plan_prompt(spec_output, self.root_path, spec_artifact=cycle.spec_artifact),
                callbacks,
                timeout,
            )
            cycle.plan_content = _truncate_output(plan_output, self.settings)
            if self.settings.spec_persist_phase_artifacts:
                cycle.plan_path = _persist_cycle_artifact(self.root_path, self.settings, self._project, cycle_num, "plan", plan_output, "json")
            cycle.plan_artifact, cycle.plan_artifact_errors = parse_plan_artifact(plan_output)

            if self.settings.spec_persist_every_phase:
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
            self._last_phase = cycle.phase
            task_output = self._run_phase(
                cycle_num,
                SpecPhase.TASK,
                build_task_prompt(plan_output, plan_artifact=cycle.plan_artifact),
                callbacks,
                timeout,
            )
            parsed_tasks = parse_tasks(task_output)
            cycle.tasks_total = len(parsed_tasks)
            cycle.tasks = parsed_tasks[: self.settings.spec_cycle_tasks_max]
            if self.settings.spec_persist_phase_artifacts:
                cycle.tasks_path = _persist_cycle_artifact(
                    self.root_path, self.settings, self._project, cycle_num,
                    "tasks",
                    json.dumps([t.to_dict() for t in parsed_tasks], ensure_ascii=False, indent=2),
                    "json",
                )

            if self.settings.spec_persist_every_phase:
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
            self._last_phase = cycle.phase
            build_output = self._run_phase(
                cycle_num,
                SpecPhase.BUILD,
                build_build_prompt(cycle.tasks, plan_output, self.root_path, self._consume_guidance(), plan_artifact=cycle.plan_artifact),
                callbacks,
                timeout,
            )
            cycle.build_output = _truncate_output(build_output, self.settings)
            if self.settings.spec_persist_phase_artifacts:
                cycle.build_path = _persist_cycle_artifact(self.root_path, self.settings, self._project, cycle_num, "build", build_output, "txt")

            if self.settings.spec_persist_every_phase:
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
                self._last_phase = cycle.phase
                review_result = self._conduct_review(cycle_num, callbacks, cycle_obj=cycle)
                cycle.review_result = review_result
                # best-effort: persist review failure decision/diagnostics for traceability
                diag = self._last_review_failure_diag
                if isinstance(diag, dict) and diag:
                    cycle.review_decision = str(diag.get("decision") or "review_failed_continue")
                    cycle.review_diagnostics = dict(diag)
                if self.settings.spec_persist_phase_artifacts:
                    cycle.review_path = _persist_cycle_artifact(
                        self.root_path, self.settings, self._project, cycle_num, "review", review_result_to_text(review_result), "txt"
                    )
                self._last_review = review_result
                review_passed = review_result.all_passed

                if self.settings.spec_persist_every_phase:
                    self._persist_state_best_effort()

            cycle.complete()

            self._project.cycles.append(cycle)
            self._project.cycle_count_total = max(self._project.cycle_count_total, cycle_num)

            if callbacks.on_cycle_done:
                callbacks.on_cycle_done(cycle_num, cycle)

            logger.info(
                "[Spec:%s] 循环 %d/%d 完成, 审查=%s",
                self._project.name,
                cycle_num,
                max_cycles,
                f"{cycle.review_result.total_suggestions}条建议" if cycle.review_result else "跳过",
            )

            # --- CRITERIA EVALUATION ---
            criteria_result = self._evaluate_criteria(self._project.acceptance_criteria, cycle_num)
            all_satisfied = criteria_result.get("all_satisfied", False)

            # --- POST-CYCLE PROBLEM DISCOVERY + SPEC GENERATION ---
            if self.settings.spec_discovery_enabled and self._run_state == EngineRunState.RUNNING:
                discovery = self._discover_optimization_questions(cycle_num)
                cycle.discovery_path = _persist_cycle_artifact(
                    self.root_path, self.settings, self._project, cycle_num, "discovery", json.dumps(discovery, ensure_ascii=False, indent=2), "json"
                )
                new_items = self._generate_specs_from_discovery(cycle_num, discovery)
                # 防止 backlog 无限制膨胀：只保留最近 N 条（长期任务可配合外部清理）
                if new_items:
                    self._project.work_items.extend(new_items)
                    self._project.work_items_total = max(self._project.work_items_total, len(self._project.work_items))
                    for wi in new_items:
                        _append_history_event(
                            self.root_path, self.settings, self._project,
                            "work_item_generated",
                            {
                                "cycle": cycle_num,
                                "item_id": wi.item_id,
                                "question": wi.question,
                                "spec_path": wi.spec_path,
                            },
                        )

            # --- METRICS SNAPSHOT (monitoring) ---
            metrics = compute_cycle_metrics(cycle, self._project)
            self._project.metrics_history.append(metrics)
            cycle.metrics_path = _persist_cycle_artifact(
                self.root_path, self.settings, self._project, cycle_num, "metrics", json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2), "json"
            )

            _append_history_event(
                self.root_path, self.settings, self._project,
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
            _cleanup_old_cycle_artifacts(self.root_path, self.settings, self._project, cycle_num)
            _cleanup_generated_specs(self._project, self.settings)

            # --- TERMINATION CHECK (ContinuationPolicy) ---
            converged = False if policy.disable_convergence else self._detect_convergence()
            if converged:
                logger.info("[Spec:%s] 收敛检测触发, 循环 %d 轮", self._project.name, cycle_num)

            decision = policy.should_stop(
                cycle_num=cycle_num,
                all_satisfied=all_satisfied,
                review_passed=review_passed,
                converged=converged,
                metrics=metrics,
            )
            if decision == "success":
                logger.info("[Spec:%s] 所有标准+审查通过, 循环 %d 轮", self._project.name, cycle_num)
                termination = "success"
                break
            if decision == "converged":
                termination = "converged"
                break

            if self.settings.spec_rebuild_session_between_cycles:
                logger.info(
                    "[Spec:%s] 循环 %d 结束, 重建 Session 以压缩对话上下文",
                    self._project.name,
                    cycle_num,
                )
                self._recreate_session_best_effort()
                if not self._session:
                    logger.error(
                        "[Spec:%s] 循环 %d Session 重建失败，session=None，下一循环将无法执行",
                        self._project.name,
                        cycle_num,
                    )

        return termination

    # ------------------------------------------------------------------
    # Long-range: work items, discovery, spec generation
    # ------------------------------------------------------------------
    def _resolve_max_cycles(self, requested: int) -> int:
        try:
            requested = int(requested)
        except Exception:
            requested = 10

        try:
            limit = int(getattr(self.settings, "spec_max_cycles_limit", 5000))
        except Exception:
            limit = 5000
        if limit <= 0:
            limit = 5000
        if requested <= 0:
            requested = 1
        return min(requested, limit)

    def _discover_optimization_questions(self, cycle_num: int) -> list[dict]:
        return _discover_optimization_questions(
            project=self._project,
            session=self._session,
            send_prompt_fn=self._send_prompt_with_retry,
            last_review=self._last_review,
            cycle_num=cycle_num,
            settings=self.settings,
        )

    def _generate_specs_from_discovery(self, cycle_num: int, discovery: list[dict]) -> list:
        return _generate_specs_from_discovery(
            project=self._project,
            session=self._session,
            send_prompt_fn=self._send_prompt_with_retry,
            root_path=self.root_path,
            settings=self.settings,
            cycle_num=cycle_num,
            discovery=discovery,
        )

    # ------------------------------------------------------------------
    # Persistence helpers (state + artifacts)
    # ------------------------------------------------------------------
    def _persist_state_best_effort(self) -> None:
        _persist_state_best_effort(self._project, self.save_state, _get_state_path(self.root_path, self.settings))

    # ------------------------------------------------------------------
    # Monitoring metrics
    # ------------------------------------------------------------------

    def _decompose_criteria_with_llm(self, text: str) -> list[str]:
        return _decompose_criteria_with_llm_impl(text, self.settings)

    # ------------------------------------------------------------------
    # Criteria evaluation (reuses loop pattern)
    # ------------------------------------------------------------------
    def _evaluate_criteria(self, criteria: list[str], cycle: int) -> dict:
        return _evaluate_criteria_impl(
            session=self._session,
            criteria=criteria,
            cycle=cycle,
            project=self._project,
            send_prompt_fn=self._send_prompt_with_retry,
            settings=self.settings,
        )

    # ------------------------------------------------------------------
    # Review (reuses loop engine's parsing infrastructure)
    # ------------------------------------------------------------------
    def _conduct_review(self, cycle: int, callbacks: SpecEngineCallbacks, cycle_obj=None) -> ReviewResult:
        # When cycle_obj is provided and parallel pipeline is enabled, collect artifacts.
        artifacts = None
        parallel_enabled = getattr(self.settings, "spec_review_parallel_enabled", True)
        if parallel_enabled and cycle_obj is not None:
            try:
                from .review_artifacts import collect_review_artifacts
                artifacts = collect_review_artifacts(
                    cycle=cycle_obj,
                    project=self._project,
                    cwd=self.root_path,
                )
            except Exception as e:
                logger.debug("[Spec] collect_review_artifacts failed, falling back to legacy: %s", repr(e))

        return _conduct_review_impl(
            session=self._session,
            settings=self.settings,
            project=self._project,
            send_prompt_with_retry_fn=self._send_prompt_with_retry,
            build_review_exception_diagnostics_fn=self._build_review_exception_diagnostics,
            circuit=self._review_circuit,
            cycle=cycle,
            on_review_done=callbacks.on_review_done,
            artifacts=artifacts,
            agent_type=self._agent_type or "coco",
            model_name=self._model_name,
        )

    def _parse_review_output(self, text: str, cycle: int) -> ReviewResult:
        return _parse_review_output_impl(
            text,
            cycle,
            parse_with_llm_fn=lambda raw: _parse_review_with_llm_impl(raw, self.settings),
        )

    def _parse_review_with_llm(self, raw_text: str) -> list[PerspectiveReview]:
        return _parse_review_with_llm_impl(raw_text, self.settings)

    @staticmethod
    def _extract_reviews_from_llm_response(text: str) -> list[PerspectiveReview]:
        return extract_reviews_from_llm_response(text)

    def _consume_guidance(self) -> str:
        if not self._user_guidance:
            return ""
        combined = "\n\n".join(self._user_guidance)
        self._user_guidance.clear()
        return f"\n## 用户引导\n{combined}\n"

    def _detect_convergence(self) -> bool:
        if not self._project:
            return False
        return detect_convergence(
            self._project,
            convergence_window=int(self.settings.spec_convergence_window or 0),
            review_enabled=self.settings.spec_review_enabled,
        )


    def refine_goal_with_guidance(self, guidance: str) -> tuple[bool, str]:
        """用 LLM 将原始需求与用户引导合并为新的综合目标，更新 project.requirement。

        Returns:
            (success, new_requirement_or_error_msg)
        """
        if not self._project:
            return False, "没有活跃的 Spec 项目"

        original = self._project.requirement
        if not original.strip():
            return False, "原始需求为空，无法合并"

        if not self.settings.ark_api_key or not self.settings.ark_model:
            # 无 LLM 配置时，退化为直接追加
            self._project.requirement = f"{original}\n\n## 补充约束/偏好\n{guidance}"
            _persist_state_best_effort(self._project, self.root_path, logger)
            logger.info("[Spec] 无 LLM 配置，直接追加引导到需求")
            return True, self._project.requirement

        prompt = build_goal_rewrite_prompt(original, guidance)
        try:
            response = self._get_llm(0.3).invoke([HumanMessage(content=prompt)])
            new_req = response.content.strip()
            if not new_req:
                return False, "LLM 返回空内容"

            self._project.requirement = new_req
            # 清除待消费的引导队列（已融入目标，无需重复注入）
            self._user_guidance.clear()
            _persist_state_best_effort(self._project, self.root_path, logger)
            logger.info("[Spec] 目标已重写(原%d字→新%d字)", len(original), len(new_req))
            return True, new_req
        except Exception as e:
            logger.warning("[Spec] 目标重写 LLM 调用失败: %s", get_error_detail(e))
            from ..utils.errors import get_error_detail as _ged
            return False, _ged(e)

    def inject_guidance(self, message: str):
        """Inject user guidance — will be included in the next phase prompt."""
        self._user_guidance.append(message)
        logger.info("[Spec] 用户引导已注入(队列=%d): %s...", len(self._user_guidance), message[:100])

    def pause(self):
        if self._project:
            self._project.status = SpecProjectStatus.PAUSED
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def _handle_max_cycles_termination(self, max_cycles: int):
        is_all_satisfied = self._project.is_all_satisfied
        last_review_passed = True
        if self.settings.spec_review_enabled:
            if self._last_review:
                last_review_passed = self._last_review.all_passed
            else:
                last_review_passed = False

        if is_all_satisfied and last_review_passed:
            msg = f"达到最大循环次数({max_cycles})。核心验收标准已满足，但仍有待办优化项（Backlog）。"
            self._project.status = SpecProjectStatus.PAUSED
            self._project.error = msg + "（已暂停，可使用 /spec_resume 继续执行优化项）"
            self._project.completed_at = time.time()
        else:
            msg = f"达到最大循环次数({max_cycles})仍未满足验收标准或审查未通过"
            if self.settings.spec_infinite_mode:
                self._project.status = SpecProjectStatus.PAUSED
                self._project.error = msg + "（已暂停，可继续 /spec_resume 或提升 SPEC_MAX_CYCLES）"
                self._project.completed_at = time.time()
            else:
                self._project.abort(msg)

    def resume(self, callbacks: Optional[SpecEngineCallbacks] = None) -> Optional[SpecProject]:
        """Resume a paused spec execution."""
        if not self._project or self._project.status not in (SpecProjectStatus.PAUSED, SpecProjectStatus.CLARIFYING):
            return self._project

        # Restore review circuit state from persistence (survives process restart)
        try:
            state_path = _get_state_path(self.root_path, self.settings)
            if os.path.isfile(state_path):
                _, circuit = self.load_state_with_circuit(state_path)
                self._review_circuit = circuit
        except Exception as e:
            logger.debug("[Spec] resume circuit restore skipped: %s", get_error_detail(e))

        callbacks = self._wrap_callbacks(callbacks or SpecEngineCallbacks())
        with self._lock:
            self._run_state = EngineRunState.RUNNING
            self._project.status = SpecProjectStatus.RUNNING
            self._termination_reason = None
        additional_cycles = self._resolve_max_cycles(self.settings.spec_max_cycles)

        last_cycle_num = 0
        if self._project.cycles:
            last_cycle_num = self._project.cycles[-1].cycle_number
        start_cycle = max(last_cycle_num, self._project.cycle_count_total) + 1
        max_cycles = start_cycle + additional_cycles - 1

        try:
            self._close_session_safely()

            # Resolve TTADK startup model (resume)
            self._session = self._create_session_fn(
                agent_type=self._agent_type,
                cwd=self.root_path,
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
                    self._project.abort(msg)
                elif reason == "max_cycles":
                    self._handle_max_cycles_termination(max_cycles)
                else:
                    msg = f"终止：{reason}"
                    self._project.abort(msg)

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

        except TimeoutError as e:
            error_msg = self._format_engine_error(e, "Spec恢复", is_timeout=True, callbacks=callbacks)
            self._project.status = SpecProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if not self._saved_task_id:
                try:
                    self._save_failed_task(error_msg, self._last_cycle_num, self._last_phase, callbacks)
                except Exception as save_err:
                    logger.warning("[Spec] 异常任务保存失败: %s", save_err)

        except Exception as e:
            error_msg = self._format_engine_error(e, "Spec恢复", is_timeout=False, callbacks=callbacks)
            self._project.status = SpecProjectStatus.ABORTED
            self._project.completed_at = time.time()
            if not self._saved_task_id:
                try:
                    self._save_failed_task(error_msg, self._last_cycle_num, self._last_phase, callbacks)
                except Exception as save_err:
                    logger.warning("[Spec] 异常任务保存失败: %s", save_err)

        finally:
            self._close_session_safely()
            self._run_state = EngineRunState.IDLE

            if self._project and self._project.status == SpecProjectStatus.COMPLETED and self._saved_task_id:
                delete_task_state(self._saved_task_id)
                self._saved_task_id = None
                self._saved_task_signature = None

        return self._project

    def _project_to_compact_dict(self) -> dict:
        return _project_to_compact_dict_impl(self._project, self.settings, self.root_path)

    def save_state(self, filepath: Optional[str] = None) -> str:
        return _save_engine_state(
            self._project,
            self.settings,
            self.root_path,
            self.chat_id,
            self._build_runtime_context,
            self._project_to_compact_dict,
            filepath,
            review_circuit=self._review_circuit.to_dict(),
        )

    @classmethod
    def load_state(cls, filepath: str) -> Optional[SpecProject]:
        project, _rc = _load_engine_state(filepath)
        return project

    @classmethod
    def load_state_with_circuit(cls, filepath: str) -> tuple[Optional[SpecProject], ReviewCircuitState]:
        """Load project + review circuit state (backward-compatible)."""
        project, rc_dict = _load_engine_state(filepath)
        circuit = ReviewCircuitState.from_dict(rc_dict) if rc_dict else ReviewCircuitState()
        return project, circuit

    def cleanup(self):
        super().cleanup()
