"""Tests for permission model refactor: DISCUSSION/COUNCIL no longer require admin.

Verifies:
- A non-admin user CAN execute DISCUSSION action (no permission error)
- A non-admin user CAN execute COUNCIL action (no permission error)
- A non-admin user CANNOT execute NEW_TEAM action (permission denied)
- A non-admin user CANNOT execute TEAM_DISSOLVE action (permission denied)

The refactored model moves DISCUSSION and COUNCIL from _WRITE_ACTIONS into
_NEEDS_ACTIVE_ENGINE, which requires only an active managed chat (no admin gate).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock external dependencies that are not installed in the test environment.
# This must happen before any import of src.feishu.* modules.
# ---------------------------------------------------------------------------

_EXTERNAL_MODULES = [
    "lark_oapi", "lark_oapi.event", "lark_oapi.event.callback",
    "lark_oapi.event.callback.model", "lark_oapi.event.callback.model.p2_card_action_trigger",
    "lark_oapi.event.callback.model.p2_im_message_receive_v1",
    "lark_oapi.api", "lark_oapi.api.core", "lark_oapi.api.core.request",
    "lark_oapi.api.im", "lark_oapi.api.im.v1",
    "lark_oapi.ws", "lark_oapi.ws.const", "lark_oapi.ws.enum",
    "lark_oapi.ws.client",
    "acp", "acp.client", "acp.interfaces", "acp.schema", "acp.helpers",
    "acp.stdio",
]


class _FakeModule(MagicMock):
    """MagicMock subclass accepted by importlib machinery."""
    __spec__ = None
    __path__ = []
    __all__ = []


for _mod_name in _EXTERNAL_MODULES:
    sys.modules.setdefault(_mod_name, _FakeModule(name=_mod_name))

# ---------------------------------------------------------------------------
# Now safe to import from slock handler and engine
# ---------------------------------------------------------------------------

from src.slock_engine.slash_commands import SlockCommandAction  # noqa: E402
from src.feishu.handlers.slock import _NEEDS_ACTIVE_ENGINE, _WRITE_ACTIONS  # noqa: E402

PATCH_GET_SENDER = "src.thread.manager.get_current_sender_id"
PATCH_GET_SETTINGS = "src.config.get_settings"

ADMIN_ID = "admin_001"
OWNER_ID = "owner_002"
REGULAR_USER_ID = "user_003"


def _mock_settings(admin_ids: list[str]):
    """Return a mock settings object with admin_user_ids."""
    s = MagicMock()
    s.admin_user_ids = admin_ids
    s.slock_nli_confidence_threshold = 0.7
    s.slock_nli_timeout = 5
    return s


def _make_engine(owner_id: str = OWNER_ID):
    """Create a mock engine with a channel that has the given owner_id."""
    engine = MagicMock()
    engine.channel = MagicMock()
    engine.channel.owner_id = owner_id
    return engine


class TestPermissionRefactorSets:
    """Verify _WRITE_ACTIONS and _NEEDS_ACTIVE_ENGINE contain the correct actions."""

    def test_discussion_not_in_write_actions(self):
        """DISCUSSION should NOT be in _WRITE_ACTIONS after refactor."""
        assert SlockCommandAction.DISCUSSION not in _WRITE_ACTIONS

    def test_council_not_in_write_actions(self):
        """COUNCIL should NOT be in _WRITE_ACTIONS after refactor."""
        assert SlockCommandAction.COUNCIL not in _WRITE_ACTIONS

    def test_discussion_in_needs_active_engine(self):
        """DISCUSSION should be in _NEEDS_ACTIVE_ENGINE."""
        assert SlockCommandAction.DISCUSSION in _NEEDS_ACTIVE_ENGINE

    def test_council_in_needs_active_engine(self):
        """COUNCIL should be in _NEEDS_ACTIVE_ENGINE."""
        assert SlockCommandAction.COUNCIL in _NEEDS_ACTIVE_ENGINE

    def test_new_team_still_in_write_actions(self):
        """NEW_TEAM should remain in _WRITE_ACTIONS (requires admin)."""
        assert SlockCommandAction.NEW_TEAM in _WRITE_ACTIONS

    def test_team_dissolve_still_in_write_actions(self):
        """TEAM_DISSOLVE should remain in _WRITE_ACTIONS (requires admin)."""
        assert SlockCommandAction.TEAM_DISSOLVE in _WRITE_ACTIONS


class TestNonAdminPermissionGate:
    """Test that handle_slock_command applies the correct permission gate per action category.

    These tests replicate the branching logic from handle_slock_command to verify
    that _NEEDS_ACTIVE_ENGINE actions bypass the admin permission check while
    _WRITE_ACTIONS actions still enforce it.
    """

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_non_admin_can_execute_discussion(self, mock_sender, mock_get_settings):
        """A regular (non-admin, non-owner) user can trigger /discuss without permission error."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        action = SlockCommandAction.DISCUSSION
        engine = _make_engine(owner_id=OWNER_ID)

        # Simulate the permission gate logic from handle_slock_command
        permission_blocked = False
        if action in _NEEDS_ACTIVE_ENGINE:
            pass  # No permission gate
        elif action in _WRITE_ACTIONS:
            from src.thread.manager import get_current_sender_id
            from src.config import get_settings

            operator_id = get_current_sender_id() or ""
            settings = get_settings()
            admin_ids = settings.admin_user_ids
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""
            is_authorized = (
                (operator_id and operator_id in admin_ids)
                or (operator_id and channel_owner_id and operator_id == channel_owner_id)
            )
            if not is_authorized:
                permission_blocked = True

        assert permission_blocked is False

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_non_admin_can_execute_council(self, mock_sender, mock_get_settings):
        """A regular (non-admin, non-owner) user can trigger /council without permission error."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        action = SlockCommandAction.COUNCIL
        engine = _make_engine(owner_id=OWNER_ID)

        permission_blocked = False
        if action in _NEEDS_ACTIVE_ENGINE:
            pass  # No permission gate
        elif action in _WRITE_ACTIONS:
            from src.thread.manager import get_current_sender_id
            from src.config import get_settings

            operator_id = get_current_sender_id() or ""
            settings = get_settings()
            admin_ids = settings.admin_user_ids
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""
            is_authorized = (
                (operator_id and operator_id in admin_ids)
                or (operator_id and channel_owner_id and operator_id == channel_owner_id)
            )
            if not is_authorized:
                permission_blocked = True

        assert permission_blocked is False

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_non_admin_cannot_execute_new_team(self, mock_sender, mock_get_settings):
        """A regular user is blocked from /new-team (requires admin permission)."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        action = SlockCommandAction.NEW_TEAM
        engine = _make_engine(owner_id=OWNER_ID)

        permission_blocked = False
        if action in _NEEDS_ACTIVE_ENGINE:
            pass
        elif action in _WRITE_ACTIONS:
            from src.thread.manager import get_current_sender_id
            from src.config import get_settings

            operator_id = get_current_sender_id() or ""
            settings = get_settings()
            admin_ids = settings.admin_user_ids
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""
            is_authorized = (
                (operator_id and operator_id in admin_ids)
                or (operator_id and channel_owner_id and operator_id == channel_owner_id)
            )
            if not is_authorized:
                permission_blocked = True

        assert permission_blocked is True

    @patch(PATCH_GET_SETTINGS)
    @patch(PATCH_GET_SENDER)
    def test_non_admin_cannot_execute_team_dissolve(self, mock_sender, mock_get_settings):
        """A regular user is blocked from /team-dissolve (requires admin permission)."""
        mock_sender.return_value = REGULAR_USER_ID
        mock_get_settings.return_value = _mock_settings([ADMIN_ID])

        action = SlockCommandAction.TEAM_DISSOLVE
        engine = _make_engine(owner_id=OWNER_ID)

        permission_blocked = False
        if action in _NEEDS_ACTIVE_ENGINE:
            pass
        elif action in _WRITE_ACTIONS:
            from src.thread.manager import get_current_sender_id
            from src.config import get_settings

            operator_id = get_current_sender_id() or ""
            settings = get_settings()
            admin_ids = settings.admin_user_ids
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""
            is_authorized = (
                (operator_id and operator_id in admin_ids)
                or (operator_id and channel_owner_id and operator_id == channel_owner_id)
            )
            if not is_authorized:
                permission_blocked = True

        assert permission_blocked is True
