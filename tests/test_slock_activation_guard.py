"""Comprehensive tests for ActivationGuard class.

Covers:
    - Permission checks (admin, chat owner, whitelist, passive mode)
    - Rate limiting (per-user, global, sliding window)
    - reset() behavior
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from src.slock_engine.activation_guard import ActivationGuard


@dataclass
class FakeSettings:
    """Minimal settings stub for ActivationGuard tests."""

    admin_user_ids: str = ""
    slock_auto_activate_whitelist_user_ids: str = ""
    slock_passive_mode: bool = False
    slock_auto_activate_default_policy: str = "allow_all"
    slock_auto_activate_rate_limit_per_user: int = 3
    slock_auto_activate_rate_limit_global: int = 10


@pytest.fixture
def guard() -> ActivationGuard:
    """Fresh ActivationGuard instance per test."""
    return ActivationGuard()


# --------------------------------------------------------------------------- #
# Permission tests
# --------------------------------------------------------------------------- #


class TestAdminAlwaysAllowed:
    """Admin users are always permitted regardless of whitelist."""

    def test_single_admin(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(admin_user_ids="admin1")
        allowed, _ = guard.can_auto_activate("admin1", "chat1", settings)
        assert allowed is True

    def test_admin_in_comma_separated_list(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(admin_user_ids="admin1, admin2, admin3")
        allowed, _ = guard.can_auto_activate("admin2", "chat1", settings)
        assert allowed is True

    def test_admin_with_whitespace(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(admin_user_ids="  admin1 , admin2 ")
        allowed, _ = guard.can_auto_activate("admin1", "chat1", settings)
        assert allowed is True

    def test_admin_bypasses_whitelist_restriction(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            admin_user_ids="admin1",
            slock_auto_activate_whitelist_user_ids="userA",
        )
        # admin1 is not in whitelist, but still allowed as admin
        allowed, _ = guard.can_auto_activate("admin1", "chat1", settings)
        assert allowed is True


class TestChatOwnerAlwaysAllowed:
    """Chat owner parameter was removed — these tests verify the removal."""

    def test_owner_allowed_without_param(self, guard: ActivationGuard) -> None:
        """Owner concept removed; any user in allow_all+passive policy is permitted."""
        settings = FakeSettings(slock_passive_mode=True)
        allowed, _ = guard.can_auto_activate("owner1", "chat1", settings)
        assert allowed is True

    def test_owner_not_in_whitelist_denied(self, guard: ActivationGuard) -> None:
        """Without owner bypass, non-whitelisted users are denied when whitelist is set."""
        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids="userA,userB",
            slock_passive_mode=True,
        )
        allowed, _ = guard.can_auto_activate("owner1", "chat1", settings)
        assert allowed is False

    def test_allow_all_policy_permits_any_user(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids="",
            slock_passive_mode=True,
        )
        # allow_all policy with empty whitelist allows everyone
        allowed, _ = guard.can_auto_activate("other_user", "chat1", settings)
        assert allowed is True


class TestWhitelistAllowed:
    """Whitelisted users are allowed when whitelist is configured."""

    def test_whitelist_user_allowed(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids="userA,userB,userC",
        )
        allowed, _ = guard.can_auto_activate("userB", "chat1", settings)
        assert allowed is True

    def test_whitelist_user_with_spaces(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids=" userA , userB ",
        )
        allowed, _ = guard.can_auto_activate("userB", "chat1", settings)
        assert allowed is True


class TestNonPrivilegedUserDenied:
    """Non-admin, non-owner, non-whitelist users are denied when whitelist is configured."""

    def test_denied_when_whitelist_configured(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids="userA,userB",
        )
        allowed, _ = guard.can_auto_activate("intruder", "chat1", settings)
        assert allowed is False

    def test_denied_with_empty_sender(self, guard: ActivationGuard) -> None:
        settings = FakeSettings()
        allowed, _ = guard.can_auto_activate("", "chat1", settings)
        assert allowed is False

    def test_denied_when_passive_mode_off_and_no_whitelist(
        self, guard: ActivationGuard
    ) -> None:
        """With no whitelist AND passive_mode=False, user is denied."""
        settings = FakeSettings(
            slock_passive_mode=False,
            slock_auto_activate_whitelist_user_ids="",
        )
        # The user must be explicitly listed somewhere to pass
        # Actually, re-check: if whitelist_str is empty and passive_mode is False,
        # the fallback does NOT grant permission.
        allowed, _ = guard.can_auto_activate("random_user", "chat1", settings)
        assert allowed is False


class TestPassiveModeBackwardCompat:
    """All users allowed when no whitelist configured AND policy=allow_all AND passive_mode=True."""

    def test_any_user_allowed(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_whitelist_user_ids="",
        )
        allowed, _ = guard.can_auto_activate("anyone", "chat1", settings)
        assert allowed is True

    def test_passive_mode_ignored_when_whitelist_present(
        self, guard: ActivationGuard
    ) -> None:
        """If a whitelist is configured, passive_mode does NOT grant blanket access."""
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_whitelist_user_ids="userA",
        )
        # 'other' not in whitelist -> denied despite passive_mode
        allowed, _ = guard.can_auto_activate("other", "chat1", settings)
        assert allowed is False


# --------------------------------------------------------------------------- #
# Rate limiting tests
# --------------------------------------------------------------------------- #


class TestPerUserRateLimit:
    """Per-user rate limit: default 3 per 60s window."""

    def test_three_calls_succeed_fourth_fails(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=3,
            slock_auto_activate_rate_limit_global=100,  # high to not interfere
        )
        allowed1, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed2, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed3, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is True
        # 4th call hits the per-user limit
        allowed4, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed4 is False

    def test_different_users_independent(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=2,
            slock_auto_activate_rate_limit_global=100,
        )
        # user1 exhausts their limit
        allowed1, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed2, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed3, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is False

        # user2 still has their own quota
        allowed4, _ = guard.can_auto_activate("user2", "chat1", settings)
        allowed5, _ = guard.can_auto_activate("user2", "chat1", settings)
        allowed6, _ = guard.can_auto_activate("user2", "chat1", settings)
        assert allowed4 is True
        assert allowed5 is True
        assert allowed6 is False


class TestGlobalRateLimit:
    """Global rate limit: default 10 per 60s window."""

    def test_global_limit_exceeded(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=100,  # high to not interfere
            slock_auto_activate_rate_limit_global=10,
        )
        # 10 different users each activate once
        for i in range(10):
            allowed, _ = guard.can_auto_activate(f"user{i}", "chat1", settings)
            assert allowed is True

        # 11th user hits global limit
        allowed, _ = guard.can_auto_activate("user_extra", "chat1", settings)
        assert allowed is False

    def test_global_limit_blocks_even_admin(self, guard: ActivationGuard) -> None:
        """Rate limit applies even to admin users after permission check."""
        settings = FakeSettings(
            admin_user_ids="admin1",
            slock_auto_activate_rate_limit_per_user=100,
            slock_auto_activate_rate_limit_global=2,
        )
        allowed1, _ = guard.can_auto_activate("admin1", "chat1", settings)
        allowed2, _ = guard.can_auto_activate("admin1", "chat1", settings)
        assert allowed1 is True
        assert allowed2 is True
        # Global limit reached
        allowed3, _ = guard.can_auto_activate("admin1", "chat1", settings)
        assert allowed3 is False


class TestRateLimitSlidingWindow:
    """Rate limit window slides: after 60s, calls succeed again."""

    def test_window_expires_per_user(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=2,
            slock_auto_activate_rate_limit_global=100,
        )

        fake_time = 1000.0

        with patch("src.slock_engine.activation_guard.time.time", return_value=fake_time):
            allowed1, _ = guard.can_auto_activate("user1", "chat1", settings)
            allowed2, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed1 is True
            assert allowed2 is True
            # Limit reached
            allowed3, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed3 is False

        # Advance time by 61 seconds (past the 60s window)
        fake_time_after = fake_time + 61.0
        with patch(
            "src.slock_engine.activation_guard.time.time", return_value=fake_time_after
        ):
            # Old timestamps are now expired, user can activate again
            allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed is True

    def test_window_expires_global(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=100,
            slock_auto_activate_rate_limit_global=3,
        )

        fake_time = 2000.0

        with patch("src.slock_engine.activation_guard.time.time", return_value=fake_time):
            for i in range(3):
                allowed, _ = guard.can_auto_activate(f"user{i}", "chat1", settings)
                assert allowed is True
            # Global limit reached
            allowed, _ = guard.can_auto_activate("user_next", "chat1", settings)
            assert allowed is False

        # Advance past window
        with patch(
            "src.slock_engine.activation_guard.time.time", return_value=fake_time + 61.0
        ):
            allowed, _ = guard.can_auto_activate("user_next", "chat1", settings)
            assert allowed is True

    def test_partial_window_expiry(self, guard: ActivationGuard) -> None:
        """Only timestamps older than 60s are pruned."""
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=3,
            slock_auto_activate_rate_limit_global=100,
        )

        # First call at t=0
        with patch("src.slock_engine.activation_guard.time.time", return_value=1000.0):
            allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed is True

        # Second call at t=30
        with patch("src.slock_engine.activation_guard.time.time", return_value=1030.0):
            allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed is True

        # Third call at t=50
        with patch("src.slock_engine.activation_guard.time.time", return_value=1050.0):
            allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed is True

        # At t=50, all 3 within window -> 4th denied
        with patch("src.slock_engine.activation_guard.time.time", return_value=1050.0):
            allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed is False

        # At t=61, the first call (t=1000) is expired, only 2 remain -> allowed
        with patch("src.slock_engine.activation_guard.time.time", return_value=1061.0):
            allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
            assert allowed is True


# --------------------------------------------------------------------------- #
# reset() tests
# --------------------------------------------------------------------------- #


class TestReset:
    """reset() clears all counters."""

    def test_reset_clears_per_user_counters(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=2,
            slock_auto_activate_rate_limit_global=100,
        )
        allowed1, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed2, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed3, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is False

        guard.reset()

        # After reset, user can activate again
        allowed4, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed4 is True

    def test_reset_clears_global_counters(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=100,
            slock_auto_activate_rate_limit_global=2,
        )
        allowed1, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed2, _ = guard.can_auto_activate("user2", "chat1", settings)
        allowed3, _ = guard.can_auto_activate("user3", "chat1", settings)
        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is False

        guard.reset()

        allowed4, _ = guard.can_auto_activate("user3", "chat1", settings)
        assert allowed4 is True

    def test_reset_is_idempotent(self, guard: ActivationGuard) -> None:
        """Calling reset multiple times does not raise."""
        guard.reset()
        guard.reset()
        settings = FakeSettings(slock_passive_mode=True)
        allowed, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed is True


# --------------------------------------------------------------------------- #
# Default-deny (admin_only) policy tests
# --------------------------------------------------------------------------- #


class TestDefaultDenyPolicy:
    """When policy=admin_only (product default), only admin/owner/whitelist can activate."""

    def test_admin_only_denies_random_user(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="admin_only",
        )
        allowed, _ = guard.can_auto_activate("random_user", "chat1", settings)
        assert allowed is False

    def test_admin_only_allows_admin(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            admin_user_ids="admin1",
            slock_passive_mode=True,
            slock_auto_activate_default_policy="admin_only",
        )
        allowed, _ = guard.can_auto_activate("admin1", "chat1", settings)
        assert allowed is True

    def test_admin_only_denies_non_whitelisted(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="admin_only",
        )
        # admin_only with no whitelist: non-admin users are denied
        allowed, _ = guard.can_auto_activate("owner1", "chat1", settings)
        assert allowed is False

    def test_admin_only_allows_whitelisted_user(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="admin_only",
            slock_auto_activate_whitelist_user_ids="userA",
        )
        allowed, _ = guard.can_auto_activate("userA", "chat1", settings)
        assert allowed is True

    def test_allow_all_policy_permits_any_user(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="allow_all",
        )
        allowed, _ = guard.can_auto_activate("random_user", "chat1", settings)
        assert allowed is True


# --------------------------------------------------------------------------- #
# purge_stale() tests
# --------------------------------------------------------------------------- #


class TestPurgeStale:
    """purge_stale() removes expired entries from tracking."""

    def test_purge_removes_old_entries(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(slock_passive_mode=True)

        # Record some activations
        with patch("src.slock_engine.activation_guard.time.time", return_value=1000.0):
            guard.can_auto_activate("user1", "chat1", settings)
            guard.can_auto_activate("user2", "chat1", settings)

        # Purge after window expires
        with patch("src.slock_engine.activation_guard.time.time", return_value=1070.0):
            removed = guard.purge_stale(window=60.0)
            assert removed >= 2

    def test_purge_keeps_fresh_entries(self, guard: ActivationGuard) -> None:
        settings = FakeSettings(slock_passive_mode=True)

        with patch("src.slock_engine.activation_guard.time.time", return_value=1000.0):
            guard.can_auto_activate("user1", "chat1", settings)

        # Purge within window — nothing to remove
        with patch("src.slock_engine.activation_guard.time.time", return_value=1030.0):
            removed = guard.purge_stale(window=60.0)
            assert removed == 0


# --------------------------------------------------------------------------- #
# AC-R15: purge_stale periodic timer & memory leak prevention
# --------------------------------------------------------------------------- #


class TestPurgeStalePeriodic:
    """AC-R15: purge_stale must clean expired entries periodically."""

    def test_purge_removes_expired_entries(self, guard: ActivationGuard) -> None:
        """After purge_stale, no expired timestamps remain."""
        # Simulate 1000 different users with old timestamps
        old_time = 1000.0
        for i in range(1000):
            user_id = f"user_{i}"
            guard._user_timestamps[user_id] = [old_time]

        assert len(guard._user_timestamps) == 1000

        # Purge with default 60s window — all entries are older than 60s
        with patch(
            "src.slock_engine.activation_guard.time.time", return_value=old_time + 120.0
        ):
            removed = guard.purge_stale(window=60.0)

        # All entries should be removed (all are older than 60s)
        assert removed == 1000
        assert len(guard._user_timestamps) == 0

    def test_purge_keeps_recent_entries(self, guard: ActivationGuard) -> None:
        """purge_stale keeps entries within the window."""
        base_time = 2000.0

        # Mix of old and recent entries
        guard._user_timestamps["old_user"] = [base_time - 120.0]
        guard._user_timestamps["recent_user"] = [base_time - 30.0]

        with patch(
            "src.slock_engine.activation_guard.time.time", return_value=base_time
        ):
            removed = guard.purge_stale(window=60.0)

        assert removed == 1  # Only old_user removed
        assert "recent_user" in guard._user_timestamps
        assert "old_user" not in guard._user_timestamps

    def test_inline_gc_triggers_every_50_calls(self, guard: ActivationGuard) -> None:
        """can_auto_activate triggers purge_stale every 50 calls."""
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=100,  # High limit to not block
            slock_auto_activate_rate_limit_global=1000,
        )

        with patch.object(guard, "purge_stale") as mock_purge:
            for i in range(100):
                guard.can_auto_activate(f"user_{i}", "chat_1", settings)

            # Should have been called twice (at call 50 and 100)
            assert mock_purge.call_count == 2


class TestMemoryLeakPrevention:
    """AC-R15: 1000 users memory leak prevention — no expired entries remain after purge."""

    def test_1000_users_no_memory_leak(self, guard: ActivationGuard) -> None:
        """After 1000 users trigger rate limit, purge cleans all stale entries."""
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_rate_limit_per_user=5,
            slock_auto_activate_rate_limit_global=10000,
        )

        base_time = 5000.0

        # Simulate 1000 different users
        with patch(
            "src.slock_engine.activation_guard.time.time", return_value=base_time
        ):
            # Patch purge_stale during insertion to avoid premature cleanup
            with patch.object(guard, "purge_stale"):
                for i in range(1000):
                    guard.can_auto_activate(f"user_{i}", f"chat_{i}", settings)

        # All users should have entries
        assert len(guard._user_timestamps) == 1000

        # Advance time past window and purge
        with patch(
            "src.slock_engine.activation_guard.time.time",
            return_value=base_time + 120.0,
        ):
            removed = guard.purge_stale(window=60.0)

        assert removed == 1000
        assert len(guard._user_timestamps) == 0

    def test_1000_users_partial_expiry(self, guard: ActivationGuard) -> None:
        """Only users with fully expired timestamps are removed."""
        base_time = 6000.0

        # 500 users with old timestamps, 500 with recent ones
        for i in range(500):
            guard._user_timestamps[f"old_user_{i}"] = [base_time - 120.0]
        for i in range(500):
            guard._user_timestamps[f"recent_user_{i}"] = [base_time - 30.0]

        assert len(guard._user_timestamps) == 1000

        with patch(
            "src.slock_engine.activation_guard.time.time", return_value=base_time
        ):
            removed = guard.purge_stale(window=60.0)

        # Only old users should be removed
        assert removed == 500
        assert len(guard._user_timestamps) == 500
        # Verify the right users remain
        assert "recent_user_0" in guard._user_timestamps
        assert "old_user_0" not in guard._user_timestamps


# --------------------------------------------------------------------------- #
# AC-R6: Non-admin manual /slock blocked by ActivationGuard
# --------------------------------------------------------------------------- #


class TestManualSlockGuard:
    """AC-R6: Non-admin manual /slock must be blocked by ActivationGuard."""

    def test_non_admin_manual_slock_blocked(self, guard: ActivationGuard) -> None:
        """Non-admin user executing /slock should be denied by ActivationGuard."""
        settings = FakeSettings(
            admin_user_ids="admin_001",
            slock_auto_activate_whitelist_user_ids="",
            slock_auto_activate_default_policy="admin_only",
            slock_auto_activate_rate_limit_per_user=5,
            slock_auto_activate_rate_limit_global=10,
        )

        # Non-admin user
        allowed, _ = guard.can_auto_activate("regular_user_123", "chat_1", settings)
        assert allowed is False

    def test_admin_manual_slock_allowed(self, guard: ActivationGuard) -> None:
        """Admin user executing /slock should be allowed."""
        settings = FakeSettings(
            admin_user_ids="admin_001",
            slock_auto_activate_whitelist_user_ids="",
            slock_auto_activate_default_policy="admin_only",
            slock_auto_activate_rate_limit_per_user=5,
            slock_auto_activate_rate_limit_global=10,
        )

        allowed, _ = guard.can_auto_activate("admin_001", "chat_1", settings)
        assert allowed is True

    def test_whitelisted_user_manual_slock_allowed(self, guard: ActivationGuard) -> None:
        """Whitelisted user executing /slock should be allowed even with admin_only policy."""
        settings = FakeSettings(
            admin_user_ids="admin_001",
            slock_auto_activate_whitelist_user_ids="user_002,user_003",
            slock_auto_activate_default_policy="admin_only",
            slock_auto_activate_rate_limit_per_user=5,
            slock_auto_activate_rate_limit_global=10,
        )

        allowed, _ = guard.can_auto_activate("user_002", "chat_1", settings)
        assert allowed is True

    def test_non_admin_blocked_even_with_passive_mode(self, guard: ActivationGuard) -> None:
        """Non-admin user blocked under admin_only policy regardless of passive_mode."""
        settings = FakeSettings(
            admin_user_ids="admin_001",
            slock_passive_mode=True,
            slock_auto_activate_whitelist_user_ids="",
            slock_auto_activate_default_policy="admin_only",
            slock_auto_activate_rate_limit_per_user=5,
            slock_auto_activate_rate_limit_global=10,
        )

        allowed, _ = guard.can_auto_activate("random_user_xyz", "chat_1", settings)
        assert allowed is False


# --------------------------------------------------------------------------- #
# WP2: ActivationGuard returns tuple[bool, str] with reason
# --------------------------------------------------------------------------- #


class TestActivationGuardReturnsReason:
    """WP2: can_auto_activate returns tuple[bool, str] with reason codes.

    Covers three denial reasons:
    - "rate_limit": rate limit exceeded
    - "admin_required": whitelist empty and policy is admin_only
    - "not_whitelisted": user not in whitelist
    """

    def test_returns_tuple_type(self, guard: ActivationGuard) -> None:
        """Verify can_auto_activate returns tuple[bool, str]."""
        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="allow_all",
        )

        result = guard.can_auto_activate("user1", "chat1", settings)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_allowed_returns_allowed_reason(self, guard: ActivationGuard) -> None:
        """When activation is permitted, reason should be 'allowed'."""
        from src.slock_engine.activation_guard import ACTIVATION_ALLOWED

        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="allow_all",
        )

        allowed, reason = guard.can_auto_activate("user1", "chat1", settings)

        assert allowed is True
        assert reason == ACTIVATION_ALLOWED

    def test_admin_allowed_returns_allowed_reason(self, guard: ActivationGuard) -> None:
        """Admin user always allowed with 'allowed' reason."""
        from src.slock_engine.activation_guard import ACTIVATION_ALLOWED

        settings = FakeSettings(
            admin_user_ids="admin1",
            slock_auto_activate_default_policy="admin_only",
        )

        allowed, reason = guard.can_auto_activate("admin1", "chat1", settings)

        assert allowed is True
        assert reason == ACTIVATION_ALLOWED

    def test_whitelisted_user_returns_allowed_reason(self, guard: ActivationGuard) -> None:
        """Whitelisted user allowed with 'allowed' reason."""
        from src.slock_engine.activation_guard import ACTIVATION_ALLOWED

        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids="userA,userB",
            slock_auto_activate_default_policy="admin_only",
        )

        allowed, reason = guard.can_auto_activate("userA", "chat1", settings)

        assert allowed is True
        assert reason == ACTIVATION_ALLOWED

    def test_denied_admin_required_reason(self, guard: ActivationGuard) -> None:
        """Denial reason 'admin_required' when whitelist empty and policy=admin_only."""
        from src.slock_engine.activation_guard import ACTIVATION_DENIED_ADMIN_REQUIRED

        settings = FakeSettings(
            admin_user_ids="admin1",
            slock_auto_activate_whitelist_user_ids="",
            slock_auto_activate_default_policy="admin_only",
            slock_passive_mode=True,
        )

        allowed, reason = guard.can_auto_activate("regular_user", "chat1", settings)

        assert allowed is False
        assert reason == ACTIVATION_DENIED_ADMIN_REQUIRED

    def test_denied_not_whitelisted_reason(self, guard: ActivationGuard) -> None:
        """Denial reason 'not_whitelisted' when user not in configured whitelist."""
        from src.slock_engine.activation_guard import ACTIVATION_DENIED_NOT_WHITELISTED

        settings = FakeSettings(
            slock_auto_activate_whitelist_user_ids="userA,userB",
            slock_auto_activate_default_policy="allow_all",
            slock_passive_mode=True,
        )

        allowed, reason = guard.can_auto_activate("intruder", "chat1", settings)

        assert allowed is False
        assert reason == ACTIVATION_DENIED_NOT_WHITELISTED

    def test_denied_empty_sender_not_whitelisted(self, guard: ActivationGuard) -> None:
        """Empty sender_id returns 'not_whitelisted' reason."""
        from src.slock_engine.activation_guard import ACTIVATION_DENIED_NOT_WHITELISTED

        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="allow_all",
        )

        allowed, reason = guard.can_auto_activate("", "chat1", settings)

        assert allowed is False
        assert reason == ACTIVATION_DENIED_NOT_WHITELISTED

    def test_denied_rate_limit_reason_per_user(self, guard: ActivationGuard) -> None:
        """Denial reason 'rate_limit' when per-user rate limit exceeded."""
        from src.slock_engine.activation_guard import ACTIVATION_DENIED_RATE_LIMIT

        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="allow_all",
            slock_auto_activate_rate_limit_per_user=2,
            slock_auto_activate_rate_limit_global=100,
        )

        # First two calls succeed
        allowed1, _ = guard.can_auto_activate("user1", "chat1", settings)
        allowed2, _ = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed1 is True
        assert allowed2 is True

        # Third call hits rate limit
        allowed3, reason3 = guard.can_auto_activate("user1", "chat1", settings)
        assert allowed3 is False
        assert reason3 == ACTIVATION_DENIED_RATE_LIMIT

    def test_denied_rate_limit_reason_global(self, guard: ActivationGuard) -> None:
        """Denial reason 'rate_limit' when global rate limit exceeded."""
        from src.slock_engine.activation_guard import ACTIVATION_DENIED_RATE_LIMIT

        settings = FakeSettings(
            slock_passive_mode=True,
            slock_auto_activate_default_policy="allow_all",
            slock_auto_activate_rate_limit_per_user=100,
            slock_auto_activate_rate_limit_global=3,
        )

        # Three different users each activate once
        for i in range(3):
            allowed, _ = guard.can_auto_activate(f"user{i}", "chat1", settings)
            assert allowed is True

        # Fourth user hits global limit
        allowed, reason = guard.can_auto_activate("user_extra", "chat1", settings)
        assert allowed is False
        assert reason == ACTIVATION_DENIED_RATE_LIMIT

    def test_all_three_denial_reasons_distinct(self) -> None:
        """Verify the three denial reason constants are distinct strings."""
        from src.slock_engine.activation_guard import (
            ACTIVATION_DENIED_ADMIN_REQUIRED,
            ACTIVATION_DENIED_NOT_WHITELISTED,
            ACTIVATION_DENIED_RATE_LIMIT,
        )

        reasons = {
            ACTIVATION_DENIED_RATE_LIMIT,
            ACTIVATION_DENIED_ADMIN_REQUIRED,
            ACTIVATION_DENIED_NOT_WHITELISTED,
        }
        assert len(reasons) == 3
        assert ACTIVATION_DENIED_RATE_LIMIT == "rate_limit"
        assert ACTIVATION_DENIED_ADMIN_REQUIRED == "admin_required"
        assert ACTIVATION_DENIED_NOT_WHITELISTED == "not_whitelisted"

