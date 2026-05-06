import unittest
from unittest.mock import MagicMock

from src.feishu.handlers.base import BaseHandler
from src.feishu.handlers.loop import LoopHandler
from src.feishu.handlers.spec import SpecHandler
from src.feishu.renderers.base import BaseRenderer
from src.feishu.renderers.loop_renderer import LoopRenderer
from src.feishu.renderers.spec_renderer import SpecRenderer


class TestRendererRefactor(unittest.TestCase):
    def setUp(self):
        # Create a mock handler to initialize BaseRenderer
        self.mock_handler = MagicMock(spec=BaseHandler)
        self.mock_handler.ctx = MagicMock()
        self.mock_handler.settings = MagicMock()
        self.mock_handler.settings.card.deep_compact_default = False

        self.base_renderer = BaseRenderer(self.mock_handler)

    def test_base_render_collapsible_section_ac(self):
        """Test BaseRenderer._render_collapsible_section with AC list"""
        # Collapse logic: > COLLAPSE_ITEM_THRESHOLD=8 items, expand=False -> Hide completed
        completed_lines = [f"- \u2705 Item {i}" for i in range(1, 9)]
        incomplete_line = "- \u2b1c\ufe0f Item 9"
        content = "\n".join(completed_lines + [incomplete_line])
        result = self.base_renderer._render_collapsible_section(
            content, total_items=9, expanded=False, completed_count=8
        )

        self.assertIn("\u2705 \u5df2\u901a\u8fc7 8 \u9879", result)  # ✅ 已通过 8 项
        self.assertNotIn("- \u2705 Item 1", result)
        self.assertIn("- \u2b1c\ufe0f Item 9", result)

        # Expand logic
        result = self.base_renderer._render_collapsible_section(
            content, total_items=9, expanded=True, completed_count=8
        )
        self.assertEqual(result, content)

    def test_base_render_collapsible_section_text(self):
        """Test BaseRenderer._render_collapsible_section with long text (Spec mode)"""
        # Create a long text > COLLAPSE_LINE_THRESHOLD=30 lines
        lines = [f"Line {i}" for i in range(40)]
        content = "\n".join(lines)

        # Should truncate if not expanded (shows first COLLAPSE_DISPLAY_LINES=15 lines)
        result = self.base_renderer._render_collapsible_section(content, total_items=40, expanded=False)
        self.assertIn("\u5185\u5bb9\u8f83\u957f (共 40 行)", result)  # 内容较长 (共 40 行)
        self.assertIn("Line 0", result)
        self.assertIn("Line 14", result)
        self.assertNotIn("Line 39", result)

        # Should show all if expanded
        result = self.base_renderer._render_collapsible_section(content, total_items=40, expanded=True)
        self.assertEqual(result, content)

    def test_loop_renderer_inheritance(self):
        """Verify LoopRenderer inherits and uses base methods"""
        mock_loop_handler = MagicMock(spec=LoopHandler)
        mock_loop_handler.ctx = MagicMock()
        mock_loop_handler.settings = MagicMock()
        renderer = LoopRenderer(mock_loop_handler)

        self.assertTrue(hasattr(renderer, "_render_collapsible_section"))

    def test_spec_renderer_inheritance(self):
        """Verify SpecRenderer inherits and uses base methods"""
        mock_spec_handler = MagicMock(spec=SpecHandler)
        mock_spec_handler.ctx = MagicMock()
        mock_spec_handler.settings = MagicMock()
        renderer = SpecRenderer(mock_spec_handler)

        self.assertTrue(hasattr(renderer, "_render_collapsible_section"))

        # Verify default state includes expand_ac
        state = renderer.get_default_ui_state()
        self.assertIn("expand_ac", state)
        self.assertFalse(state["expand_ac"])


class TestGetActiveSession(unittest.TestCase):
    """Test get_active_session() lifecycle across renderers."""

    def test_base_renderer_returns_none(self):
        """BaseRenderer.get_active_session() defaults to None."""
        mock_handler = MagicMock(spec=BaseHandler)
        mock_handler.ctx = MagicMock()
        mock_handler.settings = MagicMock()
        renderer = BaseRenderer(mock_handler)
        self.assertIsNone(renderer.get_active_session())

    def test_loop_renderer_tracks_session(self):
        """LoopRenderer._current_session is exposed via get_active_session()."""
        mock_handler = MagicMock(spec=LoopHandler)
        mock_handler.ctx = MagicMock()
        mock_handler.settings = MagicMock()
        renderer = LoopRenderer(mock_handler)

        self.assertIsNone(renderer.get_active_session())
        mock_session = MagicMock()
        renderer._current_session = mock_session
        self.assertIs(renderer.get_active_session(), mock_session)
        renderer._current_session = None
        self.assertIsNone(renderer.get_active_session())

    def test_spec_renderer_tracks_session(self):
        """SpecRenderer._current_session is exposed via get_active_session()."""
        mock_handler = MagicMock(spec=SpecHandler)
        mock_handler.ctx = MagicMock()
        mock_handler.settings = MagicMock()
        renderer = SpecRenderer(mock_handler)

        self.assertIsNone(renderer.get_active_session())
        mock_session = MagicMock()
        renderer._current_session = mock_session
        self.assertIs(renderer.get_active_session(), mock_session)


class TestOnEngineErrorUsesGetActiveSession(unittest.TestCase):
    """Test that _on_engine_error routes through get_active_session()."""

    def test_dispatches_through_active_session(self):
        """When renderer has active session, error dispatches via session."""
        from src.feishu.handlers.engine_base import BaseEngineHandler

        handler = MagicMock(spec=BaseEngineHandler)
        handler.renderer = MagicMock()
        mock_session = MagicMock()
        mock_session.closed = False
        handler.renderer.get_active_session.return_value = mock_session

        # Create a mock reporter
        reporter = MagicMock()
        reporter.format_error.return_value = "Error content"
        reporter.get_error_title.return_value = "Error"

        # Call _on_engine_error directly
        BaseEngineHandler._on_engine_error(
            handler,
            error=RuntimeError("test"),
            task_id="t1",
            chat_id="c1",
            message_id="m1",
            project=None,
            engine_name="test",
            reporter=reporter,
            request_id=None,
        )

        # session.dispatch should have been called
        mock_session.dispatch.assert_called_once()

    def test_falls_back_to_reply_text_when_no_session(self):
        """When no active session, falls back to reply_text."""
        from src.feishu.handlers.engine_base import BaseEngineHandler

        handler = MagicMock(spec=BaseEngineHandler)
        handler.renderer = MagicMock()
        handler.renderer.get_active_session.return_value = None
        handler._get_engine_name_prefix.return_value = "Test"

        reporter = MagicMock()
        reporter.format_error.return_value = "Error content"
        reporter.get_error_title.return_value = "Error"

        BaseEngineHandler._on_engine_error(
            handler,
            error=RuntimeError("test"),
            task_id="t1",
            chat_id="c1",
            message_id="m1",
            project=None,
            engine_name="test",
            reporter=reporter,
            request_id=None,
        )

        handler.reply_text.assert_called_once_with("m1", "Error\n\nError content")


class TestSessionAutoCleanupOnTerminal(unittest.TestCase):
    """E2E: terminal callbacks clear _current_session → get_active_session() returns None."""

    def _make_mock_handler(self, handler_spec):
        handler = MagicMock(spec=handler_spec)
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card.deep_compact_default = False
        handler.settings.default_reply_mode = "thread"
        handler.settings.deep_stream_interval = 0.1
        handler.settings.deep_stream_min_chars = 1
        handler.add_reaction = MagicMock()
        handler.send_text_to_chat = MagicMock()
        handler.ensure_request_id = MagicMock(return_value="req-1")
        return handler

    def _make_mock_session(self):
        session = MagicMock()
        session.closed = False
        session.dispatch = MagicMock()
        return session

    def test_deep_renderer_clears_session_on_done(self):
        """DeepRenderer: on_project_done clears _current_session."""
        from src.deep_engine.models import DeepProject, DeepProjectStatus
        from src.feishu.handlers.deep import DeepHandler
        from src.feishu.renderers.deep_renderer import DeepRenderer

        handler = self._make_mock_handler(DeepHandler)
        renderer = DeepRenderer(handler)

        mock_session = self._make_mock_session()
        with unittest.mock.patch.object(renderer, "create_session", return_value=mock_session):
            callbacks = renderer.create_deep_callbacks(
                message_id="m1", chat_id="c1", project=None, engine_name="Coco"
            )

        # Session should be active
        self.assertIs(renderer.get_active_session(), mock_session)

        # Trigger terminal: on_project_done with COMPLETED status
        deep_proj = MagicMock(spec=DeepProject)
        deep_proj.status = DeepProjectStatus.COMPLETED
        handler.ctx.deep_engine_manager.snapshot.return_value = None
        handler.ctx.deep_engine_manager.snapshot_active.return_value = []
        callbacks.on_project_done(deep_proj)

        # Session should be cleared
        self.assertIsNone(renderer.get_active_session())

    def test_deep_renderer_clears_session_on_error(self):
        """DeepRenderer: on_error clears _current_session."""
        from src.feishu.handlers.deep import DeepHandler
        from src.feishu.renderers.deep_renderer import DeepRenderer

        handler = self._make_mock_handler(DeepHandler)
        renderer = DeepRenderer(handler)

        mock_session = self._make_mock_session()
        with unittest.mock.patch.object(renderer, "create_session", return_value=mock_session):
            callbacks = renderer.create_deep_callbacks(
                message_id="m1", chat_id="c1", project=None, engine_name="Coco"
            )

        self.assertIs(renderer.get_active_session(), mock_session)
        callbacks.on_error("Something failed")
        self.assertIsNone(renderer.get_active_session())

    def test_loop_renderer_clears_session_on_done(self):
        """LoopRenderer: on_project_done clears _current_session."""
        from src.feishu.handlers.loop import LoopHandler
        from src.feishu.renderers.loop_renderer import LoopRenderer

        handler = self._make_mock_handler(LoopHandler)
        renderer = LoopRenderer(handler)

        mock_session = self._make_mock_session()
        # LoopRenderer uses a rotator, but _current_session is still set directly
        renderer._current_session = mock_session
        self.assertIs(renderer.get_active_session(), mock_session)

        # Simulate what on_project_done does: dispatch + clear
        mock_session.dispatch(MagicMock())
        renderer._current_session = None
        self.assertIsNone(renderer.get_active_session())

    def test_spec_renderer_clears_session_on_error(self):
        """SpecRenderer: _current_session cleared on terminal."""
        from src.feishu.handlers.spec import SpecHandler
        from src.feishu.renderers.spec_renderer import SpecRenderer

        handler = self._make_mock_handler(SpecHandler)
        renderer = SpecRenderer(handler)

        mock_session = self._make_mock_session()
        renderer._current_session = mock_session
        self.assertIs(renderer.get_active_session(), mock_session)

        # Simulate terminal clear
        renderer._current_session = None
        self.assertIsNone(renderer.get_active_session())


class TestBuildHooksContextHookCombination(unittest.TestCase):
    """Test _build_hooks with include_context_hook=True + context_update_fn=None."""

    def _make_renderer(self):
        mock_handler = MagicMock(spec=BaseHandler)
        mock_handler.ctx = MagicMock()
        mock_handler.settings = MagicMock()
        mock_handler.add_reaction = MagicMock()
        mock_handler.send_text_to_chat = MagicMock()
        return BaseRenderer(mock_handler)

    def test_include_context_hook_true_with_none_fn_injects_hook(self):
        """include_context_hook=True + context_update_fn=None still injects ContextPersistenceHook."""
        from src.card.hooks import ContextPersistenceHook, EmojiHook

        renderer = self._make_renderer()
        hooks = renderer._build_hooks(
            message_id="m1",
            include_context_hook=True,
            context_update_fn=None,
            chat_id="c1",
            engine_type="deep",
        )

        # Should have EmojiHook + ContextPersistenceHook
        assert len(hooks) == 2
        assert isinstance(hooks[0], EmojiHook)
        assert isinstance(hooks[1], ContextPersistenceHook)
        # update_fn should be None
        assert hooks[1]._update_fn is None

    def test_include_context_hook_false_no_fn_skips_hook(self):
        """include_context_hook=False + context_update_fn=None → only EmojiHook."""
        from src.card.hooks import EmojiHook

        renderer = self._make_renderer()
        hooks = renderer._build_hooks(
            message_id="m1",
            include_context_hook=False,
            context_update_fn=None,
            chat_id="c1",
            engine_type="deep",
        )

        assert len(hooks) == 1
        assert isinstance(hooks[0], EmojiHook)

    def test_context_update_fn_provided_injects_hook(self):
        """context_update_fn provided (even without include_context_hook=True) injects hook."""
        from src.card.hooks import ContextPersistenceHook, EmojiHook

        renderer = self._make_renderer()
        fn = MagicMock()
        hooks = renderer._build_hooks(
            message_id="m1",
            include_context_hook=False,
            context_update_fn=fn,
            chat_id="c1",
            engine_type="loop",
        )

        assert len(hooks) == 2
        assert isinstance(hooks[0], EmojiHook)
        assert isinstance(hooks[1], ContextPersistenceHook)
        assert hooks[1]._update_fn is fn


if __name__ == "__main__":
    unittest.main()
