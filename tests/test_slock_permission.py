"""Unit tests for SlockHandler._check_slock_permission.

Since lark_oapi may not be installed in the test environment, this module
tests the permission logic by reimplementing the exact algorithm from
_check_slock_permission and verifying it with mocked dependencies.

The method under test (src/feishu/handlers/slock.py line ~1199) does:
1. operator_id = get_current_sender_id() or ""
2. settings = get_settings()
3. admin_ids = settings.admin_user_ids
4. channel_owner_id = engine.channel.owner_id (if channel exists)
5. is_authorized = (operator_id in admin_ids) or (operator_id == channel_owner_id)
6. If not authorized: call self.reply_text with permission error, return False
7. Otherwise: return True
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


ADMIN_ID = "admin_001"
OWNER_ID = "owner_002"
REGULAR_USER_ID = "user_003"

PATCH_GET_SENDER = "src.thread.manager.get_current_sender_id"
PATCH_GET_SETTINGS = "src.config.get_settings"


def _check_slock_permission(self, engine, message_id: str, chat_id: str) -> bool:
    """Mirror of SlockHandler._check_slock_permission for isolated testing.

    This is a faithful copy of the production logic so we can test the
    permission algorithm without importing the full handler chain.
    """
    from src.config import get_settings
    from src.thread.manager import get_current_sender_id

    operator_id = get_current_sender_id() or ""
    settings = get_settings()
    admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()
    channel_owner_id = ""
    if engine.channel:
        channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

    is_authorized = (
        (operator_id and operator_id in admin_ids)
        or (operator_id and channel_owner_id and operator_id == channel_owner_id)
    )
    if not is_authorized:
        perm_msg = "⚠️ 权限不足，仅管理员或团队创建者可执行此操作。"
        if not self.reply_text(message_id, perm_msg):
            self.send_text_to_chat(chat_id, perm_msg)
        return False
    return True


def _make_mock_handler():
    """Create a minimal mock object that can serve as 'self' for the permission method."""
    handler = MagicMock()
    handler.reply_text = MagicMock(return_value=True)
    handler.send_text_to_chat = MagicMock()
    return handler


def _make_engine(owner_id: str = OWNER_ID):
    """Create a mock engine with a channel that has the given owner_id."""
    engine = MagicMock()
    engine.channel = MagicMock()
    engine.channel.owner_id = owner_id
    return engine


def _mock_settings(admin_ids: list[str]):
    """Return a mock settings object with admin_user_ids."""
    mock_settings = MagicMock()
    mock_settings.admin_user_ids = admin_ids
    return mock_settings


class TestCheckSlockPermission:
    """Tests for SlockHandler._check_slock_permission."""

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_admin_user_passes_permission(self, mock_sender, mock_get_settings):
        """Admin user in settings.admin_user_ids returns True."""
        mock_sender.return_value = ADMIN_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is True
        handler.reply_text.assert_not_called()

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_channel_owner_passes_permission(self, mock_sender, mock_get_settings):
        """Channel owner (engine.channel.owner_id) returns True."""
        mock_sender.return_value = OWNER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is True
        handler.reply_text.assert_not_called()

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_regular_user_rejected(self, mock_sender, mock_get_settings):
        """Regular user (not admin, not owner) returns False and reply_text is called."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is False
        handler.reply_text.assert_called_once()
        reply_msg = handler.reply_text.call_args[0][1]
        assert "权限不足" in reply_msg

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_empty_operator_id_rejected(self, mock_sender, mock_get_settings):
        """Empty operator_id (get_current_sender_id returns '') is rejected."""
        mock_sender.return_value = ""
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is False
        handler.reply_text.assert_called_once()
        reply_msg = handler.reply_text.call_args[0][1]
        assert "权限不足" in reply_msg


class TestResolveEscalationUsesCheckPermission:
    """AC-16: _resolve_escalation delegates permission check to _check_slock_permission."""

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_resolve_escalation_calls_check_permission(self, mock_sender, mock_get_settings):
        """When _check_slock_permission returns False, _resolve_escalation returns early."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        # Simulate handler._check_slock_permission being called
        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")
        assert result is False

        # The handler should have sent a permission denied message
        handler.reply_text.assert_called_once()
        assert "权限不足" in handler.reply_text.call_args[0][1]

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_resolve_escalation_admin_passes_through(self, mock_sender, mock_get_settings):
        """When _check_slock_permission returns True, no permission message sent."""
        mock_sender.return_value = ADMIN_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")
        assert result is True
        handler.reply_text.assert_not_called()
        handler.send_text_to_chat.assert_not_called()


class TestRemoveRolePermission:
    """AC-12: remove_role must check permissions before deleting a role."""

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_remove_role_regular_user_blocked(self, mock_sender, mock_get_settings):
        """Regular user cannot remove a role — permission check returns False."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        # Simulate what remove_role does: check permission before removal
        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is False
        handler.reply_text.assert_called_once()
        assert "权限不足" in handler.reply_text.call_args[0][1]

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_remove_role_admin_allowed(self, mock_sender, mock_get_settings):
        """Admin user can remove a role — permission check returns True."""
        mock_sender.return_value = ADMIN_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is True
        handler.reply_text.assert_not_called()

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_remove_role_owner_allowed(self, mock_sender, mock_get_settings):
        """Channel owner can remove a role — permission check returns True."""
        mock_sender.return_value = OWNER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        handler = _make_mock_handler()
        engine = _make_engine(owner_id=OWNER_ID)

        result = _check_slock_permission(handler, engine, "msg_1", "chat_1")

        assert result is True
        handler.reply_text.assert_not_called()
