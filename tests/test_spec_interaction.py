import unittest
from unittest.mock import MagicMock, patch
import json
from types import SimpleNamespace

from src.card.models import DeepCardState
from src.feishu.handlers.spec import SpecHandler
from src.feishu.ws_client import FeishuWSClient
from src.spec_engine.models import SpecProject, SpecProjectStatus

class TestSpecInteraction(unittest.TestCase):

    def test_spec_handler_uses_standard_dispatch(self):
        """验证 SpecHandler 调用 _dispatch_standard_card_action"""
        mock_ctx = MagicMock()
        mock_ctx.settings.card_deep_compact_default = False
        
        handler = SpecHandler(mock_ctx)
        # Mock the dispatch method
        handler._dispatch_standard_card_action = MagicMock(return_value=True)
        
        # Test spec_pause action
        handler.handle_card_action("mid", "cid", "spec_pause", {"action": "spec_pause", "project_id": "p1"})
        
        # Verify dispatch called with correct args
        handler._dispatch_standard_card_action.assert_called_once()
        call_args = handler._dispatch_standard_card_action.call_args
        self.assertEqual(call_args[1]["prefix"], "spec")
        self.assertIn("spec_pause", call_args[1]["action_map"])
        self.assertIn("spec_resume", call_args[1]["action_map"])
        self.assertIn("spec_stop", call_args[1]["action_map"])
        self.assertEqual(call_args[1]["toggle_log_method"], handler.toggle_spec_log)
        self.assertEqual(call_args[1]["switch_mode_method"], handler.switch_spec_card_mode)

    def test_ws_client_routes_spec_actions(self):
        """验证 FeishuWSClient 正确路由 spec_pause/resume/stop 动作"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
             patch('src.feishu.ws_client.IntentRecognizer'), \
             patch('src.feishu.ws_client.ProjectManager'), \
             patch('src.feishu.ws_client.MessageProjectMapper'), \
             patch('src.feishu.ws_client.DeepEngineManager'), \
             patch('src.feishu.ws_client.ProgressReporter'), \
             patch('src.feishu.ws_client.LoopEngineManager'), \
             patch('src.feishu.ws_client.LoopReporter'), \
             patch('src.feishu.ws_client.SpecEngineManager'), \
             patch('src.feishu.ws_client.SpecReporter'), \
             patch('src.mode.ModeManager'), \
             patch('src.feishu.handlers.SpecHandler'):
             
            mock_settings = MagicMock()
            mock_settings.app_id = "app_id"
            mock_settings.app_secret = "app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings
            
            client = FeishuWSClient(MagicMock())
            # Mock the spec handler instance
            client._spec_handler = MagicMock()
            
            # Test spec_pause
            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value='{"action":"spec_pause","project_id":"p1"}',
                        tag="button",
                        name="pause"
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1")
                )
            )
            
            client._process_card_action_async(data)
            
            # Verify handler called
            client._spec_handler.handle_card_action.assert_called()
            args = client._spec_handler.handle_card_action.call_args
            # args: (mid, cid, type, val)
            self.assertEqual(args[0][0], "om_1")
            self.assertEqual(args[0][1], "oc_1")
            self.assertEqual(args[0][2], "spec_pause")

if __name__ == '__main__':
    unittest.main()
