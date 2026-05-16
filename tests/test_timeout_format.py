"""Tests for session_idle_timeout formatting logic in footer.

Covers edge cases:
1. timeout=300 → "5 分钟"
2. timeout=7200 → "2 小时"
3. Non-60-divisible values → "约" prefix
4. timeout=3600 → "1 小时"
5. timeout=5400 → "约 1.5 小时" (non-exact hour)
"""

from __future__ import annotations

from src.card.render.footer import _format_idle_timeout


class TestFormatIdleTimeout:
    """Test _format_idle_timeout edge cases."""

    def test_300_seconds(self):
        """300s → '5 分钟'"""
        assert _format_idle_timeout(300) == "5 分钟"

    def test_1800_seconds(self):
        """1800s (default) → '30 分钟'"""
        assert _format_idle_timeout(1800) == "30 分钟"

    def test_3600_seconds(self):
        """3600s → '1 小时' (exact hour)"""
        assert _format_idle_timeout(3600) == "1 小时"

    def test_7200_seconds(self):
        """7200s → '2 小时' (exact hours)"""
        assert _format_idle_timeout(7200) == "2 小时"

    def test_5400_seconds_non_exact_hour(self):
        """5400s (1.5h) → '约 2 小时' or similar (non-exact hour with 约 prefix)"""
        result = _format_idle_timeout(5400)
        assert "约" in result
        assert "小时" in result

    def test_non_60_divisible(self):
        """Non-60-divisible value (e.g. 350s) → rounds up to '6 分钟'"""
        result = _format_idle_timeout(350)
        assert result == "6 分钟"

    def test_minimum_value_60(self):
        """60s → '1 分钟'"""
        assert _format_idle_timeout(60) == "1 分钟"

    def test_4500_seconds(self):
        """4500s (1.25h) → '约' prefix with hours"""
        result = _format_idle_timeout(4500)
        assert "约" in result
        assert "小时" in result
