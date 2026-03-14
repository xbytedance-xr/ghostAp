
import json
from unittest.mock import MagicMock, patch
import pytest
from src.card import CardBuilder
from src.card.models import DeepCardState
from src.feishu.handlers.spec import SpecHandler
from src.feishu.ws_client import FeishuWSClient
from src.feishu.handler_context import HandlerContext
from src.spec_engine.models import SpecProject, SpecProjectStatus
from src.project import ProjectContext

def _make_handler_context(**overrides) -> HandlerContext:
    """Build a HandlerContext with all dependencies mocked."""
    import threading
    ctx = HandlerContext(
        settings=MagicMock(),
        api_client_factory=MagicMock(),
        message_callback=MagicMock(),
        coco_manager=MagicMock(),
        claude_manager=MagicMock(),
        ttadk_manager=MagicMock(),
        intent_recognizer=MagicMock(),
        scheduler=MagicMock(),
        project_manager=MagicMock(),
        message_mapper=MagicMock(),
        message_linker=MagicMock(),
        mode_manager=MagicMock(),
        context_manager=MagicMock(),
        deep_engine_manager=MagicMock(),
        progress_reporter=MagicMock(),
        loop_engine_manager=MagicMock(),
        loop_reporter=MagicMock(),
        spec_engine_manager=MagicMock(),
        spec_reporter=MagicMock(),
        streaming_manager_factory=MagicMock(),
        image_handler_factory=MagicMock(),
        working_dirs={},
        working_dir_lock=threading.Lock(),
        pending_image_keys={},
        pending_image_lock=threading.Lock(),
        enable_streaming=False,
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx

class TestSpecInteraction:
    
    def test_card_builder_action_prefix_spec(self):
        """Verify CardBuilder respects action_prefix='spec'."""
        msg_type, card_json = CardBuilder.build_deep_card(
            project=None,
            state=DeepCardState(
                title="Spec Test",
                content="Running...",
                is_executing=True,
                action_prefix="spec",
                deep_project_id="proj1",
                engine_name="Spec(Coco)"
            )
        )
        
        assert '"action": "spec_pause"' in card_json
        assert '"action": "spec_stop"' in card_json

        msg_type, card_json = CardBuilder.build_deep_card(
            project=None,
            state=DeepCardState(
                title="Idle Spec",
                content="Done",
                is_executing=False,
                is_paused=False,
                action_prefix="spec",
                compact=True
            )
        )
        
        assert '"action": "spec_mode_full"' in card_json

    def test_spec_handler_generates_spec_buttons(self):
        """Verify SpecHandler calls build_deep_card with action_prefix='spec'."""
        ctx = _make_handler_context()
        handler = SpecHandler(ctx)
        
        # Mock dependencies
        mock_engine = MagicMock()
        mock_engine.project = SpecProject(name="test_spec", root_path="/tmp", project_id="p1", requirement="req")
        mock_engine.project.status = SpecProjectStatus.RUNNING
        mock_engine.is_running = True
        mock_engine.engine_name = "Coco"
        
        ctx.spec_engine_manager.get.return_value = mock_engine
        ctx.project_manager.get_active_project.return_value = None
        handler.get_working_dir = MagicMock(return_value="/tmp")
        
        # Mock reporter
        ctx.spec_reporter.format_status.return_value = "Status OK"
        ctx.spec_reporter.get_status_title.return_value = "Spec Status"
        ctx.spec_reporter.get_progress_info.return_value = {
            "progress_bar": "[==]", "is_running": True, "is_paused": False
        }
        
        handler.reply_message = MagicMock()
        handler.patch_message = MagicMock(return_value=False)
        
        handler.show_spec_status("msg1", "chat1")
        
        # Check calls
        assert handler.reply_message.called
        call_args = handler.reply_message.call_args
        card_json = call_args[0][1]
        
        assert '"action": "spec_pause"' in card_json

    def test_ws_client_routes_spec_action(self):
        """Verify FeishuWSClient routes spec_pause to SpecHandler."""
        with patch("src.feishu.ws_client.FeishuWSClient._get_api_client"), \
             patch("src.feishu.ws_client.FeishuWSClient._get_streaming_manager"), \
             patch("src.feishu.ws_client.FeishuWSClient._get_image_handler"), \
             patch("src.feishu.ws_client.SpecHandler") as MockSpecHandler:
            
            mock_handler_instance = MockSpecHandler.return_value
            
            client = FeishuWSClient(lambda x: None)
            client._scheduler.submit = MagicMock(side_effect=lambda spec, func: func(None))
            
            data = MagicMock()
            data.event.action.value = {"action": "spec_pause", "project_id": "proj1"}
            data.event.context.open_message_id = "msg1"
            data.event.context.open_chat_id = "chat1"
            
            client._process_card_action_async(data)
            
            # Verify routing to handle_card_action
            mock_handler_instance.handle_card_action.assert_called_once_with(
                "msg1", "chat1", "spec_pause", {"action": "spec_pause", "project_id": "proj1"}
            )

    def test_ws_client_routes_spec_expand(self):
        """Verify FeishuWSClient routes spec_expand to SpecHandler."""
        with patch("src.feishu.ws_client.FeishuWSClient._get_api_client"), \
             patch("src.feishu.ws_client.FeishuWSClient._get_streaming_manager"), \
             patch("src.feishu.ws_client.FeishuWSClient._get_image_handler"), \
             patch("src.feishu.ws_client.SpecHandler") as MockSpecHandler:
            
            mock_handler_instance = MockSpecHandler.return_value
            
            client = FeishuWSClient(lambda x: None)
            client._scheduler.submit = MagicMock(side_effect=lambda spec, func: func(None))
            
            data = MagicMock()
            data.event.action.value = {"action": "spec_expand", "project_id": "proj1", "deep_project_id": "proj1"}
            data.event.context.open_message_id = "msg1"
            data.event.context.open_chat_id = "chat1"
            
            client._process_card_action_async(data)
            
            # Verify routing to handle_card_action
            mock_handler_instance.handle_card_action.assert_called_once_with(
                "msg1", "chat1", "spec_expand", {"action": "spec_expand", "project_id": "proj1", "deep_project_id": "proj1"}
            )
