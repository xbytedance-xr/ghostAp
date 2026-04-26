def test_review_futures_unfinished_cleaned(monkeypatch):
    from src.spec_engine.review import _conduct_review_pipeline, ReviewCircuitState
    from src.spec_engine.review_artifacts import ReviewArtifacts
    from src.spec_engine.perspective_worker import PerspectiveOutcome, PerspectiveReview
    from src.engine_base import ReviewPerspective

    class MockCircuit:
        def __init__(self):
            self.last_review_failure_diag = None
            self.review_failure_consecutive = 0
            self.review_circuit_open_until_cycle = 0
            self.backoff_level = 0
            self.consecutive_timeouts = 0
            self.consecutive_skips = 0
            self.recent_outcomes = []

    circuit = MockCircuit()
    
    def fake_run(*args, **kwargs):
        # returns an outcome containing "futures unfinished" which might come from deep inside TimeoutError
        from src.spec_engine.perspective_worker import ReviewErrorCode
        return [
            PerspectiveOutcome(
                perspective=ReviewPerspective.ARCHITECT,
                review=PerspectiveReview(perspective=ReviewPerspective.ARCHITECT, passed=False, suggestions=[]),
                error="操作超时 (1 (of 5) futures unfinished)",
                error_code=ReviewErrorCode.TIMEOUT
            )
        ]
    
    import src.spec_engine.review_pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "run_review_pipeline", fake_run)
    
    class Settings:
        spec_review_timeout = 10
        spec_review_max_parallel = 2
        
    artifacts = ReviewArtifacts(
        cycle_number=1,
        requirement="",
        cwd="."
    )
    
    def diag_fn(*args, **kwargs):
        return {}
        
    res = _conduct_review_pipeline(
        artifacts=artifacts,
        settings=Settings(),
        circuit=circuit,
        cycle=1,
        agent_type="coco",
        model_name="none",
        build_review_exception_diagnostics_fn=diag_fn,
        on_review_done=None
    )
    
    diag = circuit.last_review_failure_diag
    assert diag is not None
    assert "futures unfinished" not in diag.get("err_type", "")
    assert "个视角未完成" not in diag.get("err_type", "")
    assert diag.get("err_type", "") == "当前系统较繁忙，操作已超时。建议：稍后自动重试，或通过 /spec resume 手动恢复"

