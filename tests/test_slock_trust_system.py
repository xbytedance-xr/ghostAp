"""Tests for channel trust rules system (AC20, AC21).

AC20: After storing permanent trust for a role pair, the same pair in the same
      channel skips confirmation.
AC21: After storing 1-hour trust, discussions skip confirmation within 60 minutes;
      after 60+ minutes, confirmation is required again.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

_acp_available = pytest.importorskip("acp", reason="acp package not installed")

from src.slock_engine.engine import SlockEngine  # noqa: E402


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


@patch("src.slock_engine.engine.create_engine_session")
def _make_engine(mock_create_session, tmp_path):
    """Create a minimal SlockEngine for trust-rule testing."""
    mock_create_session.return_value = None
    return SlockEngine(
        chat_id="test_chat",
        root_path=str(tmp_path / "root"),
        memory_base_path=str(tmp_path / "memory"),
    )


# ==================================================================
# AC20: Permanent Trust
# ==================================================================


class TestPermanentTrust:
    """AC20: Permanent trust bypasses confirmation for matching role pairs."""

    def test_permanent_trust_stored_and_bypasses(self, tmp_path):
        """AC20: After storing permanent trust, same pair skips confirmation."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_perm_1"
        role_pair = "coder->reviewer"

        # Save permanent trust
        engine._save_trust_rule(channel_id, role_pair, "permanent")

        # Same channel + same role pair -> bypass
        assert engine._check_trust_bypass(channel_id, role_pair) is True

    def test_permanent_trust_different_pair_no_bypass(self, tmp_path):
        """Different role pair is NOT bypassed."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_perm_2"

        engine._save_trust_rule(channel_id, "coder->reviewer", "permanent")

        # Different pair -> no bypass
        assert engine._check_trust_bypass(channel_id, "architect->coder") is False

    def test_permanent_trust_different_channel_no_bypass(self, tmp_path):
        """Same pair in different channel is NOT bypassed."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_a = "ch_perm_a"
        channel_b = "ch_perm_b"
        role_pair = "coder->reviewer"

        engine._save_trust_rule(channel_a, role_pair, "permanent")

        # Different channel -> no bypass
        assert engine._check_trust_bypass(channel_b, role_pair) is False

    def test_permanent_trust_persists_across_load(self, tmp_path):
        """Permanent trust survives cache clear (reloads from memory manager)."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_perm_persist"
        role_pair = "designer->coder"

        engine._save_trust_rule(channel_id, role_pair, "permanent")

        # Clear in-memory cache to force reload from disk
        engine._channel_trust_rules.pop(channel_id, None)

        # Should still bypass after reload
        assert engine._check_trust_bypass(channel_id, role_pair) is True

    def test_permanent_trust_multiple_pairs_same_channel(self, tmp_path):
        """Multiple permanent trust rules can coexist in one channel."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_perm_multi"

        engine._save_trust_rule(channel_id, "coder->reviewer", "permanent")
        engine._save_trust_rule(channel_id, "architect->coder", "permanent")

        assert engine._check_trust_bypass(channel_id, "coder->reviewer") is True
        assert engine._check_trust_bypass(channel_id, "architect->coder") is True
        # Unstored pair still blocked
        assert engine._check_trust_bypass(channel_id, "pm->designer") is False


# ==================================================================
# AC21: Timed Trust (1-hour window)
# ==================================================================


class TestTimedTrust:
    """AC21: 1-hour trust expires after 60 minutes."""

    def test_timed_trust_within_window_bypasses(self, tmp_path):
        """AC21: Within 60 minutes, trust bypasses confirmation."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_timed_1"
        role_pair = "coder->reviewer"

        # Store trust that expires 1 hour from now
        expires_at = str(time.time() + 3600)
        engine._save_trust_rule(channel_id, role_pair, expires_at)

        assert engine._check_trust_bypass(channel_id, role_pair) is True

    def test_timed_trust_after_expiry_requires_confirmation(self, tmp_path):
        """AC21: After 60+ minutes, trust no longer bypasses."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_timed_2"
        role_pair = "coder->reviewer"

        # Store trust that already expired (1 second ago)
        expired_at = str(time.time() - 1)
        engine._save_trust_rule(channel_id, role_pair, expired_at)

        assert engine._check_trust_bypass(channel_id, role_pair) is False

    def test_timed_trust_boundary_just_expired(self, tmp_path):
        """Just at the expiry boundary (past timestamp) requires confirmation."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_timed_boundary"
        role_pair = "architect->coder"

        # Expired 60 minutes ago (simulating expiry exactly at boundary)
        expired_at = str(time.time() - 3600)
        engine._save_trust_rule(channel_id, role_pair, expired_at)

        assert engine._check_trust_bypass(channel_id, role_pair) is False

    def test_timed_trust_far_future_bypasses(self, tmp_path):
        """Trust with far-future expiry still bypasses."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_timed_future"
        role_pair = "coder->reviewer"

        # Expires in 24 hours
        expires_at = str(time.time() + 86400)
        engine._save_trust_rule(channel_id, role_pair, expires_at)

        assert engine._check_trust_bypass(channel_id, role_pair) is True

    def test_timed_trust_different_channel_no_bypass(self, tmp_path):
        """Timed trust in channel A does not apply to channel B."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_a = "ch_timed_a"
        channel_b = "ch_timed_b"
        role_pair = "coder->reviewer"

        expires_at = str(time.time() + 3600)
        engine._save_trust_rule(channel_a, role_pair, expires_at)

        assert engine._check_trust_bypass(channel_a, role_pair) is True
        assert engine._check_trust_bypass(channel_b, role_pair) is False

    def test_timed_trust_cleanup_on_expiry(self, tmp_path):
        """Expired trust is cleaned up from rules on check."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_timed_cleanup"
        role_pair = "coder->reviewer"

        # Store already-expired trust
        expired_at = str(time.time() - 100)
        engine._save_trust_rule(channel_id, role_pair, expired_at)

        # Check triggers cleanup
        assert engine._check_trust_bypass(channel_id, role_pair) is False

        # After cleanup, the rule should be gone from the in-memory cache
        rules = engine._channel_trust_rules.get(channel_id, {})
        assert role_pair not in rules or rules.get(role_pair) == ""

    @patch("src.slock_engine.engine.time.time")
    def test_timed_trust_mock_time_progression(self, mock_time, tmp_path):
        """Simulate time progression: trust valid at T, expired at T+3601."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_timed_mock"
        role_pair = "coder->reviewer"

        base_time = 1700000000.0

        # At T=base: save trust expiring at T+3600
        mock_time.return_value = base_time
        expires_at = str(base_time + 3600)
        engine._save_trust_rule(channel_id, role_pair, expires_at)

        # At T=base+1800 (30 min later): should still bypass
        mock_time.return_value = base_time + 1800
        assert engine._check_trust_bypass(channel_id, role_pair) is True

        # At T=base+3601 (just past 1 hour): should NOT bypass
        # Clear cache to ensure fresh check
        engine._channel_trust_rules.pop(channel_id, None)
        engine._channel_trust_rules[channel_id] = {role_pair: expires_at}
        mock_time.return_value = base_time + 3601
        assert engine._check_trust_bypass(channel_id, role_pair) is False


# ==================================================================
# Edge cases — no trust stored
# ==================================================================


class TestNoTrustStored:
    """Verify default behavior when no trust rules exist."""

    def test_no_trust_returns_false(self, tmp_path):
        """With no stored rules, bypass is always False."""
        engine = _make_engine(tmp_path=tmp_path)
        assert engine._check_trust_bypass("any_channel", "any->pair") is False

    def test_invalid_trust_value_returns_false(self, tmp_path):
        """A non-numeric, non-permanent value returns False."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_invalid"
        role_pair = "coder->reviewer"

        # Directly inject invalid value
        engine._channel_trust_rules[channel_id] = {role_pair: "garbage_value"}

        assert engine._check_trust_bypass(channel_id, role_pair) is False

    def test_empty_string_trust_value_returns_false(self, tmp_path):
        """An empty string trust value (cleanup artifact) returns False."""
        engine = _make_engine(tmp_path=tmp_path)
        channel_id = "ch_empty"
        role_pair = "coder->reviewer"

        engine._channel_trust_rules[channel_id] = {role_pair: ""}

        assert engine._check_trust_bypass(channel_id, role_pair) is False
