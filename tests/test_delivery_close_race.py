"""Tests for close() vs _create_page race condition safety.

Verifies that when close() is called concurrently with an in-progress
delivery (between binding creation and page creation), no orphaned
bindings remain and no exceptions are raised.
"""

import threading
import time

import pytest

from src.card.delivery.engine import CardDelivery, MutationOutcome
from src.card.types import RenderedCard


class _SlowCreateClient:
    """Mock client that introduces a delay in create_card to widen the race window."""

    def __init__(self, delay: float = 0.1):
        self._delay = delay
        self._create_counter = 0
        self.creates: list[dict] = []
        self.create_started = threading.Event()

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self.create_started.set()  # signal that we're inside the API call
        time.sleep(self._delay)  # simulate network latency
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        card_id = f"card_{self._create_counter}"
        self.creates.append({"chat_id": chat_id})
        return (msg_id, card_id)

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass

    def create_streaming_card(self, card_json):
        raise NotImplementedError

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, idempotency_key=None):
        raise NotImplementedError


class TestDeliveryCloseRace:
    """Race conditions between deliver() and close()."""

    def _rendered(self, sig: str = "sig_1") -> list[RenderedCard]:
        return [RenderedCard(
            _card_json={"body": {"elements": []}},
            structure_signature=sig,
            page_index=0,
            total_pages=1,
        )]

    def test_close_during_create_card_no_exception(self):
        """close() called while create_card is in-flight raises no exception."""
        client = _SlowCreateClient(delay=0.15)
        delivery = CardDelivery(client)
        session_id = "race_1"
        errors: list[Exception] = []

        def deliver_thread():
            try:
                delivery.deliver(session_id, "chat_1", self._rendered())
            except Exception as e:
                errors.append(e)

        def close_thread():
            try:
                # Wait until create_card has started
                client.create_started.wait(timeout=5)
                # Now close while create_card is still sleeping
                delivery.close(session_id)
            except Exception as e:
                errors.append(e)

        t_deliver = threading.Thread(target=deliver_thread)
        t_close = threading.Thread(target=close_thread)
        t_deliver.start()
        t_close.start()
        t_deliver.join(timeout=5)
        t_close.join(timeout=5)

        assert errors == [], f"Race caused exceptions: {errors}"
        # After both complete, binding should be cleaned up
        assert delivery.get_binding(session_id) is None
        delivery._shutdown()

    def test_close_before_deliver_returns_empty(self):
        """If close() completes before deliver(), deliver returns []."""
        client = _SlowCreateClient(delay=0.01)
        delivery = CardDelivery(client)
        session_id = "race_2"

        # Close first
        delivery.close(session_id)

        # Deliver after close → should return empty (session in _closed_sessions)
        outcomes = delivery.deliver(session_id, "chat_1", self._rendered())
        assert outcomes == []
        assert len(client.creates) == 0
        delivery._shutdown()

    def test_deliver_completes_before_close_binding_removed(self):
        """Normal flow: deliver creates binding, then close removes it."""
        client = _SlowCreateClient(delay=0.01)
        delivery = CardDelivery(client)
        session_id = "race_3"

        outcomes = delivery.deliver(session_id, "chat_1", self._rendered())
        assert len(outcomes) == 1
        assert outcomes[0].kind == "applied"
        assert delivery.get_binding(session_id) is not None

        delivery.close(session_id)
        assert delivery.get_binding(session_id) is None
        delivery._shutdown()

    def test_concurrent_close_idempotent(self):
        """Multiple threads calling close() for the same session → no crash."""
        client = _SlowCreateClient(delay=0.01)
        delivery = CardDelivery(client)
        session_id = "race_4"

        # Create a binding first
        delivery.deliver(session_id, "chat_1", self._rendered())

        errors: list[Exception] = []
        barrier = threading.Barrier(5, timeout=10)

        def close_thread():
            try:
                barrier.wait()
                delivery.close(session_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=close_thread) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        assert delivery.get_binding(session_id) is None
        delivery._shutdown()

    def test_per_session_lock_serializes_deliver_and_close(self):
        """deliver() and close() on the same session are serialized via per-session lock.

        Specifically: close() waits for an in-flight deliver() to finish before
        removing the binding. The binding is created and then removed (not left orphaned).
        """
        client = _SlowCreateClient(delay=0.2)
        delivery = CardDelivery(client)
        session_id = "race_5"

        deliver_outcomes: list[MutationOutcome] = []
        close_done = threading.Event()

        def deliver_thread():
            result = delivery.deliver(session_id, "chat_1", self._rendered())
            deliver_outcomes.extend(result)

        def close_thread():
            # Wait for create_card to start (deliver holds the session lock)
            client.create_started.wait(timeout=5)
            # close() will block on the session lock until deliver finishes
            delivery.close(session_id)
            close_done.set()

        t1 = threading.Thread(target=deliver_thread)
        t2 = threading.Thread(target=close_thread)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # deliver completed first (held the lock), then close cleaned up
        assert len(deliver_outcomes) == 1
        assert deliver_outcomes[0].kind == "applied"
        assert close_done.is_set()
        assert delivery.get_binding(session_id) is None
        delivery._shutdown()
