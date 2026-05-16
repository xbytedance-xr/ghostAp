"""PerspectiveWorker: single-perspective review unit.

Step 4 of the review refactor. Each worker:
    * owns one ReviewPerspective,
    * consumes ReviewArtifacts (no session context dependency),
    * runs a single small prompt via an injected prompt_runner,
    * parses output into a PerspectiveReview,
    * reports timing + outcome for the pipeline.

Parallelism policy: workers are stateless & independent. The caller is
responsible for session allocation — `run_workers_parallel` uses a
ThreadPoolExecutor where each submitted worker gets its own
`prompt_runner` binding (typically backed by its own ephemeral session).
Passing the same runner to multiple workers concurrently is NOT supported
because the underlying ACP session is single-threaded.

Step 7 wires workers into ReviewPipeline; this module only defines the
contract + parallel runner.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..engine_base import PerspectiveReview, ReviewPerspective
from ..utils.errors import classify_timeout, get_error_detail
from ..utils.retry import RetryPolicy
from .constants import SPEC_UI_TEXT
from .prompts import build_single_perspective_review_prompt
from .review_artifacts import ReviewArtifacts
from .utils import parse_review_output_strict_tolerant

logger = logging.getLogger(__name__)

__all__ = [
    "PerspectiveOutcome",
    "PerspectiveWorker",
    "ReviewErrorCode",
    "run_workers_parallel",
]

class ReviewErrorCode(Enum):
    """Enumeration of strict error codes to distinguish perspective worker failures."""
    TIMEOUT = "TIMEOUT"
    WORKER_ERROR = "WORKER_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


PromptRunner = Callable[[str, Callable, float], str]
"""Signature: (prompt, on_event, timeout) -> raw_text.

The runner must block until the prompt completes (or raise). It owns session
lifecycle — workers do NOT touch sessions directly.
"""


@dataclass
class PerspectiveOutcome:
    """What a worker returns. Holds review + diagnostics."""

    perspective: ReviewPerspective
    review: PerspectiveReview
    elapsed_ms: int = 0
    error: Optional[str] = None  # non-empty = this worker failed; review is synthetic FAIL
    error_code: Optional[ReviewErrorCode] = None
    raw_preview: str = ""  # first 500 chars of raw output, for debugging

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_diag(self) -> dict:
        return {
            "perspective": self.perspective.value,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "error_code": self.error_code.value if self.error_code else None,
            "passed": self.review.passed,
            "suggestion_count": len(self.review.suggestions),
        }


class PerspectiveWorker:
    """Runs review for exactly one perspective.

    Not thread-safe *with itself* — a single worker instance runs at most one
    prompt at a time. Different perspectives use different worker instances.
    """

    def __init__(
        self,
        perspective: ReviewPerspective,
        *,
        timeout: float,
        retry_policy: Optional[RetryPolicy] = None,
        consecutive_timeouts: int = 0,
        parse_failure_default: str = "fail",
    ):
        self.perspective = perspective
        self.timeout = float(timeout)
        self.retry_policy = retry_policy or RetryPolicy(max_retries=2, retry_delay=1.5)
        self.consecutive_timeouts = int(consecutive_timeouts)
        self.parse_failure_default = parse_failure_default
        self._buf: list[str] = []
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def _on_event(self, event) -> None:
        from ..acp import ACPEventType

        try:
            et = getattr(event, "event_type", None)
            text = getattr(event, "text", None)
            if et == ACPEventType.TEXT_CHUNK and text:
                with self._lock:
                    self._buf.append(text)
        except Exception as e:
            logger.debug("[PerspectiveWorker:%s] on_event error: %s", self.perspective.name, repr(e))

    def _parse(self, raw: str) -> PerspectiveReview:
        # 首先尝试严格/宽松模式解析
        reviews = parse_review_output_strict_tolerant(raw or "", 0)
        for r in reviews:
            if r.perspective == self.perspective:
                return r

        # 如果没有找到，尝试更宽松的解析模式
        from .utils import parse_review_output_loose
        reviews = parse_review_output_loose(raw or "", 0)
        for r in reviews:
            if r.perspective == self.perspective:
                return r

        # 如果还是找不到，尝试从整个文本中推断一个通用的审查结果
        from .utils import (
            extract_suggestions_from_body,
            normalize_review_verdict,
        )

        # 无论如何都尝试提取信息，即使没有视角关键词
        verdict = normalize_review_verdict(raw)
        suggestions = extract_suggestions_from_body(raw, limit=5)

        if verdict == "PASS":
            return PerspectiveReview(
                perspective=self.perspective,
                passed=True,
                suggestions=[],
                summary=SPEC_UI_TEXT["worker_summary_passed"],
            )
        elif verdict == "FAIL" or suggestions:
            return PerspectiveReview(
                perspective=self.perspective,
                passed=verdict == "PASS",
                suggestions=suggestions if suggestions else [SPEC_UI_TEXT["worker_suggestion_default"]],
                summary=SPEC_UI_TEXT["worker_summary_n_suggestions"].format(n=len(suggestions)) if suggestions else SPEC_UI_TEXT["worker_summary_has_suggestions"],
            )

        # Fallback: parse failure — use config-driven default (fail-safe)
        _passed = self.parse_failure_default == "pass"
        return PerspectiveReview(
            perspective=self.perspective,
            passed=_passed,
            suggestions=[],
            summary=SPEC_UI_TEXT["worker_summary_passed"] if _passed else SPEC_UI_TEXT["worker_summary_exception"],
        )

    def run(
        self,
        artifacts: ReviewArtifacts,
        prompt_runner: PromptRunner,
    ) -> PerspectiveOutcome:
        prompt = build_single_perspective_review_prompt(
            self.perspective,
            requirement=artifacts.requirement,
            diff_patch=artifacts.diff_patch,
            touched_files=artifacts.touched_files,
            spec_output=artifacts.spec_output,
            plan_output=artifacts.plan_output,
            build_output=artifacts.build_output,
        )
        self._buf.clear()
        t0 = time.monotonic()
        raw = ""
        err: Optional[str] = None
        err_code: Optional[ReviewErrorCode] = None
        try:
            raw = prompt_runner(prompt, self._on_event, self.timeout) or ""
        except Exception as e:
            err = get_error_detail(e, default=SPEC_UI_TEXT["worker_error_unknown"])
            err_code = ReviewErrorCode.TIMEOUT if classify_timeout(e) else ReviewErrorCode.WORKER_ERROR
            logger.warning(
                "[PerspectiveWorker:%s] prompt failed: %s (elapsed_ms=%d, configured_timeout=%s)",
                self.perspective.name,
                err,
                int((time.monotonic() - t0) * 1000),
                self.timeout,
            )
            # Converge timeout errors to domain-semantic message for user-facing suggestions.
            if err_code == ReviewErrorCode.TIMEOUT:
                err = SPEC_UI_TEXT["retry_no_retry"]

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # If runner returned empty string, fall back to accumulated event buffer.
        if not raw:
            with self._lock:
                raw = "".join(self._buf)

        if err:
            _diag_suffix = f"（视角={self.perspective.name}, 配置超时={int(self.timeout)}s, 实际耗时={elapsed_ms}ms, 连续超时={self.consecutive_timeouts}次）"
            review = PerspectiveReview(
                perspective=self.perspective,
                passed=False,
                suggestions=[f"审查异常：{err}{_diag_suffix}"],
                summary=SPEC_UI_TEXT["worker_summary_exception"],
            )
        else:
            review = self._parse(raw)

        return PerspectiveOutcome(
            perspective=self.perspective,
            review=review,
            elapsed_ms=elapsed_ms,
            error=err,
            error_code=err_code,
            raw_preview=(raw or "")[:500],
        )


@dataclass
class WorkerBinding:
    """Pairs a worker with its own prompt_runner (typically own session)."""

    worker: PerspectiveWorker
    prompt_runner: PromptRunner
    label: str = ""


def run_workers_parallel(
    bindings: list[WorkerBinding],
    artifacts: ReviewArtifacts,
    *,
    max_workers: Optional[int] = None,
    per_worker_timeout: Optional[float] = None,
) -> list[PerspectiveOutcome]:
    """Run all bindings concurrently, one thread per binding.

    `per_worker_timeout` caps the wait for each future to return — if a worker
    exceeds it, the binding is marked failed (synthetic FAIL review). The
    underlying session-level timeout still applies inside the worker.
    """
    if not bindings:
        return []

    workers_n = max_workers or len(bindings)
    outcomes: list[PerspectiveOutcome] = []

    with ThreadPoolExecutor(max_workers=workers_n, thread_name_prefix="persp-") as pool:
        future_to_binding = {
            pool.submit(b.worker.run, artifacts, b.prompt_runner): b for b in bindings
        }
        processed_futures = set()
        try:
            for fut in as_completed(future_to_binding, timeout=per_worker_timeout):
                b = future_to_binding[fut]
                processed_futures.add(fut)
                try:
                    outcomes.append(fut.result())
                except Exception as e:
                    err = get_error_detail(e, default=SPEC_UI_TEXT["worker_error_unknown"])
                    err_code = ReviewErrorCode.TIMEOUT if classify_timeout(e) else ReviewErrorCode.WORKER_ERROR

                    logger.warning(
                        "[run_workers_parallel] worker %s raised: %s",
                        b.worker.perspective.name,
                        err,
                    )
                    # Converge timeout errors to domain-semantic message for user-facing suggestions.
                    if err_code == ReviewErrorCode.TIMEOUT:
                        err = SPEC_UI_TEXT["retry_no_retry"]
                    _diag_suffix = f"（视角={b.worker.perspective.name}, 配置超时={int(b.worker.timeout)}s, 实际耗时=N/A）"
                    outcomes.append(
                        PerspectiveOutcome(
                            perspective=b.worker.perspective,
                            review=PerspectiveReview(
                                perspective=b.worker.perspective,
                                passed=False,
                                suggestions=[f"审查异常：{err}{_diag_suffix}"],
                                summary=SPEC_UI_TEXT["worker_summary_exception"],
                            ),
                            elapsed_ms=0,
                            error=err,
                            error_code=err_code,
                        )
                    )
        except TimeoutError:
            # Some futures did not complete within per_worker_timeout.
            # Synthesize FAIL outcomes for all unfinished bindings.
            unprocessed_futures = set(future_to_binding.keys()) - processed_futures

            # Use domain semantics, disregarding the stdlib's internal format
            err = SPEC_UI_TEXT["retry_no_retry"]

            for fut in unprocessed_futures:
                b = future_to_binding[fut]
                fut.cancel()
                _pool_elapsed_ms = int((per_worker_timeout or 0) * 1000)
                _diag_suffix = f"（视角={b.worker.perspective.name}, 配置超时={int(b.worker.timeout)}s, 实际耗时={_pool_elapsed_ms}ms）"
                logger.warning(
                    "[run_workers_parallel] worker %s timeout: %s",
                    b.worker.perspective.name,
                    err,
                )
                outcomes.append(
                    PerspectiveOutcome(
                        perspective=b.worker.perspective,
                        review=PerspectiveReview(
                            perspective=b.worker.perspective,
                            passed=False,
                            suggestions=[f"审查异常：{err}{_diag_suffix}"],
                            summary=SPEC_UI_TEXT["worker_summary_exception"],
                        ),
                        elapsed_ms=_pool_elapsed_ms,
                        error=err,
                        error_code=ReviewErrorCode.TIMEOUT,
                    )
                )
            logger.warning(
                "[run_workers_parallel] pool timeout summary: %d worker(s) unfinished, per_worker_timeout=%s",
                len(unprocessed_futures),
                per_worker_timeout,
            )

    # Stable ordering by ReviewPerspective enum order (for reproducible UI).
    order = {p: i for i, p in enumerate(ReviewPerspective)}
    outcomes.sort(key=lambda o: order.get(o.perspective, 999))
    return outcomes
