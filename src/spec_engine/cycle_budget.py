"""CycleBudget: wall-clock cap for a single review cycle.

Step 5 of the review refactor. Prior to this, an unlucky cycle could spend
5 × spec_review_timeout (~10 min) on a single review round while the user
waits, because each perspective retries independently and serially.

A CycleBudget gives the pipeline a hard wall-clock limit. Workers still
finish individually on the worker-level timeout; the budget governs the
aggregate. When the budget is exceeded, unfinished workers are degraded
to synthetic FAIL outcomes ("预算超时") so the cycle keeps moving.

This module is standalone — it does not touch engines or sessions.
Step 7 wires it into ReviewPipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..engine_base import PerspectiveReview, ReviewPerspective
from .perspective_worker import (
    PerspectiveOutcome,
    WorkerBinding,
    run_workers_parallel,
)
from .review_artifacts import ReviewArtifacts

logger = logging.getLogger(__name__)

__all__ = [
    "CycleBudget",
    "run_with_budget",
]


@dataclass
class CycleBudget:
    """Monotonic wall-clock budget for one review cycle.

    `total_seconds <= 0` means "no budget" (infinite). `start()` must be
    called exactly once before `remaining()` / `exceeded()` are meaningful.
    """

    total_seconds: float
    started_at: float = 0.0
    label: str = "spec_review"
    _started: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        if self._started:
            return
        self.started_at = time.monotonic()
        self._started = True

    @property
    def started(self) -> bool:
        return self._started

    @property
    def unlimited(self) -> bool:
        return self.total_seconds <= 0

    def elapsed(self) -> float:
        if not self._started:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

    def remaining(self) -> float:
        """Seconds left, clamped to >=0. Returns +inf when unlimited."""
        if self.unlimited:
            return float("inf")
        if not self._started:
            return float(self.total_seconds)
        return max(0.0, float(self.total_seconds) - self.elapsed())

    def exceeded(self) -> bool:
        if self.unlimited:
            return False
        if not self._started:
            return False
        return self.elapsed() >= float(self.total_seconds)

    def snapshot(self) -> dict:
        return {
            "label": self.label,
            "total_seconds": float(self.total_seconds),
            "started": self._started,
            "elapsed": round(self.elapsed(), 3),
            "remaining": round(self.remaining(), 3) if not self.unlimited else -1.0,
            "exceeded": self.exceeded(),
            "unlimited": self.unlimited,
        }


def _budget_exceeded_outcome(perspective: ReviewPerspective, elapsed_ms: int) -> PerspectiveOutcome:
    return PerspectiveOutcome(
        perspective=perspective,
        review=PerspectiveReview(
            perspective=perspective,
            passed=False,
            suggestions=["审查预算超时：本视角未在周期内完成，已跳过"],
            summary="预算超时",
        ),
        elapsed_ms=elapsed_ms,
        error="cycle_budget_exceeded",
    )


def run_with_budget(
    bindings: list[WorkerBinding],
    artifacts: ReviewArtifacts,
    budget: CycleBudget,
    *,
    max_workers: Optional[int] = None,
    min_per_worker_s: float = 5.0,
) -> list[PerspectiveOutcome]:
    """Run perspective workers under a wall-clock cycle budget.

    Parallelism still comes from run_workers_parallel; this wrapper:
        1. Starts the budget (idempotent).
        2. Computes per_worker_timeout = remaining budget (or unlimited).
        3. Synthesizes "budget exceeded" outcomes if run_workers_parallel
           returns fewer outcomes than bindings.

    `min_per_worker_s` guards against starting a cycle with essentially
    zero remaining time: if remaining < min, we skip all workers and emit
    synthetic FAIL outcomes immediately.
    """
    if not bindings:
        return []

    budget.start()

    if budget.exceeded():
        logger.warning("[CycleBudget] already exceeded at start; skipping all perspectives")
        return [_budget_exceeded_outcome(b.worker.perspective, 0) for b in bindings]

    if budget.unlimited:
        per_worker_timeout: Optional[float] = None
    else:
        remaining = budget.remaining()
        if remaining < float(min_per_worker_s):
            logger.warning(
                "[CycleBudget] remaining %.2fs < min %.2fs; skipping all perspectives",
                remaining,
                min_per_worker_s,
            )
            return [_budget_exceeded_outcome(b.worker.perspective, 0) for b in bindings]
        per_worker_timeout = remaining

    outcomes = run_workers_parallel(
        bindings,
        artifacts,
        max_workers=max_workers,
        per_worker_timeout=per_worker_timeout,
    )

    # Safety net: ensure every binding has an outcome.
    seen = {o.perspective for o in outcomes}
    missing = [b for b in bindings if b.worker.perspective not in seen]
    if missing:
        elapsed_ms = int(budget.elapsed() * 1000)
        for b in missing:
            logger.warning(
                "[CycleBudget] missing outcome for %s; synthesizing budget-exceeded FAIL",
                b.worker.perspective.name,
            )
            outcomes.append(_budget_exceeded_outcome(b.worker.perspective, elapsed_ms))

    order = {p: i for i, p in enumerate(ReviewPerspective)}
    outcomes.sort(key=lambda o: order.get(o.perspective, 999))
    return outcomes
