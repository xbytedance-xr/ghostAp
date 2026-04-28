"""Review orchestration and circuit-breaker logic for SpecEngine."""

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, List, Optional

from .constants import SPEC_UI_TEXT
from ..engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from ..utils.retry import RetryPolicy
from ..utils.review_diagnostics import (
    build_review_exception_diagnostics,
    format_review_exception_log_line,  # noqa: F401 — re-exported for engine.py
    normalize_review_diagnostics,
)
from ..utils.review_helpers import compute_adaptive_timeout
from .retry_status import RetryEvent, RetryStatus
from .review_parsing import (  # noqa: F401 — re-export for backward compat
    extract_reviews_from_llm_response,
    parse_review_output,
    parse_review_with_llm,
)
from .review_retry import (  # noqa: F401 — re-export for backward compat
    PipelineRetryContext,
    RetryCallbacks,
    RetryConfig,
    attempt_pipeline_retry,
    build_retry_diagnostics,
    handle_pipeline_errors_with_retry,
    outcomes_to_review_result,
)
from .review_types import ReviewCircuitState, ReviewPipelineConfig  # noqa: F401 — re-exported for engine.py

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


def _build_all_perspectives_failed(
    cycle: int,
    *,
    suggestions: list[str],
    summary: str = "异常",
) -> ReviewResult:
    """Build a ReviewResult where every perspective is marked as failed.

    Extracted to eliminate DRY violations across circuit-skip, legacy-serial,
    and pipeline exception paths.
    """
    return ReviewResult(
        reviews=[
            PerspectiveReview(
                perspective=p,
                passed=False,
                suggestions=suggestions,
                summary=summary,
            )
            for p in ReviewPerspective
        ],
        iteration=cycle,
    )


def review_result_to_text(review: ReviewResult) -> str:
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


def conduct_review(
    *,
    pipeline_cfg: Optional[ReviewPipelineConfig] = None,
    # --- Flat kwargs (backward-compat; ignored when pipeline_cfg is given) ---
    session=None,
    settings: Optional["Settings"] = None,
    project=None,
    send_prompt_with_retry_fn: Optional[Callable] = None,
    build_review_exception_diagnostics_fn: Optional[Callable[..., dict]] = None,
    circuit: Optional[ReviewCircuitState] = None,
    cycle: int = 0,
    on_review_done: Optional[Callable] = None,
    # --- Pipeline params (Step 7a) ---
    artifacts: Optional["ReviewArtifacts"] = None,
    agent_type: str = "coco",
    model_name: Optional[str] = None,
    # --- Retry control ---
    cancel_event: Optional[threading.Event] = None,
    on_retry_status: Optional[Callable[[RetryEvent], None]] = None,
    skip_retry_event: Optional[threading.Event] = None,
) -> ReviewResult:
    # Materialise locals from pipeline_cfg when provided, else use flat kwargs.
    if pipeline_cfg is not None:
        settings = pipeline_cfg.settings
        circuit = pipeline_cfg.circuit
        cycle = pipeline_cfg.cycle
        session = pipeline_cfg.session
        project = pipeline_cfg.project
        send_prompt_with_retry_fn = pipeline_cfg.send_prompt_with_retry_fn
        build_review_exception_diagnostics_fn = pipeline_cfg.build_review_exception_diagnostics_fn
        on_review_done = pipeline_cfg.on_review_done
        artifacts = pipeline_cfg.artifacts
        agent_type = pipeline_cfg.agent_type
        model_name = pipeline_cfg.model_name
        cancel_event = pipeline_cfg.cancel_event
        on_retry_status = pipeline_cfg.on_retry_status
        skip_retry_event = pipeline_cfg.skip_retry_event

    # Guard: settings and circuit are mandatory.
    assert settings is not None, "conduct_review requires settings"
    assert circuit is not None, "conduct_review requires circuit"
    from .prompts import build_review_prompt

    enabled = settings.spec_review_failure_circuit_enabled
    max_consecutive = max(1, settings.spec_review_failure_max_consecutive)
    cooldown_cycles = max(0, settings.spec_review_failure_cooldown_cycles)

    # ---- Circuit breaker skip (unchanged) ----
    if (
        enabled
        and int(circuit.review_circuit_open_until_cycle or 0)
        and int(cycle or 0) <= int(circuit.review_circuit_open_until_cycle or 0)
    ):
        circuit.consecutive_skips += 1
        skip_overrun_threshold = max(1, max_consecutive) * 2
        is_skip_overrun = circuit.consecutive_skips >= skip_overrun_threshold
        if is_skip_overrun:
            logger.warning(
                "[Spec] review_skip_overrun: consecutive_skips=%d >= threshold=%d cycle=%d open_until=%d, 跳过次数异常偏高",
                circuit.consecutive_skips,
                skip_overrun_threshold,
                int(cycle or 0),
                int(circuit.review_circuit_open_until_cycle or 0),
            )

        diag_raw = {
            "phase": "review",
            "role": "multi_perspective",
            "cycle": int(cycle or 0),
            "decision": "review_circuit_open_skip",
            "fail_reason": "circuit_open",
            "err_type": "ReviewCircuitOpen",
            "err_repr": "<ReviewCircuitOpen>",
            "error_text": "review_circuit_open",
            "cycle_number": int(cycle or 0),
            "exception_type": "ReviewCircuitOpen",
            "review_role": "multi_perspective",
            "traceback_snippet": "",
            "consecutive_failures": int(circuit.review_failure_consecutive or 0),
            "open_until_cycle": int(circuit.review_circuit_open_until_cycle or 0),
        }

        diag = normalize_review_diagnostics(diag_raw)
        circuit.last_review_failure_diag = dict(diag)
        logger.warning(
            "[Spec] review_circuit_open: phase=review role=multi_perspective cycle=%s decision=review_circuit_open_skip open_until=%s consecutive=%s, 将跳过本轮审查",
            diag_raw.get("cycle_number"),
            diag_raw.get("open_until_cycle"),
            diag_raw.get("consecutive_failures"),
        )
        _base_msg = SPEC_UI_TEXT["circuit_breaker_skip_with_count"].format(count=int(circuit.review_failure_consecutive or 0))
        if is_skip_overrun:
            logger.warning(
                "[Spec] skip-overrun: consecutive_skips=%d, 熔断器可能卡住，建议排查",
                circuit.consecutive_skips,
            )

        # Lightweight lint fallback: run local lint when circuit is open
        _lint_msg = ""
        try:
            _lint_enabled = settings.review_circuit_lint_fallback_enabled
            if _lint_enabled and project and hasattr(project, "root_path") and project.root_path:
                import glob as _glob
                import os

                from ..utils.lightweight_lint import run_lightweight_lint
                _lint_timeout = settings.review_circuit_lint_timeout
                _py_files = _glob.glob(os.path.join(project.root_path, "**/*.py"), recursive=True)[:50]
                if _py_files:
                    _lint_result = run_lightweight_lint(_py_files, timeout=_lint_timeout)
                    _lint_msg = _lint_result.summary()
        except Exception:
            logger.debug("lightweight lint failed", exc_info=True)

        _suggestions = [_base_msg]
        if _lint_msg:
            _suggestions.append(_lint_msg)

        review_result = _build_all_perspectives_failed(
            cycle, suggestions=_suggestions, summary="审查暂停",
        )

        # Emit structured metrics for circuit-breaker skip (aligned with exception path).
        _skip_metrics = {
            "metric_type": "review_circuit_skip",
            "engine": "spec",
            "cycle": int(cycle or 0),
            "consecutive_failures": int(circuit.review_failure_consecutive or 0),
            "consecutive_skips": int(circuit.consecutive_skips or 0),
            "open_until_cycle": int(circuit.review_circuit_open_until_cycle or 0),
            "backoff_level": int(circuit.backoff_level or 0),
            "is_skip_overrun": is_skip_overrun,
        }
        try:
            import json as _json
            from ..utils.metrics_exporter import get_metrics_exporter
            _exporter_type = getattr(settings, "review_metrics_exporter_type", "logger") or "logger"
            _exporter_kwargs: dict = {}
            if _exporter_type == "jsonl":
                _exporter_kwargs["path"] = getattr(settings, "review_metrics_jsonl_path", "review_metrics.jsonl")
            exporter = get_metrics_exporter(exporter_type=_exporter_type, **_exporter_kwargs)
            exporter.export_metrics(_skip_metrics, prefix="[Spec]")
        except Exception:
            try:
                import json as _json
                logger.info("[Spec] review_circuit_skip_metrics: %s", _json.dumps(_skip_metrics, ensure_ascii=False))
            except Exception:
                logger.debug("review_circuit_skip_metrics serialization failed", exc_info=True)

        if on_review_done:
            on_review_done(cycle, review_result)
        return review_result

    # ---- Pipeline path: use parallel review pipeline when artifacts provided ----
    if artifacts is not None:
        return _conduct_review_pipeline(
            artifacts=artifacts,
            settings=settings,
            circuit=circuit,
            cycle=cycle,
            agent_type=agent_type,
            model_name=model_name,
            build_review_exception_diagnostics_fn=build_review_exception_diagnostics_fn,
            on_review_done=on_review_done,
            cancel_event=cancel_event,
            on_retry_status=on_retry_status,
            skip_retry_event=skip_retry_event,
        )

    # ---- Legacy serial path (kept for backward compat until callers provide artifacts) ----
    if not session:
        review_result = ReviewResult(iteration=cycle)
        if on_review_done:
            on_review_done(cycle, review_result)
        return review_result

    review_prompt = build_review_prompt(project.requirement if project else "")
    review_text: list[str] = []
    thought_text: list[str] = []

    from ..acp import ACPEvent, ACPEventType

    def on_review_event(event: ACPEvent):
        if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
            review_text.append(event.text)
        elif event.event_type == ACPEventType.THOUGHT_CHUNK and event.text:
            thought_text.append(event.text)

    circuit.last_review_failure_diag = None
    review_timeout: int = 0  # sentinel — overwritten inside try; safe fallback for metrics

    _t0 = time.monotonic()

    try:
        base_timeout = settings.spec_review_timeout
        min_timeout = settings.spec_review_min_timeout
        hard_floor = settings.spec_review_hard_floor
        review_timeout = compute_adaptive_timeout(
            circuit.consecutive_timeouts, base_timeout=base_timeout, min_timeout=min_timeout,
            hard_floor=hard_floor,
        )
        send_prompt_with_retry_fn(
            review_prompt,
            on_event=on_review_event,
            timeout=review_timeout,
            retry_policy=RetryPolicy(max_retries=2, retry_delay=2.0),
            before_retry=lambda a, e: (review_text.clear(), thought_text.clear()) if a > 0 else None,
            total_timeout=float(review_timeout * 2),
        )
        full_text = "".join(review_text)
        combined_text = full_text
        if thought_text:
            combined_text = full_text + "\n" + "".join(thought_text)
        review_result = parse_review_output(
            combined_text,
            cycle,
            parse_with_llm_fn=lambda raw: parse_review_with_llm(raw, settings),
        )
        circuit.reset_on_success()
    except Exception as e:
        from ..utils.review_helpers import handle_review_exception

        _elapsed_ms = int((time.monotonic() - _t0) * 1000)
        result = handle_review_exception(
            e,
            circuit=circuit,
            cycle=cycle,
            settings=settings,
            engine="spec",
            build_diag_fn=build_review_exception_diagnostics_fn,
            review_timeout=review_timeout,
            review_elapsed_ms=_elapsed_ms,
        )
        review_result = _build_all_perspectives_failed(
            cycle, suggestions=[result.suggestion_text],
        )

    if on_review_done:
        on_review_done(cycle, review_result)

    return review_result




def _conduct_review_pipeline(
    *,
    artifacts: "ReviewArtifacts",
    settings: "Settings",
    circuit: ReviewCircuitState,
    cycle: int,
    agent_type: str,
    model_name: Optional[str],
    build_review_exception_diagnostics_fn: Callable[..., dict],
    on_review_done: Optional[Callable],
    cancel_event: Optional[threading.Event] = None,
    on_retry_status: Optional[Callable[[RetryEvent], None]] = None,
    skip_retry_event: Optional[threading.Event] = None,
) -> ReviewResult:
    """Run the parallel review pipeline (Step 7a) with circuit-breaker bookkeeping."""
    import math

    from .cycle_budget import CycleBudget
    from .review_pipeline import run_review_pipeline

    # Budget: Compute total budget by considering concurrency limit.
    base_timeout = settings.spec_review_timeout
    max_parallel = max(1, settings.spec_review_max_parallel)

    # 动态引用枚举长度，新增/删减视角时 budget 自动联动。
    # NOTE: perspective_count 始终为全量枚举长度。lint_gate 短路在 run_review_pipeline
    # 内部 early return（所有视角统一返回 lint_gate_short_circuit），不影响此处 budget 计算。
    # circuit_breaker 在 conduct_review 入口跳过整个 pipeline 调用，也不进入此路径。
    # 因此此处不需要 active_perspective_count — 要么全量执行，要么整个函数不被调用。
    perspective_count = len(ReviewPerspective)

    # 如果允许的并发度低于视角的数量，则至少需要排队分批执行。
    # 为了避免排队引发 timeout，预估批次数 multiplier = ceil(perspective_count / max_parallel)
    multiplier = math.ceil(perspective_count / max_parallel)

    # 我们给予一定冗余（默认原先是 * 2，现在可以动态放大预估值）。
    budget_seconds = float(base_timeout * max(2, multiplier + 3))
    budget = CycleBudget(total_seconds=budget_seconds, label=f"spec_review_c{cycle}")

    circuit.last_review_failure_diag = None
    _t0 = time.monotonic()

    try:
        outcomes = run_review_pipeline(
            artifacts,
            budget,
            agent_type=agent_type,
            model_name=model_name,
        )
        review_result = outcomes_to_review_result(outcomes, cycle)

        # Pipeline succeeded — reset circuit counters.
        has_real_errors = any(o.error and o.error != "lint_gate_short_circuit" for o in outcomes)
        if not has_real_errors:
            circuit.reset_on_success()
        else:
            retry_ctx = PipelineRetryContext(
                config=RetryConfig(
                    base_timeout=base_timeout,
                    multiplier=multiplier,
                    pipeline_fn=run_review_pipeline,
                    budget_cls=CycleBudget,
                    artifacts=artifacts,
                    agent_type=agent_type,
                    model_name=model_name,
                ),
                callbacks=RetryCallbacks(
                    cancel_event=cancel_event,
                    on_retry_status=on_retry_status,
                    skip_retry_event=skip_retry_event,
                ),
            )
            review_result, diag = handle_pipeline_errors_with_retry(
                outcomes=outcomes,
                review_result=review_result,
                circuit=circuit,
                settings=settings,
                cycle=cycle,
                ctx=retry_ctx,
                retry_texts={
                    "retry_no_retry": SPEC_UI_TEXT["retry_no_retry"],
                    "retry_exhausted": SPEC_UI_TEXT["retry_exhausted"],
                },
            )
            circuit.last_review_failure_diag = diag

        logger.info(
            "[Spec] pipeline review done: cycle=%d perspectives=%d all_passed=%s elapsed_ms=%d",
            cycle,
            len(outcomes),
            review_result.all_passed,
            int((time.monotonic() - _t0) * 1000),
        )
    except Exception as e:
        from ..utils.review_helpers import handle_review_exception

        _elapsed_ms = int((time.monotonic() - _t0) * 1000)
        result = handle_review_exception(
            e,
            circuit=circuit,
            cycle=cycle,
            settings=settings,
            engine="spec",
            build_diag_fn=build_review_exception_diagnostics_fn,
            review_timeout=int(budget_seconds),
            review_elapsed_ms=_elapsed_ms,
        )
        review_result = _build_all_perspectives_failed(
            cycle, suggestions=[result.suggestion_text],
        )

    if on_review_done:
        on_review_done(cycle, review_result)

    return review_result


class ReviewOrchestrator:
    """Encapsulates review orchestration: circuit-breaker state, cancel event, and conduct_review coordination.

    SpecEngine holds an instance of this class and delegates all review-related
    operations to it, reducing the God Object's cognitive load.
    """

    def __init__(self) -> None:
        self._circuit = ReviewCircuitState()
        self._cancel_event = threading.Event()
        self._skip_retry_event = threading.Event()

    # ------------------------------------------------------------------
    # Public properties (allow external access for compatibility)
    # ------------------------------------------------------------------
    @property
    def circuit(self) -> ReviewCircuitState:
        return self._circuit

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def skip_retry_event(self) -> threading.Event:
        return self._skip_retry_event

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------
    def conduct_review(
        self,
        *,
        pipeline_cfg: Optional[ReviewPipelineConfig] = None,
        session=None,
        settings: Optional["Settings"] = None,
        project=None,
        send_prompt_with_retry_fn: Optional[Callable] = None,
        build_review_exception_diagnostics_fn: Optional[Callable[..., dict]] = None,
        cycle: int = 0,
        on_review_done: Optional[Callable] = None,
        artifacts=None,
        agent_type: str = "coco",
        model_name: Optional[str] = None,
        on_retry_status: Optional[Callable[[RetryEvent], None]] = None,
    ) -> ReviewResult:
        """Delegate to the module-level conduct_review with internal circuit/cancel state."""
        if pipeline_cfg is not None:
            # Inject orchestrator-owned state into the config.
            pipeline_cfg.circuit = self._circuit
            pipeline_cfg.cancel_event = self._cancel_event
            pipeline_cfg.skip_retry_event = self._skip_retry_event
            return conduct_review(pipeline_cfg=pipeline_cfg)
        return conduct_review(
            session=session,
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=send_prompt_with_retry_fn,
            build_review_exception_diagnostics_fn=build_review_exception_diagnostics_fn,
            circuit=self._circuit,
            cycle=cycle,
            on_review_done=on_review_done,
            artifacts=artifacts,
            agent_type=agent_type,
            model_name=model_name,
            cancel_event=self._cancel_event,
            on_retry_status=on_retry_status,
            skip_retry_event=self._skip_retry_event,
        )

    def reset_cancel_event(self, *, is_running: bool) -> bool:
        """Reset _cancel_event, guarding against stop/pause races.

        Args:
            is_running: Whether the engine is in RUNNING state (caller reads
                        under its own lock before calling this method).

        Returns True if the event was successfully cleared (engine is RUNNING),
        False if the engine is no longer running (event is set immediately).
        """
        self._cancel_event.clear()
        if not is_running:
            self._cancel_event.set()
            return False
        return True

    def signal_stop(self) -> None:
        """Signal that the engine is stopping — interrupts any waiting review retry."""
        self._cancel_event.set()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return self._circuit.to_dict()

    def restore_circuit(self, data) -> None:
        """Restore circuit state from a persisted dict or a ReviewCircuitState instance."""
        if isinstance(data, ReviewCircuitState):
            self._circuit = data
        elif data:
            self._circuit = ReviewCircuitState.from_dict(data)
        else:
            self._circuit = ReviewCircuitState()

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewOrchestrator":
        """Create a ReviewOrchestrator with circuit state restored from dict."""
        instance = cls()
        instance._circuit = ReviewCircuitState.from_dict(data) if data else ReviewCircuitState()
        return instance
