"""Verify that SpecEngine no longer calls gc.collect() inline.

gc.collect() is now delegated to GCMonitor and should not appear in
spec_engine cleanup paths.
"""

from unittest.mock import MagicMock, patch

from src.spec_engine.engine import SpecEngine, SpecEngineCallbacks


@patch("gc.collect")
def test_spec_engine_no_inline_gc_collect(mock_gc_collect):
    """SpecEngine.execute should NOT call gc.collect() directly."""
    fast_settings = MagicMock()
    fast_settings.spec_max_cycles = 3
    fast_settings.spec_max_cycles_limit = 3
    fast_settings.spec_execution_timeout = 30
    fast_settings.spec_convergence_window = 0
    fast_settings.spec_min_cycles = 1
    fast_settings.spec_review_enabled = False
    fast_settings.spec_discovery_enabled = False
    fast_settings.spec_disable_convergence = False
    fast_settings.spec_disable_early_stop = False
    fast_settings.spec_infinite_mode = False
    fast_settings.spec_persist_phase_artifacts = False
    fast_settings.spec_allow_resume_from_disk = False
    fast_settings.spec_rebuild_session_between_cycles = False

    with patch("src.engine_base.get_settings", return_value=fast_settings):
        engine = SpecEngine(
            chat_id="test",
            root_path="/tmp",
            agent_type="coco",
        )

    engine._run_phase = MagicMock(return_value="dummy_prompt")
    engine._close_session_safely = MagicMock()
    engine._parse_requirement_to_criteria = MagicMock(return_value=["test criteria"])
    engine._evaluate_criteria = MagicMock(return_value=True)
    engine._conduct_review = MagicMock()

    for attr, val in (
        ("spec_max_cycles", 3),
        ("spec_max_cycles_limit", 3),
        ("spec_execution_timeout", 30),
        ("spec_convergence_window", 0),
        ("spec_min_cycles", 1),
        ("spec_review_enabled", False),
        ("spec_discovery_enabled", False),
    ):
        try:
            setattr(engine.settings, attr, val)
        except Exception:
            pass

    with (
        patch("src.spec_engine.engine.create_engine_session") as mock_create,
        patch("src.spec_engine.session_utils.get_coco_model_manager") as mock_model_manager,
    ):
        mock_create.return_value = MagicMock()
        mock_model_manager.return_value = MagicMock()

        callbacks = SpecEngineCallbacks()
        try:
            engine.execute("test req", callbacks)
        except Exception:
            pass

    # gc.collect() should NOT be called inline by SpecEngine anymore
    mock_gc_collect.assert_not_called()
