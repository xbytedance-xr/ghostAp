import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.feishu.handlers.system import SystemHandler
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

    def test_handle_worktree_confirm_start_with_input_goal(self):
        """handle_worktree_confirm_start should trigger execution if goal input is present."""
        handler = self._build_system_handler()
        project = MagicMock()
        handler.project_manager.get_project.return_value = project
        
        handler._worktree_manager = MagicMock()
        mock_state = MagicMock()
        mock_state.last_error = None
        mock_state.units = [MagicMock()]
        handler._worktree_manager().ensure_worktrees.return_value = mock_state
        
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

    def test_handle_worktree_command_sends_card(self):
        """handle_worktree_command sends an interactive card with tool buttons."""
        handler = self._build_system_handler()

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
        action_elements = [e for e in elements if e.get("tag") == "action"]
        self.assertEqual(len(action_elements), 1)

        buttons = action_elements[0]["actions"]
        self.assertEqual(len(buttons), 2)
        for btn in buttons:
            self.assertEqual(btn["tag"], "button")
            self.assertEqual(btn["value"]["action"], "worktree_select_tool")
            self.assertEqual(btn["value"]["project_id"], "proj-42")

    def test_handle_worktree_command_no_project_error(self):
        """handle_worktree_command replies error when no active project."""
        handler = self._build_system_handler()
        handler.project_manager.get_active_project.return_value = None

        handler.handle_worktree_command("msg-1", "chat-1")

        handler.reply_error.assert_called_once()
        args, _ = handler.reply_error.call_args
        self.assertIn("项目", args[1])

    def test_handle_worktree_command_no_tools_error(self):
        """handle_worktree_command replies error when no tools available."""
        handler = self._build_system_handler()

        project = MagicMock()
        project.project_id = "proj-42"
        handler.project_manager.get_active_project.return_value = project
        handler._get_available_worktree_tools = MagicMock(return_value=[])

        handler.handle_worktree_command("msg-1", "chat-1")

        handler.reply_error.assert_called_once()
        args, _ = handler.reply_error.call_args
        self.assertIn("工具", args[1])
