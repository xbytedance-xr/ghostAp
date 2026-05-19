"""Unit tests for SlockHandler._check_assign_rate_limit.

Tests the sliding-window rate-limit logic for assign_task submissions.
The method under test (src/feishu/handlers/slock.py) does:
1. operator_id = get_current_sender_id() or ""
2. settings = get_settings() -> admin_user_ids, slock_assign_rate_limit
3. channel_owner_id from engine.channel.owner_id
4. Admin/owner users bypass rate-limit (return True always)
5. Regular users: sliding 60s window, max slock_assign_rate_limit submissions
6. When rate limited: reply_text with warning, return False
"""

from __future__ import annotations

import time as _real_time
from unittest.mock import MagicMock, patch

import pytest

PATCH_GET_SENDER = "src.thread.manager.get_current_sender_id"
PATCH_GET_SETTINGS = "src.config.get_settings"

ADMIN_ID = "admin1"
OWNER_ID = "owner1"
REGULAR_USER_ID = "user1"
CHAT_ID = "chat_001"
MESSAGE_ID = "msg_001"


def _make_settings(admin_ids=None, rate_limit=5):
    """Create a mock settings object with admin_user_ids and slock_assign_rate_limit."""
    settings = MagicMock()
    settings.admin_user_ids = frozenset(admin_ids or {ADMIN_ID})
    settings.slock_assign_rate_limit = rate_limit
    return settings


def _make_engine(owner_id=OWNER_ID):
    """Create a mock engine with a channel that has the given owner_id."""
    engine = MagicMock()
    engine.channel = MagicMock()
    engine.channel.owner_id = owner_id
    return engine


def _make_handler():
    """Create a minimal mock handler with _rate_limit_tracker and reply_text."""
    handler = MagicMock()
    handler._rate_limit_tracker = {}
    handler.reply_text = MagicMock(return_value=True)
    return handler


def _check_assign_rate_limit(self, engine, message_id: str, chat_id: str) -> bool:
    """Mirror of SlockHandler._check_assign_rate_limit for isolated testing.

    Faithful copy of the production logic so we can test the rate-limit
    algorithm without importing the full handler chain (which requires lark_oapi).
    """
    import time as _time

    from src.config import get_settings
    from src.thread.manager import get_current_sender_id

    operator_id = get_current_sender_id() or ""
    settings = get_settings()
    admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()
    channel_owner_id = ""
    if engine.channel:
        channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

    # Admin and owner bypass rate-limit
    is_privileged = (
        (operator_id and operator_id in admin_ids)
        or (operator_id and channel_owner_id and operator_id == channel_owner_id)
    )
    if is_privileged:
        return True

    # Rate-limit for regular users: sliding window of 60s
    rate_limit = settings.slock_assign_rate_limit
    tracker_key = f"{chat_id}:{operator_id}"
    now = _time.time()
    window = 60.0

    timestamps = self._rate_limit_tracker.get(tracker_key, [])
    # Prune expired entries
    timestamps = [t for t in timestamps if now - t < window]

    if len(timestamps) >= rate_limit:
        self.reply_text(
            message_id,
            f"\u26a0\ufe0f \u4efb\u52a1\u63d0\u4ea4\u9891\u7387\u8d85\u9650\uff08\u6bcf\u5206\u949f\u6700\u591a {rate_limit} \u6b21\uff09\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002",
        )
        self._rate_limit_tracker[tracker_key] = timestamps
        return False

    timestamps.append(now)
    self._rate_limit_tracker[tracker_key] = timestamps
    return True


class TestAssignTaskRateLimit:
    """Tests for the assign_task rate-limit mechanism."""

    def test_regular_user_within_limit(self):
        """Regular user can submit up to 5 tasks within 60s window."""
        handler = _make_handler()
        engine = _make_engine()
        settings = _make_settings(rate_limit=5)

        with (
            patch(PATCH_GET_SENDER, return_value=REGULAR_USER_ID),
            patch(PATCH_GET_SETTINGS, return_value=settings),
        ):
            for i in range(5):
                result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
                assert result is True, f"Call {i+1} should be allowed"

        # reply_text should NOT have been called (no rate-limit hit)
        handler.reply_text.assert_not_called()

    def test_regular_user_exceeds_limit(self):
        """6th call by regular user within 60s is blocked with rate-limit message."""
        handler = _make_handler()
        engine = _make_engine()
        settings = _make_settings(rate_limit=5)

        with (
            patch(PATCH_GET_SENDER, return_value=REGULAR_USER_ID),
            patch(PATCH_GET_SETTINGS, return_value=settings),
        ):
            # First 5 calls succeed
            for _ in range(5):
                assert _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID) is True

            # 6th call should be blocked
            result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
            assert result is False

        # Verify rate-limit warning was sent
        handler.reply_text.assert_called_once()
        call_args = handler.reply_text.call_args
        assert MESSAGE_ID == call_args[0][0]
        assert "\u4efb\u52a1\u63d0\u4ea4\u9891\u7387\u8d85\u9650" in call_args[0][1]

    def test_admin_bypass(self):
        """Admin user can submit unlimited tasks (rate-limit bypassed)."""
        handler = _make_handler()
        engine = _make_engine()
        settings = _make_settings(admin_ids={ADMIN_ID}, rate_limit=5)

        with (
            patch(PATCH_GET_SENDER, return_value=ADMIN_ID),
            patch(PATCH_GET_SETTINGS, return_value=settings),
        ):
            # Admin should be able to submit many more than the limit
            for i in range(20):
                result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
                assert result is True, f"Admin call {i+1} should be allowed"

        handler.reply_text.assert_not_called()

    def test_owner_bypass(self):
        """Channel owner can submit unlimited tasks (rate-limit bypassed)."""
        handler = _make_handler()
        engine = _make_engine(owner_id=OWNER_ID)
        settings = _make_settings(admin_ids=set(), rate_limit=5)  # Owner not in admin list

        with (
            patch(PATCH_GET_SENDER, return_value=OWNER_ID),
            patch(PATCH_GET_SETTINGS, return_value=settings),
        ):
            for i in range(20):
                result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
                assert result is True, f"Owner call {i+1} should be allowed"

        handler.reply_text.assert_not_called()

    def test_window_expiration(self):
        """After 60s window expires, counter resets and user can submit again."""
        handler = _make_handler()
        engine = _make_engine()
        settings = _make_settings(rate_limit=5)

        base_time = 1000000.0

        with (
            patch(PATCH_GET_SENDER, return_value=REGULAR_USER_ID),
            patch(PATCH_GET_SETTINGS, return_value=settings),
            patch("time.time") as mock_time,
        ):
            # Submit 5 tasks at base_time
            mock_time.return_value = base_time
            for _ in range(5):
                assert _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID) is True

            # 6th call at base_time still blocked
            mock_time.return_value = base_time + 30.0  # only 30s later
            result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
            assert result is False

            # After 60s window passes, should be allowed again
            mock_time.return_value = base_time + 61.0
            result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
            assert result is True

    def test_expired_entries_cleaned(self):
        """Old timestamps outside the 60s window are pruned from the tracker."""
        handler = _make_handler()
        engine = _make_engine()
        settings = _make_settings(rate_limit=5)
        tracker_key = f"{CHAT_ID}:{REGULAR_USER_ID}"

        base_time = 1000000.0

        with (
            patch(PATCH_GET_SENDER, return_value=REGULAR_USER_ID),
            patch(PATCH_GET_SETTINGS, return_value=settings),
            patch("time.time") as mock_time,
        ):
            # Pre-populate tracker with old timestamps (> 60s ago)
            old_timestamps = [base_time - 120.0, base_time - 100.0, base_time - 80.0]
            handler._rate_limit_tracker[tracker_key] = old_timestamps.copy()

            # Current time: all old entries should be expired
            mock_time.return_value = base_time
            result = _check_assign_rate_limit(handler, engine, MESSAGE_ID, CHAT_ID)
            assert result is True

            # Verify old entries were pruned — only the new one remains
            stored = handler._rate_limit_tracker[tracker_key]
            assert len(stored) == 1
            assert stored[0] == base_time
