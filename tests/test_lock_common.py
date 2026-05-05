"""Tests for src/card/builders/lock_common.py — format_undo_window boundary cases."""

import pytest

from src.card.builders.lock_common import format_undo_window


class TestFormatUndoWindowBoundary:
    """Boundary and defensive tests for format_undo_window."""

    def test_float_input_rounds_correctly(self):
        """float(90.5) → int(90) → rounds to 2 minutes."""
        result = format_undo_window(90.5)
        assert "2" in result
        assert "分钟" in result

    def test_float_exactly_60(self):
        """float(60.0) → 1 minute."""
        result = format_undo_window(60.0)
        assert "1" in result
        assert "分钟" in result

    def test_none_input_returns_empty(self):
        """None input should return empty string, not crash."""
        result = format_undo_window(None)
        assert result == ""

    def test_negative_value_returns_empty(self):
        """Negative value (-60) should return empty string."""
        result = format_undo_window(-60)
        assert result == ""

    def test_zero_returns_empty(self):
        """Zero should return empty string."""
        result = format_undo_window(0)
        assert result == ""

    def test_string_input_returns_empty(self):
        """Non-numeric string should return empty, not crash."""
        result = format_undo_window("not-a-number")
        assert result == ""

    def test_numeric_string_works(self):
        """Numeric string '120' should work via int() coercion."""
        result = format_undo_window("120")
        assert "2" in result
        assert "分钟" in result

    def test_inf_returns_empty(self):
        """float('inf') should return empty string (OverflowError on int())."""
        result = format_undo_window(float("inf"))
        assert result == ""

    def test_nan_returns_empty(self):
        """float('nan') should return empty string."""
        result = format_undo_window(float("nan"))
        assert result == ""

    def test_normal_multiples_of_60(self):
        """Standard valid values: 60, 120, 300, 600."""
        assert "1" in format_undo_window(60) and "分钟" in format_undo_window(60)
        assert "5" in format_undo_window(300) and "分钟" in format_undo_window(300)
        assert "10" in format_undo_window(600) and "分钟" in format_undo_window(600)
