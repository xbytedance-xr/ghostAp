from unittest.mock import patch


def test_review_futures_unfinished_cleaned(monkeypatch):
    from src.engine_base import ReviewPerspective
    from src.spec_engine.perspective_worker import PerspectiveOutcome, PerspectiveReview
    from src.spec_engine.review import ReviewCircuitState, _conduct_review_pipeline
    from src.spec_engine.review_artifacts import ReviewArtifacts

    circuit = ReviewCircuitState()

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
        spec_review_min_timeout = 5
        spec_review_hard_floor = 3
        spec_review_max_parallel = 2
        spec_review_retry_max_delay = 1
        spec_review_retry_max_attempts = 1

    artifacts = ReviewArtifacts(
        cycle_number=1,
        requirement="",
        cwd="."
    )

    def diag_fn(*args, **kwargs):
        return {}

    with patch("src.spec_engine.review_retry.time.sleep", return_value=None):
        _conduct_review_pipeline(
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
    # After retry exhaustion, err_type should be the retry_exhausted message
    from src.card.ui_text import UI_TEXT
    from src.utils.text import format_friendly_duration
    # elapsed_sec = spec_review_timeout + spec_review_retry_max_delay * attempts = 10 + 1*1 = 11
    assert diag.get("err_type", "") == UI_TEXT["retry_exhausted"].format(n=1, elapsed_friendly=format_friendly_duration(11))
