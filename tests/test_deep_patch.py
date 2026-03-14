
import json
import pytest
from unittest.mock import MagicMock, patch
from src.feishu.handlers.deep import DeepHandler
from src.deep_engine.models import DeepProjectStatus

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

    def test_send_deep_message_patch_sends_schema_v2(self, handler):
        # Setup mock client
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = True

        # Setup callbacks with an initial message ID (to trigger update path)
        callbacks = handler._create_deep_callbacks(
            message_id="msg_123",
            chat_id="chat_123",
            project=None,
            initial_message_id="init_msg_id"
        )

        # Mock CardBuilder to return a valid V2 card structure
        v2_card = {
            "schema": "2.0",
            "header": {"title": "Test"},
            "body": {"elements": []}
        }
        v2_card_json = json.dumps(v2_card)

        # Mock project for the callback
        mock_project = MagicMock()
        mock_project.name = "Test Project"
        mock_project.root_path = "/tmp"
        mock_project.project_id = "proj_123"

        with patch('src.feishu.renderers.deep_renderer.CardBuilder') as mock_builder:
            # Configure CardBuilder to return our V2 card
            mock_builder.build_deep_card.return_value = ("interactive", v2_card_json)
            
            # Trigger the callback which calls _send_deep_message(is_update=True)
            callbacks.on_planning_done(mock_project)

            # Verification
            # Get the argument passed to patch()
            args, _ = client.im.v1.message.patch.call_args
            req = args[0]
            
            # The content should be exactly our v2_card_json, NOT modified/legacy
            sent_content = req.request_body.content
            sent_json = json.loads(sent_content)
            
            assert "schema" in sent_json
            assert sent_json["schema"] == "2.0"
            assert sent_json == v2_card

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
        with patch('src.feishu.renderers.deep_renderer.CardBuilder') as mock_builder:
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

        with patch('src.feishu.renderers.deep_renderer.CardBuilder') as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            with patch('time.sleep') as mock_sleep:
                with patch.object(handler, 'reply_message') as mock_reply:
                    with patch.object(handler, 'send_message') as mock_send:
                        callbacks.on_planning_done(mock_project)
                        
                        # Verify patch called 1 time (max_retries=1)
                        assert client.im.v1.message.patch.call_count == 1
                        
                        # Verify sleep NOT called
                        mock_sleep.assert_not_called()
                        
                        # Verify NO fallback
                        mock_reply.assert_not_called()
                        mock_send.assert_not_called()

class TestDeepStatusPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.progress_reporter = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.deep_engine_manager = MagicMock()
        
        # Mock settings
        ctx.settings = MagicMock()
        
        handler = DeepHandler(ctx)
        
        # Mock common methods
        handler.reply_message = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp")
        handler.get_engine_name = MagicMock(return_value="Coco")
        
        return handler

    def test_show_deep_status_patch_success(self, handler):
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_project.project_id = "p1"
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0
        
        handler.ctx.deep_engine_manager.get.return_value = mock_engine
        
        # Mock settings
        handler.settings.card_deep_compact_default = False
        
        # Mock API client
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = True
        
        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id="origin1")
            
            # Verify patch called
            client.im.v1.message.patch.assert_called_once()
            
            # Verify build_deep_card called with compact=False
            _, kwargs = mock_builder.build_deep_card.call_args
            state = kwargs.get('state')
            assert state is not None
            assert state.compact is False
            
            # Verify reply NOT called
            handler.reply_message.assert_not_called()

    def test_show_deep_status_patch_failure_fallback(self, handler):
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0
        
        handler.ctx.deep_engine_manager.get.return_value = mock_engine
        
        # Mock API client failure
        client = handler.ctx.api_client_factory.return_value
        client.im.v1.message.patch.return_value.success.return_value = False
        client.im.v1.message.patch.return_value.code = 400
        client.im.v1.message.patch.return_value.msg = "Bad Request"
        
        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id="origin1")
            
            # Verify patch called
            client.im.v1.message.patch.assert_called_once()
            # Verify fallback to reply
            handler.reply_message.assert_called_once()

    def test_show_deep_status_no_origin_id(self, handler):
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0
        
        handler.ctx.deep_engine_manager.get.return_value = mock_engine
        
        # Mock API client failure
        client = handler.ctx.api_client_factory.return_value
        
        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_deep_card.return_value = ("interactive", "{}")
            
            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id=None)
            
            client.im.v1.message.patch.assert_not_called()
            handler.reply_message.assert_called_once()
