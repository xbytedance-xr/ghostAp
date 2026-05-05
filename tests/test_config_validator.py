"""Tests for CardSessionConfig field validators — auto-ceil and range checks."""

import pytest

from src.config import CardSessionConfig


class TestLockTTLAutoCeil:
    """Verify CARD_SESSION_LOCK_TTL auto-ceil to nearest 60 multiple."""

    def test_exact_multiple_unchanged(self):
        """60, 120, 600 should pass through unchanged."""
        config = CardSessionConfig(session_lock_ttl=60)
        assert config.session_lock_ttl == 60.0

        config = CardSessionConfig(session_lock_ttl=120)
        assert config.session_lock_ttl == 120.0

        config = CardSessionConfig(session_lock_ttl=600)
        assert config.session_lock_ttl == 600.0

    def test_90_ceils_to_120(self):
        """90 is not a multiple of 60 → ceil to 120."""
        config = CardSessionConfig(session_lock_ttl=90)
        assert config.session_lock_ttl == 120.0

    def test_121_ceils_to_180(self):
        """121 → ceil(121/60)*60 = 180."""
        config = CardSessionConfig(session_lock_ttl=121)
        assert config.session_lock_ttl == 180.0

    def test_61_ceils_to_120(self):
        """61 → ceil(61/60)*60 = 120."""
        config = CardSessionConfig(session_lock_ttl=61)
        assert config.session_lock_ttl == 120.0

    def test_3599_ceils_to_3600(self):
        """3599 → ceil(3599/60)*60 = 3600. Must also set idle_timeout >= 3600."""
        config = CardSessionConfig(session_lock_ttl=3599, session_idle_timeout=7200)
        assert config.session_lock_ttl == 3600.0


class TestLockTTLRangeRejection:
    """Verify out-of-range values raise ValueError."""

    def test_below_minimum_raises(self):
        """Values < 60 should raise ValueError."""
        with pytest.raises(ValueError, match="60.*3600"):
            CardSessionConfig(session_lock_ttl=30)

    def test_above_maximum_raises(self):
        """Values > 3600 should raise ValueError."""
        with pytest.raises(ValueError, match="60.*3600"):
            CardSessionConfig(session_lock_ttl=4000)

    def test_zero_raises(self):
        """Zero should raise ValueError."""
        with pytest.raises(ValueError):
            CardSessionConfig(session_lock_ttl=0)

    def test_negative_raises(self):
        """Negative should raise ValueError."""
        with pytest.raises(ValueError):
            CardSessionConfig(session_lock_ttl=-60)
