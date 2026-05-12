import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.feishu.handlers.system import SystemHandler
from src.feishu.handlers.worktree import WorktreeHandler
from src.card.ui_text import UI_TEXT
from src.card.builder import CardBuilder
from src.feishu.slash_command_parser import SlashCommandParser
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import (
    WorktreeJourneyStatus,
    WorktreeRuntimeState,
    WorktreeUnit,
    WorktreeUnitStatus,
)
from src.feishu.ws_client import FeishuWSClient


class TestWorktreeCommandRouting(unittest.TestCase):
    def test_system_handler_recognizes_worktree_commands(self):
        m = SlashCommandParser.parse
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/wt")))
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/worktree")))
        self.assertFalse(SystemHandler.is_interceptable_command_match(m("/wt-extra")))

    def test_system_handler_recognizes_worktree_commands_with_common_whitespace(self):
        m = SlashCommandParser.parse
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("  /WT  ")))
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/wt\t实现登录功能")))
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/worktree\t实现登录功能")))

    def test_system_handler_keeps_other_slash_commands_interceptable(self):
        m = SlashCommandParser.parse
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/help")))
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/status\tall")))
        self.assertTrue(SystemHandler.is_interceptable_command_match(m("/model\tgpt-5")))
        self.assertFalse(SystemHandler.is_interceptable_command_match(m("/wt-extra")))

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
            mock_settings.message_cache_ttl = 300
            mock_settings.message_cache_max_size = 1000
            mock_settings.card.action_dedup_ttl = 1
            mock_settings.card.action_dedup_max_size = 5000
            mock_settings.system_command_concurrency = 10
            mock_settings.spec_rate_limit_capacity = 100
            mock_settings.spec_rate_limit_fill_rate = 50.0
            mock_settings.spec_circuit_breaker_threshold = 10
            mock_settings.spec_circuit_breaker_recovery = 5.0
            mock_settings.message_expire_seconds = 30
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
            mock_settings.message_cache_ttl = 300
            mock_settings.message_cache_max_size = 1000
            mock_settings.card.action_dedup_ttl = 1
            mock_settings.card.action_dedup_max_size = 5000
            mock_settings.system_command_concurrency = 10
            mock_settings.spec_rate_limit_capacity = 100
            mock_settings.spec_rate_limit_fill_rate = 50.0
            mock_settings.spec_circuit_breaker_threshold = 10
            mock_settings.spec_circuit_breaker_recovery = 5.0
            mock_settings.message_expire_seconds = 30
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
        mock_settings.message_cache_ttl = 300
        mock_settings.message_cache_max_size = 1000
        mock_settings.card.action_dedup_ttl = 1
        mock_settings.card.action_dedup_max_size = 5000
        mock_settings.system_command_concurrency = 10
        mock_settings.spec_rate_limit_capacity = 100
        mock_settings.spec_rate_limit_fill_rate = 50.0
        mock_settings.spec_circuit_breaker_threshold = 10
        mock_settings.spec_circuit_breaker_recovery = 5.0
        mock_settings.message_expire_seconds = 30
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

            # 避免进入编程模式 / Deep / Spec / 拦截命令等其他分支
            client._get_effective_mode = MagicMock(return_value=(MagicMock(), False))
            client._is_deep_command = MagicMock(return_value=False)
            client._is_spec_command = MagicMock(return_value=False)
            client._is_interceptable_command_match = MagicMock(return_value=False)
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
                command_match = SlashCommandParser.parse("实现登录功能")
                client._process_with_intent(
                    "m1",
                    "c1",
                    "实现登录功能",
                    project,
                    command_match=command_match,
                    shell_fast_tracked=False,
                )

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
            client._is_spec_command = MagicMock(return_value=False)
            client._is_interceptable_command_match = MagicMock(return_value=False)
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
                command_match = SlashCommandParser.parse("实现登录功能")
                client._process_with_intent(
                    "m2",
                    "c1",
                    "实现登录功能",
                    project,
                    command_match=command_match,
                    shell_fast_tracked=False,
                )

            mock_flag.assert_called_once_with(state)
            client._handle_worktree_execute.assert_not_called()
            client._intent_recognizer.recognize.assert_called_once()

    def test_is_awaiting_goal_returns_true_only_for_pending_with_ready_units(self):
        state = WorktreeRuntimeState()
        state.journey.status = WorktreeJourneyStatus.PENDING
        state.units = [WorktreeUnit(unit_id="u1", status=WorktreeUnitStatus.READY)]

        self.assertTrue(WorktreeManager.is_awaiting_goal(state))

    def test_is_awaiting_goal_returns_false_for_failed_journey_even_with_ready_units(self):
        state = WorktreeRuntimeState()
        state.journey.status = WorktreeJourneyStatus.FAILED
        state.units = [WorktreeUnit(unit_id="u1", status=WorktreeUnitStatus.READY)]

        self.assertFalse(WorktreeManager.is_awaiting_goal(state))

    def test_dispatch_message_logic_routes_wt_goal_to_process_with_intent_in_auto_enter_mode(self):
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
            programming_handler = MagicMock()
            client._get_mode_handler = MagicMock(return_value=programming_handler)
            client._process_with_intent = MagicMock()
            client._is_exit_command = MagicMock(return_value=False)
            client._is_programming_entry_command = MagicMock(return_value=False)
            client._is_deep_command = MagicMock(return_value=False)
            client._is_spec_command = MagicMock(return_value=False)

            client._dispatch_message_logic(
                "msg-1", "chat-1", "/wt\t实现登录功能", None, auto_enter_mode="coco", shell_fast_tracked=False
            )

            client._process_with_intent.assert_called_once_with(
                "msg-1",
                "chat-1",
                "/wt\t实现登录功能",
                None,
                command_match=SlashCommandParser.parse("/wt\t实现登录功能"),
                shell_fast_tracked=False,
            )
            programming_handler.handle_message.assert_not_called()

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

    def test_handle_intercepted_command_routes_exact_wt_to_worktree_handler(self):
        handler = self._build_system_handler()
        worktree_handler = MagicMock()
        handler.get_handler = MagicMock(return_value=worktree_handler)

        handler.handle_intercepted_command(
            "msg-1",
            "chat-1",
            "/wt",
            project=None,
            command_match=SlashCommandParser.parse("/wt"),
        )

        worktree_handler.handle_worktree_command.assert_called_once_with("msg-1", "chat-1", None)

    def test_handle_intercepted_command_routes_wt_goal_with_tab_separator(self):
        handler = self._build_system_handler()
        worktree_handler = MagicMock()
        handler.get_handler = MagicMock(return_value=worktree_handler)

        handler.handle_intercepted_command(
            "msg-1",
            "chat-1",
            "/wt\t实现登录功能",
            project=None,
            command_match=SlashCommandParser.parse("/wt\t实现登录功能"),
        )

        worktree_handler.handle_worktree_command_match.assert_called_once()
        _args, _kwargs = worktree_handler.handle_worktree_command_match.call_args
        assert _args[0] == "msg-1"
        assert _args[1] == "chat-1"
        assert getattr(_args[2], "command", None) == "/worktree"
        assert getattr(_args[2], "args", None) == "实现登录功能"

    def test_handle_intercepted_command_routes_worktree_goal_with_tab_and_uppercase(self):
        handler = self._build_system_handler()
        worktree_handler = MagicMock()
        handler.get_handler = MagicMock(return_value=worktree_handler)

        handler.handle_intercepted_command(
            "msg-1",
            "chat-1",
            "/WORKTREE\tgoal",
            project=None,
            command_match=SlashCommandParser.parse("/WORKTREE\tgoal"),
        )

        worktree_handler.handle_worktree_command_match.assert_called_once()
        _args, _kwargs = worktree_handler.handle_worktree_command_match.call_args
        assert _args[0] == "msg-1"
        assert _args[1] == "chat-1"
        assert getattr(_args[2], "command", None) == "/worktree"
        assert getattr(_args[2], "args", None) == "goal"

    def test_handle_intercepted_command_routes_switch_uses_parsed_args(self):
        """/switch 参数解析应基于 SlashCommandParser.args，而不是 text 切片。"""
        handler = self._build_system_handler()
        project_handler = MagicMock()

        def _get(key: str):
            if key == "project":
                return project_handler
            # These are passed through as kw handlers
            if key in {"coco", "claude"}:
                return MagicMock()
            return None

        handler.get_handler = MagicMock(side_effect=_get)

        handler.handle_intercepted_command(
            "msg-1",
            "chat-1",
            "/SWITCH\tproj-x",
            project=None,
            command_match=SlashCommandParser.parse("/SWITCH\tproj-x"),
        )

        project_handler.switch_project.assert_called_once()
        _args, _kwargs = project_handler.switch_project.call_args
        assert _args[:3] == ("msg-1", "chat-1", "proj-x")

    def test_handle_intercepted_command_routes_new_preserves_multi_word_path(self):
        """/new <name> <path with spaces> 应保留 path 内部空格。"""
        handler = self._build_system_handler()
        project_handler = MagicMock()
        handler.get_handler = MagicMock(return_value=project_handler)
        handler.get_working_dir = MagicMock(return_value="/cwd")

        handler.handle_intercepted_command(
            "msg-1",
            "chat-1",
            "/new proj /tmp/a b",
            project=None,
            command_match=SlashCommandParser.parse("/new proj /tmp/a b"),
        )

        project_handler.create_project.assert_called_once_with("msg-1", "chat-1", "proj", "/tmp/a b")

    def test_handle_intercepted_command_routes_close_uses_parsed_args(self):
        handler = self._build_system_handler()
        project_handler = MagicMock()
        handler.get_handler = MagicMock(return_value=project_handler)

        handler.handle_intercepted_command(
            "msg-1",
            "chat-1",
            "/close\tproj-y",
            project=None,
            command_match=SlashCommandParser.parse("/close\tproj-y"),
        )

        project_handler.close_project.assert_called_once_with("msg-1", "chat-1", "proj-y")

    def _build_system_handler(self):
        """Build a minimally-mocked SystemHandler for direct method tests."""
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.message_callback = MagicMock()
        ctx.project_manager = MagicMock()
        handler = SystemHandler(ctx)
        # Prevent actual Feishu API calls
        handler.reply_text = MagicMock()
        handler.reply_error = MagicMock()
        handler.update_card = MagicMock()
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
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler.update_card = MagicMock()
        handler.update_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
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
            elif el.get("tag") == "action":
                buttons.extend(TestWorktreeCommandRouting._find_all_buttons(el.get("actions", [])))
        return buttons

    def test_handle_worktree_command_sends_card(self):
        """handle_worktree_command dispatches WORKTREE_TOOL_SELECT event via session."""
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

        mock_session = MagicMock()
        mock_session.closed = False
        with patch.object(handler, "_get_or_create_session", return_value=mock_session):
            handler.handle_worktree_command("msg-1", "chat-1")

        mock_session.dispatch.assert_called_once()
        event = mock_session.dispatch.call_args[0][0]
        from src.card.events import CardEventType
        self.assertEqual(event.type, CardEventType.WORKTREE_TOOL_SELECT)
        self.assertEqual(event.payload["project_id"], "proj-42")
        self.assertEqual(len(event.payload["tools"]), 2)

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

    def test_handle_worktree_select_tool_dispatches_explicit_model_prompt(self):
        """选择支持模型的工具后，应进入清晰的模型选择卡。"""
        handler = self._build_worktree_handler()

        project = MagicMock()
        project.project_id = "proj-42"
        project.root_path = "/tmp/project"
        handler.project_manager.get_project_for_chat.return_value = project
        handler._get_models_for_tool = MagicMock(return_value=[
            {"name": "doubao-pro", "display_name": "Doubao Pro", "is_default": True},
        ])

        mock_session = MagicMock()
        mock_session.closed = False
        with patch.object(handler, "_get_or_create_session", return_value=mock_session):
            handler.handle_worktree_select_tool(
                "msg-1",
                "chat-1",
                project_id="proj-42",
                value={
                    "provider": "acp",
                    "tool_name": "coco",
                    "display_name": "Coco",
                    "supports_model": True,
                },
            )

        mock_session.dispatch.assert_called_once()
        event = mock_session.dispatch.call_args[0][0]
        self.assertEqual(event.payload["select_action"], "worktree_select_model")
        self.assertEqual(event.payload["message"], "为 Coco 选择模型：")

    def test_handle_worktree_prefix_command_with_goal_dispatches_visible_start_feedback(self):
        """带 goal 的 /wt 前缀命令应启动选择流并下发可见卡片反馈。"""
        handler = self._build_worktree_handler()

        project = MagicMock()
        project.project_id = "proj-42"
        fake_tools = [
            {
                "provider": "acp", "tool_name": "coco", "display_name": "Coco",
                "supports_model": False, "description": "AI", "model_optional": False,
            }
        ]
        handler._get_available_worktree_tools = MagicMock(return_value=fake_tools)

        mock_mgr = MagicMock()
        mock_state = MagicMock()
        mock_state.selection.selected_items = []
        mock_mgr.get_state.return_value = mock_state
        handler._worktree_manager = MagicMock(return_value=mock_mgr)

        mock_session = MagicMock()
        mock_session.closed = False
        with patch.object(handler, "_get_or_create_session", return_value=mock_session):
            handler.handle_worktree_prefix_command("msg-1", "chat-1", "/wt\t实现登录功能", project)

        mock_mgr.start_selection.assert_called_once_with(project, goal="实现登录功能")
        mock_session.dispatch.assert_called_once()

    def test_handle_worktree_prefix_command_returns_error_when_project_missing(self):
        """带 goal 的 /wt 前缀命令在无项目时必须返回显式错误。"""
        handler = self._build_worktree_handler()
        handler.project_manager.get_active_project.return_value = None

        handler.handle_worktree_prefix_command("msg-1", "chat-1", "/wt\t实现登录功能")

        handler.reply_error.assert_called_once()
        args, _ = handler.reply_error.call_args
        self.assertIn("项目", args[1])

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


class TestSlashCommandParser(unittest.TestCase):
    def test_parse_worktree_alias_and_args(self):
        m = SlashCommandParser.parse("  /WT\t实现登录功能  ")
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.raw_command, "/wt")
        self.assertEqual(m.command, "/worktree")
        self.assertEqual(m.args, "实现登录功能")
        self.assertTrue(m.has_args)

    def test_parse_worktree_canonical_and_args(self):
        m = SlashCommandParser.parse("/WORKTREE\tgoal")
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.command, "/worktree")
        self.assertEqual(m.args, "goal")
        self.assertTrue(m.has_args)

    def test_parse_non_command_returns_none(self):
        self.assertIsNone(SlashCommandParser.parse(""))
        self.assertIsNone(SlashCommandParser.parse("hello"))

    def test_match_filters_allowed_commands(self):
        self.assertIsNotNone(SlashCommandParser.match("/wt\tgoal", allowed_commands=["/worktree"]))
        self.assertIsNone(SlashCommandParser.match("/wt\tgoal", allowed_commands=["/status"]))
