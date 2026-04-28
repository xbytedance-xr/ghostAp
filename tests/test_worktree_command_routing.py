import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.feishu.handlers.system import SystemHandler
from src.feishu.handlers.worktree import WorktreeHandler
from src.card.styles import UI_TEXT
from src.card.builder import CardBuilder
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import WorktreeRuntimeState, WorktreeSelectionItem
from src.feishu.ws_client import FeishuWSClient


class TestWorktreeCommandRouting(unittest.TestCase):
    def test_system_handler_recognizes_worktree_commands(self):
        self.assertTrue(SystemHandler.is_interceptable_command("/wt"))
        self.assertTrue(SystemHandler.is_interceptable_command("/worktree"))
        self.assertFalse(SystemHandler.is_interceptable_command("/wt-extra"))

    def test_process_card_action_routes_show_worktree_menu(self):
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
            client._handle_worktree_command = MagicMock()

            project = SimpleNamespace(project_id="p1")
            client._project_manager.get_project.return_value = project
            client._project_manager.get_project_for_chat.return_value = project

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value={"action": "show_worktree_menu", "project_id": "p1"},
                        tag="button",
                        name="menu",
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)

            client._handle_worktree_command.assert_called_once()
            args, _ = client._handle_worktree_command.call_args
            self.assertEqual(args[0], "om_1")
            self.assertEqual(args[1], "oc_1")
            self.assertEqual(args[2], project)
            self.assertTrue(args[3])

    def test_process_card_action_routes_finish_worktree_selection(self):
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
            client._handle_finish_worktree_selection = MagicMock()

            data = SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value={"action": "worktree_finish_selection", "project_id": "p1"},
                        tag="button",
                        name="finish",
                    ),
                    operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                    context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
                )
            )

            client._process_card_action_async(data)

            client._handle_finish_worktree_selection.assert_called_once_with(
                "om_1", "oc_1", "p1",
                {"action": "worktree_finish_selection", "project_id": "p1"},
            )

    def _build_ws_client(self, mock_get_settings):
        """Helper to build a minimally-patched FeishuWSClient for card-action tests."""
        mock_settings = MagicMock()
        mock_settings.app_id = "test_app_id"
        mock_settings.app_secret = "test_app_secret"
        mock_settings.streaming_enabled = False
        mock_settings.task_scheduler_max_concurrent = 2
        mock_settings.task_scheduler_per_key_concurrency = 1
        mock_get_settings.return_value = mock_settings
        return FeishuWSClient(MagicMock())

    def _make_card_event(self, action_name: str, project_id: str = "p1", extra_value: dict | None = None):
        val = {"action": action_name, "project_id": project_id}
        if extra_value:
            val.update(extra_value)
        return SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(value=val, tag="button", name="btn"),
                operator=SimpleNamespace(open_id="ou_x", user_id="u_x"),
                context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
            )
        )

    def test_card_action_routes_worktree_select_tool(self):
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
            client = self._build_ws_client(mock_get_settings)
            client._handle_worktree_select_tool = MagicMock()
            data = self._make_card_event("worktree_select_tool")
            client._process_card_action_async(data)
            client._handle_worktree_select_tool.assert_called_once()

    def test_card_action_routes_worktree_select_model(self):
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
            client = self._build_ws_client(mock_get_settings)
            client._handle_worktree_select_model = MagicMock()
            data = self._make_card_event("worktree_select_model")
            client._process_card_action_async(data)
            client._handle_worktree_select_model.assert_called_once()

    def test_card_action_routes_worktree_confirm_start(self):
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
            client = self._build_ws_client(mock_get_settings)
            client._handle_worktree_confirm_start = MagicMock()
            data = self._make_card_event("worktree_confirm_start")
            client._process_card_action_async(data)
            client._handle_worktree_confirm_start.assert_called_once()

    def test_card_action_routes_worktree_execute_action(self):
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
            client = self._build_ws_client(mock_get_settings)
            client._handle_worktree_execute_action = MagicMock()
            data = self._make_card_event("worktree_execute_action")
            client._process_card_action_async(data)
            client._handle_worktree_execute_action.assert_called_once()

    def test_card_action_routes_worktree_merge(self):
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
            client = self._build_ws_client(mock_get_settings)
            client._handle_worktree_merge = MagicMock()
            data = self._make_card_event("worktree_merge")
            client._process_card_action_async(data)
            client._handle_worktree_merge.assert_called_once()

    def test_card_action_routes_worktree_cleanup(self):
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
            client = self._build_ws_client(mock_get_settings)
            client._handle_worktree_cleanup = MagicMock()
            data = self._make_card_event("worktree_cleanup")
            client._process_card_action_async(data)
            client._handle_worktree_cleanup.assert_called_once()

    def test_worktree_actions_in_system_card_whitelist(self):
        """All worktree card actions should be in the _is_system_card_action whitelist."""
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
            client = self._build_ws_client(mock_get_settings)
            worktree_actions = [
                "show_worktree_menu",
                "worktree_finish_selection",
                "worktree_select_tool",
                "worktree_select_model",
                "worktree_confirm_start",
                "worktree_execute_action",
                "worktree_merge",
                "worktree_cleanup",
            ]
            for action in worktree_actions:
                data = self._make_card_event(action)
                self.assertTrue(
                    client._is_system_card_action(data),
                    f"{action} should be in system card action whitelist",
                )

    def test_process_with_intent_routes_to_worktree_execute_when_awaiting_goal(self):
        """当 is_awaiting_goal 返回 True 时，应拦截消息并走 worktree 执行路径。"""

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
            client = self._build_ws_client(mock_get_settings)

            # 避免进入编程模式 / Deep / Loop / Spec / 拦截命令等其他分支
            client._get_effective_mode = MagicMock(return_value=(MagicMock(), False))
            client._is_deep_command = MagicMock(return_value=False)
            client._is_loop_command = MagicMock(return_value=False)
            client._is_spec_command = MagicMock(return_value=False)
            client._is_interceptable_command = MagicMock(return_value=False)
            client._is_exit_command = MagicMock(return_value=False)

            client._add_reaction = MagicMock()
            client._handle_worktree_execute = MagicMock()

            # Intent 路径打桩，确保不会真的走复杂逻辑
            client._intent_recognizer = MagicMock()
            client._execute_multi_tasks = MagicMock()
            client._execute_single_task = MagicMock()

            state = WorktreeRuntimeState()
            project = SimpleNamespace(project_id="p1", worktree_state=state)

            with patch("src.feishu.ws_client.WorktreeManager.is_awaiting_goal", return_value=True) as mock_flag:
                client._process_with_intent("m1", "c1", "实现登录功能", project, shell_fast_tracked=False)

            mock_flag.assert_called_once_with(state)
            client._handle_worktree_execute.assert_called_once_with("m1", "c1", "实现登录功能", project)
            client._add_reaction.assert_called_once()
            client._intent_recognizer.recognize.assert_not_called()

    def test_process_with_intent_skips_worktree_execute_when_not_awaiting_goal(self):
        """当 is_awaiting_goal 为 False 时，不应走 worktree 执行分支，而是进入正常意图识别。"""

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
            client = self._build_ws_client(mock_get_settings)

            client._get_effective_mode = MagicMock(return_value=(MagicMock(), False))
            client._is_deep_command = MagicMock(return_value=False)
            client._is_loop_command = MagicMock(return_value=False)
            client._is_spec_command = MagicMock(return_value=False)
            client._is_interceptable_command = MagicMock(return_value=False)
            client._is_exit_command = MagicMock(return_value=False)

            client._add_reaction = MagicMock()
            client._handle_worktree_execute = MagicMock()

            # Stub intent result，确保后续路径可安全运行
            intent_result = SimpleNamespace(
                primary_intent=SimpleNamespace(value="shell"),
                confidence=0.9,
                tasks=[],
                is_multi_task=False,
            )
            client._intent_recognizer = MagicMock()
            client._intent_recognizer.recognize.return_value = intent_result
            client._execute_multi_tasks = MagicMock()
            client._execute_single_task = MagicMock()

            state = WorktreeRuntimeState()
            project = SimpleNamespace(project_id="p1", worktree_state=state)

            with patch("src.feishu.ws_client.WorktreeManager.is_awaiting_goal", return_value=False) as mock_flag:
                client._process_with_intent("m2", "c1", "实现登录功能", project, shell_fast_tracked=False)

            mock_flag.assert_called_once_with(state)
            client._handle_worktree_execute.assert_not_called()
            client._intent_recognizer.recognize.assert_called_once()

    def test_show_tools_status_last_used_uses_timeago_bucket(self):
        """show_tools_status 应基于 TimeAgo 语义层渲染 last_used 文案。"""

        handler = self._build_system_handler()
        handler.reply_interactive_card = MagicMock()

        now = time.time()
        # 模拟 coco manager 有一个在 5 分钟前活跃的会话
        coco_sessions = [
            {
                "session_key": "chat-1:_default_",
                "chat_id": "chat-1",
                "session_id": "s1",
                "last_active": now - 300,
                "message_count": 3,
            }
        ]

        handler.ctx.coco_manager.list_active_sessions.return_value = coco_sessions
        # 其他工具无活跃会话
        for name in ("claude_manager", "aiden_manager", "codex_manager", "gemini_manager"):
            getattr(handler.ctx, name).list_active_sessions.return_value = []

        # 拦截 CardBuilder.build_tools_status_card 以检查传入的 tools 数据
        with (
            patch("src.feishu.handlers.system.tool_registry") as mock_registry,
            patch("src.feishu.handlers.system.CardBuilder.build_tools_status_card") as mock_build_card,
            patch("src.feishu.handlers.system.time.time", return_value=now),
        ):
            mock_registry.get_availability.return_value = True

            mock_build_card.return_value = ("interactive", "{}")

            handler.show_tools_status("msg-1", "chat-1", project=None)

            assert mock_build_card.called
            tools_arg = mock_build_card.call_args[0][0]
            coco_tool = next(t for t in tools_arg if t["name"] == "coco")

            # 5 分钟前：应使用分钟+秒模板，分钟为 5，秒为 0
            expected = UI_TEXT["time_mins_secs_ago"].format(minutes=5, seconds=0)
            assert coco_tool["last_used"] == expected

    def test_handle_worktree_confirm_start_with_input_goal(self):
        """handle_worktree_confirm_start should trigger execution if goal input is present."""
        handler = self._build_worktree_handler()
        project = MagicMock()
        project.project_id = "p1"
        handler.project_manager.get_project.return_value = project
        handler.project_manager.get_project_for_chat.return_value = project
    
        handler._worktree_manager = MagicMock()
        mock_state = MagicMock()
        mock_state.last_error = None
        mock_state.units = [MagicMock()]
        mock_state.selection.selected_items = []
        handler._worktree_manager().ensure_worktrees.return_value = mock_state
        handler._worktree_manager().get_state.return_value = mock_state
    
        handler.handle_worktree_execute = MagicMock()
    
        value = {"action": "worktree_confirm_start", "worktree_goal": "Refactor everything"}
        handler.handle_worktree_confirm_start("msg-1", "chat-1", project_id="p1", value=value)
    
        handler.handle_worktree_execute.assert_called_once_with("msg-1", "chat-1", "Refactor everything", project=project)

    def _build_system_handler(self):
        """Build a minimally-mocked SystemHandler for direct method tests."""
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.message_callback = MagicMock()
        ctx.project_manager = MagicMock()
        handler = SystemHandler(ctx)
        # Prevent actual Feishu API calls
        handler.reply_message = MagicMock()
        handler.reply_error = MagicMock()
        handler.patch_message = MagicMock()
        handler.send_error_card = MagicMock()
        return handler

    def _build_worktree_handler(self):
        """Build a minimally-mocked WorktreeHandler for direct method tests."""
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.message_callback = MagicMock()
        ctx.project_manager = MagicMock()
        handler = WorktreeHandler(ctx)
        # Prevent actual Feishu API calls
        handler.reply_message = MagicMock()
        handler.reply_error = MagicMock()
        handler.patch_message = MagicMock()
        handler.send_error_card = MagicMock()
        return handler

    @staticmethod
    def _find_all_buttons(elements):
        """递归从 elements 树中提取所有 button。"""
        buttons = []
        for el in elements:
            if el.get("tag") == "button":
                buttons.append(el)
            elif el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    buttons.extend(TestWorktreeCommandRouting._find_all_buttons(col.get("elements", [])))
            elif el.get("tag") == "column":
                buttons.extend(TestWorktreeCommandRouting._find_all_buttons(col.get("elements", [])))
        return buttons

    def test_handle_worktree_command_sends_card(self):
        """handle_worktree_command sends an interactive card with tool buttons."""
        handler = self._build_worktree_handler()

        project = MagicMock()
        project.project_id = "proj-42"
        handler.project_manager.get_active_project.return_value = project

        fake_tools = [
            {
                "provider": "acp", "tool_name": "coco", "display_name": "Coco",
                "supports_model": False, "description": "AI", "model_optional": False,
            },
            {
                "provider": "cli", "tool_name": "claude", "display_name": "Claude",
                "supports_model": False, "description": "CLI", "model_optional": False,
            },
        ]
        handler._get_available_worktree_tools = MagicMock(return_value=fake_tools)

        handler.handle_worktree_command("msg-1", "chat-1")

        handler.reply_message.assert_called_once()
        _args, _kwargs = handler.reply_message.call_args
        card_json = _args[1]  # (message_id, content, ...)
        card = json.loads(card_json)

        # Verify card structure
        self.assertEqual(card["header"]["title"]["content"], "🌳 Worktree — 选择工具")
        elements = card["body"]["elements"]
        buttons = self._find_all_buttons(elements)
        tool_buttons = [b for b in buttons if b.get("value", {}).get("action") == "worktree_select_tool"]
        self.assertEqual(len(tool_buttons), 2)
        for btn in tool_buttons:
            self.assertEqual(btn["tag"], "button")
            self.assertEqual(btn["value"]["action"], "worktree_select_tool")
            self.assertEqual(btn["value"]["project_id"], "proj-42")

    def test_handle_worktree_command_no_project_error(self):
        """handle_worktree_command replies error when no active project."""
        handler = self._build_worktree_handler()
        handler.project_manager.get_active_project.return_value = None

        handler.handle_worktree_command("msg-1", "chat-1")

        handler.reply_error.assert_called_once()
        args, _ = handler.reply_error.call_args
        self.assertIn("项目", args[1])

    def test_handle_worktree_command_no_tools_error(self):
        """handle_worktree_command replies error when no tools available."""
        handler = self._build_worktree_handler()

        project = MagicMock()
        project.project_id = "proj-42"
        handler.project_manager.get_active_project.return_value = project
        handler._get_available_worktree_tools = MagicMock(return_value=[])

        handler.handle_worktree_command("msg-1", "chat-1")

        handler.reply_error.assert_called_once()
        args, _ = handler.reply_error.call_args
        self.assertIn("工具", args[1])

    def test_worktree_confirm_card_banner_includes_goal_and_selection_summary(self):
        """自动执行路径下的确认卡片 Banner 应包含 goal 摘要与工具/模型标签。"""

        # 构造一个典型的 selection item（包含工具与模型信息）
        item = WorktreeSelectionItem(
            provider="ttadk",
            tool_name="coco",
            display_name="Coco",
            model_name="gpt-5.1",
            model_display_name="gpt-5.1",
            supports_model=True,
        )
        selected = [item.to_dict()]

        goal = "Refactor everything"
        _, card_json = CardBuilder.build_worktree_confirm_card(
            selected,
            project_id="p1",
            message=UI_TEXT["worktree_auto_executing_banner"],
            goal=goal,
        )
        card = json.loads(card_json)

        # 提取所有 markdown 内容，定位包含自动执行 Banner 的那一块
        contents: list[str] = []
        for el in card["body"]["elements"]:
            if el.get("tag") != "column_set":
                continue
            for col in el.get("columns", []):
                for inner in col.get("elements", []):
                    if inner.get("tag") == "markdown":
                        contents.append(inner.get("content", ""))

        banner_md = next(c for c in contents if UI_TEXT["worktree_auto_executing_banner"] in c)

        # 1. 原始自动执行文案仍然存在
        assert UI_TEXT["worktree_auto_executing_banner"] in banner_md
        # 2. Goal 摘要以「...」形式出现
        assert "「Refactor everything" in banner_md
        # 3. 工具/模型标签采用 "Coco · gpt-5.1" 形式
        assert "Coco · gpt-5.1" in banner_md

    def test_worktree_confirm_card_banner_truncates_long_goal(self):
        """超长 goal 文本在 Banner 中应被安全截断。"""

        item = WorktreeSelectionItem(
            provider="ttadk",
            tool_name="coco",
            display_name="Coco",
            model_name="gpt-5.1",
            model_display_name="gpt-5.1",
            supports_model=True,
        )
        selected = [item.to_dict()]

        long_goal = "A" * 200
        _, card_json = CardBuilder.build_worktree_confirm_card(
            selected,
            project_id="p1",
            message=UI_TEXT["worktree_auto_executing_banner"],
            goal=long_goal,
        )
        card = json.loads(card_json)

        contents: list[str] = []
        for el in card["body"]["elements"]:
            if el.get("tag") != "column_set":
                continue
            for col in el.get("columns", []):
                for inner in col.get("elements", []):
                    if inner.get("tag") == "markdown":
                        contents.append(inner.get("content", ""))

        banner_md = next(c for c in contents if UI_TEXT["worktree_auto_executing_banner"] in c)

        # 根据 _shorten_goal_for_banner 的策略，期望形如 "AAA..." 的截断结果
        expected_goal_snippet = "A" * 77 + "..."
        assert expected_goal_snippet in banner_md
        # 不应包含连续 100 个 'A'，以避免未截断的长文案
        assert "A" * 100 not in banner_md

    def test_worktree_confirm_card_banner_handles_empty_selection_gracefully(self):
        """当 selected_items 为空时，Banner 不应因工具信息缺失而报错。"""

        goal = "Do something"
        _, card_json = CardBuilder.build_worktree_confirm_card(
            [],
            project_id="p1",
            message=UI_TEXT["worktree_auto_executing_banner"],
            goal=goal,
        )
        card = json.loads(card_json)

        contents: list[str] = []
        for el in card["body"]["elements"]:
            if el.get("tag") != "column_set":
                continue
            for col in el.get("columns", []):
                for inner in col.get("elements", []):
                    if inner.get("tag") == "markdown":
                        contents.append(inner.get("content", ""))

        banner_md = next(c for c in contents if UI_TEXT["worktree_auto_executing_banner"] in c)

        # Goal 摘要仍然存在
        assert "「Do something" in banner_md
        # 但由于没有选择任何工具，"使用：" 行不会出现
        assert "使用：" not in banner_md


    def test_show_tools_status_active_sessions_use_parsed_chat_id_field(self):
        """show_tools_status 应优先使用 list_active_sessions 暴露的 chat_id 字段。"""

        handler = self._build_system_handler()
        handler.reply_interactive_card = MagicMock()

        now = time.time()
        coco_sessions = [
            {
                "session_key": "chat-xyz:_default_",
                "chat_id": "chat-xyz",
                "session_id": "sess-1",
                "last_active": now - 10,
                "message_count": 1,
            }
        ]

        handler.ctx.coco_manager.list_active_sessions.return_value = coco_sessions
        for name in ("claude_manager", "aiden_manager", "codex_manager", "gemini_manager"):
            getattr(handler.ctx, name).list_active_sessions.return_value = []

        with (
            patch("src.feishu.handlers.system.tool_registry") as mock_registry,
            patch("src.feishu.handlers.system.CardBuilder.build_tools_status_card") as mock_build_card,
            patch("src.feishu.handlers.system.time.time", return_value=now),
        ):
            mock_registry.get_availability.return_value = True
            mock_build_card.return_value = ("interactive", "{}")

            handler.show_tools_status("msg-2", "chat-xyz", project=None)

            assert mock_build_card.called
            _tools_arg, active_sessions_arg, _project = mock_build_card.call_args[0]
            coco_active = active_sessions_arg.get("coco")
            assert coco_active is not None
            assert coco_active["chat_id"] == "chat-xyz"
