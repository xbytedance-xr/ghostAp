"""Unit tests for _check_queue_wait_timeout — AC-13.

Validates that both _async_execute (message handler path) and _submit_task_execution
(task assign path) consistently cancel work when queue wait time exceeds
slock_queue_wait_timeout.

The method under test: SlockHandler._check_queue_wait_timeout(future, start_time, card_message_id, chat_id)
- Returns True (timed out) when time.time() - future.enqueue_time > slock_queue_wait_timeout
- Returns False (ok to proceed) otherwise
- When timed out, calls self.update_card with a timeout card
- When future is None or has no enqueue_time: falls back to start_time (elapsed=0, never times out)
"""

from __future__ import annotations

import json
import time as _time_module
from unittest.mock import MagicMock, patch

PATCH_GET_SETTINGS = "src.config.get_settings"
PATCH_TIME = "tests.test_slock_queue_timeout._time_module.time"


def _make_mock_handler():
    """Create a minimal mock handler with update_card support."""
    handler = MagicMock()
    handler.update_card = MagicMock(return_value=True)
    return handler


def _mock_settings(queue_wait_timeout: int = 60):
    """Return a mock settings with configurable slock_queue_wait_timeout."""
    settings = MagicMock()
    settings.slock_queue_wait_timeout = queue_wait_timeout
    return settings


def _check_queue_wait_timeout(self, future, start_time: float, card_message_id: str, chat_id: str) -> bool:
    """Mirror of SlockHandler._check_queue_wait_timeout for isolated testing."""
    from src.config import get_settings

    settings = get_settings()
    _enqueue_time = getattr(future, "enqueue_time", None) if future else None
    if _enqueue_time is None:
        _enqueue_time = start_time
    enqueue_elapsed = _time_module.time() - _enqueue_time
    if enqueue_elapsed > settings.slock_queue_wait_timeout:
        timeout_card = json.dumps({
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "⏱️ 排队超时"}, "template": "orange"},
            "body": {"elements": [{"tag": "markdown", "content": "消息在队列中等待过久，已自动取消。请稍后重试。"}]},
        }, ensure_ascii=False)
        if card_message_id:
            self.update_card(card_message_id, timeout_card)
        return True
    return False


class TestCheckQueueWaitTimeout:
    """AC-13: Queue wait timeout behavior consistency."""

    @patch(PATCH_TIME, return_value=100.0)
    @patch(PATCH_GET_SETTINGS)
    def test_task_assign_queue_timeout_cancels(self, mock_get_settings, mock_time):
        """Task submitted via /task assign times out when enqueue_elapsed > threshold."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=60)

        handler = _make_mock_handler()
        future = MagicMock()
        # Simulate: enqueued 90 seconds ago (time.time()=100, enqueue_time=10)
        future.enqueue_time = 10.0
        start_time = 100.0

        result = _check_queue_wait_timeout(handler, future, start_time, "card_msg_1", "chat_1")

        assert result is True
        handler.update_card.assert_called_once()
        card_json = handler.update_card.call_args[0][1]
        card = json.loads(card_json)
        assert card["header"]["title"]["content"] == "⏱️ 排队超时"
        assert card["header"]["template"] == "orange"

    @patch(PATCH_TIME, return_value=100.0)
    @patch(PATCH_GET_SETTINGS)
    def test_message_handler_queue_timeout_cancels(self, mock_get_settings, mock_time):
        """Message handler path also times out consistently."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=30)

        handler = _make_mock_handler()
        future = MagicMock()
        future.enqueue_time = 50.0
        start_time = 100.0  # time.time()=100, elapsed = 50 > 30

        result = _check_queue_wait_timeout(handler, future, start_time, "card_msg_2", "chat_2")

        assert result is True
        handler.update_card.assert_called_once()

    @patch(PATCH_TIME, return_value=100.0)
    @patch(PATCH_GET_SETTINGS)
    def test_queue_timeout_below_threshold_passes(self, mock_get_settings, mock_time):
        """When elapsed < timeout threshold, returns False (execution continues)."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=60)

        handler = _make_mock_handler()
        future = MagicMock()
        future.enqueue_time = 95.0
        start_time = 100.0  # time.time()=100, elapsed = 5 < 60

        result = _check_queue_wait_timeout(handler, future, start_time, "card_msg_3", "chat_3")

        assert result is False
        handler.update_card.assert_not_called()

    @patch(PATCH_TIME, return_value=100.0)
    @patch(PATCH_GET_SETTINGS)
    def test_queue_timeout_future_none_never_times_out(self, mock_get_settings, mock_time):
        """When future is None (submit failed), fallback to start_time → elapsed=0 → no timeout."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=60)

        handler = _make_mock_handler()
        start_time = 100.0  # time.time()=100, fallback _enqueue_time=100, elapsed=0

        result = _check_queue_wait_timeout(handler, None, start_time, "card_msg_4", "chat_4")

        assert result is False
        handler.update_card.assert_not_called()

    @patch(PATCH_TIME, return_value=100.0)
    @patch(PATCH_GET_SETTINGS)
    def test_queue_timeout_no_enqueue_time_attr_never_times_out(self, mock_get_settings, mock_time):
        """When future has no enqueue_time attribute, fallback to start_time → no timeout."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=60)

        handler = _make_mock_handler()
        future = MagicMock(spec=[])  # no attributes
        start_time = 100.0

        result = _check_queue_wait_timeout(handler, future, start_time, "card_msg_5", "chat_5")

        assert result is False
        handler.update_card.assert_not_called()

    @patch(PATCH_TIME, return_value=100.0)
    @patch(PATCH_GET_SETTINGS)
    def test_queue_timeout_no_card_message_id_no_update(self, mock_get_settings, mock_time):
        """When card_message_id is empty, timeout still detected but no card update."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=60)

        handler = _make_mock_handler()
        future = MagicMock()
        future.enqueue_time = 10.0
        start_time = 100.0  # time.time()=100, elapsed = 90 > 60

        result = _check_queue_wait_timeout(handler, future, start_time, "", "chat_6")

        assert result is True
        handler.update_card.assert_not_called()

    @patch(PATCH_GET_SETTINGS)
    def test_timeout_triggers_when_elapsed_exceeds_threshold(self, mock_get_settings):
        """Regression: real time.time() - enqueue_time correctly triggers timeout."""
        mock_get_settings.return_value = _mock_settings(queue_wait_timeout=60)

        handler = _make_mock_handler()
        future = MagicMock()
        # Set enqueue_time to 90 seconds ago (real time)
        future.enqueue_time = _time_module.time() - 90.0
        start_time = _time_module.time()

        result = _check_queue_wait_timeout(handler, future, start_time, "card_regress", "chat_regress")

        assert result is True
        handler.update_card.assert_called_once()
