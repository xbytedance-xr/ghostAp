"""Tests for Step 5 — CycleBudget + run_with_budget."""

from __future__ import annotations

import time

from src.engine_base import ReviewPerspective
from src.spec_engine.cycle_budget import CycleBudget, run_with_budget
from src.spec_engine.perspective_worker import PerspectiveWorker, WorkerBinding
from src.spec_engine.review_artifacts import ReviewArtifacts


def _artifacts() -> ReviewArtifacts:
    return ReviewArtifacts(
        cycle_number=1,
        requirement="X",
        cwd="/tmp",
    )


def _runner(text: str, delay: float = 0.0):
    def r(prompt, on_event, timeout):
        if delay:
            time.sleep(delay)
        return text

    return r


def test_budget_start_idempotent_and_elapsed():
    b = CycleBudget(total_seconds=10.0)
    assert not b.started
    assert b.elapsed() == 0.0
    b.start()
    t1 = b.started_at
    assert b.started
    time.sleep(0.05)
    assert b.elapsed() > 0
    b.start()  # idempotent
    assert b.started_at == t1


def test_budget_remaining_and_exceeded():
    b = CycleBudget(total_seconds=0.1)
    b.start()
    time.sleep(0.15)
    assert b.exceeded()
    assert b.remaining() == 0.0


def test_budget_unlimited_never_exceeds():
    b = CycleBudget(total_seconds=0)
    b.start()
    assert b.unlimited
    assert not b.exceeded()
    assert b.remaining() == float("inf")


def test_budget_snapshot_shape():
    b = CycleBudget(total_seconds=5.0, label="test")
    b.start()
    snap = b.snapshot()
    assert snap["label"] == "test"
    assert snap["total_seconds"] == 5.0
    assert snap["started"] is True
    assert snap["exceeded"] is False


def test_run_with_budget_fast_workers_succeed():
    bindings = [
        WorkerBinding(
            PerspectiveWorker(p, timeout=2.0),
            _runner(f"[{p.name}]\nPASS\n"),
        )
        for p in [ReviewPerspective.ARCHITECT, ReviewPerspective.PRODUCT]
    ]
    budget = CycleBudget(total_seconds=10.0)
    outs = run_with_budget(bindings, _artifacts(), budget)
    assert len(outs) == 2
    assert all(o.ok and o.review.passed for o in outs)


def test_run_with_budget_already_exceeded_skips_all():
    bindings = [
        WorkerBinding(
            PerspectiveWorker(p, timeout=2.0),
            _runner(f"[{p.name}]\nPASS\n"),
        )
        for p in [ReviewPerspective.ARCHITECT, ReviewPerspective.PRODUCT]
    ]
    budget = CycleBudget(total_seconds=0.05)
    budget.start()
    time.sleep(0.1)  # exceed before calling run_with_budget
    outs = run_with_budget(bindings, _artifacts(), budget)
    assert len(outs) == 2
    assert all(o.error == "cycle_budget_exceeded" for o in outs)
    assert all(o.review.summary == "预算超时" for o in outs)


def test_run_with_budget_min_per_worker_guard():
    bindings = [
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=2.0),
            _runner("[ARCHITECT]\nPASS\n"),
        )
    ]
    budget = CycleBudget(total_seconds=0.5)
    budget.start()
    time.sleep(0.3)  # leave ~0.2s remaining, below default 5s floor
    outs = run_with_budget(bindings, _artifacts(), budget, min_per_worker_s=5.0)
    assert len(outs) == 1
    assert outs[0].error == "cycle_budget_exceeded"


def test_run_with_budget_partial_timeout_synthesizes_fail():
    # Fast worker completes; slow worker exceeds remaining budget -> synthetic FAIL.
    bindings = [
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=2.0),
            _runner("[ARCHITECT]\nPASS\n", delay=0.1),
        ),
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.PRODUCT, timeout=10.0),
            _runner("[PRODUCT]\nPASS\n", delay=2.0),
        ),
    ]
    budget = CycleBudget(total_seconds=0.5)
    outs = run_with_budget(bindings, _artifacts(), budget, min_per_worker_s=0.1)
    assert len(outs) == 2
    by_p = {o.perspective: o for o in outs}
    assert by_p[ReviewPerspective.ARCHITECT].ok
    # PRODUCT should have been degraded (either via parallel runner timeout or safety-net)
    prod = by_p[ReviewPerspective.PRODUCT]
    assert not prod.ok or prod.review.summary == "预算超时"


def test_run_with_budget_empty_returns_empty():
    b = CycleBudget(total_seconds=1.0)
    assert run_with_budget([], _artifacts(), b) == []


def test_run_with_budget_unlimited_no_timeout_cap():
    bindings = [
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=2.0),
            _runner("[ARCHITECT]\nPASS\n", delay=0.2),
        )
    ]
    budget = CycleBudget(total_seconds=0)  # unlimited
    outs = run_with_budget(bindings, _artifacts(), budget)
    assert len(outs) == 1
    assert outs[0].ok and outs[0].review.passed
