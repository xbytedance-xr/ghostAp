import pytest
from unittest.mock import MagicMock, patch
import time
from src.spec_engine.engine import SpecEngine
from src.loop_engine.engine import LoopEngine
from src.deep_engine.engine import DeepEngine
from src.utils.trace import TraceContext

class TestTraceIntegration:
    @patch("src.spec_engine.engine.TraceContext")
    @patch("src.spec_engine.engine.get_settings")
    @patch("src.spec_engine.engine.create_engine_session")
    def test_spec_engine_trace_integration(self, mock_create_session, mock_get_settings, mock_trace_context):
        # Setup
        mock_settings = MagicMock()
        mock_settings.spec_max_cycles = 1
        mock_settings.spec_execution_timeout = 1
        mock_settings.spec_review_enabled = False
        mock_settings.spec_persist_every_phase = False
        mock_settings.spec_discovery_enabled = False
        mock_settings.spec_max_cycles_limit = 5000  # Fix: Ensure this is an int
        mock_settings.ark_api_key = "test"
        mock_settings.ark_model = "test"
        mock_settings.ark_base_url = "test"
        mock_get_settings.return_value = mock_settings
        
        mock_session = MagicMock()
        mock_create_session.return_value = mock_session
        
        mock_trace_ctx_instance = MagicMock()
        mock_trace_context.return_value = mock_trace_ctx_instance
        
        engine = SpecEngine(chat_id="test_chat", root_path="/tmp/test_spec")
        
        # Patch internal methods to avoid actual execution logic
        with patch.object(engine, '_run_cycle_loop', return_value="success"):
            # Execute
            engine.execute(requirement_text="test requirement", task_id="test_task_id")
            
        # Verify TraceContext initialization
        mock_trace_context.assert_called_once()
        call_args = mock_trace_context.call_args
        assert call_args.kwargs.get('request_id') == "test_task_id"
        
        # Verify TraceContext usage as context manager
        mock_trace_ctx_instance.__enter__.assert_called_once()
        mock_trace_ctx_instance.__exit__.assert_called_once()

    @patch("src.loop_engine.engine.TraceContext")
    @patch("src.engine_base.get_settings")
    @patch("src.loop_engine.engine.create_engine_session")
    def test_loop_engine_trace_integration(self, mock_create_session, mock_get_settings, mock_trace_context):
        # Setup
        mock_settings = MagicMock()
        mock_settings.loop_max_iterations = 1
        mock_settings.loop_execution_timeout = 1
        mock_settings.loop_watchdog_timeout = 1
        mock_get_settings.return_value = mock_settings
        
        # Mock session to raise exception to stop execution immediately after entering context
        mock_create_session.side_effect = RuntimeError("Stop execution for test")
        
        mock_trace_ctx_instance = MagicMock()
        mock_trace_context.return_value = mock_trace_ctx_instance
        
        engine = LoopEngine(chat_id="test_chat", root_path="/tmp/test_loop")
        
        # Execute
        # We expect RuntimeError because we force it, but TraceContext should still be used
        # Note: LoopEngine catches Exception and returns project with ABORTED status
        with patch.object(engine, '_parse_requirement', return_value=MagicMock()):
             engine.execute(requirement_text="test requirement", task_id="test_task_id")
            
        # Verify TraceContext initialization
        mock_trace_context.assert_called_once()
        call_args = mock_trace_context.call_args
        assert call_args.kwargs.get('trace_id') == "test_task_id"
        
        # Verify TraceContext usage as context manager
        mock_trace_ctx_instance.__enter__.assert_called_once()
        mock_trace_ctx_instance.__exit__.assert_called_once()

    @patch("src.deep_engine.engine.TraceContext")
    @patch("src.engine_base.get_settings")
    @patch("src.deep_engine.engine.create_engine_session")
    def test_deep_engine_trace_integration(self, mock_create_session, mock_get_settings, mock_trace_context):
        # Setup
        mock_settings = MagicMock()
        mock_settings.deep_memory_threshold = 80.0
        mock_get_settings.return_value = mock_settings
        
        # Mock session to raise exception to stop execution immediately after entering context
        mock_create_session.side_effect = RuntimeError("Stop execution for test")
        
        mock_trace_ctx_instance = MagicMock()
        mock_trace_context.return_value = mock_trace_ctx_instance
        
        engine = DeepEngine(chat_id="test_chat", root_path="/tmp/test_deep")
        
        # Execute
        # DeepEngine catches Exception and fails the project
        engine.plan_and_execute(requirement_text="test requirement", task_id="test_task_id")
            
        # Verify TraceContext initialization
        mock_trace_context.assert_called_once()
        call_args = mock_trace_context.call_args
        # DeepEngine uses trace_id
        trace_id = call_args.kwargs.get('trace_id') or call_args.kwargs.get('request_id')
        assert trace_id == "test_task_id"
        
        # Verify TraceContext usage as context manager
        mock_trace_ctx_instance.__enter__.assert_called_once()
        mock_trace_ctx_instance.__exit__.assert_called_once()
