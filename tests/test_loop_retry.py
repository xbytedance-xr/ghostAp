from unittest.mock import MagicMock, patch

from src.loop_engine.engine import LoopEngine, LoopEngineCallbacks
from src.loop_engine.models import LoopRequirement


@patch("src.loop_engine.engine.create_engine_session")
@patch("src.loop_engine.engine.LoopEngine.save_state")
def test_loop_engine_retry_logic(mock_save_state, mock_create_session, tmp_path):
    engine = LoopEngine(chat_id="test_chat", root_path=str(tmp_path), agent_type="coco")

    mock_session = MagicMock()
    mock_create_session.return_value = mock_session

    # We will simulate the send_prompt failing once with a retryable error,
    # then succeeding.
    call_count = 0

    def side_effect_send_prompt(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("timeout error")  # 'timeout' is in RETRYABLE_ERROR_PATTERNS

        result = MagicMock()
        result.stop_reason = "end_turn"
        return result

    mock_session.send_prompt.side_effect = side_effect_send_prompt

    # Mock other methods to avoid side effects
    engine._parse_requirement = MagicMock(return_value=LoopRequirement(goal="test", acceptance_criteria=["c1"]))
    engine._conduct_review = MagicMock()
    engine._evaluate_criteria = MagicMock(return_value={"all_satisfied": True})
    engine.settings.loop_execution_timeout = 1
    engine.settings.loop_max_iterations = 1
    engine.settings.loop_review_enabled = False

    callbacks = LoopEngineCallbacks()

    # To avoid time.sleep slowing down tests
    with patch("time.sleep", return_value=None):
        project = engine.execute("test req", callbacks)

    assert call_count == 2
    assert project.status.value == "completed"
    assert mock_save_state.called
