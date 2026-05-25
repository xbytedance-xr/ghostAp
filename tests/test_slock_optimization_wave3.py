"""Unit tests for Slock optimization wave 3.

Covers:
- Task 12: Duplicate discussion trigger path removal (handler._maybe_trigger_discussion deleted)
- Task 13: Permission checks on mark_done/switch_role card actions
- Task 16: Discussion confirmation callback registration (confirm/cancel handlers)
"""

from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Task 12: _maybe_trigger_discussion removed from handler
# ---------------------------------------------------------------------------


class TestDuplicateDiscussionPathRemoved:
    """Verify the handler no longer has the duplicate discussion trigger."""

    def test_handler_has_no_maybe_trigger_discussion(self):
        from src.feishu.handlers.slock import SlockHandler

        assert not hasattr(SlockHandler, "_maybe_trigger_discussion")

    def test_handler_has_no_uncertainty_markers(self):
        from src.feishu.handlers.slock import SlockHandler

        assert not hasattr(SlockHandler, "_UNCERTAINTY_MARKERS")


# ---------------------------------------------------------------------------
# Task 13: Permission checks on card actions
# ---------------------------------------------------------------------------


class TestMarkDonePermission:
    """Verify slock_agent_mark_done requires admin/owner permission."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler.handle_card_action = SlockHandler.handle_card_action.__get__(handler, SlockHandler)
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        return handler

    def test_mark_done_denied_without_permission(self):
        handler = self._make_handler()
        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager
        handler._has_slock_permission.return_value = False

        handler.handle_card_action(
            "msg1", "chat1", "slock_agent_mark_done",
            {"task_id": "t1"},
        )

        handler.send_text_to_chat.assert_called_once()
        assert "权限不足" in handler.send_text_to_chat.call_args[0][1]
        engine._force_complete_task.assert_not_called()

    def test_mark_done_allowed_with_permission(self):
        handler = self._make_handler()
        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager
        handler._has_slock_permission.return_value = True

        handler.handle_card_action(
            "msg1", "chat1", "slock_agent_mark_done",
            {"task_id": "t1"},
        )

        engine._force_complete_task.assert_called_once_with("t1")


class TestSwitchRolePermission:
    """Verify slock_agent_switch_role requires admin/owner permission."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler.handle_card_action = SlockHandler.handle_card_action.__get__(handler, SlockHandler)
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        return handler

    def test_switch_role_denied_without_permission(self):
        handler = self._make_handler()
        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager
        handler._has_slock_permission.return_value = False

        handler.handle_card_action(
            "msg1", "chat1", "slock_agent_switch_role",
            {"agent_id": "a1"},
        )

        handler.send_text_to_chat.assert_called_once()
        assert "权限不足" in handler.send_text_to_chat.call_args[0][1]


# ---------------------------------------------------------------------------
# Task 16: Discussion confirmation callbacks
# ---------------------------------------------------------------------------


class TestDiscussionConfirmationCallbacks:
    """Verify confirm/cancel discussion card action handlers."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler.handle_card_action = SlockHandler.handle_card_action.__get__(handler, SlockHandler)
        handler._has_slock_permission = MagicMock(return_value=True)
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        return handler

    def test_confirm_discussion_calls_engine(self):
        handler = self._make_handler()
        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(
            "msg1", "chat1", "slock_confirm_discussion",
            {"thread_id": "th_123", "channel_id": "chat1"},
        )

        engine.confirm_discussion.assert_called_once_with("th_123", trust_type="")
        handler.send_text_to_chat.assert_called_once()
        assert "讨论已启动" in handler.send_text_to_chat.call_args[0][1]

    def test_cancel_discussion_calls_engine(self):
        handler = self._make_handler()
        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(
            "msg1", "chat1", "slock_cancel_discussion",
            {"thread_id": "th_456", "channel_id": "chat1"},
        )

        engine.cancel_discussion.assert_called_once_with("th_456")
        handler.send_text_to_chat.assert_called_once()
        assert "已取消" in handler.send_text_to_chat.call_args[0][1]

    def test_confirm_discussion_no_engine_sends_error(self):
        handler = self._make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(
            "msg1", "chat1", "slock_confirm_discussion",
            {"thread_id": "th_789", "channel_id": "chat1"},
        )

        handler.send_text_to_chat.assert_called_once()
        assert "未找到" in handler.send_text_to_chat.call_args[0][1]
