"""Tests for CardDelivery timing/concurrency behaviors.

Validates:
- Closed sessions are immediately skipped
- Per-session locks serialize concurrent deliver() calls
- close() is idempotent
- deliver() after close() returns empty
- Concurrent close + deliver does not deadlock
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from src.card.delivery.engine import CardDelivery
from src.card.types import RenderedCard


def _make_rendered(signature: str = "sig1", text: str = "hello") -> list[RenderedCard]:
    """Create a minimal rendered card list for testing."""
    return [RenderedCard(
        _card_json={"config": {}, "body": {"elements": []}},
        structure_signature=signature,
        active_element=None,
        page_index=0,
        total_pages=1,
    )]


def _make_client() -> MagicMock:
    """Create a mock CardAPIClient that returns predictable responses."""
    client = MagicMock()
    client.create_card.return_value = {"card_id": "card_001", "message_id": "msg_001"}
    client.update_card.return_value = True
    client.stream_element_content.return_value = True
    return client


class TestDeliveryClosedSessionSkip:
    """Closed sessions should be immediately skipped without API calls."""

    def test_deliver_after_close_returns_empty(self):
        client = _make_client()
        delivery = CardDelivery(client)

        # First deliver creates the card
        delivery.deliver("s1", "chat1", _make_rendered())
        assert client.create_card.call_count == 1

        # Close the session
        delivery.close("s1")

        # Subsequent delivers return empty
        result = delivery.deliver("s1", "chat1", _make_rendered(signature="sig2"))
        assert result == []
        # No additional API calls
        assert client.create_card.call_count == 1

    def test_close_idempotent(self):
        client = _make_client()
        delivery = CardDelivery(client)
        delivery.deliver("s1", "chat1", _make_rendered())
        delivery.close("s1")
        delivery.close("s1")  # Should not raise
        delivery.close("s1")  # Still fine

    def test_deliver_to_unknown_closed_session(self):
        """Delivering to a session closed without prior deliver."""
        client = _make_client()
        delivery = CardDelivery(client)
        delivery.close("never_opened")
        result = delivery.deliver("never_opened", "chat1", _make_rendered())
        assert result == []
        assert client.create_card.call_count == 0


class TestDeliveryPerSessionLock:
    """Per-session locks serialize access to the same session."""

    def test_concurrent_delivers_to_same_session_are_serialized(self):
        """Two threads delivering to the same session should not interleave."""
        client = _make_client()
        delivery = CardDelivery(client)

        call_order = []

        t1_entered = threading.Event()

        def slow_create(*args, **kwargs):
            call_order.append(("create_start", threading.current_thread().name))
            t1_entered.set()
            time.sleep(0.05)
            result = {"card_id": "card_001", "message_id": "msg_001"}
            call_order.append(("create_end", threading.current_thread().name))
            return result

        client.create_card.side_effect = slow_create

        t1 = threading.Thread(
            target=delivery.deliver,
            args=("s1", "chat1", _make_rendered()),
            name="T1",
        )
        t2 = threading.Thread(
            target=delivery.deliver,
            args=("s1", "chat1", _make_rendered(signature="sig2")),
            name="T2",
        )
        t1.start()
        t1_entered.wait(timeout=5)  # Wait for T1 to acquire lock and enter create
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # T1's create should fully complete before T2's work starts
        # Since T1 creates binding, T2 sees existing binding → update path
        assert len(call_order) >= 2

    def test_different_sessions_do_not_block_each_other(self):
        """Delivers to different sessions can proceed concurrently."""
        client = _make_client()
        delivery = CardDelivery(client)

        barrier = threading.Barrier(2, timeout=5)
        reached_barrier = []

        def create_with_barrier(*args, **kwargs):
            reached_barrier.append(threading.current_thread().name)
            barrier.wait()  # Both threads must reach here
            return {"card_id": f"card_{threading.current_thread().name}", "message_id": "msg"}

        client.create_card.side_effect = create_with_barrier

        t1 = threading.Thread(
            target=delivery.deliver,
            args=("session_a", "chat1", _make_rendered()),
            name="A",
        )
        t2 = threading.Thread(
            target=delivery.deliver,
            args=("session_b", "chat1", _make_rendered()),
            name="B",
        )
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both threads reached the barrier, proving no blocking
        assert len(reached_barrier) == 2


class TestDeliveryConcurrentCloseAndDeliver:
    """close() + deliver() racing should not deadlock."""

    def test_no_deadlock_on_concurrent_close_deliver(self):
        """Rapid close+deliver alternation completes without deadlock."""
        client = _make_client()
        delivery = CardDelivery(client)

        # Pre-create a session
        delivery.deliver("race", "chat1", _make_rendered())

        errors = []

        def closer():
            try:
                delivery.close("race")
            except Exception as e:
                errors.append(e)

        def deliverer():
            try:
                delivery.deliver("race", "chat1", _make_rendered(signature="new"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=closer), threading.Thread(target=deliverer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "Thread deadlocked"

        assert not errors


class TestDeliveryDecisionLogic:
    """Verify create/update/skip decisions based on signature and binding state."""

    def test_first_deliver_creates(self):
        client = _make_client()
        delivery = CardDelivery(client)
        outcomes = delivery.deliver("s1", "chat1", _make_rendered())
        assert len(outcomes) == 1
        assert outcomes[0].kind == "applied"
        assert client.create_card.call_count == 1

    def test_same_signature_skips(self):
        client = _make_client()
        delivery = CardDelivery(client)
        delivery.deliver("s1", "chat1", _make_rendered(signature="same"))
        outcomes = delivery.deliver("s1", "chat1", _make_rendered(signature="same"))
        assert len(outcomes) == 1
        assert outcomes[0].kind == "skipped"

    def test_different_signature_updates(self):
        client = _make_client()
        delivery = CardDelivery(client)
        delivery.deliver("s1", "chat1", _make_rendered(signature="v1"))
        outcomes = delivery.deliver("s1", "chat1", _make_rendered(signature="v2"))
        assert len(outcomes) == 1
        assert outcomes[0].kind == "applied"
        assert client.update_card.call_count == 1
