"""Tests for STATUS_BG_STYLE_MAP legal values and validate_background_style guard (AC-18)."""
from __future__ import annotations

import logging

import pytest

from src.slock_engine.card_templates.common import (
    STATUS_BG_STYLE_MAP,
    validate_background_style,
)


_VALID_BACKGROUND_STYLES = {"default", "grey", "card_primary"}


class TestStatusBgStyleMap:
    """Verify all values in STATUS_BG_STYLE_MAP are legal Feishu enum values."""

    def test_all_values_are_legal(self):
        """Every value in STATUS_BG_STYLE_MAP must be in the legal set."""
        for status, style in STATUS_BG_STYLE_MAP.items():
            assert style in _VALID_BACKGROUND_STYLES, (
                f"STATUS_BG_STYLE_MAP[{status}] = '{style}' is not a legal Feishu value. "
                f"Legal values: {_VALID_BACKGROUND_STYLES}"
            )

    def test_no_blue_yellow_purple(self):
        """Explicitly verify old illegal values are gone."""
        illegal = {"blue", "yellow", "purple"}
        for status, style in STATUS_BG_STYLE_MAP.items():
            assert style not in illegal, (
                f"STATUS_BG_STYLE_MAP[{status}] = '{style}' — "
                f"this was an illegal value that should have been replaced"
            )


class TestValidateBackgroundStyle:
    """Verify the guard function behavior."""

    def test_legal_values_pass_through(self):
        """Legal values are returned unchanged."""
        for val in _VALID_BACKGROUND_STYLES:
            assert validate_background_style(val) == val

    def test_illegal_value_falls_back_to_default(self):
        """Illegal values fall back to 'default'."""
        assert validate_background_style("blue") == "default"
        assert validate_background_style("yellow") == "default"
        assert validate_background_style("purple") == "default"
        assert validate_background_style("nonexistent") == "default"

    def test_illegal_value_logs_warning(self, caplog):
        """Illegal values trigger a warning log."""
        with caplog.at_level(logging.WARNING):
            validate_background_style("blue")
        assert "Invalid background_style 'blue'" in caplog.text

    def test_empty_string_falls_back(self):
        """Empty string is not a legal value."""
        assert validate_background_style("") == "default"
