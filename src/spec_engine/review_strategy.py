"""ReviewStrategy interface and strategy registry.

Step 2 of the review refactor. Current `conduct_review` is wrapped as
`MultiPerspectiveStrategy` without behavior changes — the engine may
continue to call `conduct_review` directly; this module offers a parallel,
forward-compatible entry point that later steps will swap in.

Strategies:
    * NoReviewStrategy          — always returns empty (fastest path).
    * MultiPerspectiveStrategy  — current 5-perspective review, unchanged.

Future steps will add: LintOnlyStrategy, PerspectiveParallelStrategy,
HeterogeneousAgentsStrategy. They all implement the same ABC so the engine
code path stays the same.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..engine_base import ReviewResult
from .review import ReviewCircuitState, ReviewPipelineConfig, conduct_review
from .review_artifacts import ReviewArtifacts

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewContext",
    "ReviewStrategy",
    "NoReviewStrategy",
    "MultiPerspectiveStrategy",
    "select_review_strategy",
]


@dataclass
class ReviewContext:
    """Inputs a strategy needs to conduct a review.

    `artifacts` is optional in this step — the current MultiPerspectiveStrategy
    still uses the live session's in-memory context. Later strategies will
    require `artifacts` to be populated.
    """

    cycle: int
    session: Any
    settings: Any
    project: Any
    send_prompt_with_retry_fn: Callable
    build_review_exception_diagnostics_fn: Callable[..., dict]
    circuit: ReviewCircuitState
    on_review_done: Optional[Callable] = None
    artifacts: Optional[ReviewArtifacts] = None
    cancel_event: Optional[threading.Event] = None
    on_retry_status: Optional[Callable[[str], None]] = None
    agent_type: str = "coco"
    model_name: Optional[str] = None


class ReviewStrategy(ABC):
    """Strategy contract — every review variant implements this."""

    name: str = "base"

    @abstractmethod
    def run(self, ctx: ReviewContext) -> ReviewResult:
        ...


class NoReviewStrategy(ReviewStrategy):
    """Skip review entirely. Useful for fast-iteration prototypes."""

    name = "none"

    def run(self, ctx: ReviewContext) -> ReviewResult:
        result = ReviewResult(iteration=ctx.cycle)
        if ctx.on_review_done:
            try:
                ctx.on_review_done(ctx.cycle, result)
            except Exception as e:
                logger.debug("[ReviewStrategy:none] on_review_done raised: %s", repr(e))
        return result


class MultiPerspectiveStrategy(ReviewStrategy):
    """Current production behavior — 5 perspectives in one ACP prompt.

    Thin wrapper over `conduct_review`. Preserves every side effect
    (circuit breaker, diagnostics, lint fallback) exactly as before.
    """

    name = "multi_perspective"

    def run(self, ctx: ReviewContext) -> ReviewResult:
        return conduct_review(
            pipeline_cfg=ReviewPipelineConfig(
                settings=ctx.settings,
                circuit=ctx.circuit,
                cycle=ctx.cycle,
                session=ctx.session,
                project=ctx.project,
                send_prompt_with_retry_fn=ctx.send_prompt_with_retry_fn,
                build_review_exception_diagnostics_fn=ctx.build_review_exception_diagnostics_fn,
                on_review_done=ctx.on_review_done,
                cancel_event=ctx.cancel_event,
                on_retry_status=ctx.on_retry_status,
                agent_type=ctx.agent_type,
                model_name=ctx.model_name,
            ),
        )


_STRATEGY_REGISTRY: dict[str, type[ReviewStrategy]] = {
    NoReviewStrategy.name: NoReviewStrategy,
    MultiPerspectiveStrategy.name: MultiPerspectiveStrategy,
}


def select_review_strategy(settings) -> ReviewStrategy:
    """Pick a strategy by `settings.spec_review_strategy` (default: multi_perspective).

    Unknown names fall back to MultiPerspectiveStrategy with a warning so
    misconfig never breaks cycles.
    """
    name = str(getattr(settings, "spec_review_strategy", "") or "multi_perspective").strip().lower()
    cls = _STRATEGY_REGISTRY.get(name)
    if cls is None:
        logger.warning(
            "[ReviewStrategy] unknown strategy=%r, falling back to multi_perspective",
            name,
        )
        cls = MultiPerspectiveStrategy
    return cls()
