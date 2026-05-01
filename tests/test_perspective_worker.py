"""Tests for Step 4 — PerspectiveWorker + run_workers_parallel."""

from __future__ import annotations

import threading
import time

import pytest

from src.engine_base import ReviewPerspective
from src.spec_engine.perspective_worker import (
    PerspectiveWorker,
    WorkerBinding,
    run_workers_parallel,
)
from src.spec_engine.review_artifacts import ReviewArtifacts


def _make_artifacts(cycle: int = 1) -> ReviewArtifacts:
    return ReviewArtifacts(
        cycle_number=cycle,
        requirement="实现 X",
        cwd="/tmp",
        spec_output="spec summary",
        plan_output="plan summary",
        build_output="build summary",
        diff_patch="diff --git a/x b/x\n+added line\n",
        touched_files=["src/x.py"],
    )


def _runner_returning(text: str):
    def runner(prompt: str, on_event, timeout: float) -> str:
        return text

    return runner


def _runner_raising(msg: str = "boom"):
    def runner(prompt: str, on_event, timeout: float) -> str:
        raise RuntimeError(msg)

    return runner


def _runner_slow(text: str, delay: float):
    def runner(prompt: str, on_event, timeout: float) -> str:
        time.sleep(delay)
        return text

    return runner


def test_worker_parses_pass_block():
    worker = PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=10.0)
    raw = "[ARCHITECT]\nPASS\n"
    out = worker.run(_make_artifacts(), _runner_returning(raw))
    assert out.ok
    assert out.review.passed is True
    assert out.review.perspective == ReviewPerspective.ARCHITECT
    assert out.review.suggestions == []


def test_worker_parses_fail_block_with_suggestions():
    worker = PerspectiveWorker(ReviewPerspective.PRODUCT, timeout=10.0)
    raw = "[PRODUCT]\nFAIL\n- 文案不清晰\n- 边界未处理\n"
    out = worker.run(_make_artifacts(), _runner_returning(raw))
    assert out.ok
    assert out.review.passed is False
    assert len(out.review.suggestions) == 2
    assert "文案不清晰" in out.review.suggestions[0]


def test_worker_unparseable_output_falls_back_to_fail():
    worker = PerspectiveWorker(ReviewPerspective.TESTER, timeout=10.0)
    out = worker.run(_make_artifacts(), _runner_returning("完全无结构的文字"))
    assert out.ok is True  # runner succeeded
    assert out.review.passed is False  # parse failure defaults to fail (security-safe)


def test_worker_runner_exception_yields_synthetic_fail():
    worker = PerspectiveWorker(ReviewPerspective.USER, timeout=10.0)
    out = worker.run(_make_artifacts(), _runner_raising("timeout"))
    assert out.ok is False
    assert out.error and "timeout" in out.error
    assert out.review.passed is False


def test_worker_ignores_other_perspective_blocks():
    worker = PerspectiveWorker(ReviewPerspective.DESIGNER, timeout=10.0)
    raw = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nFAIL\n- x\n"  # no DESIGNER block
    out = worker.run(_make_artifacts(), _runner_returning(raw))
    assert out.review.perspective == ReviewPerspective.DESIGNER
    # Text contains PASS verdict from ARCHITECT block — loose parser picks it up
    assert out.review.passed is True


def test_run_workers_parallel_all_succeed():
    bindings = [
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=5.0),
            _runner_returning("[ARCHITECT]\nPASS\n"),
        ),
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.PRODUCT, timeout=5.0),
            _runner_returning("[PRODUCT]\nFAIL\n- x\n"),
        ),
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.USER, timeout=5.0),
            _runner_returning("[USER]\nPASS\n"),
        ),
    ]
    outs = run_workers_parallel(bindings, _make_artifacts())
    assert len(outs) == 3
    # Stable ordering by enum order: ARCHITECT < PRODUCT < USER
    assert [o.perspective for o in outs] == [
        ReviewPerspective.ARCHITECT,
        ReviewPerspective.PRODUCT,
        ReviewPerspective.USER,
    ]
    assert outs[0].review.passed is True
    assert outs[1].review.passed is False
    assert all(o.ok for o in outs)


def test_run_workers_parallel_isolates_failures():
    bindings = [
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=5.0),
            _runner_returning("[ARCHITECT]\nPASS\n"),
        ),
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.PRODUCT, timeout=5.0),
            _runner_raising("network error"),
        ),
    ]
    outs = run_workers_parallel(bindings, _make_artifacts())
    assert len(outs) == 2
    by_p = {o.perspective: o for o in outs}
    assert by_p[ReviewPerspective.ARCHITECT].ok
    assert not by_p[ReviewPerspective.PRODUCT].ok
    assert "network error" in by_p[ReviewPerspective.PRODUCT].error


def test_run_workers_parallel_empty_returns_empty():
    assert run_workers_parallel([], _make_artifacts()) == []


def test_run_workers_parallel_actually_concurrent():
    """With 3 workers each sleeping 0.05s, parallel elapsed must be < serial (0.15s)."""
    delay = 0.05
    bindings = [
        WorkerBinding(
            PerspectiveWorker(p, timeout=5.0),
            _runner_slow(f"[{p.name}]\nPASS\n", delay),
        )
        for p in [ReviewPerspective.ARCHITECT, ReviewPerspective.PRODUCT, ReviewPerspective.USER]
    ]
    t0 = time.monotonic()
    outs = run_workers_parallel(bindings, _make_artifacts(), max_workers=3)
    elapsed = time.monotonic() - t0
    assert len(outs) == 3
    assert all(o.ok for o in outs)
    # Serial would be ~0.15s; parallel should be ~0.05-0.1s. Leave slack for CI.
    assert elapsed < 0.12, f"workers did not run in parallel (elapsed={elapsed:.2f}s)"

def test_run_workers_parallel_race_condition():
    """Tests that no results are lost when futures complete at the exact moment of a timeout."""

    # We want one worker to be fast and complete normally
    # We want another worker to be slow, but finish RIGHT when the timeout happens
    # We want a third to block indefinitely (cancelled by timeout)

    _blocked = threading.Event()

    def _runner_fast(prompt: str, on_event, timeout: float) -> str:
        return "[ARCHITECT]\nPASS\n"

    def _runner_exact_timeout(prompt: str, on_event, timeout: float) -> str:
        time.sleep(0.08)  # Matches per_worker_timeout closely
        return "[PRODUCT]\nPASS\n"

    def _runner_very_slow(prompt: str, on_event, timeout: float) -> str:
        _blocked.wait(timeout=0.5)  # Blocks until cancelled by per_worker_timeout
        return "[USER]\nPASS\n"

    bindings = [
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.ARCHITECT, timeout=5.0),
            _runner_fast,
        ),
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.PRODUCT, timeout=5.0),
            _runner_exact_timeout,
        ),
        WorkerBinding(
            PerspectiveWorker(ReviewPerspective.USER, timeout=5.0),
            _runner_very_slow,
        )
    ]

    # Run with a 0.1s timeout.
    outs = run_workers_parallel(bindings, _make_artifacts(), per_worker_timeout=0.1)

    assert len(outs) == 3

    # Fast one must succeed
    assert outs[0].perspective == ReviewPerspective.ARCHITECT
    assert outs[0].ok is True

    # At least one must fail due to timeout (the very slow one)
    assert any(not o.ok and o.perspective == ReviewPerspective.USER for o in outs)
    _blocked.set()  # Unblock any lingering threads


def test_run_workers_parallel_timeout():
    _blocked = threading.Event()

    def slow_runner(prompt, on_event, timeout):
        _blocked.wait(timeout=0.5)  # Blocks until per_worker_timeout cancels it
        return "slow"

    b1 = WorkerBinding(
        worker=PerspectiveWorker(perspective=ReviewPerspective.ARCHITECT, timeout=0.1),
        prompt_runner=slow_runner
    )

    outcomes = run_workers_parallel([b1], _make_artifacts(), max_workers=1, per_worker_timeout=0.1)

    assert len(outcomes) == 1
    assert not outcomes[0].review.passed
    from src.card.styles import UI_TEXT
    expected_err = UI_TEXT["retry_no_retry"]
    assert outcomes[0].error == expected_err
    assert len(outcomes[0].review.suggestions) == 1
    assert outcomes[0].review.suggestions[0].startswith(f"审查异常：{expected_err}（视角=")
    _blocked.set()  # Unblock any lingering threads

