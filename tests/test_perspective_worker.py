"""Tests for Step 4 — PerspectiveWorker + run_workers_parallel."""

from __future__ import annotations

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
    assert out.review.passed is False
    assert "解析失败" in out.review.summary


def test_worker_runner_exception_yields_synthetic_fail():
    worker = PerspectiveWorker(ReviewPerspective.USER, timeout=10.0)
    out = worker.run(_make_artifacts(), _runner_raising("timeout"))
    assert out.ok is False
    assert out.error and "timeout" in out.error
    assert out.review.passed is False
    assert out.review.summary == "异常"


def test_worker_ignores_other_perspective_blocks():
    worker = PerspectiveWorker(ReviewPerspective.DESIGNER, timeout=10.0)
    raw = "[ARCHITECT]\nPASS\n\n[PRODUCT]\nFAIL\n- x\n"  # no DESIGNER block
    out = worker.run(_make_artifacts(), _runner_returning(raw))
    assert out.review.perspective == ReviewPerspective.DESIGNER
    assert out.review.passed is False  # synthetic parse-failure FAIL
    assert "解析失败" in out.review.summary


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
    """With 3 workers each sleeping 0.2s, parallel elapsed must be < serial (0.6s)."""
    delay = 0.2
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
    # Serial would be ~0.6s; parallel should be ~0.2-0.35s. Leave slack for CI.
    assert elapsed < 0.5, f"workers did not run in parallel (elapsed={elapsed:.2f}s)"
