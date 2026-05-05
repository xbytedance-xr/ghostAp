"""Tests for check_warning_banner elapsed-time threshold logic."""

from unittest.mock import MagicMock

import pytest

from src.feishu.renderers.base import BaseRenderer


class TestCheckWarningBanner:
    """Verify check_warning_banner returns correct results for different elapsed times."""

    @pytest.fixture
    def renderer(self):
        handler = MagicMock()
        handler.settings = MagicMock()
        handler.settings.engine_timeout_warning_seconds = 300  # 5 minutes
        handler.get_card_delivery.return_value = MagicMock()
        r = BaseRenderer.__new__(BaseRenderer)
        r.handler = handler
        r.settings = handler.settings
        return r

    def test_below_threshold_returns_none(self, renderer):
        """Duration below timeout_warning_seconds → None."""
        result = renderer.check_warning_banner(100.0, is_executing=True)
        assert result is None

    def test_at_threshold_returns_none(self, renderer):
        """Duration exactly at threshold → None (must exceed, not equal)."""
        result = renderer.check_warning_banner(300.0, is_executing=True)
        assert result is None

    def test_above_threshold_returns_warning(self, renderer):
        """Duration above timeout_warning_seconds → warning string."""
        result = renderer.check_warning_banner(301.0, is_executing=True)
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    def test_not_executing_returns_none(self, renderer):
        """Even with high duration, is_executing=False → None."""
        result = renderer.check_warning_banner(600.0, is_executing=False)
        assert result is None

    def test_zero_timeout_config_returns_none(self, renderer):
        """If timeout_warning_seconds is 0 (disabled), always returns None."""
        renderer.settings.engine_timeout_warning_seconds = 0
        result = renderer.check_warning_banner(9999.0, is_executing=True)
        assert result is None
