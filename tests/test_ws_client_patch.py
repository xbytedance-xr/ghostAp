import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from src.agent.intent_recognizer import IntentType, TaskStep

from src.feishu.ws_client import FeishuWSClient
from src.mode import InteractionMode


class TestCardActionHandler(unittest.TestCase):
    def test_handle_card_action_returns_none(self):
        """验证 _handle_card_action 返回 None"""
        # Mock settings
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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
                        value='{"action":"enter_coco","project_id":"p1"}', tag="button", name="enter"
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)

            # Now expects value dict as 4th argument
            client._handle_card_enter_coco.assert_called_once_with(
                "om_1", "oc_1", "p1", {"action": "enter_coco", "project_id": "p1"}
            )

    def test_process_card_action_routes_refresh_ttadk_models(self):
        """验证 TTADK 模型选择卡的『刷新模型列表』按钮可被正确路由。"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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

    def test_process_card_action_routes_toggle_ttadk_yolo(self):
        """验证 TTADK YOLO 切换按钮可被正确路由。"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._handle_toggle_ttadk_yolo = MagicMock()

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value={
                            "action": "toggle_ttadk_yolo",
                            "enabled": True,
                            "view": "model_select",
                            "tool_name": "codex",
                            "project_id": "p1",
                        },
                        tag="button",
                        name="toggle",
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)
            client._handle_toggle_ttadk_yolo.assert_called_once_with(
                "om_1", "oc_1", True, "model_select", "codex", "p1"
            )

    def test_process_card_action_routes_show_ttadk_menu_force_select(self):
        """验证 TTADK 菜单按钮强制进入选择菜单。"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._handle_ttadk_command = MagicMock()

            project = SimpleNamespace(project_id="p1")
            client._project_manager.get_project.return_value = project

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value={
                            "action": "show_ttadk_menu",
                            "project_id": "p1",
                        },
                        tag="button",
                        name="menu",
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)
            client._handle_ttadk_command.assert_called_once()
            args, _ = client._handle_ttadk_command.call_args
            self.assertEqual(args[0], "om_1")
            self.assertEqual(args[1], "oc_1")
            self.assertIs(args[2], project)
            self.assertTrue(args[3])

    def test_process_card_action_ttadk_exception_uses_soft_failure_card(self):
        """验证 TTADK 卡片动作异常时返回软失败提示。"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._reply_message = MagicMock()
            client._action_dispatcher.dispatch = MagicMock(side_effect=Exception("boom"))

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value={"action": "select_ttadk_tool", "tool_name": "codex", "project_id": "p1"},
                        tag="button",
                        name="tool",
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)

            client._reply_message.assert_called_once()
            _, content = client._reply_message.call_args.args[:2]
            self.assertIn("已为你保留选择", content)
            self.assertIn("继续进入TTADK", content)

    def test_handle_card_enter_claude_passes_project(self):
        """验证卡片入口 Claude 时把 project 透传给 enter_mode（避免选错项目导致显示 Coco 卡片）"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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

    def test_handle_card_enter_ttadk_passes_project(self):
        """验证卡片入口 TTADK 时把 project 透传给 enter_mode（避免加载错误项目）"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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
                ttadk_session_snapshot=None,
                coco_session_snapshot=None,
                claude_session_snapshot=None,
            )
            # Mock at handler level: project_manager lives inside handler context
            client._ttadk_handler.project_manager.get_project.return_value = project
            client._ttadk_handler.enter_mode = MagicMock()

            client._handle_card_enter_ttadk("om_1", "oc_1", "p1")

            client._ttadk_handler.enter_mode.assert_called_once()
            args, kwargs = client._ttadk_handler.enter_mode.call_args
            self.assertEqual(args[0], "om_1")
            self.assertEqual(args[1], "oc_1")
            self.assertIs(kwargs.get("project"), project)

    def test_is_system_command_message_detects_all_slash_commands(self):
        """验证所有 /command 格式的消息被识别为系统命令，走 SYSTEM 快速通道"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
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


    def test_process_with_intent_routes_ttadk(self):
        """Test that _process_with_intent routes to TTADK handler in TTADK mode."""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._mode_manager = MagicMock()
            client._mode_manager.is_programming_mode.return_value = True
            client._mode_manager.get_mode.return_value = InteractionMode.TTADK

            client._is_deep_command = MagicMock(return_value=False)
            client._is_loop_command = MagicMock(return_value=False)
            client._is_spec_command = MagicMock(return_value=False)
            client._is_interceptable_command = MagicMock(return_value=False)
            client._is_exit_command = MagicMock(return_value=False)

            client._ttadk_handler = MagicMock()
            client._ttadk_handler.handle_message = MagicMock()
            client._add_reaction = MagicMock()

            mock_project = MagicMock()

            client._process_with_intent(
                message_id="msg_1",
                chat_id="chat_1",
                text="hello ttadk",
                project=mock_project,
            )

            client._ttadk_handler.handle_message.assert_called_once_with(
                "msg_1", "chat_1", "hello ttadk", mock_project
            )

    def test_execute_single_task_ttadk_message_routes_to_ttadk_handler(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._ttadk_handler = MagicMock()
            client._ttadk_handler.handle_message = MagicMock()
            client._mode_manager.get_mode.return_value = InteractionMode.TTADK

            task = TaskStep(intent=IntentType.TTADK_MESSAGE, description="ttadk", data={})
            client._execute_single_task("m1", "c1", task, "refactor this", project=None)
            client._ttadk_handler.handle_message.assert_called_once_with("m1", "c1", "refactor this", None)

    def test_process_with_intent_routes_gemini(self):
        """Test that _process_with_intent routes to Gemini handler in GEMINI mode."""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._mode_manager = MagicMock()
            client._mode_manager.is_programming_mode.return_value = True
            client._mode_manager.get_mode.return_value = InteractionMode.GEMINI

            client._is_deep_command = MagicMock(return_value=False)
            client._is_loop_command = MagicMock(return_value=False)
            client._is_spec_command = MagicMock(return_value=False)
            client._is_interceptable_command = MagicMock(return_value=False)
            client._is_exit_command = MagicMock(return_value=False)

            client._handle_gemini_message = MagicMock()
            client._add_reaction = MagicMock()

            mock_project = MagicMock()

            client._process_with_intent(
                message_id="msg_1",
                chat_id="chat_1",
                text="hello gemini",
                project=mock_project,
            )

            client._handle_gemini_message.assert_called_once_with(
                "msg_1", "chat_1", "hello gemini", mock_project
            )

    def test_dispatch_empty_text_routes_gemini_mode(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._mode_manager.get_mode.return_value = InteractionMode.GEMINI
            client._project_manager.get_active_project.return_value = MagicMock()
            client._handle_gemini_message = MagicMock()

            client._dispatch_empty_text("msg_1", "chat_1", project=None, task_ctx=None)

            client._handle_gemini_message.assert_called_once()
            args = client._handle_gemini_message.call_args.args
            self.assertEqual(args[:3], ("msg_1", "chat_1", ""))

    def test_resolve_project_from_message_auto_enters_gemini(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            project = MagicMock(
                project_name="demo",
                ttadk_mode=False,
                gemini_mode=True,
                codex_mode=False,
                aiden_mode=False,
                claude_mode=False,
                coco_mode=False,
            )
            client._message_mapper.get_project_id.return_value = "p1"
            client._project_manager.get_project.return_value = project

            resolved_project, auto_enter_mode = client._resolve_project_from_message("msg_1", "chat_1", "parent_1")

            self.assertIs(resolved_project, project)
            self.assertEqual(auto_enter_mode, "gemini")
            client._project_manager.set_active_project.assert_called_once_with("chat_1", "p1")

    def test_build_control_queue_key_for_programming_and_spec_commands(self):
        """/coco 与 /spec* 应落在同一控制队列，确保先后顺序执行。"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())

            assert client._build_control_queue_key(chat_id="c1", project_id="p1", text="/coco") == "c1:control:p1"
            assert client._build_control_queue_key(chat_id="c1", project_id="p1", text="/spec do x") == "c1:control:p1"
            assert client._build_control_queue_key(chat_id="c1", project_id=None, text="/spec_status") == "c1:control:default"
            assert client._build_control_queue_key(chat_id="c1", project_id="p1", text="ls -la") is None

    def test_close_waits_engine_shutdown_before_cleanup(self):
        """close() 应先 stop 长任务，再短暂等待停稳后执行 cleanup_all。"""
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
            patch("src.feishu.ws_client.time.sleep") as mock_sleep,
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())

            class _Engine:
                def __init__(self):
                    self.stop_called = False
                    self._poll = 0

                def stop(self):
                    self.stop_called = True

                @property
                def is_running(self):
                    if not self.stop_called:
                        return True
                    self._poll += 1
                    return self._poll < 2

            deep_engine = _Engine()
            client._deep_engine_manager.list_engines = MagicMock(return_value=[deep_engine])
            client._loop_engine_manager.list_engines = MagicMock(return_value=[])
            client._spec_engine_manager.list_engines = MagicMock(return_value=[])
            client._deep_engine_manager.cleanup_all = MagicMock()
            client._loop_engine_manager.cleanup_all = MagicMock()
            client._spec_engine_manager.cleanup_all = MagicMock()

            client.close()

            assert deep_engine.stop_called is True
            assert mock_sleep.called
            assert client._coco_manager.cleanup_all.call_count >= 6
            client._deep_engine_manager.cleanup_all.assert_called_once()
            client._loop_engine_manager.cleanup_all.assert_called_once()
            client._spec_engine_manager.cleanup_all.assert_called_once()

    def test_ws_watchdog_triggers_disconnect_on_stale_connection(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._client = SimpleNamespace(_conn=object(), _ping_interval=120)
            client._trigger_ws_disconnect = MagicMock(return_value=True)

            now = 1_000.0
            with client._ws_health_lock:
                client._ws_last_connect_at = now - 400.0
                client._ws_last_frame_at = now - 400.0
                client._ws_last_pong_at = now - 400.0
                client._ws_reconnect_requested_at = 0.0

            triggered = client._check_ws_health_once(now=now)

            self.assertTrue(triggered)
            client._trigger_ws_disconnect.assert_called_once()

    def test_ws_watchdog_does_not_reconnect_when_recent_pong_exists(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._client = SimpleNamespace(_conn=object(), _ping_interval=120)
            client._trigger_ws_disconnect = MagicMock(return_value=True)

            now = 1_000.0
            with client._ws_health_lock:
                client._ws_last_connect_at = now - 400.0
                client._ws_last_frame_at = now - 400.0
                client._ws_last_pong_at = now - 10.0
                client._ws_reconnect_requested_at = 0.0

            triggered = client._check_ws_health_once(now=now)

            self.assertFalse(triggered)
            client._trigger_ws_disconnect.assert_not_called()

    def test_process_with_intent_routes_acp_command_to_system_handler(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._mode_manager.get_mode.return_value = InteractionMode.SMART
            client._mode_manager.is_programming_mode.return_value = False
            client._system_handler.handle_acp_command = MagicMock()

            client._process_with_intent("m1", "c1", "/acp", project=None)

            client._system_handler.handle_acp_command.assert_called_once_with("m1", "c1", None)

    def _make_client(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings
            return FeishuWSClient(MagicMock())


class TestSystemCmdGateReadonlyBypass(unittest.TestCase):

    def _make_client_with_gate_active(self, chat_id="oc_1"):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._scheduler = MagicMock()
            client._reply_message = MagicMock()

            with client._system_cmd_gate_lock:
                client._system_cmd_inflight_by_chat[chat_id] = 1

            return client

    def _make_card_data(self, action_type, chat_id="oc_1", message_id="om_1"):
        return SimpleNamespace(
            header=SimpleNamespace(event_id="evt_1", event_type="card.action.trigger"),
            event=SimpleNamespace(
                action=SimpleNamespace(
                    value={"action": action_type, "project_id": "p1"},
                    tag="button",
                    name="btn",
                ),
                operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                context=SimpleNamespace(open_message_id=message_id, open_chat_id=chat_id),
            ),
        )

    def test_readonly_action_bypasses_gate(self):
        from src.feishu.ws_client import _READONLY_CARD_ACTIONS

        client = self._make_client_with_gate_active()
        for action in ["deep_expand", "loop_collapse", "spec_mode_full", "deep_expand_ac"]:
            self.assertIn(action, _READONLY_CARD_ACTIONS)
            client._card_event_cache = MagicMock()
            client._card_event_cache.is_duplicate.return_value = False
            client._card_action_dedup_cache = MagicMock()
            client._card_action_dedup_cache.is_duplicate.return_value = True
            data = self._make_card_data(action)
            result = client._handle_card_action(data)
            self.assertIsNone(result)
            client._reply_message.assert_not_called()

    def test_non_readonly_action_blocked_by_gate(self):
        client = self._make_client_with_gate_active()
        client._card_event_cache = MagicMock()
        client._card_event_cache.is_duplicate.return_value = False
        client._card_action_dedup_cache = MagicMock()
        client._card_action_dedup_cache.is_duplicate.return_value = True
        data = self._make_card_data("enter_coco")
        result = client._handle_card_action(data)
        self.assertIsNone(result)
        client._reply_message.assert_called_once()
        args = client._reply_message.call_args.args
        self.assertIn("系统指令处理中", args[1])
        client._scheduler.submit.assert_not_called()

    def test_gate_not_active_allows_all_actions(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
            client._scheduler = MagicMock()
            client._reply_message = MagicMock()
            client._card_event_cache = MagicMock()
            client._card_event_cache.is_duplicate.return_value = False
            client._card_action_dedup_cache = MagicMock()
            client._card_action_dedup_cache.is_duplicate.return_value = True

            data = self._make_card_data("enter_coco")
            result = client._handle_card_action(data)
            self.assertIsNone(result)
            client._reply_message.assert_not_called()


class TestIsSystemCardActionEngineControls(unittest.TestCase):

    def _make_client(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_get_settings.return_value = mock_settings
            return FeishuWSClient(MagicMock())

    def _make_data(self, action_type):
        return SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(value={"action": action_type}),
            )
        )

    def test_engine_control_actions_are_system(self):
        client = self._make_client()
        engine_controls = [
            "deep_pause", "deep_stop", "deep_resume",
            "loop_pause", "loop_stop", "loop_resume",
            "spec_pause", "spec_stop", "spec_resume",
        ]
        for action in engine_controls:
            self.assertTrue(
                client._is_system_card_action(self._make_data(action)),
                f"{action} should be a system card action",
            )

    def test_existing_system_actions_still_recognized(self):
        client = self._make_client()
        for action in ["show_status", "switch_project", "show_board", "enter_deep_prompt"]:
            self.assertTrue(
                client._is_system_card_action(self._make_data(action)),
                f"{action} should still be a system card action",
            )

    def test_non_system_actions_not_flagged(self):
        client = self._make_client()
        for action in ["enter_coco", "enter_claude", "deep_expand", "unknown_action"]:
            self.assertFalse(
                client._is_system_card_action(self._make_data(action)),
                f"{action} should NOT be a system card action",
            )


class TestOneShotDispatchToThread(unittest.TestCase):

    def _make_client(self):
        with (
            patch("src.feishu.ws_client.get_settings") as mock_get_settings,
            patch("src.feishu.ws_client.ACPSessionManager"),
            patch("src.feishu.ws_client.IntentRecognizer"),
            patch("src.feishu.ws_client.ProjectManager"),
            patch("src.feishu.ws_client.MessageProjectMapper"),
            patch("src.feishu.ws_client.DeepEngineManager"),
            patch("src.feishu.ws_client.ProgressReporter"),
            patch("src.mode.ModeManager"),
        ):
            mock_settings = MagicMock()
            mock_settings.app_id = "test_app_id"
            mock_settings.app_secret = "test_app_secret"
            mock_settings.streaming_enabled = False
            mock_settings.task_scheduler_max_concurrent = 2
            mock_settings.task_scheduler_per_key_concurrency = 1
            mock_settings.thread_programming_enabled = True
            mock_get_settings.return_value = mock_settings

            client = FeishuWSClient(MagicMock())
        return client

    @patch("src.feishu.ws_client.get_current_thread_id", return_value=None)
    def test_dispatch_to_thread_success_exits_to_smart(self, _):
        client = self._make_client()
        handler = MagicMock()
        handler.mode_name = "Coco"
        handler.mode_emoji = "🤖"
        handler.reply_message_with_id.return_value = "thread_root_1"
        mock_session = MagicMock()
        handler._get_session_manager.return_value.get_session.return_value = mock_session

        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp/test"
        project.project_name = "test"

        client._dispatch_to_thread("m1", "c1", "写个函数", project, InteractionMode.COCO, handler)

        client._mode_manager.exit_to_smart.assert_called_once_with("c1", project_id="p1")
        handler._set_mode_on_project.assert_called_once_with(project, False)
        handler._register_thread_context.assert_called_once()
        handler.handle_message.assert_called_once_with("m1", "c1", "写个函数", project)

    @patch("src.feishu.ws_client.get_current_thread_id", return_value=None)
    def test_dispatch_to_thread_reply_fail_no_crash(self, _):
        client = self._make_client()
        client._reply_message = MagicMock()
        handler = MagicMock()
        handler.mode_name = "Coco"
        handler.mode_emoji = "🤖"
        handler.reply_message_with_id.return_value = None

        client._dispatch_to_thread("m1", "c1", "写个函数", MagicMock(), InteractionMode.COCO, handler)

        client._reply_message.assert_called_once()
        assert "失败" in str(client._reply_message.call_args)
        handler.enter_mode.assert_not_called()

    @patch("src.feishu.ws_client.get_current_thread_id", return_value=None)
    def test_dispatch_to_thread_session_fail_exits_to_smart(self, _):
        client = self._make_client()
        handler = MagicMock()
        handler.mode_name = "Coco"
        handler.mode_emoji = "🤖"
        handler.reply_message_with_id.return_value = "thread_root_1"
        handler._get_session_manager.return_value.get_session.return_value = None

        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp/test"
        project.project_name = "test"

        client._dispatch_to_thread("m1", "c1", "写个函数", project, InteractionMode.COCO, handler)

        client._mode_manager.exit_to_smart.assert_called_once_with("c1", project_id="p1")
        handler._set_mode_on_project.assert_called_once_with(project, False)
        handler.handle_message.assert_not_called()

    @patch("src.feishu.ws_client.get_current_thread_id", return_value=None)
    def test_dispatch_to_thread_exception_exits_to_smart(self, _):
        client = self._make_client()
        handler = MagicMock()
        handler.mode_name = "Coco"
        handler.mode_emoji = "🤖"
        handler.reply_message_with_id.return_value = "thread_root_1"
        handler.enter_mode.side_effect = RuntimeError("ACP crashed")

        project = MagicMock()
        project.project_id = "p1"
        project.root_path = "/tmp/test"
        project.project_name = "test"

        client._dispatch_to_thread("m1", "c1", "写个函数", project, InteractionMode.COCO, handler)

        client._mode_manager.exit_to_smart.assert_called_once_with("c1", project_id="p1")
        handler._set_mode_on_project.assert_called_once_with(project, False)

    @patch("src.feishu.ws_client.get_current_thread_id", return_value=None)
    def test_is_one_shot_pending_true_when_no_session(self, _):
        client = self._make_client()
        client.settings.thread_programming_enabled = True
        mock_handler = MagicMock()
        mock_handler._get_session_manager.return_value.get_session.return_value = None
        client._coco_handler = mock_handler

        pending, handler = client._is_one_shot_pending("c1", "p1", InteractionMode.COCO)

        self.assertTrue(pending)
        self.assertEqual(handler, mock_handler)

    @patch("src.feishu.ws_client.get_current_thread_id", return_value=None)
    def test_is_one_shot_pending_false_when_session_exists(self, _):
        client = self._make_client()
        client.settings.thread_programming_enabled = True
        mock_handler = MagicMock()
        mock_handler._get_session_manager.return_value.get_session.return_value = MagicMock()
        client._coco_handler = mock_handler

        pending, handler = client._is_one_shot_pending("c1", "p1", InteractionMode.COCO)

        self.assertFalse(pending)

    @patch("src.feishu.ws_client.get_current_thread_id", return_value="thread_123")
    def test_is_one_shot_pending_false_when_in_thread(self, _):
        client = self._make_client()
        client.settings.thread_programming_enabled = True

        pending, handler = client._is_one_shot_pending("c1", "p1", InteractionMode.COCO)

        self.assertFalse(pending)


if __name__ == "__main__":
    unittest.main()
