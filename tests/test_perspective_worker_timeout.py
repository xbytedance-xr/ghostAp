from concurrent.futures import TimeoutError

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
    assert outcomes[0].error == "当前系统较繁忙，操作已超时"
    assert outcomes[0].review.suggestions == ["审查异常：当前系统较繁忙，操作已超时"]
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
    assert outcome.error == "当前系统较繁忙，操作已超时"
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
    assert outcomes[0].error == "当前系统较繁忙，操作已超时"
    for s in outcomes[0].review.suggestions:
        assert "futures unfinished" not in s

