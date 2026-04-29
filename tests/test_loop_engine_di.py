"""Tests for LoopEngine Dependency Injection (DI) mechanism."""

from unittest.mock import MagicMock, patch
import pytest

from src.loop_engine.engine import LoopEngine
from src.loop_engine.models import LoopProject, LoopProjectStatus
from src.utils.retry import RetryPolicy
from src.engine_base import EngineRunState

def test_loop_engine_di_injected_dependencies_used():
    """Verify that injected dependencies are stored and used by LoopEngine."""
    mock_retry_policy = RetryPolicy(max_retries=99, retry_delay=0.1)
    
    mock_session = MagicMock()
    mock_create_session_fn = MagicMock(return_value=mock_session)
    
    engine = LoopEngine(
        chat_id="test_chat",
        root_path="/tmp/test_root",
        retry_policy=mock_retry_policy,
        create_session_fn=mock_create_session_fn,
    )
    
    # Verify the injected instances are stored
    assert engine._retry_policy is mock_retry_policy
    assert engine._create_session_fn is mock_create_session_fn
    
    # Verify create_session_fn is used
    # Mock evaluate_criteria to avoid LLM calls
    with patch.object(LoopEngine, '_parse_requirement', return_value=MagicMock()):
        engine._run_state = EngineRunState.STOPPING
        try:
            engine.execute("requirement")
        except Exception:
            pass
            
        mock_create_session_fn.assert_called()
