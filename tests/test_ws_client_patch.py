import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from src.feishu.ws_client import FeishuWSClient
from src.mode import InteractionMode

class TestCardActionHandler(unittest.TestCase):
    def test_handle_card_action_returns_none(self):
        """验证 _handle_card_action 返回 None"""
        # Mock settings
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
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
             patch('src.feishu.ws_client.ACPSessionManager'), \
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
            # Must re-register because the original registration captured the bound method
            client._register_action(client._handle_card_enter_coco, exact="enter_coco")

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

            # Now expects value dict as 4th argument
            client._handle_card_enter_coco.assert_called_once_with("om_1", "oc_1", "p1", {"action": "enter_coco", "project_id": "p1"})

    def test_process_card_action_routes_refresh_ttadk_models(self):
        """验证 TTADK 模型选择卡的『刷新模型列表』按钮可被正确路由。"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
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
            client._handle_refresh_ttadk_models = MagicMock()

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value={"action": "refresh_ttadk_models", "tool_name": "codex", "project_id": "p1"},
                        tag="button",
                        name="refresh",
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)
            client._handle_refresh_ttadk_models.assert_called_once_with("om_1", "oc_1", "codex", "p1")

    def test_handle_card_enter_claude_passes_project(self):
        """验证卡片入口 Claude 时把 project 透传给 enter_mode（避免选错项目导致显示 Coco 卡片）"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
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

            project = SimpleNamespace(
                project_id="p1",
                claude_session_snapshot=None,
                coco_session_snapshot=None,
            )
            # Mock at handler level: project_manager lives inside handler context
            client._claude_handler.project_manager.get_project.return_value = project
            client._claude_handler.enter_mode = MagicMock()

            client._handle_card_enter_claude("om_1", "oc_1", "p1")

            client._claude_handler.enter_mode.assert_called_once()
            args, kwargs = client._claude_handler.enter_mode.call_args
            self.assertEqual(args[0], "om_1")
            self.assertEqual(args[1], "oc_1")
            self.assertIs(kwargs.get("project"), project)

    def test_is_system_command_message_detects_all_slash_commands(self):
        """验证所有 /command 格式的消息被识别为系统命令，走 SYSTEM 快速通道"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
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

            def _make_data(text):
                data = MagicMock()
                data.event.message.content = json.dumps({"text": text})
                return data

            # Previously blocked commands — now should be system commands
            self.assertTrue(client._is_system_command_message(_make_data("/stop_deep")))
            self.assertTrue(client._is_system_command_message(_make_data("/stop_loop")))
            self.assertTrue(client._is_system_command_message(_make_data("/deep_status")))
            self.assertTrue(client._is_system_command_message(_make_data("/loop_status")))
            self.assertTrue(client._is_system_command_message(_make_data("/exit")))
            self.assertTrue(client._is_system_command_message(_make_data("/coco")))
            self.assertTrue(client._is_system_command_message(_make_data("/claude")))
            self.assertTrue(client._is_system_command_message(_make_data("/deep do stuff")))
            self.assertTrue(client._is_system_command_message(_make_data("/loop do stuff")))
            self.assertTrue(client._is_system_command_message(_make_data("/loop_guide keep going")))

            # Already-supported interceptable commands still work
            self.assertTrue(client._is_system_command_message(_make_data("/help")))
            self.assertTrue(client._is_system_command_message(_make_data("/projects")))
            self.assertTrue(client._is_system_command_message(_make_data("/diff")))

            # Chinese exit keywords (no slash) — should also be system commands
            self.assertTrue(client._is_system_command_message(_make_data("退出模式")))
            self.assertTrue(client._is_system_command_message(_make_data("退出编程模式")))

            # Non-commands should NOT be system commands
            self.assertFalse(client._is_system_command_message(_make_data("hello")))
            self.assertFalse(client._is_system_command_message(_make_data("ls -la")))
            self.assertFalse(client._is_system_command_message(_make_data("git status")))
            self.assertFalse(client._is_system_command_message(_make_data("")))

    def test_is_interceptable_command_includes_diff(self):
        """验证 /diff 会被识别为系统拦截命令（避免被当成 shell 或转发给 AI）"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
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
            self.assertTrue(client._is_interceptable_command("/diff"))
            self.assertTrue(client._is_interceptable_command("/diff current"))

    def test_process_with_intent_routes_diff_in_smart_mode(self):
        """验证 Smart 模式下 /diff 走系统命令分支，而不是进入 intent 识别/执行"""
        with patch('src.feishu.ws_client.get_settings') as mock_get_settings, \
             patch('src.feishu.ws_client.ACPSessionManager'), \
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
            client._mode_manager.get_mode.return_value = InteractionMode.SMART
            client._handle_intercepted_command = MagicMock()

            client._process_with_intent("m1", "c1", "/diff", project=None)
            client._handle_intercepted_command.assert_called_once()

if __name__ == '__main__':
    unittest.main()
