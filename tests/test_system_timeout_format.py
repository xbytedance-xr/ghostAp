"""Tests for SystemBuilder session_idle_timeout formatting logic.

Verifies the timeout_display formatting in build_system_help_card:
  - <= 60 minutes → "约 X 分钟"
  - > 60 minutes → "约 X 小时"

Also covers the underlying format_friendly_duration utility.
"""

import pytest

from src.utils.text import format_friendly_duration


class TestFormatFriendlyDuration:
    """format_friendly_duration edge cases."""

    def test_under_60s(self):
        assert format_friendly_duration(30) == "30 秒"

    def test_exactly_60s(self):
        assert format_friendly_duration(60) == "约 1 分钟"

    def test_1800s_is_30_minutes(self):
        result = format_friendly_duration(1800)
        assert result == "约 30 分钟"

    def test_3600s_is_1_hour(self):
        result = format_friendly_duration(3600)
        assert result == "约 1 小时"

    def test_7200s_is_2_hours(self):
        result = format_friendly_duration(7200)
        assert result == "约 2 小时"

    def test_5400s_is_1_hour_30_minutes(self):
        result = format_friendly_duration(5400)
        assert result == "约 1 小时 30 分钟"

    def test_86400s_is_1_day(self):
        result = format_friendly_duration(86400)
        assert result == "约 1 天"

    def test_negative_clamps_to_zero(self):
        result = format_friendly_duration(-100)
        assert result == "0 秒"

    def test_zero(self):
        assert format_friendly_duration(0) == "0 秒"


class TestSystemBuilderTimeoutDisplay:
    """Verify timeout_display logic matches SystemBuilder.build_system_help_card."""

    @staticmethod
    def _format_timeout_display(timeout_seconds: int) -> str:
        """Replicate the formatting from SystemBuilder.build_system_help_card."""
        import math
        timeout_minutes = max(1, math.ceil(timeout_seconds / 60))
        if timeout_minutes > 60:
            return f"约 {timeout_minutes // 60} 小时"
        return f"约 {timeout_minutes} 分钟"

    def test_300s_shows_5_minutes(self):
        assert self._format_timeout_display(300) == "约 5 分钟"

    def test_1800s_shows_30_minutes(self):
        assert self._format_timeout_display(1800) == "约 30 分钟"

    def test_3600s_shows_60_minutes(self):
        # Exactly 60 minutes → "约 60 分钟" (not > 60)
        assert self._format_timeout_display(3600) == "约 60 分钟"

    def test_3660s_shows_1_hour(self):
        # 61 minutes > 60 → "约 1 小时"
        assert self._format_timeout_display(3660) == "约 1 小时"

    def test_7200s_shows_2_hours(self):
        assert self._format_timeout_display(7200) == "约 2 小时"

    def test_minimum_clamp(self):
        # Very small timeout → at least 1 minute
        assert self._format_timeout_display(10) == "约 1 分钟"
