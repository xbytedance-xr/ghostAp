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
    assert outcomes[0].error == "当前系统较繁忙，操作已超时（1/1 个视角未完成）"
    assert outcomes[0].review.suggestions == ["审查异常：当前系统较繁忙，操作已超时（1/1 个视角未完成）"]
    assert "futures unfinished" not in outcomes[0].error
    assert "futures unfinished" not in outcomes[0].review.suggestions[0]

