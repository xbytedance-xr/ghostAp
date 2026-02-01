import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from src.feishu.ws_client import FeishuWSClient

class TestCardActionHandler(unittest.TestCase):
    def test_handle_card_action_returns_none(self):
        """验证 _handle_card_action 返回 None"""
        # Mock settings
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.CocoSessionManager'), \
             patch('src.feishu.ws_client.ClaudeSessionManager'), \
             patch('src.feishu.ws_client.IntentRecognizer'), \
             patch('src.feishu.ws_client.ProjectManager'), \
             patch('src.feishu.ws_client.MessageProjectMapper'), \
             patch('src.feishu.ws_client.DeepEngineManager'), \
             patch('src.feishu.ws_client.ProgressReporter'), \
             patch('src.mode.ModeManager'):
            
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings
            
            # 实例化 client
            mock_callback = MagicMock()
            client = FeishuWSClient(mock_callback)
            
            # Mock scheduler to avoid running async task
            client._scheduler = MagicMock()

            # 调用 handler
            result = client._handle_card_action(MagicMock())
            
            # 验证返回 None
            self.assertIsNone(result)
            
            # 验证异步任务被提交
            client._scheduler.submit.assert_called_once()

    def test_process_card_action_parses_string_value(self):
        """验证字符串 value 被解析后触发对应处理"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.CocoSessionManager'), \
             patch('src.feishu.ws_client.ClaudeSessionManager'), \
             patch('src.feishu.ws_client.IntentRecognizer'), \
             patch('src.feishu.ws_client.ProjectManager'), \
             patch('src.feishu.ws_client.MessageProjectMapper'), \
             patch('src.feishu.ws_client.DeepEngineManager'), \
             patch('src.feishu.ws_client.ProgressReporter'), \
             patch('src.mode.ModeManager'):

            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._handle_card_enter_coco = MagicMock()

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value='{"action":"enter_coco","project_id":"p1"}',
                        tag="button",
                        name="enter"
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1")
                )
            )

            client._process_card_action_async(data)

            client._handle_card_enter_coco.assert_called_once_with("om_1", "oc_1", "p1")

    def test_handle_card_enter_claude_passes_project(self):
        """验证卡片入口 Claude 时把 project 透传给 _enter_claude_mode（避免选错项目导致显示 Coco 卡片）"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.CocoSessionManager'), \
             patch('src.feishu.ws_client.ClaudeSessionManager'), \
             patch('src.feishu.ws_client.IntentRecognizer'), \
             patch('src.feishu.ws_client.ProjectManager'), \
             patch('src.feishu.ws_client.MessageProjectMapper'), \
             patch('src.feishu.ws_client.DeepEngineManager'), \
             patch('src.feishu.ws_client.ProgressReporter'), \
             patch('src.mode.ModeManager'):

            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._project_manager = MagicMock()

            project = SimpleNamespace(
                project_id="p1",
                claude_session_snapshot=None,
                coco_session_snapshot=None,
            )
            client._project_manager.get_project.return_value = project
            client._enter_claude_mode = MagicMock()

            client._handle_card_enter_claude("om_1", "oc_1", "p1")

            client._enter_claude_mode.assert_called_once()
            args, kwargs = client._enter_claude_mode.call_args
            self.assertEqual(args[0], "om_1")
            self.assertEqual(args[1], "oc_1")
            self.assertIs(kwargs.get("project"), project)

if __name__ == '__main__':
    unittest.main()
