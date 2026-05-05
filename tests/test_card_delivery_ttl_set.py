"""Tests for TTLSet: bounded set with TTL-based expiry and lazy eviction."""

import threading

import pytest

from src.card.delivery.ttl_set import TTLSet


class TestBasicOperations:
    """Add, contains, and length."""

    def test_add_and_contains(self):
        s = TTLSet(ttl=10.0, clock=lambda: 0.0)
        s.add("a")
        assert "a" in s
        assert "b" not in s

    def test_len(self):
        s = TTLSet(ttl=10.0, clock=lambda: 0.0)
        s.add("a")
        s.add("b")
        assert len(s) == 2

    def test_add_duplicate_refreshes(self):
        clock_time = [0.0]
        s = TTLSet(ttl=5.0, clock=lambda: clock_time[0])
        s.add("a")
        clock_time[0] = 4.0
        s.add("a")  # refresh
        clock_time[0] = 8.0
        # Original would have expired at 5.0, but refreshed at 4.0 → expires at 9.0
        assert "a" in s


class TestTTLExpiry:
    """Entries expire after TTL."""

    def test_expired_entry_not_found(self):
        clock_time = [0.0]
        s = TTLSet(ttl=5.0, clock=lambda: clock_time[0])
        s.add("a")
        clock_time[0] = 6.0
        assert "a" not in s

    def test_not_expired_within_ttl(self):
        clock_time = [0.0]
        s = TTLSet(ttl=5.0, clock=lambda: clock_time[0])
        s.add("a")
        clock_time[0] = 4.9
        assert "a" in s


class TestLazyEviction:
    """Expired entries are lazily evicted on add() and purge()."""

    def test_add_evicts_expired_entries(self):
        clock_time = [0.0]
        s = TTLSet(ttl=5.0, clock=lambda: clock_time[0])
        s.add("old1")
        s.add("old2")
        clock_time[0] = 6.0
        s.add("new")
        # old entries evicted, only "new" remains
        assert len(s) == 1
        assert "new" in s

    def test_purge_removes_expired(self):
        clock_time = [0.0]
        s = TTLSet(ttl=5.0, clock=lambda: clock_time[0])
        s.add("a")
        s.add("b")
        clock_time[0] = 6.0
        removed = s.purge()
        assert removed == 2
        assert len(s) == 0

    def test_purge_respects_batch_limit(self):
        clock_time = [0.0]
        s = TTLSet(ttl=5.0, max_evict_batch=2, clock=lambda: clock_time[0])
        for i in range(5):
            s.add(f"k{i}")
        clock_time[0] = 6.0
        removed = s.purge()
        assert removed == 2  # batch limit
        assert len(s) == 3  # remaining entries still present (expired but not evicted yet)


class TestMaxSizeCap:
    """Hard cap on size enforces bounded memory."""

    def test_force_evict_on_overflow(self):
        clock_time = [0.0]
        s = TTLSet(ttl=100.0, max_size=3, clock=lambda: clock_time[0])
        s.add("a")
        s.add("b")
        s.add("c")
        s.add("d")  # should evict oldest ("a")
        assert len(s) == 3
        assert "a" not in s
        assert "d" in s


class TestValidation:
    """Constructor validation."""

    def test_ttl_must_be_positive(self):
        with pytest.raises(ValueError, match="ttl must be positive"):
            TTLSet(ttl=0)

    def test_max_size_must_be_positive(self):
        with pytest.raises(ValueError, match="max_size must be >= 1"):
            TTLSet(max_size=0)

    def test_max_evict_batch_must_be_positive(self):
        with pytest.raises(ValueError, match="max_evict_batch must be >= 1"):
            TTLSet(max_evict_batch=0)


class TestConcurrency:
    """Thread safety of TTLSet."""

    def test_concurrent_add_and_contains(self):
        s = TTLSet(ttl=60.0, max_size=10_000)
        errors = []

        def writer(start: int):
            try:
                for i in range(500):
                    s.add(f"w{start}_{i}")
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(500):
                    _ = "w0_0" in s
                    _ = len(s)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
