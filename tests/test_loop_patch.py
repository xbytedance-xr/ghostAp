
import json
import pytest
from unittest.mock import MagicMock, patch, call
from src.feishu.handlers.loop import LoopHandler
from src.loop_engine.models import LoopProject, LoopProjectStatus, IterationRecord, IterationStatus, ReviewResult

class TestLoopHandlerPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.loop_reporter = MagicMock()
        ctx.loop_engine_manager = MagicMock()
        
        # Mock settings in context
        ctx.settings = MagicMock()
        ctx.settings.default_reply_mode = "thread"
        
        handler = LoopHandler(ctx)
        
        # Mock common methods to avoid side effects
        handler.reply_message = MagicMock()
        handler.send_message = MagicMock()
        handler.ensure_request_id = MagicMock(return_value="req_123")
        handler.format_ref_note = MagicMock(return_value="")
        
        return handler

    def test_loop_message_patch_success(self, handler):
        """Test that loop callbacks use patch_message when possible."""
        # Setup mock client for success
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = True
        
        # Setup reply_message to return a message_id so we have an initial ID
        handler.reply_message.return_value = "msg_initial"
        
        # Configure mock project duration
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine
        
        # Create callbacks
        callbacks = handler._create_loop_callbacks(
            message_id="msg_origin",
            chat_id="chat_123",
            project=None,
            engine_name="Coco"
        )
        
        # Mock CardBuilder
        with patch('src.feishu.renderers.loop_renderer.CardBuilder') as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            # 1. First call: Analyzing done (Should be a NEW message)
            callbacks.on_analyzing_done(mock_project)
            
            # Should have called reply_message (since it's the first message)
            handler.reply_message.assert_called()
            # And NOT patch
            client.im.v1.message.patch.assert_not_called()
            
            # Reset mocks
            handler.reply_message.reset_mock()
            client.im.v1.message.patch.reset_mock()
            
            # 2. Second call: Iteration start (Should try to PATCH the previous message "msg_initial")
            callbacks.on_iteration_start(1, 10)
            
            # Verify patch called on "msg_initial"
            args, _ = client.im.v1.message.patch.call_args
            assert args[0].message_id == "msg_initial"
            
            # Verify NO new message sent
            handler.reply_message.assert_not_called()
            handler.send_message.assert_not_called()

    def test_loop_message_patch_fail_fallback(self, handler):
        """Test fallback to new message when patch fails."""
        # Setup mock client for FAILURE
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = False
        client.im.v1.message.patch.return_value.code = 400
        
        # Setup reply_message to return IDs
        handler.reply_message.side_effect = ["msg_1", "msg_2"]

        # Configure mock project duration
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine
        
        # Create callbacks
        callbacks = handler._create_loop_callbacks(
            message_id="msg_origin",
            chat_id="chat_123",
            project=None,
            engine_name="Coco"
        )
        
        with patch('src.feishu.renderers.loop_renderer.CardBuilder') as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            # 1. First call: Analyzing done -> msg_1
            callbacks.on_analyzing_done(mock_project)
            assert handler.reply_message.call_count == 1
            
            # 2. Second call: Iteration start -> Try patch msg_1 -> Fail -> Send msg_2
            callbacks.on_iteration_start(1, 10)
            
            # Verify patch was attempted on msg_1
            patch_args, _ = client.im.v1.message.patch.call_args
            assert patch_args[0].message_id == "msg_1"
            
            # Verify fallback to reply_message (sending msg_2)
            assert handler.reply_message.call_count == 2
            
            # 3. Third call: Iteration done -> Should try patch msg_2 (the new valid ID)
            client.im.v1.message.patch.reset_mock()
            handler.reply_message.reset_mock()
            
            # Now let patch succeed for the 3rd call to prove ID updated
            client.im.v1.message.patch.return_value.success.return_value = True
            
            record = MagicMock(spec=IterationRecord)
            record.status = IterationStatus.SUCCESS
            callbacks.on_iteration_done(1, record)
            
            # Verify patch called on msg_2
            patch_args, _ = client.im.v1.message.patch.call_args
            assert patch_args[0].message_id == "msg_2"
            
            # Verify no new message
            handler.reply_message.assert_not_called()

    def test_loop_message_non_update_event(self, handler):
        """Test that some events might force a new message if designed (though current spec says reuse).
           Actually, let's verify on_project_done also reuses if possible, or maybe we want it to be new?
           According to plan: 'prioritize calling self.patch_message'.
        """
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = True
        handler.reply_message.return_value = "msg_1"
        
        callbacks = handler._create_loop_callbacks("msg_origin", "chat_1", None)
        
        with patch('src.feishu.renderers.loop_renderer.CardBuilder') as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            # 1. Send initial
            callbacks.on_analyzing_done(MagicMock())
            
            # 2. Project Done
            mock_project = MagicMock(spec=LoopProject)
            mock_project.satisfied_count = 5
            mock_project.total_criteria = 5
            
            callbacks.on_project_done(mock_project)
            
            # Verify it patched the existing message
            client.im.v1.message.patch.assert_called()
            
            # And didn't send new one
            handler.reply_message.assert_called_once() # Only the first one

