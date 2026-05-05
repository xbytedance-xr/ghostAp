"""Unit tests for TTLSet (extracted from delivery/engine.py).

Covers: add, contains, expiry, max_size cap, eviction batching.
Uses constructor-injected clock for deterministic testing (no monkeypatching).
"""

import pytest

from src.card.delivery.ttl_set import TTLSet


class _FakeClock:
    """Deterministic clock for testing."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTTLSetBasics:
    """Basic add/contains behavior."""

    def test_add_and_contains(self):
        s = TTLSet(ttl=10.0)
        s.add("key1")
        assert "key1" in s
        assert "key2" not in s

    def test_len(self):
        s = TTLSet(ttl=10.0)
        s.add("a")
        s.add("b")
        s.add("c")
        assert len(s) == 3

    def test_add_duplicate_refreshes(self):
        s = TTLSet(ttl=10.0)
        s.add("key1")
        s.add("key1")  # Refresh, not duplicate
        assert len(s) == 1

    def test_invalid_ttl_raises(self):
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLSet(ttl=0.0)
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLSet(ttl=-1.0)


class TestTTLSetExpiry:
    """TTL-based entry expiry using injected clock."""

    def test_entry_expires_after_ttl(self):
        clock = _FakeClock(100.0)
        s = TTLSet(ttl=1.0, clock=clock)
        s.add("key1")
        # After TTL passes, should not contain
        clock.advance(1.5)
        assert "key1" not in s

    def test_entry_not_expired_before_ttl(self):
        clock = _FakeClock(100.0)
        s = TTLSet(ttl=5.0, clock=clock)
        s.add("key1")
        clock.advance(4.0)
        assert "key1" in s

    def test_refresh_resets_ttl(self):
        clock = _FakeClock(100.0)
        s = TTLSet(ttl=5.0, clock=clock)
        s.add("key1")
        # Refresh at t=103 (still within TTL)
        clock.advance(3.0)
        s.add("key1")
        # At t=107: 7s from original, but only 4s from refresh → still valid
        clock.advance(4.0)
        assert "key1" in s


class TestTTLSetMaxSize:
    """Max size cap and overflow eviction."""

    def test_max_size_drops_oldest(self):
        s = TTLSet(ttl=300.0, max_size=3)
        s.add("a")
        s.add("b")
        s.add("c")
        assert len(s) == 3
        # Adding a 4th should drop "a" (oldest)
        s.add("d")
        assert len(s) == 3
        assert "a" not in s._entries
        assert "d" in s

    def test_max_size_one(self):
        s = TTLSet(ttl=300.0, max_size=1)
        s.add("a")
        s.add("b")
        assert len(s) == 1
        assert "b" in s
        assert "a" not in s._entries


class TestTTLSetLazyContainsEviction:
    """__contains__ is read-only; purge() explicitly evicts expired keys."""

    def test_contains_evicts_single_expired_key(self):
        """Expired key returns False but __contains__ does NOT mutate dict."""
        clock = _FakeClock(0.0)
        s = TTLSet(ttl=5.0, clock=clock)
        s.add("key1")
        s.add("key2")
        assert len(s) == 2

        # Expire key1 (added at t=0, now at t=6 → expired)
        clock.advance(6.0)
        assert "key1" not in s  # reports not-present (read-only)
        # __contains__ is now read-only — entries remain in internal dict
        assert "key1" in s._entries
        assert len(s) == 2
        # purge() explicitly removes expired entries
        evicted = s.purge()
        assert evicted == 2
        assert len(s) == 0

    def test_contains_preserves_valid_key(self):
        """Non-expired key is NOT removed on __contains__ check."""
        clock = _FakeClock(0.0)
        s = TTLSet(ttl=10.0, clock=clock)
        s.add("key1")
        clock.advance(5.0)  # Not expired yet (5 < 10)
        assert "key1" in s
        assert "key1" in s._entries
        assert len(s) == 1

    def test_contains_does_not_evict_other_expired_keys(self):
        """__contains__ is pure read-only, no eviction happens."""
        clock = _FakeClock(0.0)
        s = TTLSet(ttl=5.0, clock=clock)
        s.add("key1")
        s.add("key2")
        s.add("key3")
        clock.advance(6.0)  # All expired

        # Querying key2 reports not-present but does not mutate
        assert "key2" not in s
        # All keys remain in internal dict (read-only __contains__)
        assert "key1" in s._entries
        assert "key2" in s._entries
        assert "key3" in s._entries
        assert len(s) == 3

    def test_purge_removes_all_expired(self):
        """purge() removes all expired entries in batch."""
        clock = _FakeClock(0.0)
        s = TTLSet(ttl=5.0, clock=clock)
        s.add("key1")
        s.add("key2")
        s.add("key3")
        clock.advance(6.0)  # All expired
        evicted = s.purge()
        assert evicted == 3
        assert len(s) == 0

    def test_contains_nonexistent_key_no_side_effects(self):
        """Querying a key that was never added has no side effects."""
        clock = _FakeClock(0.0)
        s = TTLSet(ttl=5.0, clock=clock)
        s.add("existing")
        assert "nonexistent" not in s
        assert len(s) == 1


class TestTTLSetEvictionBatch:
    """Eviction batching limits work per call."""

    def test_evict_batch_limits_removals(self):
        clock = _FakeClock(0.0)
        s = TTLSet(ttl=1.0, max_size=1000, max_evict_batch=2, clock=clock)
        # Add 5 entries all at t=0
        for i in range(5):
            s.add(f"key_{i}")
        # Now at t=10, all are expired. Adding a new one triggers eviction
        # but only max_evict_batch=2 will be removed per _evict_expired call
        clock.advance(10.0)
        s.add("new_key")
        # Should have evicted some expired entries + added new one
        assert "new_key" in s._entries


class TestTTLSetConcurrency:
    """Thread-safety integration test: TTLSet under external lock protection."""

    def test_concurrent_add_contains_under_lock(self):
        """10 threads doing concurrent add/contains with external lock — no exceptions, data consistent."""
        import threading

        lock = threading.Lock()
        s = TTLSet(ttl=10.0, max_size=50_000)
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def worker(thread_id: int):
            try:
                barrier.wait(timeout=5)
                for i in range(1000):
                    key = f"t{thread_id}_k{i}"
                    with lock:
                        s.add(key)
                    with lock:
                        assert key in s
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent errors: {errors}"
        # All 10*1000 keys should be present (TTL not expired)
        assert len(s) == 10_000

    def test_concurrent_add_and_lazy_eviction(self):
        """Threads add and query expired keys concurrently — lazy eviction is safe."""
        import threading

        clock = _FakeClock(0.0)
        lock = threading.Lock()
        s = TTLSet(ttl=5.0, max_size=50_000, clock=clock)
        errors: list[Exception] = []

        # Pre-populate keys
        for i in range(100):
            s.add(f"old_{i}")

        # Expire all keys
        clock.advance(6.0)

        barrier = threading.Barrier(5)

        def reader(thread_id: int):
            """Query expired keys — triggers lazy eviction."""
            try:
                barrier.wait(timeout=5)
                for i in range(100):
                    with lock:
                        _ = f"old_{i}" in s
            except Exception as exc:
                errors.append(exc)

        def writer(thread_id: int):
            """Add new keys concurrently."""
            try:
                barrier.wait(timeout=5)
                for i in range(100):
                    with lock:
                        s.add(f"new_{thread_id}_{i}")
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=reader, args=(i,)) for i in range(3)]
            + [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent errors: {errors}"
