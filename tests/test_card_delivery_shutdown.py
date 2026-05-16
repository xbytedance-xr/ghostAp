"""Tests for CardDelivery shutdown_all, shutdown idempotency, registry lifecycle, and to_feishu_json."""

import threading

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.delivery.registry import delivery_registry
from src.card.types import RenderedCard
from tests.helpers.delivery_internals import DeliveryInspector


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Reset the idempotent shutdown flag before each test."""
    delivery_registry.reset()
    yield
    delivery_registry.reset()


class _MockClient:
    """Minimal mock for CardAPIClient protocol."""

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        return ("msg_id", "card_id")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass

    def create_streaming_card(self, card_json):
        return "card_id"

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, idempotency_key=None):
        return "msg_id"


class TestShutdownAll:
    """Test CardDelivery.shutdown_all() stops all instances."""

    def test_shutdown_all_stops_all_instances(self):
        """Create 3 instances, call shutdown_all, verify all eviction threads stopped."""
        instances = [CardDelivery(_MockClient()) for _ in range(3)]

        delivery_registry.shutdown_all()

        for inst in instances:
            inspector = DeliveryInspector.from_delivery(inst)
            assert inspector.eviction_stop.is_set()
            assert not inspector.eviction_thread.is_alive()

        # Cleanup: instances should have been discarded
        for inst in instances:
            assert inst not in delivery_registry.instances

    def test_shutdown_all_empty_is_noop(self):
        """shutdown_all on empty _instances does nothing."""
        # Registry is already empty after reset
        delivery_registry.shutdown_all()  # Should not raise


class TestShutdownIdempotent:
    """Test that shutdown() is idempotent."""

    def test_shutdown_idempotent(self):
        """Calling _shutdown() twice on the same instance does not raise."""
        inst = CardDelivery(_MockClient())
        inspector = DeliveryInspector.from_delivery(inst)

        inst._shutdown()
        assert inspector.eviction_stop.is_set()
        assert not inspector.eviction_thread.is_alive()

        # Second call should not raise
        inst._shutdown()
        assert inspector.eviction_stop.is_set()


class TestRegistryLifecycle:
    """Test that registry uses explicit set + unregister semantics."""

    def test_instances_is_set(self):
        """Registry instances should be a frozenset (immutable snapshot for thread safety)."""
        assert isinstance(delivery_registry.instances, frozenset)

    def test_unregister_removes_instance(self):
        """Explicit unregister removes instance from registry; deletion alone does not."""
        inst = CardDelivery(_MockClient())
        assert inst in delivery_registry.instances

        inst._shutdown()  # triggers unregister
        assert inst not in delivery_registry.instances


class TestToFeishuJson:
    """Test RenderedCard.to_feishu_json()."""

    def test_to_feishu_json_returns_card_json(self):
        """to_feishu_json() should return the card_json dict equal to original."""
        card_data = {"header": {"title": "test"}, "elements": []}
        card = RenderedCard(_card_json=card_data)
        assert card.to_feishu_json() == card_data

    def test_to_feishu_json_empty_card(self):
        """to_feishu_json() on empty card returns empty dict."""
        card = RenderedCard()
        assert card.to_feishu_json() == {}

    def test_to_feishu_json_returns_shallow_copy(self):
        """to_feishu_json() returns a shallow copy, not the same reference."""
        card_data = {"key": "value", "nested": [1, 2, 3]}
        card = RenderedCard(_card_json=card_data)
        result = card.to_feishu_json()
        # Must be equal but not the same object
        assert result == card_data
        assert result is not card_data
        # Mutation of result should not affect internal state
        result["key"] = "mutated"
        assert card._card_json["key"] == "value"

    def test_to_feishu_json_deep_copy_isolates_nested(self):
        """Deep copy isolates nested mutable objects from internal state."""
        inner_list = [1, 2, 3]
        card_data = {"items": inner_list}
        card = RenderedCard(_card_json=card_data)
        result = card.to_feishu_json()
        # Nested objects are NOT shared (deep copy)
        assert result["items"] is not inner_list
        assert result["items"] == inner_list


class TestSlowBindingDuringShutdown:
    """Test shutdown_all does not hang when an instance has a slow binding."""

    def test_shutdown_all_does_not_block_on_slow_client(self):
        """shutdown_all with a client doing a slow create should still complete."""
        import time

        class SlowClient(_MockClient):
            def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
                time.sleep(0.5)
                return ("msg_id", "card_id")

        inst = CardDelivery(SlowClient())
        inspector = DeliveryInspector.from_delivery(inst)
        # Verify the eviction thread is alive before shutdown
        assert inspector.eviction_thread.is_alive()

        # Shutdown should stop eviction thread regardless of client speed
        inst._shutdown()
        assert inspector.eviction_stop.is_set()
        assert not inspector.eviction_thread.is_alive()

    def test_shutdown_all_with_active_session_lock(self):
        """shutdown_all completes even when session locks exist."""
        inst = CardDelivery(_MockClient())
        inspector = DeliveryInspector.from_delivery(inst)
        # Simulate some sessions with locks
        inspector.session_locks["sess_1"] = threading.RLock()
        inspector.session_locks["sess_2"] = threading.RLock()

        delivery_registry.shutdown_all()

        assert inspector.eviction_stop.is_set()
        assert not inspector.eviction_thread.is_alive()


class TestDrainInFlightDeliveries:
    """Test delivery_registry.drain_in_flight waits for held locks before returning."""

    def test_drain_waits_for_held_lock(self):
        """Drain should block until in-flight delivery completes."""
        import time

        inst = CardDelivery(_MockClient())

        # Simulate an in-flight delivery held for 0.3s
        released = threading.Event()

        def simulate_delivery():
            inst._lock_pool.enter_delivery()
            time.sleep(0.3)
            inst._lock_pool.exit_delivery()
            released.set()

        t = threading.Thread(target=simulate_delivery)
        t.start()
        # Give the thread time to enter delivery
        time.sleep(0.05)

        start = time.monotonic()
        delivery_registry.drain_in_flight(timeout=5.0)
        elapsed = time.monotonic() - start

        t.join()
        # Drain should have waited at least 0.2s (delivery held for 0.3s)
        assert elapsed >= 0.2, f"Drain returned too quickly: {elapsed:.3f}s"
        assert released.is_set()
        inst._shutdown()

    def test_drain_respects_timeout(self):
        """Drain should give up after drain_timeout even if delivery never finishes."""
        import time

        inst = CardDelivery(_MockClient())

        # Simulate a stuck delivery that never exits
        def hold_forever():
            inst._lock_pool.enter_delivery()
            # Hold until test is done - sleep longer than drain_timeout
            time.sleep(3.0)
            inst._lock_pool.exit_delivery()

        t = threading.Thread(target=hold_forever, daemon=True)
        t.start()
        time.sleep(0.05)

        start = time.monotonic()
        delivery_registry.drain_in_flight(timeout=0.5)
        elapsed = time.monotonic() - start

        # Should have given up around 0.5s
        assert 0.4 <= elapsed <= 1.5, f"Drain took unexpected time: {elapsed:.3f}s"

        inst._shutdown()

    def test_drain_in_flight_returns_false_on_timeout(self):
        """delivery_registry.drain_in_flight() should return False when timeout is reached."""
        import time

        inst = CardDelivery(_MockClient())

        # Simulate a stuck in-flight delivery
        def hold():
            inst._lock_pool.enter_delivery()
            time.sleep(3.0)
            inst._lock_pool.exit_delivery()

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        time.sleep(0.05)

        result = delivery_registry.drain_in_flight(timeout=0.3)
        assert result is False

        inst._shutdown()

    def test_drain_returns_immediately_when_no_locks_held(self):
        """When no session locks exist, drain returns instantly."""
        import time

        inst = CardDelivery(_MockClient())
        # No locks at all

        start = time.monotonic()
        delivery_registry.drain_in_flight(timeout=5.0)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"Drain should be instant but took {elapsed:.3f}s"
        inst._shutdown()

    def test_drain_acquires_released_lock_immediately(self):
        """When session locks exist but are not held, drain acquires and releases them."""
        import time

        inst = CardDelivery(_MockClient())
        inspector = DeliveryInspector.from_delivery(inst)
        inspector.session_locks["idle_session"] = threading.RLock()

        start = time.monotonic()
        delivery_registry.drain_in_flight(timeout=5.0)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"Drain on idle lock should be instant: {elapsed:.3f}s"
        inst._shutdown()


class TestDrainInFlightClassmethod:
    """Test delivery_registry.drain_in_flight() directly."""

    def test_drain_in_flight_returns_true_when_no_instances(self):
        """drain_in_flight returns True when no instances exist."""
        # Ensure no stale instances
        delivery_registry.shutdown_all()
        result = delivery_registry.drain_in_flight(timeout=1.0)
        assert result is True

    def test_drain_in_flight_returns_true_when_locks_idle(self):
        """drain_in_flight returns True when locks exist but are not held."""
        import time
        inst = CardDelivery(_MockClient())
        inspector = DeliveryInspector.from_delivery(inst)
        inspector.session_locks["s1"] = threading.RLock()
        inspector.session_locks["s2"] = threading.RLock()

        start = time.monotonic()
        result = delivery_registry.drain_in_flight(timeout=2.0)
        elapsed = time.monotonic() - start

        assert result is True
        assert elapsed < 0.5
        inst._shutdown()

    def test_drain_in_flight_waits_for_held_lock(self):
        """drain_in_flight blocks until in-flight delivery completes."""
        import time
        inst = CardDelivery(_MockClient())

        released = threading.Event()

        def simulate_delivery():
            inst._lock_pool.enter_delivery()
            time.sleep(0.3)
            inst._lock_pool.exit_delivery()
            released.set()

        t = threading.Thread(target=simulate_delivery)
        t.start()
        time.sleep(0.05)  # Let thread enter delivery

        start = time.monotonic()
        delivery_registry.drain_in_flight(timeout=5.0)
        elapsed = time.monotonic() - start

        t.join()
        assert elapsed >= 0.2
        assert released.is_set()
        inst._shutdown()

    def test_drain_in_flight_multiple_instances(self):
        """drain_in_flight iterates over all instances."""
        inst1 = CardDelivery(_MockClient())
        inst2 = CardDelivery(_MockClient())
        # No in-flight deliveries → drain returns immediately

        result = delivery_registry.drain_in_flight(timeout=2.0)
        assert result is True

        inst1._shutdown()
        inst2._shutdown()


class TestDrainPartialAcquireFailure:
    """Test drain_in_flight returns False when in-flight delivery never finishes."""

    def test_drain_returns_false_on_single_acquire_timeout(self):
        """If in-flight delivery is stuck, drain should return False."""
        delivery = CardDelivery(_MockClient(), max_session_locks=100, session_lock_ttl=600)
        try:
            # Simulate a stuck delivery
            delivery._lock_pool.enter_delivery()

            result = delivery_registry.drain_in_flight(timeout=0.3)
            assert result is False

            # Clean up
            delivery._lock_pool.exit_delivery()
        finally:
            delivery._shutdown()


class TestDrainThenDeliverRejected:
    """Test that deliver() returns rejected after drain_in_flight."""

    def test_drain_then_deliver_returns_rejected(self):
        """After drain_in_flight(), calling deliver() on the same instance returns rejected."""
        inst = CardDelivery(_MockClient())
        # Do a normal delivery first
        card = RenderedCard(_card_json={"header": {"title": "test"}, "elements": []})
        outcomes = inst.deliver("s1", "chat1", [card])
        assert outcomes[0].kind == "applied"

        # Drain
        delivery_registry.drain_in_flight(timeout=1.0)

        # Now deliver should be rejected
        card2 = RenderedCard(_card_json={"header": {"title": "test2"}, "elements": []})
        outcomes = inst.deliver("s2", "chat1", [card2])
        assert len(outcomes) == 1
        assert outcomes[0].kind == "rejected"
        assert "shutting down" in outcomes[0].message

        inst._shutdown()


class TestDrainBudgetSplitting:
    """Test drain_in_flight correctly handles multiple instances with active in-flight work."""

    def test_drain_multiple_instances_with_inflight(self):
        """Two instances each with in-flight work should both be drained within total timeout."""
        import time

        inst1 = CardDelivery(_MockClient())
        inst2 = CardDelivery(_MockClient())

        # Simulate in-flight deliveries on both instances
        inst1._lock_pool.enter_delivery()
        inst2._lock_pool.enter_delivery()

        # Release both after a short delay in background threads
        def release_after(pool, delay):
            time.sleep(delay)
            pool.exit_delivery()

        t1 = threading.Thread(target=release_after, args=(inst1._lock_pool, 0.3))
        t2 = threading.Thread(target=release_after, args=(inst2._lock_pool, 0.5))
        t1.start()
        t2.start()

        t0 = time.monotonic()
        result = delivery_registry.drain_in_flight(timeout=3.0)
        elapsed = time.monotonic() - t0

        assert result is True, "Both instances should drain within timeout"
        assert elapsed < 2.0, f"Expected drain < 2s, got {elapsed:.1f}s"

        t1.join(timeout=2)
        t2.join(timeout=2)

        inst1._shutdown()
        inst2._shutdown()
