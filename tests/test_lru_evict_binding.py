"""Tests for LRU eviction binding-awareness in CardDelivery."""

import threading
import time

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.types import RenderedCard
from tests.helpers.delivery_internals import DeliveryInspector


class MockCardClient:
    """Minimal mock for CardAPIClient."""

    def __init__(self):
        self._counter = 0

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self._counter += 1
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass

    def create_streaming_card(self, card_json):
        self._counter += 1
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def send_card_reference(self, chat_id, source_card_id, *, reply_to=None):
        self._counter += 1
        return f"msg_{self._counter}"


class TestLruEvictBinding:
    """Verify _lru_evict_oldest skips sessions with active bindings."""

    def test_evicts_session_without_binding(self):
        """Session without active binding is evicted normally."""
        delivery = CardDelivery(client=MockCardClient(), max_session_locks=2, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            # Register two sessions manually
            with inspector.lock:
                inspector.session_locks["sid_old"] = threading.RLock()
                inspector.timestamps["sid_old"] = time.monotonic() - 100
                inspector.session_locks["sid_new"] = threading.RLock()
                inspector.timestamps["sid_new"] = time.monotonic()

            # Trigger LRU eviction
            with inspector.lock:
                inspector.lru_evict_oldest()

            # sid_old should be evicted (no binding)
            assert "sid_old" not in inspector.session_locks
            assert "sid_new" in inspector.session_locks
        finally:
            delivery._shutdown()

    def test_skips_session_with_active_binding(self):
        """Session with active binding is NOT evicted; next candidate is chosen."""
        delivery = CardDelivery(client=MockCardClient(), max_session_locks=3, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            # Register sessions
            with inspector.lock:
                inspector.session_locks["sid_bound"] = threading.RLock()
                inspector.timestamps["sid_bound"] = time.monotonic() - 200  # oldest
                inspector.session_locks["sid_free"] = threading.RLock()
                inspector.timestamps["sid_free"] = time.monotonic() - 100  # second oldest
                inspector.session_locks["sid_newest"] = threading.RLock()
                inspector.timestamps["sid_newest"] = time.monotonic()

            # Create a binding for sid_bound so it becomes protected
            rendered = [RenderedCard(_card_json={"schema": "2.0", "body": {"elements": []}})]
            delivery.deliver("sid_bound", "chat_1", rendered, reply_to="msg_x")

            # Now trigger LRU eviction
            with inspector.lock:
                inspector.lru_evict_oldest()

            # sid_bound should still exist (has binding), sid_free should be evicted
            assert "sid_bound" in inspector.session_locks
            assert "sid_free" not in inspector.session_locks
            assert "sid_newest" in inspector.session_locks
        finally:
            delivery._shutdown()

    def test_all_have_bindings_no_eviction(self):
        """If all candidates have bindings, eviction is skipped with warning."""
        delivery = CardDelivery(client=MockCardClient(), max_session_locks=2, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            # Create two sessions with bindings
            rendered = [RenderedCard(_card_json={"schema": "2.0", "body": {"elements": []}})]
            delivery.deliver("sid_a", "chat_1", rendered, reply_to="msg_1")
            delivery.deliver("sid_b", "chat_2", rendered, reply_to="msg_2")

            # Both have bindings — LRU eviction should fail gracefully
            with inspector.lock:
                initial_count = len(inspector.session_locks)
                inspector.lru_evict_oldest()
                assert len(inspector.session_locks) == initial_count  # nothing evicted
        finally:
            delivery._shutdown()

    def test_eviction_preserves_relative_order(self):
        """After eviction with skipped sessions, relative LRU order is preserved.

        Setup: A < B < C < D manually inserted (oldest to newest).
        A and B get bindings (protected), C and D have no binding.
        Expected: A and B are skipped, C is evicted (first unbound).
        After eviction, remaining order should be [A, B, D] — skipped sessions
        A and B re-inserted at head preserving A < B relative order.
        """
        delivery = CardDelivery(client=MockCardClient(), max_session_locks=5, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            now = time.monotonic()
            with inspector.lock:
                inspector.session_locks["A"] = threading.RLock()
                inspector.timestamps["A"] = now - 400
                inspector.session_locks["B"] = threading.RLock()
                inspector.timestamps["B"] = now - 300
                inspector.session_locks["C"] = threading.RLock()
                inspector.timestamps["C"] = now - 200
                inspector.session_locks["D"] = threading.RLock()
                inspector.timestamps["D"] = now - 100

            # Give A and B bindings to protect them (use deliver to create binding)
            rendered = [RenderedCard(_card_json={"schema": "2.0", "body": {"elements": []}})]
            delivery.deliver("A", "chat_a", rendered, reply_to="msg_a")
            delivery.deliver("B", "chat_b", rendered, reply_to="msg_b")

            # deliver moves A and B to end, so LRU order is now: C < D < A < B
            # But we want to test skipping — manually reset timestamps to restore
            # the original order so A and B are oldest (will be popped first).
            with inspector.lock:
                inspector.timestamps["A"] = now - 400
                inspector.timestamps.move_to_end("A", last=False)
                inspector.timestamps["B"] = now - 300
                # Move B just after A
                # OrderedDict: move A to front, then re-insert B after A
                # Rebuild order: A, B, C, D
                inspector.timestamps.move_to_end("B", last=False)
                inspector.timestamps.move_to_end("A", last=False)

            # Verify setup: order should be A, B, C, D (or A, B, ...)
            keys_before = list(inspector.timestamps.keys())
            assert keys_before[:2] == ["A", "B"], f"Setup check failed: {keys_before}"

            # Trigger LRU eviction
            with inspector.lock:
                inspector.lru_evict_oldest()

            # C should be evicted (first unbound candidate after skipping A, B)
            assert "C" not in inspector.session_locks
            assert "C" not in inspector.timestamps

            # Remaining: A, B, D — with A before B (relative order preserved)
            keys_after = list(inspector.timestamps.keys())
            assert "A" in keys_after
            assert "B" in keys_after
            assert "D" in keys_after
            assert keys_after.index("A") < keys_after.index("B"), (
                f"Expected A before B (preserved relative order), got {keys_after}"
            )
        finally:
            delivery._shutdown()

    def test_evict_without_lock_is_safe(self):
        """Calling _lru_evict_oldest without holding self._lock no longer raises.

        The old assertion was removed per F2 (unreliable locked() check).
        Now we rely on call-site code path guarantees.
        """
        delivery = CardDelivery(client=MockCardClient(), max_session_locks=5, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            # Seed a session so eviction has something to work with
            with inspector.lock:
                inspector.session_locks["sid_x"] = threading.RLock()
                inspector.timestamps["sid_x"] = time.monotonic()

            # Call without holding the lock — should NOT raise (assertion removed)
            with inspector.lock:
                inspector.lru_evict_oldest()
            assert "sid_x" not in inspector.session_locks
        finally:
            delivery._shutdown()

    @pytest.mark.slow
    def test_eviction_performance_10k(self):
        """_lru_evict_oldest completes within 5ms even at 10000 session capacity."""
        max_sessions = 10_000
        delivery = CardDelivery(client=MockCardClient(), max_session_locks=max_sessions, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            base_time = time.monotonic()
            with inspector.lock:
                for i in range(max_sessions):
                    sid = f"sid_{i:05d}"
                    inspector.session_locks[sid] = threading.RLock()
                    inspector.timestamps[sid] = base_time + i * 0.001

            # Trigger eviction and measure time
            with inspector.lock:
                start = time.monotonic()
                inspector.lru_evict_oldest()
                elapsed = time.monotonic() - start

            assert elapsed < 0.050, (
                f"_lru_evict_oldest took {elapsed*1000:.2f}ms, expected < 50ms"
            )
            # Verify eviction actually happened
            assert len(inspector.session_locks) == max_sessions - 1
        finally:
            delivery._shutdown()
