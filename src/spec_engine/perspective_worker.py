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
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from ..engine_base import PerspectiveReview, ReviewPerspective
from ..utils.errors import get_error_detail
from ..utils.retry import RetryPolicy
from .prompts import build_single_perspective_review_prompt
from .review_artifacts import ReviewArtifacts

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
    ):
        self.perspective = perspective
        self.timeout = float(timeout)
        self.retry_policy = retry_policy or RetryPolicy(max_retries=1, retry_delay=1.0)
        self._buf: list[str] = []
        self._lock = threading.Lock()

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
        from ..utils.spec_utils import parse_review_output_strict_tolerant

        reviews = parse_review_output_strict_tolerant(raw or "", 0)
        for r in reviews:
            if r.perspective == self.perspective:
                return r
        # No block for our perspective — treat as parse failure.
        return PerspectiveReview(
            perspective=self.perspective,
            passed=False,
            suggestions=["审查输出解析失败，请检查实现质量"],
            summary="解析失败",
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
            err = get_error_detail(e) or repr(e)
            if isinstance(e, TimeoutError):
                err_code = ReviewErrorCode.TIMEOUT
            else:
                from ..utils.errors import _has_timeout_in_chain
                if _has_timeout_in_chain(e):
                    err_code = ReviewErrorCode.TIMEOUT
                else:
                    err_code = ReviewErrorCode.WORKER_ERROR
            logger.warning(
                "[PerspectiveWorker:%s] prompt failed: %s",
                self.perspective.name,
                err,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # If runner returned empty string, fall back to accumulated event buffer.
        if not raw:
            with self._lock:
                raw = "".join(self._buf)

        if err:
            review = PerspectiveReview(
                perspective=self.perspective,
                passed=False,
                suggestions=[f"审查异常：{err}"],
                summary="异常",
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
                    err = get_error_detail(e) or repr(e)
                    if isinstance(e, TimeoutError):
                        err_code = ReviewErrorCode.TIMEOUT
                    else:
                        from ..utils.errors import _has_timeout_in_chain
                        if _has_timeout_in_chain(e):
                            err_code = ReviewErrorCode.TIMEOUT
                        else:
                            err_code = ReviewErrorCode.WORKER_ERROR

                    logger.warning(
                        "[run_workers_parallel] worker %s raised: %s",
                        b.worker.perspective.name,
                        err,
                    )
                    outcomes.append(
                        PerspectiveOutcome(
                            perspective=b.worker.perspective,
                            review=PerspectiveReview(
                                perspective=b.worker.perspective,
                                passed=False,
                                suggestions=[f"审查异常：{err}"],
                                summary="异常",
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
            err = "当前系统较繁忙，操作已超时"

            for fut in unprocessed_futures:
                b = future_to_binding[fut]
                fut.cancel()
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
                            suggestions=[f"审查异常：{err}"],
                            summary="异常",
                        ),
                        elapsed_ms=int((per_worker_timeout or 0) * 1000),
                        error=err,
                        error_code=ReviewErrorCode.TIMEOUT,
                    )
                )

    # Stable ordering by ReviewPerspective enum order (for reproducible UI).
    order = {p: i for i, p in enumerate(ReviewPerspective)}
    outcomes.sort(key=lambda o: order.get(o.perspective, 999))
    return outcomes
