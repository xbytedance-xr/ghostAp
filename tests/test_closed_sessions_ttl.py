"""Tests for _TTLSet bounded set and CardDelivery closed-session memory management.

Uses injected clock for deterministic, instant tests.
"""

import threading
from unittest.mock import MagicMock

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.delivery.ttl_set import TTLSet as _TTLSet
from src.card.types import RenderedCard


# ---------------------------------------------------------------------------
# _TTLSet unit tests
# ---------------------------------------------------------------------------


class TestTTLSetExpiry:
    """TTL-based expiry behaviour with injected clock."""

    def test_contains_within_ttl(self):
        clock = [100.0]
        s = _TTLSet(ttl=10.0, max_size=100, clock=lambda: clock[0])
        s.add("a")
        clock[0] = 105.0
        assert "a" in s

    def test_contains_expired(self):
        clock = [100.0]
        s = _TTLSet(ttl=5.0, max_size=100, clock=lambda: clock[0])
        s.add("a")
        clock[0] = 106.0
        assert "a" not in s

    def test_expired_entry_removed_from_internal_dict(self):
        clock = [100.0]
        s = _TTLSet(ttl=5.0, max_size=100, clock=lambda: clock[0])
        s.add("a")
        clock[0] = 106.0
        # __contains__ is read-only; eviction happens on next add()
        assert "a" not in s  # reports expired but doesn't remove
        s.add("b")  # triggers eviction of expired entries
        assert len(s) == 1  # only "b" remains

    def test_add_refreshes_existing_key(self):
        clock = [100.0]
        s = _TTLSet(ttl=10.0, max_size=100, clock=lambda: clock[0])
        s.add("a")
        # Refresh at t=108 (8s after initial add)
        clock[0] = 108.0
        s.add("a")
        # At t=115: 15s since first add, but only 7s since refresh → still alive (ttl=10)
        clock[0] = 115.0
        assert "a" in s
        # At t=119: 11s since refresh → expired
        clock[0] = 119.0
        assert "a" not in s


class TestTTLSetMaxSize:
    """Hard cap eviction behaviour."""

    def test_evicts_oldest_when_full(self):
        clock = [100.0]
        s = _TTLSet(ttl=60.0, max_size=3, clock=lambda: clock[0])
        s.add("a")
        s.add("b")
        s.add("c")
        s.add("d")  # should evict "a"
        assert "a" not in s
        assert "b" in s
        assert "c" in s
        assert "d" in s

    def test_size_never_exceeds_max(self):
        clock = [100.0]
        s = _TTLSet(ttl=60.0, max_size=5, clock=lambda: clock[0])
        for i in range(100):
            s.add(f"key_{i}")
        assert len(s) <= 5

    def test_no_memory_leak_after_ttl(self):
        clock = [100.0]
        s = _TTLSet(ttl=2.0, max_size=50, clock=lambda: clock[0])
        for i in range(200):
            s.add(f"key_{i}")
            if i % 20 == 0:
                clock[0] += 3.0  # advance past TTL
        # Advance well past TTL
        clock[0] += 10.0
        s.add("trigger_eviction")
        assert len(s) <= 50


class TestTTLSetEdgeCases:
    """Edge cases."""

    def test_contains_nonexistent_key(self):
        s = _TTLSet(ttl=10.0, max_size=100)
        assert "missing" not in s

    def test_add_same_key_twice(self):
        clock = [100.0]
        s = _TTLSet(ttl=10.0, max_size=100, clock=lambda: clock[0])
        s.add("a")
        s.add("a")
        assert len(s) == 1

    def test_empty_set_len(self):
        s = _TTLSet(ttl=10.0, max_size=100)
        assert len(s) == 0


class TestTTLSetBoundary:
    """Boundary conditions for TTL values."""

    def test_ttlset_zero_ttl_raises(self):
        with pytest.raises(ValueError, match="ttl must be positive"):
            _TTLSet(ttl=0, max_size=100)

    def test_ttlset_negative_ttl_raises(self):
        with pytest.raises(ValueError, match="ttl must be positive"):
            _TTLSet(ttl=-1, max_size=100)


class TestTTLSetRefreshPath:
    """Verify that refresh (add existing key) does NOT trigger _evict_expired."""

    def test_ttlset_refresh_does_not_evict_head(self):
        """When an existing key is refreshed via add(), expired head entries are not cleaned."""
        clock = [100.0]
        s = _TTLSet(ttl=5.0, max_size=100, clock=lambda: clock[0])
        s.add("b")
        s.add("c")
        assert len(s._entries) == 2

        # Both expired
        clock[0] = 106.0
        # Refresh "c" — refresh path does NOT call _evict_expired
        s.add("c")
        # Internal dict still contains "b" (expired but not evicted by refresh)
        assert len(s._entries) == 2  # "b" (expired, not evicted) + "c" (refreshed)

        # __contains__ is read-only — does NOT evict expired keys
        assert "b" not in s  # reports expired
        assert len(s._entries) == 2  # "b" still in dict (read-only __contains__)

        # purge() explicitly cleans expired entries
        s.purge()
        assert len(s._entries) == 1  # only "c" (refreshed) remains

        # Eviction also happens on next add() of a new key
        s.add("d")
        assert len(s._entries) == 2  # "c" + "d" remain


class TestTTLSetBatchEviction:
    """Verify max_evict_batch limits work per eviction call."""

    def test_eviction_respects_batch_limit(self):
        clock = [100.0]
        s = _TTLSet(ttl=5.0, max_size=1000, max_evict_batch=3, clock=lambda: clock[0])
        for i in range(10):
            s.add(f"key_{i}")
        # All expired
        clock[0] = 106.0
        # Trigger eviction via add — should evict at most 3
        s.add("new_key")
        # 10 expired entries minus up to 3 evicted = at least 7 remaining + 1 new
        # (exact count depends on implementation ordering)
        assert len(s._entries) >= 7


# ---------------------------------------------------------------------------
# CardDelivery integration tests
# ---------------------------------------------------------------------------


class _MockClient:
    """Minimal mock Feishu client for CardDelivery integration tests."""

    def __init__(self):
        self.created = []

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self.created.append((chat_id, card_json))
        return ("msg_1", "card_1")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass

    def create_streaming_card(self, card_json):
        return "card_streaming_1"

    def send_card_reference(self, chat_id, card_id, *, reply_to=None):
        return "msg_ref_1"


class TestCardDeliveryClosedSessionTTL:
    """Integration: CardDelivery uses _TTLSet for closed session tracking."""

    def test_close_then_deliver_returns_empty(self):
        """After close(), deliver() returns empty list."""
        client = _MockClient()
        delivery = CardDelivery(client)
        delivery.close("sess_1")
        result = delivery.deliver("sess_1", "chat_1", [])
        assert result == []

    def test_closed_session_ttl_field_is_ttl_set(self):
        """_closed_sessions is a _TTLSet instance."""
        client = _MockClient()
        delivery = CardDelivery(client)
        assert isinstance(delivery._closed_sessions, _TTLSet)

    def test_deliver_not_blocked_after_ttl_expires(self):
        """After TTL expires, session_id is no longer blocked and API is called."""
        client = MagicMock()
        client.create_card.return_value = ("msg_1", "card_1")
        delivery = CardDelivery(client)
        rendered = [RenderedCard(_card_json={"body": {}}, structure_signature="sig1", page_index=0)]

        clock = [100.0]
        # Use a short TTL with injected clock
        delivery._closed_sessions = _TTLSet(ttl=5.0, max_size=100, clock=lambda: clock[0])
        delivery.close("sess_1")

        # Immediately blocked
        assert delivery.deliver("sess_1", "chat_1", rendered) == []
        assert client.create_card.call_count == 0

        # After TTL expires
        clock[0] = 106.0
        result = delivery.deliver("sess_1", "chat_1", rendered)
        assert client.create_card.called is True
        assert len(result) == 1
        assert result[0].kind == "applied"

    def test_many_closes_bounded_memory(self):
        """Closing many sessions doesn't cause unbounded memory growth."""
        clock = [100.0]
        client = _MockClient()
        delivery = CardDelivery(client)
        delivery._closed_sessions = _TTLSet(ttl=5.0, max_size=100, clock=lambda: clock[0])
        for i in range(500):
            delivery.close(f"sess_{i}")
            # Advance past TTL
            clock[0] += 10.0
            delivery.close("trigger")
            assert len(delivery._closed_sessions) <= 100


# ---------------------------------------------------------------------------
# Concurrency correctness tests
# ---------------------------------------------------------------------------


class TestCardDeliveryConcurrency:
    """Concurrent close/deliver correctness."""

    def test_concurrent_close_then_deliver_no_api_calls(self):
        """After close, concurrent delivers from multiple threads never call API."""
        client = MagicMock()
        client.create_card.return_value = ("msg_1", "card_1")
        delivery = CardDelivery(client)

        rendered = [RenderedCard(_card_json={"body": {}}, structure_signature="sig1", page_index=0)]

        # Close the session first
        delivery.close("sess_concurrent")

        # 20 threads, each delivers 3 times, synchronized start via Barrier
        num_threads = 20
        delivers_per_thread = 3
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=5)
                for _ in range(delivers_per_thread):
                    result = delivery.deliver("sess_concurrent", "chat_1", rendered)
                    assert result == []
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert client.create_card.call_count == 0
        assert client.update_card.call_count == 0


# ---------------------------------------------------------------------------
# TTL expiry reentry test
# ---------------------------------------------------------------------------


class TestCardDeliveryTTLReentry:
    """Verify session reentry after TTL expiry."""

    def test_ttl_expired_session_reentry(self):
        """After close + TTL expiry, a new deliver creates a fresh binding."""
        client = MagicMock()
        client.create_card.return_value = ("msg_new", "card_new")
        delivery = CardDelivery(client)
        rendered = [RenderedCard(_card_json={"body": {}}, structure_signature="sig_re", page_index=0)]

        clock = [100.0]
        delivery._closed_sessions = _TTLSet(ttl=5.0, max_size=100, clock=lambda: clock[0])
        delivery.close("sess_reentry")

        # Blocked while TTL active
        assert delivery.deliver("sess_reentry", "chat_1", rendered) == []
        assert client.create_card.call_count == 0

        # After TTL expiry
        clock[0] = 106.0
        result = delivery.deliver("sess_reentry", "chat_1", rendered)
        assert client.create_card.called is True
        assert len(result) == 1
        assert result[0].kind == "applied"
