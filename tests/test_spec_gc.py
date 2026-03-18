from unittest.mock import MagicMock, patch

from src.spec_engine.engine import SpecEngine, SpecEngineCallbacks


@patch("gc.collect")
def test_spec_engine_gc_collect(mock_gc_collect):
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

    with (
        patch("src.spec_engine.engine.create_engine_session") as mock_create,
        patch("src.spec_engine.engine.get_coco_model_manager") as mock_model_manager,
    ):
        mock_create.return_value = MagicMock()
        mock_model_manager.return_value = MagicMock()

        callbacks = SpecEngineCallbacks()
        try:
            engine.execute("test req", callbacks)
        except Exception:
            pass

    mock_gc_collect.assert_called()
