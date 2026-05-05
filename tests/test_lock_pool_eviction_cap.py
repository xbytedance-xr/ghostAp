"""Tests for SessionLockPool max-50 batch eviction cap.

Verifies that _evict_stale_two_phase never removes more than 50 entries
per invocation, even when many more are eligible.
"""

import threading
import time

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.delivery.lock_pool import SessionLockPool
from tests.helpers.delivery_internals import DeliveryInspector


class TestEvictionBatchCap:
    """Two-phase eviction caps at max-50 removals per pass."""

    def _make_pool(self, *, max_locks: int = 10_000, lock_ttl: float = 600.0) -> SessionLockPool:
        return SessionLockPool(
            max_locks=max_locks,
            lock_ttl=lock_ttl,
            eviction_interval=9999.0,  # disable auto-eviction for deterministic testing
            has_active_binding=lambda _sid: False,  # all considered stale
        )

    def test_eviction_cap_50_with_160_zombies(self):
        """160 expired entries → single pass removes at most 50."""
        # max_locks=100: overflow_count = 160 - 80 = 80, max_evict = min(50, 80) = 50
        pool = self._make_pool(max_locks=100, lock_ttl=600.0)

        # Inject 160 zombie entries with expired timestamps
        now = time.monotonic()
        with pool._lock:
            for i in range(160):
                sid = f"zombie_{i:03d}"
                pool._session_locks[sid] = threading.RLock()
                pool._timestamps[sid] = now - 700.0  # past TTL

        assert pool.count == 160

        # Trigger single eviction pass
        evicted = pool._evict_stale_two_phase()

        # Batch cap: at most 50 removed per pass
        assert evicted <= 50
        assert evicted > 0
        assert pool.count == 160 - evicted

    def test_eviction_cap_respects_overflow_count(self):
        """When overflow is less than 50, eviction is capped at overflow_count."""
        pool = self._make_pool(max_locks=100, lock_ttl=600.0)

        # Inject 85 entries → overflow = 85 - 80 = 5 → max_evict = min(50, max(1, 5)) = 5
        now = time.monotonic()
        with pool._lock:
            for i in range(85):
                sid = f"stale_{i:02d}"
                pool._session_locks[sid] = threading.RLock()
                pool._timestamps[sid] = now - 700.0

        evicted = pool._evict_stale_two_phase()
        assert evicted <= 5

    def test_eviction_skips_sessions_with_bindings(self):
        """Sessions with active bindings are not evicted even when expired."""
        # has_active_binding returns True for even-numbered sessions
        pool = SessionLockPool(
            max_locks=10_000,
            lock_ttl=600.0,
            eviction_interval=9999.0,
            has_active_binding=lambda sid: int(sid.split("_")[1]) % 2 == 0,
        )

        now = time.monotonic()
        with pool._lock:
            for i in range(60):
                sid = f"sess_{i}"
                pool._session_locks[sid] = threading.RLock()
                pool._timestamps[sid] = now - 700.0

        evicted = pool._evict_stale_two_phase()

        # Only odd-numbered sessions eligible; cap still applies
        assert evicted <= 50
        # Verify even-numbered sessions remain
        for i in range(0, 60, 2):
            assert pool.contains(f"sess_{i}")

    def test_multiple_passes_eventually_reach_target(self):
        """Multiple eviction passes reduce count to 80% threshold (target watermark)."""
        # max_locks=100: target = int(100*0.8) = 80
        # overflow_count = len - 80, max_evict = min(50, max(1, overflow))
        pool = self._make_pool(max_locks=100, lock_ttl=600.0)

        now = time.monotonic()
        with pool._lock:
            for i in range(160):
                sid = f"z_{i:03d}"
                pool._session_locks[sid] = threading.RLock()
                pool._timestamps[sid] = now - 700.0

        total_evicted = 0
        for _ in range(10):
            evicted = pool._evict_stale_two_phase()
            if evicted == 0:
                break
            total_evicted += evicted

        # Should have evicted down to ~80 (the 80% watermark)
        assert pool.count <= 80
        assert total_evicted >= 80  # at least 80 removed (from 160 to ≤80)

    def test_eviction_toctou_protection(self):
        """If timestamp changed between phase-1 and phase-3, entry is NOT evicted."""
        # max_locks=5 with 10 entries → overflow = 10 - 4 = 6, max_evict = min(50, 6) = 6
        pool = self._make_pool(max_locks=5, lock_ttl=600.0)

        now = time.monotonic()
        with pool._lock:
            for i in range(10):
                sid = f"s_{i}"
                pool._session_locks[sid] = threading.RLock()
                pool._timestamps[sid] = now - 700.0

        # Monkey-patch has_active_binding to refresh timestamp for s_2 during phase-2
        original_check = pool._has_active_binding

        def refreshing_check(sid):
            if sid == "s_2":
                # Simulate a concurrent deliver() refreshing the timestamp
                with pool._lock:
                    pool._timestamps["s_2"] = time.monotonic()
            return original_check(sid)

        pool._has_active_binding = refreshing_check

        evicted = pool._evict_stale_two_phase()

        # s_2 should NOT be evicted (timestamp changed)
        assert pool.contains("s_2")
        # At least some others were evicted (cap is 6, minus s_2 = up to 5 eligible in batch)
        assert evicted >= 1
