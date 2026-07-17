"""Handler-level integration tests for slock escalation resolve flow.

Tests the full dispatch path: handle_card_action → _resolve_escalation → engine + card update.
Uses mocked dependencies (engine_manager, update_card, send_text_to_chat, get_current_sender_id).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.slock_engine.models import (
    EscalationLevel,
    EscalationRequest,
    SlockChannel,
)


class TestSlockHandlerEscalationResolve:
    """Test _resolve_escalation handler dispatch with mocked dependencies."""

    def _make_handler(self):
        """Create a SlockHandler with fully mocked context."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.admin_user_ids = frozenset({"admin-001", "admin-002"})
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        return handler

    def _make_engine_with_escalation(self, *, resolved=False, owner_id="owner-001"):
        """Create a mock engine with a pending escalation."""
        engine = MagicMock()
        channel = SlockChannel(
            channel_id="chat-001",
            name="Test Team",
            team_name="test",
            owner_id=owner_id,
        )
        engine.channel = channel

        escalation = EscalationRequest(
            escalation_id="esc-001",
            agent_id="agent-001",
            agent_name="Coder-A",
            level=EscalationLevel.BLOCKED,
            reason="Cannot access API",
            options=["Retry", "Skip", "Abort"],
            resolved=resolved,
            resolution="Retry" if resolved else "",
        )

        engine.get_escalation = MagicMock(return_value=escalation)

        if resolved:
            engine.resolve_escalation = MagicMock(return_value=None)
        else:
            resolved_esc = EscalationRequest(
                escalation_id="esc-001",
                agent_id="agent-001",
                agent_name="Coder-A",
                level=EscalationLevel.BLOCKED,
                reason="Cannot access API",
                options=["Retry", "Skip", "Abort"],
                resolved=True,
                resolution="Retry",
                resolved_at=1700000000.0,
            )
            engine.resolve_escalation = MagicMock(return_value=resolved_esc)

        return engine

    # ------------------------------------------------------------------
    # AC9/AC16: Admin click → resolve + card update + confirmation
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_admin_click_resolves_successfully(self, mock_sender, mock_settings, mock_resolve_name):
        """AC9/AC16: Admin clicks Retry → engine.resolve called + card updated + text sent."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Retry", "channel_id": "chat-001"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        # Engine resolve was called
        engine.resolve_escalation.assert_called_once_with("esc-001", "Retry")
        # Card was updated
        handler.update_card.assert_called_once()
        card_json = handler.update_card.call_args[0][1]
        card = json.loads(card_json)
        assert card["header"]["template"] == "green"
        assert "[已解决]" in card["header"]["title"]["content"]
        # No text sent when card update succeeds
        handler.send_text_to_chat.assert_not_called()
        # Agent recovery triggered
        engine.resume_after_escalation.assert_called_once()

    # ------------------------------------------------------------------
    # AC10/AC16: Non-admin click → rejected, engine not called
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Random User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_non_admin_click_rejected(self, mock_sender, mock_settings, mock_resolve_name):
        """AC10/AC16: Non-admin user clicks → permission denied, engine NOT called."""
        mock_sender.return_value = "random-user-999"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation(owner_id="owner-001")
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Retry"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        # Engine resolve NOT called
        engine.resolve_escalation.assert_not_called()
        # Permission denied via reply_text
        handler.reply_text.assert_called_once()
        call_text = handler.reply_text.call_args[0][1]
        assert "权限不足" in call_text

    # ------------------------------------------------------------------
    # AC10: Team owner (non-global-admin) can resolve
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Owner User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_team_owner_can_resolve(self, mock_sender, mock_settings, mock_resolve_name):
        """Team owner (not global admin) can successfully resolve escalation."""
        mock_sender.return_value = "owner-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation(owner_id="owner-001")
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Skip"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        engine.resolve_escalation.assert_called_once_with("esc-001", "Skip")
        engine.resume_after_escalation.assert_called_once()

    # ------------------------------------------------------------------
    # AC16: Missing escalation_id/resolution → early return, no engine call
    # ------------------------------------------------------------------

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_missing_escalation_id_early_return(self, mock_sender, mock_settings):
        """AC16: Missing escalation_id → early return with user feedback, engine not touched."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "", "resolution": "Retry"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        engine.resolve_escalation.assert_not_called()
        handler.update_card.assert_not_called()
        # AC-15: User must receive feedback about missing params
        handler.send_text_to_chat.assert_called_once()
        assert "参数缺失" in handler.send_text_to_chat.call_args[0][1]

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_missing_resolution_early_return(self, mock_sender, mock_settings):
        """AC16: Missing resolution → early return with user feedback, engine not touched."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": ""}
        handler._resolve_escalation("msg-001", "chat-001", value)

        engine.resolve_escalation.assert_not_called()
        # AC-15: User must receive feedback about missing params
        handler.send_text_to_chat.assert_called_once()
        assert "参数缺失" in handler.send_text_to_chat.call_args[0][1]

    # ------------------------------------------------------------------
    # AC16: No active engine → no crash
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_no_active_engine_no_crash(self, mock_sender, mock_settings, mock_resolve_name):
        """AC16: No active engine for chat → early return with user feedback, no crash."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        value = {"escalation_id": "esc-001", "resolution": "Retry"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        # Should not crash — user gets feedback about no active engine
        handler.update_card.assert_not_called()
        handler.send_text_to_chat.assert_called_once()
        assert "未激活" in handler.send_text_to_chat.call_args[0][1]

    # ------------------------------------------------------------------
    # AC17: Already resolved → 'already handled' message
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_already_resolved_escalation(self, mock_sender, mock_settings, mock_resolve_name):
        """AC17: Already resolved escalation → info message, no re-resolve."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation(resolved=True)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Retry"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        engine.resolve_escalation.assert_not_called()
        handler.update_card.assert_not_called()
        handler.send_text_to_chat.assert_called_once()
        call_text = handler.send_text_to_chat.call_args[0][1]
        assert "已处理" in call_text

    # ------------------------------------------------------------------
    # AC11: Invalid resolution value → rejected
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_invalid_resolution_rejected(self, mock_sender, mock_settings, mock_resolve_name):
        """AC11: Invalid resolution 'ForceMerge' → rejected with warning."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "ForceMerge"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        engine.resolve_escalation.assert_not_called()
        handler.send_text_to_chat.assert_called_once()
        call_text = handler.send_text_to_chat.call_args[0][1]
        assert "无效" in call_text

    # ------------------------------------------------------------------
    # R2: update_card fails → fallback text confirmation
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_update_card_failure_fallback(self, mock_sender, mock_settings, mock_resolve_name):
        """R2: update_card fails → still sends text confirmation."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        handler.update_card = MagicMock(return_value=False)  # Simulate failure
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Retry"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        # Engine was still resolved
        engine.resolve_escalation.assert_called_once()
        # Text confirmation sent even though card update failed
        handler.send_text_to_chat.assert_called()
        calls = handler.send_text_to_chat.call_args_list
        text_calls = [c[0][1] for c in calls]
        assert any("Escalation resolved" in t for t in text_calls)
        # Agent recovery still triggered
        engine.resume_after_escalation.assert_called_once()

    # ------------------------------------------------------------------
    # R3: card update succeeds → no redundant text
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_resolve_success_no_redundant_text(self, mock_sender, mock_settings, mock_resolve_name):
        """AC-16: card update succeeds → no redundant text confirmation sent."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        handler.update_card = MagicMock(return_value=True)  # Success
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Retry"}
        handler._resolve_escalation("msg-001", "chat-001", value)

        engine.resolve_escalation.assert_called_once()
        handler.update_card.assert_called_once()
        # No text should be sent when card update succeeds
        handler.send_text_to_chat.assert_not_called()

    # ------------------------------------------------------------------
    # Permission: dissolve_team and stop_slock_engine
    # ------------------------------------------------------------------

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_dissolve_team_permission_denied(self, mock_sender, mock_settings):
        """dissolve_team by non-admin, non-owner → permission denied, engine NOT deactivated."""
        mock_sender.return_value = "random-user-999"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation(owner_id="owner-001")
        engine.deactivate = MagicMock()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)
        handler.ctx.slock_engine_manager.find_team = MagicMock(return_value=engine)

        handler.dissolve_team("msg-001", "chat-001", name="test")

        engine.deactivate.assert_not_called()
        handler.reply_text.assert_called_once()
        assert "权限不足" in handler.reply_text.call_args[0][1]

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_stop_slock_engine_permission_denied(self, mock_sender, mock_settings):
        """stop_slock_engine by non-admin, non-owner → permission denied, engine NOT deactivated."""
        mock_sender.return_value = "random-user-999"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation(owner_id="owner-001")
        engine.deactivate = MagicMock()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.stop_slock_engine("msg-001", "chat-001")

        engine.deactivate.assert_not_called()
        handler.reply_text.assert_called_once()
        assert "权限不足" in handler.reply_text.call_args[0][1]

    # ------------------------------------------------------------------
    # handle_card_action dispatches slock_escalation_resolve correctly
    # ------------------------------------------------------------------

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Admin User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_handle_card_action_dispatches_to_resolve(self, mock_sender, mock_settings, mock_resolve_name):
        """handle_card_action routes slock_escalation_resolve to _resolve_escalation."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_escalation()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-001", "resolution": "Abort", "channel_id": "chat-001"}
        handler.handle_card_action("msg-001", "chat-001", "slock_escalation_resolve", value)

        engine.resolve_escalation.assert_called_once_with("esc-001", "Abort")
        engine.resume_after_escalation.assert_called_once()


class TestOwnerIdRestoredResolvePermission:
    """AC-14 + AC-09: owner_id restored via marker merge allows team creator to resolve."""

    @patch("src.feishu.handlers.slock.resolve_display_name_nonblocking", return_value="Creator User")
    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_restored_owner_can_resolve_after_restart(self, mock_sender, mock_settings, mock_resolve_name):
        """After restart, owner_id recovered from marker allows creator to resolve escalation."""
        from src.feishu.handlers.slock import SlockHandler

        # The owner is NOT a global admin, but is the team creator
        mock_sender.return_value = "ou_team_creator"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-global-only"}))

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.admin_user_ids = frozenset({"admin-global-only"})
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()

        # Simulate engine with owner_id restored from marker (as if after merge + restart)
        engine = MagicMock()
        channel = SlockChannel(
            channel_id="chat-restored",
            name="Restored Team",
            team_name="RestoredTeam",
            owner_id="ou_team_creator",  # This was recovered via marker merge
        )
        engine.channel = channel

        escalation = EscalationRequest(
            escalation_id="esc-restored-001",
            agent_id="agent-restored-001",
            agent_name="Coder-Restored",
            level=EscalationLevel.BLOCKED,
            reason="Access denied to production",
            options=["Retry", "Skip", "Abort"],
        )
        engine.get_escalation = MagicMock(return_value=escalation)
        resolved_esc = EscalationRequest(
            escalation_id="esc-restored-001",
            agent_id="agent-restored-001",
            agent_name="Coder-Restored",
            level=EscalationLevel.BLOCKED,
            reason="Access denied to production",
            options=["Retry", "Skip", "Abort"],
            resolved=True,
            resolution="Skip",
            resolved_at=1700000000.0,
        )
        engine.resolve_escalation = MagicMock(return_value=resolved_esc)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"escalation_id": "esc-restored-001", "resolution": "Skip", "channel_id": "chat-restored"}
        handler._resolve_escalation("msg-restored", "chat-restored", value)

        # Team creator (non-admin) should be authorized via owner_id
        engine.resolve_escalation.assert_called_once_with("esc-restored-001", "Skip")
        engine.resume_after_escalation.assert_called_once()
        # No permission denied
        handler.reply_text.assert_not_called()


class TestSafeErrorMessage:
    """AC-15: Error cards must not leak internal paths or sensitive details."""

    def _make_handler(self):
        """Create a SlockHandler with fully mocked context."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.admin_user_ids = frozenset({"admin-001"})
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="card-msg-id")
        handler.reply_text = MagicMock()
        return handler

    def test_safe_error_message_no_path_leak(self):
        """safe_error_message never exposes internal file paths."""
        from src.utils.errors import safe_error_message

        # Simulate internal errors that might contain paths
        err = ValueError("/home/jiataorui/work/ghostAp/src/secret/module.py: failed")
        msg = safe_error_message(err)
        assert "/home" not in msg
        assert "secret" not in msg
        assert msg == "内部错误，请联系管理员"

    def test_safe_error_timeout(self):
        """TimeoutError maps to Chinese timeout message."""
        from src.utils.errors import safe_error_message

        msg = safe_error_message(TimeoutError("ACP session timed out after 7200s"))
        assert msg == "执行超时"
        assert "7200" not in msg

    def test_safe_error_permission(self):
        """PermissionError maps to Chinese permission message."""
        from src.utils.errors import safe_error_message

        msg = safe_error_message(PermissionError("Cannot access /etc/shadow"))
        assert msg == "权限不足"
        assert "/etc" not in msg

    def test_safe_error_connection(self):
        """ConnectionError maps to Chinese connection message."""
        from src.utils.errors import safe_error_message

        msg = safe_error_message(ConnectionError("Failed to connect to api.internal.corp:8443"))
        assert msg == "连接失败"
        assert "internal" not in msg
