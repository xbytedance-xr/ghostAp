"""Unit tests for /model command in SystemHandler.

Covers:
- /model (no args) → show model card
- /model list → show model card
- /model <name> → direct switch
- /model switch <name> → direct switch
- /model (bad args) → error message
- is_interceptable_command recognises /model variants
- _resolve_current_acp_tool priority logic
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from src.feishu.handlers.system import SystemHandler
from src.feishu.slash_command_parser import SlashCommandParser


def _make_handler():
    """Build a minimal SystemHandler with all dependencies mocked."""
    ctx = MagicMock()
    ctx.settings.app_id = "test"
    ctx.settings.app_secret = "secret"
    ctx.mode_manager.is_coco_mode.return_value = False
    ctx.mode_manager.is_claude_mode.return_value = False
    ctx.mode_manager.is_aiden_mode.return_value = False
    ctx.mode_manager.is_codex_mode.return_value = False
    ctx.mode_manager.is_gemini_mode.return_value = False
    ctx.project_manager.get_active_project.return_value = None
    ctx.working_dirs = {}

    ctx.handlers = {
        "coco": MagicMock(),
        "claude": MagicMock(),
        "aiden": MagicMock(),
        "codex": MagicMock(),
        "gemini": MagicMock(),
    }

    handler = SystemHandler(ctx)
    handler.reply_card = MagicMock()
    handler.reply_text = MagicMock()
    handler.reply_error = MagicMock()
    handler.get_working_dir = MagicMock(return_value="/tmp")
    return handler


# ---------------------------------------------------------------------------
# is_interceptable_command
# ---------------------------------------------------------------------------
class TestIsInterceptableCommand(unittest.TestCase):
    def test_model_exact(self):
        self.assertTrue(SystemHandler.is_interceptable_command_match(SlashCommandParser.parse("/model")))

    def test_model_list(self):
        self.assertTrue(SystemHandler.is_interceptable_command_match(SlashCommandParser.parse("/model list")))

    def test_model_name(self):
        self.assertTrue(SystemHandler.is_interceptable_command_match(SlashCommandParser.parse("/model gpt-5.2")))

    def test_model_switch(self):
        self.assertTrue(
            SystemHandler.is_interceptable_command_match(
                SlashCommandParser.parse("/model switch claude-3.7-sonnet")
            )
        )

    def test_model_upper_case(self):
        # case-insensitive check
        self.assertTrue(SystemHandler.is_interceptable_command_match(SlashCommandParser.parse("/Model")))

    def test_unrelated_not_matched(self):
        self.assertFalse(SystemHandler.is_interceptable_command_match(SlashCommandParser.parse("/mode")))
        self.assertFalse(SystemHandler.is_interceptable_command_match(SlashCommandParser.parse("model")))


# ---------------------------------------------------------------------------
# _resolve_current_acp_tool
# ---------------------------------------------------------------------------
class TestResolveCurrentAcpTool(unittest.TestCase):
    def setUp(self):
        self.handler = _make_handler()

    def test_returns_coco_by_default(self):
        tool = self.handler._resolve_current_acp_tool("chat1")
        self.assertEqual(tool, "coco")

    def test_returns_acp_tool_from_project(self):
        project = MagicMock()
        project.acp_tool_name = "aiden"
        tool = self.handler._resolve_current_acp_tool("chat1", project=project)
        self.assertEqual(tool, "aiden")

    def test_returns_tool_from_mode_manager_coco(self):
        self.handler.mode_manager.is_coco_mode.return_value = True
        tool = self.handler._resolve_current_acp_tool("chat1")
        self.assertEqual(tool, "coco")

    def test_returns_tool_from_mode_manager_aiden(self):
        self.handler.mode_manager.is_aiden_mode.return_value = True
        tool = self.handler._resolve_current_acp_tool("chat1")
        self.assertEqual(tool, "aiden")

    def test_project_takes_priority_over_mode(self):
        self.handler.mode_manager.is_aiden_mode.return_value = True
        project = MagicMock()
        project.acp_tool_name = "codex"
        tool = self.handler._resolve_current_acp_tool("chat1", project=project)
        self.assertEqual(tool, "codex")


# ---------------------------------------------------------------------------
# handle_model_command — list / show card
# ---------------------------------------------------------------------------
class TestHandleModelCommandList(unittest.TestCase):
    def setUp(self):
        self.handler = _make_handler()
        # Provide fake model options
        self._fake_models = [
            MagicMock(name="gpt-5.2", description="GPT-5.2", is_default=True),
            MagicMock(name="claude-3.7-sonnet", description="Claude 3.7 Sonnet", is_default=False),
        ]
        for m in self._fake_models:
            m.name = m._mock_name

    def _patch_fetch(self, models):
        return patch.object(self.handler, "_fetch_acp_models", return_value=models)

    def test_model_no_args_shows_card(self):
        fake_models = [
            MagicMock(name="gpt-5.2", description="GPT-5.2", is_default=True),
        ]
        fake_models[0].name = "gpt-5.2"
        with self._patch_fetch(fake_models):
            self.handler.handle_model_command("msg1", "chat1", "/model")
        # Should send an interactive card via reply_card
        self.handler.reply_card.assert_called_once()

    def test_model_list_shows_card(self):
        fake_models = [
            MagicMock(name="gpt-5.2", description="GPT-5.2", is_default=True),
        ]
        fake_models[0].name = "gpt-5.2"
        with self._patch_fetch(fake_models):
            self.handler.handle_model_command("msg1", "chat1", "/model list")
        self.handler.reply_card.assert_called_once()

    def test_model_ls_shows_card(self):
        fake_models = [MagicMock()]
        fake_models[0].name = "gpt-5.2"
        fake_models[0].description = "GPT-5.2"
        fake_models[0].is_default = True
        with self._patch_fetch(fake_models):
            self.handler.handle_model_command("msg1", "chat1", "/model ls")
        self.handler.reply_card.assert_called_once()

    def test_model_list_card_contains_tool_name(self):
        fake_models = [MagicMock()]
        fake_models[0].name = "gpt-5.2"
        fake_models[0].description = "GPT-5.2"
        fake_models[0].is_default = True
        with self._patch_fetch(fake_models):
            self.handler.handle_model_command("msg1", "chat1", "/model list")
        card_str = self.handler.reply_card.call_args[0][1]
        card = json.loads(card_str)
        # card title should mention "coco" (the default tool)
        title = card["header"]["title"]["content"].lower()
        self.assertIn("coco", title)

    def test_model_list_card_carries_thread_root_id(self):
        fake_models = [MagicMock()]
        fake_models[0].name = "gpt-5.2"
        fake_models[0].description = "GPT-5.2"
        fake_models[0].is_default = True
        with self._patch_fetch(fake_models), patch("src.thread.get_current_thread_id", return_value="thread1"):
            self.handler.handle_model_command("msg1", "chat1", "/model list")

        card_str = self.handler.reply_card.call_args[0][1]
        card = json.loads(card_str)
        values = []
        for element in card["body"]["elements"]:
            for column in element.get("columns", []):
                for child in column.get("elements", []):
                    if child.get("tag") == "button":
                        values.append(child.get("value", {}))

        self.assertTrue(values)
        self.assertTrue(all(v.get("thread_root_id") == "thread1" for v in values))

    def test_model_list_error_when_no_models(self):
        with self._patch_fetch([]):
            self.handler.handle_model_command("msg1", "chat1", "/model list")
        self.handler.reply_error.assert_called_once()


# ---------------------------------------------------------------------------
# handle_model_command — direct switch
# ---------------------------------------------------------------------------
class TestHandleModelCommandSwitch(unittest.TestCase):
    def setUp(self):
        self.handler = _make_handler()

    def test_model_name_calls_enter_mode(self):
        with patch.object(self.handler, "_enter_mode_with_acp_model") as mock_enter:
            self.handler.handle_model_command("msg1", "chat1", "/model gpt-5.2")
        mock_enter.assert_called_once()
        args = mock_enter.call_args[0]
        self.assertEqual(args[2], "coco")   # tool
        self.assertEqual(args[3], "gpt-5.2")  # model

    def test_model_switch_subcommand(self):
        with patch.object(self.handler, "_enter_mode_with_acp_model") as mock_enter:
            self.handler.handle_model_command("msg1", "chat1", "/model switch claude-3.7-sonnet")
        mock_enter.assert_called_once()
        args = mock_enter.call_args[0]
        self.assertEqual(args[3], "claude-3.7-sonnet")

    def test_model_switch_with_aiden_tool(self):
        self.handler.mode_manager.is_aiden_mode.return_value = True
        with patch.object(self.handler, "_enter_mode_with_acp_model") as mock_enter:
            self.handler.handle_model_command("msg1", "chat1", "/model gpt-5.2")
        mock_enter.assert_called_once()
        args = mock_enter.call_args[0]
        self.assertEqual(args[2], "aiden")

    def test_model_empty_name_shows_error(self):
        # /model switch (no name) → error
        with patch.object(self.handler, "_enter_mode_with_acp_model") as mock_enter:
            self.handler.handle_model_command("msg1", "chat1", "/model switch")
        mock_enter.assert_not_called()
        self.handler.reply_error.assert_called_once()

    def test_model_switch_does_not_send_progress_card(self):
        with patch.object(self.handler, "_enter_mode_with_acp_model"):
            self.handler.handle_model_command("msg1", "chat1", "/model gpt-5.2")
        self.handler.reply_card.assert_not_called()


class TestHandleSelectAcpModelPendingPrompt(unittest.TestCase):
    def setUp(self):
        self.handler = _make_handler()

    def test_pending_prompt_starts_thread_instead_of_top_level_mode(self):
        self.handler.settings.thread_programming_enabled = True
        self.handler._stash_pending_prompt("chat1", "coco", "这个项目是干什么的")

        project = MagicMock()
        project.project_id = "ghostap"
        project.project_name = "ghostAp"
        project.root_path = "/repo/ghostAp"
        project.theme_color = "blue"

        coco_handler = self.handler.get_handler("coco")
        coco_handler.mode_name = "Coco"
        coco_handler.mode_emoji = "💭"
        coco_handler.reply_card.return_value = "thread1"
        coco_handler._get_session_manager.return_value.get_session.return_value = MagicMock()

        with patch("src.thread.get_current_thread_id", return_value=None):
            self.handler.handle_select_acp_model("msg1", "chat1", "coco", "gpt-5.2", project)

        self.handler.reply_card.assert_not_called()
        coco_handler.enter_mode.assert_called_once_with(
            "thread1",
            "chat1",
            silent=True,
            project=project,
            thread_id="thread1",
        )
        coco_handler.handle_message.assert_called_once_with(
            "msg1",
            "chat1",
            "这个项目是干什么的",
            project,
        )
