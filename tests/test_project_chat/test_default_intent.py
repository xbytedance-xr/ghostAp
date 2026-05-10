"""Tests for project-chat default-Coco routing and slash priority.

Covers the behavior introduced to resolve the "项目群自由文本默认走 Coco"
requirement while preserving highest priority for slash commands and
preserving shell-like fast-track.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.agent.intent_recognizer import IntentRecognizer
from src.feishu.slash_command_parser import SlashCommandParser
from src.feishu.ws_client import FeishuWSClient
from src.project.manager import ProjectManager


class TestFindByBoundChatId(unittest.TestCase):
    """ProjectManager.find_by_bound_chat_id reverse index maintenance."""

    def _mgr(self, tmp_path):
        return ProjectManager(storage_path=str(tmp_path / "projects.json"))

    def test_find_returns_none_before_binding(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            mgr = self._mgr(pathlib.Path(td))
            self.assertIsNone(mgr.find_by_bound_chat_id("oc_nope"))

    def test_find_returns_project_after_binding(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            mgr = self._mgr(pathlib.Path(td))
            ok, _, ctx = mgr.create_project(None, "proj", td, chat_id="oc_main")
            assert ok and ctx is not None
            ctx.bound_chat_id = "oc_group_1"
            mgr._save_projects()  # triggers reverse-index rebuild

            hit = mgr.find_by_bound_chat_id("oc_group_1")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.project_id, ctx.project_id)

    def test_find_returns_none_after_close(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            mgr = self._mgr(pathlib.Path(td))
            ok, _, ctx = mgr.create_project(None, "proj", td, chat_id="oc_main")
            assert ok and ctx is not None
            ctx.bound_chat_id = "oc_group_1"
            mgr._save_projects()
            mgr.close_project(ctx.project_id)
            self.assertIsNone(mgr.find_by_bound_chat_id("oc_group_1"))

    def test_empty_chat_id_returns_none(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            mgr = self._mgr(pathlib.Path(td))
            self.assertIsNone(mgr.find_by_bound_chat_id(""))


class TestIntentRecognizerLooksLikeShell(unittest.TestCase):
    """looks_like_shell heuristic used by project-chat routing."""

    def setUp(self):
        self.ir = IntentRecognizer.__new__(IntentRecognizer)
        # Only settings access is needed; stub with a MagicMock
        self.ir.settings = MagicMock()

    def test_shell_whitelist_matches(self):
        for text in ("ls", "ls -la", "git status", "cd /tmp"):
            self.assertTrue(self.ir.looks_like_shell(text), text)

    def test_command_like_token_matches(self):
        self.assertTrue(self.ir.looks_like_shell("mytool --help"))

    def test_natural_language_does_not_match(self):
        for text in ("帮我重构这个模块", "请帮我写一个函数", "我想做一个 API"):
            self.assertFalse(self.ir.looks_like_shell(text), text)

    def test_empty_does_not_match(self):
        self.assertFalse(self.ir.looks_like_shell(""))
        self.assertFalse(self.ir.looks_like_shell("   "))


def _make_client():
    """Build a FeishuWSClient with all heavy deps stubbed out."""
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
        mock_settings.app_id = "app"
        mock_settings.app_secret = "sec"
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
        mock_settings.thread_programming_enabled = True
        mock_get_settings.return_value = mock_settings
        return FeishuWSClient(MagicMock())


class TestProjectChatDefaultCocoRouting(unittest.TestCase):
    """Priority chain in _dispatch_message_logic for project chats."""

    def setUp(self):
        self.client = _make_client()
        self.client._add_reaction = MagicMock()
        self.client._process_with_intent = MagicMock()
        self.client._message_dispatcher = MagicMock()
        # By default make looks_like_shell predictable via real IR logic stub
        self.client._intent_recognizer = MagicMock()
        self.client._intent_recognizer.looks_like_shell.return_value = False
        # Bound project for a specific chat id
        self.bound_project = MagicMock()
        self.bound_project.project_id = "pid_bound"

        def _find(cid):
            return self.bound_project if cid == "oc_project" else None

        self.client._project_manager.find_by_bound_chat_id.side_effect = _find

    def test_free_text_in_project_chat_routes_to_handle_enter_coco_with_pending_prompt(self):
        self.client._dispatch_message_logic(
            "m1", "oc_project", "帮我重构 foo.py",
            project=self.bound_project, auto_enter_mode=None,
            command_match=None, is_image_only=False,
        )
        self.client._message_dispatcher._handle_enter_coco.assert_called_once()
        _, kwargs = self.client._message_dispatcher._handle_enter_coco.call_args
        self.assertEqual(kwargs.get("pending_prompt"), "帮我重构 foo.py")
        self.client._process_with_intent.assert_not_called()

    def test_slash_command_in_project_chat_goes_to_intent_not_coco(self):
        match = SlashCommandParser.parse("/help")
        self.client._dispatch_message_logic(
            "m1", "oc_project", "/help",
            project=self.bound_project, auto_enter_mode=None,
            command_match=match, is_image_only=False,
        )
        self.client._message_dispatcher._handle_enter_coco.assert_not_called()
        self.client._process_with_intent.assert_called_once()

    def test_slash_coco_in_project_chat_goes_to_intent_not_auto_coco(self):
        match = SlashCommandParser.parse("/coco")
        self.client._dispatch_message_logic(
            "m1", "oc_project", "/coco",
            project=self.bound_project, auto_enter_mode=None,
            command_match=match, is_image_only=False,
        )
        self.client._message_dispatcher._handle_enter_coco.assert_not_called()
        self.client._process_with_intent.assert_called_once()

    def test_shell_like_text_in_project_chat_falls_through_to_intent(self):
        self.client._intent_recognizer.looks_like_shell.return_value = True
        self.client._dispatch_message_logic(
            "m1", "oc_project", "ls -la",
            project=self.bound_project, auto_enter_mode=None,
            command_match=None, is_image_only=False,
        )
        self.client._message_dispatcher._handle_enter_coco.assert_not_called()
        self.client._process_with_intent.assert_called_once()

    def test_non_project_chat_free_text_goes_to_intent(self):
        self.client._dispatch_message_logic(
            "m1", "oc_other", "帮我重构 foo.py",
            project=None, auto_enter_mode=None,
            command_match=None, is_image_only=False,
        )
        self.client._message_dispatcher._handle_enter_coco.assert_not_called()
        self.client._process_with_intent.assert_called_once()

    def test_image_only_in_project_chat_not_intercepted_by_auto_coco(self):
        self.client._dispatch_message_logic(
            "m1", "oc_project", "",
            project=self.bound_project, auto_enter_mode=None,
            command_match=None, is_image_only=True,
        )
        self.client._message_dispatcher._handle_enter_coco.assert_not_called()
        self.client._process_with_intent.assert_called_once()

    def test_auto_enter_mode_coco_bypasses_auto_coco_branch(self):
        """When user is already in Coco (auto_enter_mode='coco'), messages go
        to the mode handler, not through _handle_enter_coco again."""
        handler = MagicMock()
        self.client._get_mode_handler = MagicMock(return_value=handler)
        self.client._dispatch_message_logic(
            "m1", "oc_project", "继续写",
            project=self.bound_project, auto_enter_mode="coco",
            command_match=None, is_image_only=False,
        )
        handler.handle_message.assert_called_once()
        self.client._message_dispatcher._handle_enter_coco.assert_not_called()


class TestProjectChatContextResolution(unittest.TestCase):
    """Project-bound group chats should resolve their one-to-one project.

    This covers slash/system commands such as /status: unlike free-form text,
    they do not pass through the default-Coco project-chat branch, so project
    context must be resolved before intent dispatch.
    """

    def test_resolve_project_from_message_falls_back_to_bound_project_chat(self):
        client = _make_client()
        bound_project = MagicMock()
        bound_project.project_id = "pid_bound"

        client._project_manager.find_by_bound_chat_id.return_value = bound_project
        client._project_manager.get_active_project.return_value = None

        project, auto_mode = client._resolve_project_from_message("m1", "oc_project", parent_id=None)

        self.assertIs(project, bound_project)
        self.assertIsNone(auto_mode)

    def test_resolve_project_from_message_keeps_parent_reference_priority(self):
        client = _make_client()
        referenced_project = MagicMock()
        referenced_project.project_id = "pid_ref"
        bound_project = MagicMock()
        bound_project.project_id = "pid_bound"

        client._message_mapper.get_project_id.return_value = "pid_ref"
        client._project_manager.get_project_for_chat.return_value = referenced_project
        client._project_manager.find_by_bound_chat_id.return_value = bound_project
        client._mode_manager.get_mode.return_value.value = "smart"

        project, auto_mode = client._resolve_project_from_message("m1", "oc_project", parent_id="parent_1")

        self.assertIs(project, referenced_project)
        self.assertIsNone(auto_mode)
        client._project_manager.find_by_bound_chat_id.assert_not_called()

    def test_resolve_project_from_message_uses_active_project_when_not_bound(self):
        client = _make_client()
        active_project = MagicMock()
        active_project.project_id = "pid_active"

        client._project_manager.find_by_bound_chat_id.return_value = None
        client._project_manager.get_active_project.return_value = active_project

        project, auto_mode = client._resolve_project_from_message("m1", "oc_regular", parent_id=None)

        self.assertIs(project, active_project)
        self.assertIsNone(auto_mode)


class TestSystemHandlerPendingPrompt(unittest.TestCase):
    """pending_prompt stash/consume across the model-select card callback."""

    def _make_system_handler(self):
        from src.feishu.handlers.system import SystemHandler
        # Bypass __init__ heavy wiring; only the OrderedDict/stash logic is under test.
        h = SystemHandler.__new__(SystemHandler)
        from collections import OrderedDict
        h._pending_prompts = OrderedDict()
        h._PENDING_PROMPTS_MAX_SIZE = 3
        return h

    def test_stash_and_pop_roundtrip(self):
        h = self._make_system_handler()
        h._stash_pending_prompt("c1", "coco", "请帮我改 foo")
        self.assertEqual(h._pop_pending_prompt("c1", "coco"), "请帮我改 foo")
        # Second pop is None (consumed)
        self.assertIsNone(h._pop_pending_prompt("c1", "coco"))

    def test_stash_noop_for_empty_inputs(self):
        h = self._make_system_handler()
        h._stash_pending_prompt("", "coco", "x")
        h._stash_pending_prompt("c1", "", "x")
        h._stash_pending_prompt("c1", "coco", "")
        self.assertEqual(len(h._pending_prompts), 0)

    def test_lru_eviction_keeps_max_size(self):
        h = self._make_system_handler()
        for i in range(5):
            h._stash_pending_prompt(f"c{i}", "coco", f"p{i}")
        self.assertEqual(len(h._pending_prompts), 3)
        # Oldest two evicted
        self.assertIsNone(h._pop_pending_prompt("c0", "coco"))
        self.assertIsNone(h._pop_pending_prompt("c1", "coco"))
        # Newest three retained
        self.assertEqual(h._pop_pending_prompt("c2", "coco"), "p2")
        self.assertEqual(h._pop_pending_prompt("c3", "coco"), "p3")
        self.assertEqual(h._pop_pending_prompt("c4", "coco"), "p4")

    def test_pending_prompt_key_is_tool_case_insensitive(self):
        h = self._make_system_handler()
        h._stash_pending_prompt("c1", "Coco", "hi")
        self.assertEqual(h._pop_pending_prompt("c1", "coco"), "hi")


if __name__ == "__main__":
    unittest.main()
