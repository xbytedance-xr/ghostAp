
import pytest
from unittest.mock import MagicMock, patch
from src.feishu.handlers.deep import DeepHandler

class TestDeepHandlerPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.progress_reporter = MagicMock()
        
        # Mock settings in context
        ctx.settings = MagicMock()
        ctx.settings.default_reply_mode = "thread"
        
        handler = DeepHandler(ctx)
        return handler

    def test_send_deep_message_patch_success(self, handler):
        # Setup: Patch returns success
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = True
        
        callbacks = handler._create_deep_callbacks(
            message_id="msg_123",
            chat_id="chat_123",
            project=None,
            initial_message_id="init_msg_id"
        )
        
        mock_project = MagicMock()
        mock_project.name = "Test Project"
        mock_project.root_path = "/tmp"
        mock_project.project_id = "proj_123"
        
        # We also need to mock CardBuilder because on_planning_done calls it
        with patch('src.feishu.handlers.deep.CardBuilder') as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            # Ensure reply_message/send_message are not called
            with patch.object(handler, 'reply_message') as mock_reply:
                with patch.object(handler, 'send_message') as mock_send:
                    callbacks.on_planning_done(mock_project)
                    
                    # Verify patch called
                    assert client.im.v1.message.patch.called
                    
                    # Verify NO fallback
                    mock_reply.assert_not_called()
                    mock_send.assert_not_called()

    def test_send_deep_message_patch_retry_then_fail(self, handler):
        # Setup: Patch returns failure
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = False
        client.im.v1.message.patch.return_value.code = 999
        client.im.v1.message.patch.return_value.msg = "Fail"
        
        callbacks = handler._create_deep_callbacks(
            message_id="msg_123",
            chat_id="chat_123",
            project=None,
            initial_message_id="init_msg_id"
        )
        
        mock_project = MagicMock()
        mock_project.name = "Test Project"
        mock_project.root_path = "/tmp"
        mock_project.project_id = "proj_123"

        with patch('src.feishu.handlers.deep.CardBuilder') as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            with patch('time.sleep') as mock_sleep:
                with patch.object(handler, 'reply_message') as mock_reply:
                    with patch.object(handler, 'send_message') as mock_send:
                        callbacks.on_planning_done(mock_project)
                        
                        # Verify patch called 3 times
                        assert client.im.v1.message.patch.call_count == 3
                        
                        # Verify sleep called
                        assert mock_sleep.called
                        
                        # Verify NO fallback
                        mock_reply.assert_not_called()
                        mock_send.assert_not_called()
