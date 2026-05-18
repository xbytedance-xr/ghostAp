"""Tests for session_idle_timeout formatting logic in footer.

Covers edge cases: exact minutes, exact hours, non-exact rounding, 约 prefix.
"""

from __future__ import annotations

import pytest

from src.card.render.footer import _format_idle_timeout


class TestFormatIdleTimeout:
    """Test _format_idle_timeout edge cases."""

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (60, "1 分钟"),         # test_minimum_value_60
            (300, "5 分钟"),        # test_300_seconds
            (350, "6 分钟"),        # test_non_60_divisible (rounds up)
            (1800, "30 分钟"),      # test_1800_seconds (default)
            (3600, "1 小时"),       # test_3600_seconds (exact hour)
            (7200, "2 小时"),       # test_7200_seconds (exact hours)
        ],
        ids=[
            "60s-1min", "300s-5min", "350s-round-up-6min",
            "1800s-30min", "3600s-1h", "7200s-2h",
        ],
    )
    def test_exact_format(self, seconds, expected):
        assert _format_idle_timeout(seconds) == expected

    @pytest.mark.parametrize(
        "seconds",
        [4500, 5400],
        ids=["4500s-1.25h", "5400s-1.5h"],
    )
    def test_non_exact_hour_has_approx_prefix(self, seconds):
        """Non-exact hour values get '约' prefix with '小时'."""
        result = _format_idle_timeout(seconds)
        assert "约" in result
        assert "小时" in result
