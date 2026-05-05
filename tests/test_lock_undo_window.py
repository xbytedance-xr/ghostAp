"""Tests for lock_undo_window_seconds integration.

Verifies that:
1. Config validator enforces [60, 600] range and 60-multiple constraint
2. build_lock_success_card correctly formats the undo window minutes
3. The undo button value contains the correct _ue expiry timestamp
"""

import math
import time
from unittest.mock import patch, MagicMock

import pytest

from src.card.builders.lock_chat import build_lock_success_card


class TestLockUndoWindowConfig:
    """Validator enforces lock_undo_window_seconds constraints."""

    def test_valid_300_seconds(self):
        """300s (5 minutes) is a valid value."""
        from src.config import Settings
        # Should not raise — 300 is in [60, 600] and multiple of 60
        s = Settings(lock_undo_window_seconds=300)
        assert s.lock_undo_window_seconds == 300

    def test_valid_60_seconds(self):
        from src.config import Settings
        s = Settings(lock_undo_window_seconds=60)
        assert s.lock_undo_window_seconds == 60

    def test_valid_600_seconds(self):
        from src.config import Settings
        s = Settings(lock_undo_window_seconds=600)
        assert s.lock_undo_window_seconds == 600

    def test_reject_below_60(self):
        from src.config import Settings
        with pytest.raises(Exception):  # ValidationError
            Settings(lock_undo_window_seconds=30)

    def test_reject_above_600(self):
        from src.config import Settings
        with pytest.raises(Exception):
            Settings(lock_undo_window_seconds=660)

    def test_reject_non_multiple_of_60(self):
        from src.config import Settings
        with pytest.raises(Exception):
            Settings(lock_undo_window_seconds=90)


class TestLockUndoWindowInCard:
    """build_lock_success_card uses lock_undo_window_seconds for button text and value."""

    @patch("src.card.builders.lock_chat._t_wall")
    def test_300s_shows_5_minutes_in_button(self, mock_time):
        """300s config → markdown mentions '5 分钟', button is verb label '撤销'."""
        mock_time.time.return_value = 1000.0

        result = build_lock_success_card("lock", lock_undo_window_seconds=300)
        assert isinstance(result, tuple)
        md, buttons = result

        # Check markdown mentions 5 minutes
        assert "5 分钟" in md

        # Check button text is verb-style (no time info)
        assert len(buttons) == 1
        btn_text = buttons[0]["text"]["content"]
        assert "撤销" in btn_text

        # Check _ue expiry = current_time + 300
        assert buttons[0]["value"]["_ue"] == 1300

    @patch("src.card.builders.lock_chat._t_wall")
    def test_60s_shows_1_minute(self, mock_time):
        """60s config → button shows 1 minute."""
        mock_time.time.return_value = 2000.0

        result = build_lock_success_card("lock", lock_undo_window_seconds=60)
        md, buttons = result

        assert "1 分钟" in md
        assert buttons[0]["value"]["_ue"] == 2060

    @patch("src.card.builders.lock_chat._t_wall")
    def test_600s_shows_10_minutes(self, mock_time):
        """600s config → button shows 10 minutes."""
        mock_time.time.return_value = 5000.0

        result = build_lock_success_card("lock", lock_undo_window_seconds=600)
        md, buttons = result

        assert "10 分钟" in md
        assert buttons[0]["value"]["_ue"] == 5600

    @patch("src.card.builders.lock_chat._t_wall")
    def test_undo_button_action_is_unlock(self, mock_time):
        """Undo button carries /unlock as the retry command."""
        mock_time.time.return_value = 1000.0

        result = build_lock_success_card("lock", lock_undo_window_seconds=300)
        _, buttons = result

        assert buttons[0]["value"]["action"] == "retry_command"
        assert buttons[0]["value"]["_t"] == "/unlock"
        assert buttons[0]["value"]["_ul"] is True
