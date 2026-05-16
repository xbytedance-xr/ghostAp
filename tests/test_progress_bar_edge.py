"""Edge-case tests for render_progress_bar().

Covers:
- Boundary values: 0%, 100%, negative, >100
- Fractional rounding (e.g., 0.4 → 0%, 0.5 → 1%)
- Small pct guarantees at least 1 filled segment
- Custom total_segments
- Zero and negative total_segments
"""

from __future__ import annotations

from src.card.render.progress import render_progress_bar


class TestProgressBarEdgeCases:

    def test_zero_percent(self):
        result = render_progress_bar(0)
        assert result.endswith("0%")
        assert "▰" not in result  # all empty

    def test_100_percent(self):
        result = render_progress_bar(100)
        assert result.endswith("100%")
        assert "▱" not in result  # all filled

    def test_negative_clamped_to_zero(self):
        result = render_progress_bar(-10)
        assert "0%" in result

    def test_over_100_clamped(self):
        result = render_progress_bar(150)
        assert "100%" in result
        assert "150" not in result

    def test_fractional_rounds_down(self):
        """0.4 rounds to 0."""
        result = render_progress_bar(0.4)
        assert "0%" in result

    def test_fractional_rounds_up(self):
        """0.5 rounds to 0 (banker's rounding) or 1 depending on Python."""
        result = render_progress_bar(0.6)
        assert "1%" in result

    def test_small_positive_has_at_least_one_filled(self):
        """pct=1 should show at least one filled segment."""
        result = render_progress_bar(1)
        assert "▰" in result

    def test_50_percent_is_half_filled(self):
        result = render_progress_bar(50)
        # Default 10 segments: 5 filled
        assert result.count("▰") == 5
        assert result.count("▱") == 5
        assert "50%" in result

    def test_custom_total_segments(self):
        result = render_progress_bar(50, total_segments=20)
        assert result.count("▰") == 10
        assert result.count("▱") == 10

    def test_total_segments_zero_returns_empty(self):
        assert render_progress_bar(50, total_segments=0) == ""

    def test_total_segments_negative_returns_empty(self):
        assert render_progress_bar(50, total_segments=-5) == ""

    def test_total_segments_one(self):
        """Single segment bar: 0% → 0 filled, 50% → 1 filled."""
        result_0 = render_progress_bar(0, total_segments=1)
        assert "▱" in result_0
        assert "▰" not in result_0
        result_100 = render_progress_bar(100, total_segments=1)
        assert "▰" in result_100
        assert "▱" not in result_100

    def test_percentage_included_in_output(self):
        """All non-empty results include % suffix."""
        for pct in (0, 25, 50, 75, 100):
            result = render_progress_bar(pct)
            assert "%" in result

    def test_integer_input(self):
        """Integer pct works without error."""
        result = render_progress_bar(33)
        assert "33%" in result

    def test_float_input(self):
        """Float pct works without error."""
        result = render_progress_bar(33.7)
        assert "34%" in result

    # --- is_started parameter ---

    def test_is_started_zero_pct_shows_wathet(self):
        """is_started=True with pct=0 shows wathet-colored first segment."""
        result = render_progress_bar(0, is_started=True)
        assert "wathet" in result
        assert "▰" in result
        assert result.endswith("0%")

    def test_is_started_false_zero_pct_no_wathet(self):
        """is_started=False (default) with pct=0 shows no colored segment."""
        result = render_progress_bar(0, is_started=False)
        assert "wathet" not in result
        assert "▰" not in result

    def test_is_started_nonzero_pct_uses_normal_color(self):
        """is_started=True with pct>0 uses normal color, not wathet."""
        result = render_progress_bar(50, is_started=True)
        assert "wathet" not in result
        assert "blue" in result
