"""Unit tests for WorktreeRenderer session lifecycle management."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.card.hooks import EmojiHook
from src.card.session import CardSession
from src.card.session.factory import CardSessionFactory
from src.card.state.models import CardMetadata
from src.feishu.renderers.worktree_renderer import WorktreeRenderer


def _make_mock_handler():
    """Create a mock WorktreeHandler with required attributes."""
    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    delivery = MagicMock()
    handler.get_card_delivery.return_value = delivery
    return handler


def _make_renderer():
    """Create a WorktreeRenderer with mocked handler."""
    handler = _make_mock_handler()
    return WorktreeRenderer(handler)


class TestGetOrCreateSession:
    """Tests for get_or_create_session lifecycle."""

    def test_creates_new_session(self):
        """First call creates a new session."""
        renderer = _make_renderer()
        session = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
        assert session is not None
        assert not session.closed

    def test_reuses_existing_session(self):
        """Second call with same project_id returns same session."""
        renderer = _make_renderer()
        s1 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
        s2 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_2")
        assert s1 is s2

    def test_different_project_gets_different_session(self):
        """Different project_id creates a different session."""
        renderer = _make_renderer()
        s1 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
        s2 = renderer.get_or_create_session("chat_1", "proj_2", reply_to="msg_2")
        assert s1 is not s2

    def test_recreates_after_closed_session(self):
        """If existing session is closed, creates a new one."""
        renderer = _make_renderer()
        s1 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
        s1.close()
        s2 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_2")
        assert s2 is not s1
        assert not s2.closed


class TestCloseSession:
    """Tests for close_session."""

    def test_close_removes_session(self):
        """close_session removes the session from the pool."""
        renderer = _make_renderer()
        renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
        renderer.close_session("proj_1")
        assert renderer.get_session("proj_1") is None

    def test_close_nonexistent_is_safe(self):
        """Closing a non-existent project_id doesn't raise."""
        renderer = _make_renderer()
        renderer.close_session("no_such_project")  # should not raise


class TestClosedSessionCleanup:
    """Tests for closed session lazy cleanup (TTL managed by CardSession)."""

    def test_closed_session_cleaned_on_access(self):
        """If session is closed (e.g. by CardSession TTL), get_or_create creates a new one."""
        renderer = _make_renderer()
        s1 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
        s1.close()

        s2 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_2")
        assert s2 is not s1
        assert not s2.closed

    def test_active_session_not_replaced(self):
        """Sessions that are not closed are reused."""
        renderer = _make_renderer()
        s1 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")

        s2 = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_2")
        assert s2 is s1
        assert not s1.closed


class TestConcurrentGetOrCreate:
    """Test thread-safety of get_or_create_session."""

    def test_concurrent_same_project_creates_single_session(self):
        """10 threads calling get_or_create for same project: only 1 session created."""
        renderer = _make_renderer()
        sessions = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            s = renderer.get_or_create_session("chat_1", "proj_1", reply_to="msg_1")
            sessions.append(s)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # All threads should get the same session
        assert len(sessions) == 10
        assert all(s is sessions[0] for s in sessions)


class TestCreateSessionFactory:
    """Test that create_session uses CardSessionFactory."""

    def test_create_session_calls_factory(self):
        """create_session should delegate to CardSessionFactory.create."""
        from src.feishu.renderers.base import BaseRenderer

        handler = _make_mock_handler()
        mock_session = MagicMock()

        with patch("src.card.session.factory.CardSessionFactory") as MockFactory:
            mock_factory_instance = MagicMock()
            mock_factory_instance.create.return_value = mock_session
            MockFactory.return_value = mock_factory_instance

            renderer = BaseRenderer(handler)
            result = renderer.create_session(handler, "chat_1", "msg_1")

            MockFactory.assert_called_once()
            mock_factory_instance.create.assert_called_once()
            assert result is mock_session


class TestWorktreeRendererHooksInjection:
    """Tests that WorktreeRenderer injects hooks when creating sessions."""

    def test_session_has_hooks_when_reply_to_provided(self):
        """get_or_create_session with reply_to should inject EmojiHook."""
        from src.card.hooks import EmojiHook

        renderer = _make_renderer()
        session = renderer.get_or_create_session("chat_1", "proj_hooks", reply_to="msg_hooks")

        # Session should have hooks injected (at least EmojiHook)
        assert session._hooks is not None
        assert len(session._hooks) >= 1
        assert any(isinstance(h, EmojiHook) for h in session._hooks)

    def test_session_degraded_hooks_when_no_reply_to(self):
        """get_or_create_session without reply_to should have degraded EmojiHook (empty message_id)."""
        renderer = _make_renderer()
        session = renderer.get_or_create_session("chat_1", "proj_no_hook", reply_to=None)

        # Degraded EmojiHook is still injected (skips reaction at runtime)
        # Plus WorktreeGCHook for proactive session cleanup
        assert len(session._hooks) == 2
        emoji_hooks = [h for h in session._hooks if isinstance(h, EmojiHook)]
        assert len(emoji_hooks) == 1
        assert emoji_hooks[0]._message_id == ""
