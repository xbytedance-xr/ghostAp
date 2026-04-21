"""ReviewPipeline: higher-level assembly of the parallel review process.

Step 7a of the review refactor. This module coordinates:
    1. L1 Lint Gate (syntactic fast-fail).
    2. Ephemeral review sessions (Step 3).
    3. Parallel workers (Step 4).
    4. Cycle budget (Step 5).

By using ephemeral sessions and parallel workers, we solve the "context bloating"
and "serialized latency" issues of the old review process.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from ..engine_base import ReviewPerspective
from ..agent_session import EphemeralReviewSession
from .cycle_budget import CycleBudget, run_with_budget
from .lint_gate import LintGateDecision, build_lint_gate_outcomes, evaluate_lint_gate
from .perspective_worker import PerspectiveOutcome, PerspectiveWorker, WorkerBinding
from .review_artifacts import ReviewArtifacts

logger = logging.getLogger(__name__)

__all__ = ["run_review_pipeline"]


def run_review_pipeline(
    artifacts: ReviewArtifacts,
    budget: CycleBudget,
    *,
    perspectives: Optional[List[ReviewPerspective]] = None,
    agent_type: str = "coco",
    model_name: Optional[str] = None,
    max_parallel: Optional[int] = None,
    min_per_worker_s: float = 15.0,
) -> List[PerspectiveOutcome]:
    """Assemble and run the high-performance parallel review pipeline.

    1. Checks the L1 Lint Gate (syntactic fast-fail).
    2. Spawns N ephemeral sessions (one per perspective) in parallel threads.
    3. Runs workers under a CycleBudget wall-clock cap.
    """
    # 1. Lint Gate (Step 6)
    decision = evaluate_lint_gate(artifacts)
    if decision.should_short_circuit:
        logger.info("[ReviewPipeline] lint gate short-circuit: %s", decision.summary)
        return build_lint_gate_outcomes(decision, perspectives=perspectives)

    # 2. Prepare bindings (Step 4)
    if not perspectives:
        perspectives = list(ReviewPerspective)

    from ..config import get_settings
    settings = get_settings()
    if max_parallel is None:
        max_parallel = int(getattr(settings, "spec_review_max_parallel", 2) or 2)
    max_parallel = max(1, max_parallel)

    bindings: List[WorkerBinding] = []
    cwd = artifacts.cwd or "."

    # To achieve true parallelism, we need N independent sessions.
    # Each binding gets its own EphemeralReviewSession via a closure.
    def _create_ephemeral_runner(p: ReviewPerspective) -> Callable[[str, Callable, float], str]:
        def runner(prompt: str, on_event: Callable, timeout: float) -> str:
            # Step 3: ephemeral session factory
            with EphemeralReviewSession(agent_type, cwd, model_name) as session:
                res = session.send_prompt(prompt, on_event=on_event, timeout=timeout)
                return res.text

        return runner

    for p in perspectives:
        # Each worker gets a slice of the remaining budget.
        # run_workers_parallel uses this as the aggregate wait timeout.
        worker = PerspectiveWorker(p, timeout=budget.remaining())
        binding = WorkerBinding(
            worker=worker,
            prompt_runner=_create_ephemeral_runner(p),
            label=f"review-{p.value}",
        )
        bindings.append(binding)

    # 3. Run with budget (Step 5)
    logger.info(
        "[ReviewPipeline] starting parallel review: %d perspectives, agent=%s, budget=%.1fs",
        len(bindings),
        agent_type,
        budget.remaining(),
    )

    outcomes = run_with_budget(
        bindings,
        artifacts,
        budget,
        max_workers=max_parallel,
        min_per_worker_s=min_per_worker_s,
    )

    # 4. Inject lint hints if any (style issues that didn't block)
    if decision.lint_result and decision.lint_result.issues:
        _inject_lint_hints(outcomes, decision)

    return outcomes


def _inject_lint_hints(outcomes: List[PerspectiveOutcome], decision: LintGateDecision):
    """Inject lint findings as suggestions to all outcomes."""
    if not outcomes or not decision.lint_result:
        return

    hint = f"\n[lint-gate hint] {decision.summary}"
    for o in outcomes:
        # Only inject if it's a real review (not already a synthetic failure from the pipeline itself)
        # though build_lint_gate_outcomes returns early so we only get here if decision didn't short-circuit.
        if o.review.suggestions is None:
            o.review.suggestions = []
        o.review.suggestions.append(hint)
