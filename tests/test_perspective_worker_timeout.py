from concurrent.futures import TimeoutError
import logging

from src.card.styles import UI_TEXT

_RETRY_NO_RETRY = UI_TEXT["retry_no_retry"]


def test_run_workers_parallel_timeout():
    from src.spec_engine.perspective_worker import run_workers_parallel, PerspectiveWorker, ReviewArtifacts, WorkerBinding
    from src.engine_base import ReviewPerspective
    
    def slow_runner(prompt, on_event, timeout):
        import time
        time.sleep(0.5)
        return "slow"
        
    artifacts = ReviewArtifacts(
        cycle_number=1,
        cwd=".",
        requirement="test req",
        diff_patch="test patch",
        touched_files=["test.py"],
        spec_output="test spec",
        plan_output="test plan",
        build_output="test build"
    )
    
    b1 = WorkerBinding(
        worker=PerspectiveWorker(perspective=ReviewPerspective.ARCHITECT, timeout=0.1),
        prompt_runner=slow_runner
    )
    
    outcomes = run_workers_parallel([b1], artifacts, max_workers=1, per_worker_timeout=0.1)
    
    assert len(outcomes) == 1
    assert not outcomes[0].review.passed
    assert outcomes[0].error == _RETRY_NO_RETRY
    assert len(outcomes[0].review.suggestions) == 1
    assert outcomes[0].review.suggestions[0].startswith(f"审查异常：{_RETRY_NO_RETRY}（视角=")
    assert "futures unfinished" not in outcomes[0].error
    assert "futures unfinished" not in outcomes[0].review.suggestions[0]


def test_worker_run_timeout_with_futures_unfinished_message():
    """PerspectiveWorker.run() sanitizes TimeoutError('N (of M) futures unfinished') in suggestions."""
    from src.spec_engine.perspective_worker import PerspectiveWorker, ReviewErrorCode
    from src.spec_engine.review_artifacts import ReviewArtifacts
    from src.engine_base import ReviewPerspective

    def raising_runner(prompt, on_event, timeout):
        raise TimeoutError("1 (of 5) futures unfinished")

    artifacts = ReviewArtifacts(
        cycle_number=1,
        cwd=".",
        requirement="test req",
        diff_patch="test patch",
        touched_files=["test.py"],
        spec_output="test spec",
        plan_output="test plan",
        build_output="test build",
    )

    worker = PerspectiveWorker(perspective=ReviewPerspective.ARCHITECT, timeout=10)
    outcome = worker.run(artifacts, raising_runner)

    assert not outcome.ok
    assert outcome.error_code == ReviewErrorCode.TIMEOUT
    assert "futures unfinished" not in outcome.error
    assert outcome.error == _RETRY_NO_RETRY
    for s in outcome.review.suggestions:
        assert "futures unfinished" not in s


def test_run_workers_parallel_inner_worker_timeout_sanitized():
    """run_workers_parallel inner except: worker raising TimeoutError produces clean suggestions."""
    from src.spec_engine.perspective_worker import run_workers_parallel, PerspectiveWorker, WorkerBinding
    from src.spec_engine.review_artifacts import ReviewArtifacts
    from src.engine_base import ReviewPerspective

    def raising_runner(prompt, on_event, timeout):
        raise TimeoutError("2 (of 5) futures unfinished")

    artifacts = ReviewArtifacts(
        cycle_number=1,
        cwd=".",
        requirement="test req",
        diff_patch="test patch",
        touched_files=["test.py"],
        spec_output="test spec",
        plan_output="test plan",
        build_output="test build",
    )

    b1 = WorkerBinding(
        worker=PerspectiveWorker(perspective=ReviewPerspective.ARCHITECT, timeout=10),
        prompt_runner=raising_runner,
    )

    # Use a long per_worker_timeout so the outer TimeoutError is NOT hit;
    # the worker itself raises inside fut.result() → inner except path.
    outcomes = run_workers_parallel([b1], artifacts, max_workers=1, per_worker_timeout=30)

    assert len(outcomes) == 1
    assert not outcomes[0].review.passed
    assert "futures unfinished" not in outcomes[0].error
    assert outcomes[0].error == _RETRY_NO_RETRY
    for s in outcomes[0].review.suggestions:
        assert "futures unfinished" not in s


def test_worker_timeout_log_contains_structured_fields(caplog):
    """PerspectiveWorker.run() timeout log must include elapsed_ms and configured_timeout."""
    from src.spec_engine.perspective_worker import PerspectiveWorker, ReviewErrorCode
    from src.spec_engine.review_artifacts import ReviewArtifacts
    from src.engine_base import ReviewPerspective

    def raising_runner(prompt, on_event, timeout):
        raise TimeoutError("test timeout")

    artifacts = ReviewArtifacts(
        cycle_number=1,
        cwd=".",
        requirement="test req",
        diff_patch="test patch",
        touched_files=["test.py"],
        spec_output="test spec",
        plan_output="test plan",
        build_output="test build",
    )

    worker = PerspectiveWorker(perspective=ReviewPerspective.ARCHITECT, timeout=42)
    with caplog.at_level(logging.WARNING, logger="src.spec_engine.perspective_worker"):
        outcome = worker.run(artifacts, raising_runner)

    assert outcome.error_code == ReviewErrorCode.TIMEOUT
    # Find the log line with our structured fields
    matched = [r for r in caplog.records if "PerspectiveWorker" in r.message and "elapsed_ms=" in r.message]
    assert len(matched) >= 1
    log_msg = matched[0].message
    assert "elapsed_ms=" in log_msg
    assert "configured_timeout=42" in log_msg


def test_run_workers_parallel_timeout_summary_log(caplog):
    """run_workers_parallel outer TimeoutError path must produce a summary log with unfinished count."""
    from src.spec_engine.perspective_worker import run_workers_parallel, PerspectiveWorker, WorkerBinding
    from src.spec_engine.review_artifacts import ReviewArtifacts
    from src.engine_base import ReviewPerspective
    import time

    def slow_runner(prompt, on_event, timeout):
        time.sleep(1.0)
        return "slow"

    artifacts = ReviewArtifacts(
        cycle_number=1,
        cwd=".",
        requirement="test req",
        diff_patch="test patch",
        touched_files=["test.py"],
        spec_output="test spec",
        plan_output="test plan",
        build_output="test build",
    )

    b1 = WorkerBinding(
        worker=PerspectiveWorker(perspective=ReviewPerspective.ARCHITECT, timeout=0.05),
        prompt_runner=slow_runner,
    )

    with caplog.at_level(logging.WARNING, logger="src.spec_engine.perspective_worker"):
        outcomes = run_workers_parallel([b1], artifacts, max_workers=1, per_worker_timeout=0.05)

    assert len(outcomes) >= 1
    # Check for the summary log
    summary_logs = [r for r in caplog.records if "pool timeout summary" in r.message]
    assert len(summary_logs) >= 1
    summary_msg = summary_logs[0].message
    assert "worker(s) unfinished" in summary_msg
    assert "per_worker_timeout=" in summary_msg

