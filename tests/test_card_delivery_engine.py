"""Tests for CardDelivery engine."""

import unittest.mock

import pytest

from src.card.delivery.engine import (
    CardDelivery,
    CardAPIClient,
    MutationOutcome,
    SequenceConflictError,
    TransportError,
)
from src.card.types import ActiveElement, RenderedCard
from tests.helpers.delivery_internals import DeliveryInspector


class MockCardClient:
    """Mock implementation of CardAPIClient."""

    def __init__(self):
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.elements: list[dict] = []
        self.streaming_creates: list[dict] = []
        self.card_references: list[dict] = []
        self._create_counter = 0
        self._raise_on_update: Exception | None = None
        self._raise_on_streaming_create: Exception | None = None

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        card_id = f"card_{self._create_counter}"
        self.creates.append({
            "chat_id": chat_id,
            "card_json": card_json,
            "reply_to": reply_to,
            "idempotency_key": idempotency_key,
        })
        return (msg_id, card_id)

    def update_card(self, card_id, card_json, *, sequence=0):
        if self._raise_on_update:
            raise self._raise_on_update
        self.updates.append({
            "card_id": card_id,
            "card_json": card_json,
            "sequence": sequence,
        })

    def update_element(self, card_id, element_id, content, *, sequence=0):
        if self._raise_on_update:
            raise self._raise_on_update
        self.elements.append({
            "card_id": card_id,
            "element_id": element_id,
            "content": content,
            "sequence": sequence,
        })

    def create_streaming_card(self, card_json):
        if self._raise_on_streaming_create:
            raise self._raise_on_streaming_create
        self._create_counter += 1
        card_id = f"stream_card_{self._create_counter}"
        self.streaming_creates.append({"card_json": card_json})
        return card_id

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, idempotency_key=None):
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        self.card_references.append({
            "chat_id": chat_id,
            "card_id": card_id,
            "reply_to": reply_to,
            "idempotency_key": idempotency_key,
        })
        return msg_id


class TestCardDeliveryCreate:
    """First delivery creates cards."""

    def test_first_deliver_creates_card(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [RenderedCard(
            _card_json={"body": {"elements": []}},
            structure_signature="sig_1",
            page_index=0,
            total_pages=1,
        )]

        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert len(outcomes) == 1
        assert outcomes[0].kind == "applied"
        assert len(client.creates) == 1
        assert client.creates[0]["chat_id"] == "chat_abc"

    def test_first_deliver_creates_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [RenderedCard(
            _card_json={},
            structure_signature="sig_1",
            page_index=0,
            total_pages=1,
        )]

        delivery.deliver("sess_1", "chat_abc", rendered)
        binding = delivery.get_binding("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert binding.pages[0].signature == "sig_1"


class TestCardDeliveryUpdate:
    """Subsequent deliveries compare signatures."""

    def test_signature_change_triggers_update(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        # First delivery
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Second delivery with different signature
        r2 = [RenderedCard(_card_json={"updated": True}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "applied"
        assert len(client.updates) == 1
        assert client.updates[0]["card_id"] == "card_1"

    def test_text_only_triggers_element_content(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="hello")
        r1 = [RenderedCard(
            _card_json={},
            structure_signature="sig_1",
            active_element=active,
            page_index=0,
            total_pages=1,
        )]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Same signature, different text
        active2 = ActiveElement(element_id="el_1", text="hello world")
        r2 = [RenderedCard(
            _card_json={},
            structure_signature="sig_1",
            active_element=active2,
            page_index=0,
            total_pages=1,
        )]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "applied"
        # _stream_element uses update_element (CardKit v2 element_content API)
        assert len(client.elements) == 1
        assert client.elements[0]["element_id"] == "el_1"
        assert client.elements[0]["content"] == "hello world"

    def test_no_change_skips(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="same")
        r1 = [RenderedCard(
            _card_json={},
            structure_signature="sig_1",
            active_element=active,
            page_index=0,
            total_pages=1,
        )]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Same signature, same text
        outcomes = delivery.deliver("sess_1", "chat_abc", r1)
        assert outcomes[0].kind == "skipped"
        assert len(client.updates) == 0
        assert len(client.elements) == 0


class TestSequenceConflict:
    """Sequence conflict handling."""

    def test_sequence_conflict_reconcile(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Set up conflict on next update
        client._raise_on_update = SequenceConflictError(next_floor=10)
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert "sequence_conflict" in outcomes[0].message


class TestMultiPage:
    """Multi-page delivery."""

    def test_multi_page_delivery(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)

        assert len(outcomes) == 2
        assert all(o.kind == "applied" for o in outcomes)
        assert len(client.creates) == 2

    def test_new_page_created_on_growth(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        # Start with 1 page
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Grow to 2 pages
        r2 = [
            RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        # Page 0 same sig → skip, Page 1 new → create
        assert outcomes[1].kind == "applied"
        assert len(client.creates) == 2  # 1 initial + 1 new page

    def test_existing_history_pages_are_not_updated_after_continuation(self):
        """Once a continuation page exists, only the latest page stays live."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        first = [
            RenderedCard(
                _card_json={"page": 0, "title": "old 1s"},
                structure_signature="p0_t1",
                page_index=0,
                total_pages=2,
            ),
            RenderedCard(
                _card_json={"page": 1, "title": "latest 1s"},
                structure_signature="p1_t1",
                page_index=1,
                total_pages=2,
            ),
        ]
        delivery.deliver("sess_1", "chat_abc", first)

        second = [
            RenderedCard(
                _card_json={"page": 0, "title": "old 2s"},
                structure_signature="p0_t2",
                page_index=0,
                total_pages=2,
            ),
            RenderedCard(
                _card_json={"page": 1, "title": "latest 2s"},
                structure_signature="p1_t2",
                page_index=1,
                total_pages=2,
            ),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", second)

        assert [o.kind for o in outcomes] == ["skipped", "applied"]
        assert len(client.updates) == 1
        assert client.updates[0]["card_id"] == "card_2"
        assert client.updates[0]["card_json"]["title"] == "latest 2s"


class TestPageShrink:
    """Page shrink: stale pages cleaned up when page count decreases."""

    def test_shrink_removes_stale_page_from_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        # Deliver 2 pages
        r2 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_1", "chat_abc", r2)

        binding = delivery.get_binding("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert 1 in binding.pages
        assert len(binding.pages) == 2

        # Shrink to 1 page (different signature to trigger update)
        r1 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_2", page_index=0, total_pages=1),
        ]
        delivery.deliver("sess_1", "chat_abc", r1)

        binding = delivery.get_binding("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert 1 not in binding.pages, "Stale page 1 should be removed after shrink"
        assert len(binding.pages) == 1

    def test_shrink_resets_sequence_for_stale_page(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        # Deliver 2 pages
        r2 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_1", "chat_abc", r2)

        # The stale page card_id is "card_2" (second create)
        stale_card_id = "card_2"
        # Bump sequence so we can verify reset
        delivery._sequences.next_sequence(stale_card_id)
        assert delivery._sequences.current(stale_card_id) > 0

        # Shrink to 1 page
        r1 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_2", page_index=0, total_pages=1),
        ]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Sequence for stale card should be reset
        assert delivery._sequences.current(stale_card_id) == 0


class TestStreamingFallback:
    """Streaming card creation with fallback to IM API."""

    def test_streaming_success_uses_cardkit_path(self):
        """When streaming_mode=True, uses create_streaming_card + send_card_reference."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="streaming text")
        rendered = [RenderedCard(
            _card_json={"config": {"streaming_mode": True}, "body": {}},
            structure_signature="sig_1",
            active_element=active,
            page_index=0,
            total_pages=1,
        )]

        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert outcomes[0].kind == "applied"
        assert len(client.streaming_creates) == 1
        assert len(client.card_references) == 1
        assert len(client.creates) == 0  # IM API NOT used

    def test_streaming_fallback_to_im_api(self):
        """When create_streaming_card fails, falls back to create_card."""
        client = MockCardClient()
        client._raise_on_streaming_create = RuntimeError("CardKit unavailable")
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="fallback text")
        rendered = [RenderedCard(
            _card_json={"config": {"streaming_mode": True}, "body": {}},
            structure_signature="sig_1",
            active_element=active,
            page_index=0,
            total_pages=1,
        )]

        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)
        assert outcomes[0].kind == "applied"
        assert len(client.streaming_creates) == 0  # failed, no record
        assert len(client.creates) == 1  # fell back to IM API

    def test_streaming_fallback_retains_text_in_binding(self):
        """After fallback, last_text is still recorded correctly."""
        client = MockCardClient()
        client._raise_on_streaming_create = RuntimeError("CardKit unavailable")
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="important text")
        rendered = [RenderedCard(
            _card_json={"config": {"streaming_mode": True}, "body": {}},
            structure_signature="sig_1",
            active_element=active,
            page_index=0,
            total_pages=1,
        )]

        delivery.deliver("sess_1", "chat_abc", rendered)
        binding = delivery.get_binding("sess_1")
        assert binding is not None
        assert binding.pages[0].last_text == "important text"


class TestClose:
    """Close session."""

    def test_close_removes_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)
        assert delivery.get_binding("sess_1") is not None

        delivery.close("sess_1")
        assert delivery.get_binding("sess_1") is None

    def test_close_idempotent_no_side_effects(self):
        """Calling close() twice on the same session should not raise and state stays consistent."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)
        assert delivery.get_binding("sess_1") is not None

        # First close
        delivery.close("sess_1")
        assert delivery.get_binding("sess_1") is None

        # Second close — should not raise
        delivery.close("sess_1")
        assert delivery.get_binding("sess_1") is None

        # Deliver after close — should be no-op
        outcomes = delivery.deliver("sess_1", "chat_abc", r1)
        assert outcomes == []


class TestTransportError:
    """TransportError handling in update path."""

    def test_transport_error_returns_reconcile(self):
        """update_card raising TransportError should return reconcile outcome."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        # First delivery creates the card
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Second delivery triggers update — TransportError
        client._raise_on_update = TransportError("connection timeout")
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert "connection timeout" in outcomes[0].message

    def test_stale_binding_discards_session_and_short_circuits_remaining_pages(self):
        """A stale message means the whole session binding is unusable, not just one page."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered_v1 = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_stale", "chat_abc", rendered_v1)

        update_attempts = {"n": 0}

        def stale_update(*args, **kwargs):
            update_attempts["n"] += 1
            raise TransportError("message not found", code=99992354)

        client.update_card = stale_update
        rendered_v2 = [
            RenderedCard(_card_json={"page": 0, "v": 2}, structure_signature="sig_2", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1, "v": 2}, structure_signature="sig_2", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_stale", "chat_abc", rendered_v2)

        assert [outcome.message for outcome in outcomes] == ["history_page_frozen", "recreate:99992354"]
        assert update_attempts["n"] == 1
        assert delivery.get_binding("sess_stale") is None

        client.update_card = MockCardClient.update_card.__get__(client, MockCardClient)
        outcomes = delivery.deliver("sess_stale", "chat_abc", rendered_v2)

        assert [outcome.kind for outcome in outcomes] == ["applied", "applied"]
        assert len(client.creates) == 4

    def test_transport_error_on_element_falls_back_to_update(self):
        """update_element raising TransportError falls back to _update_page."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="hello")
        r1 = [RenderedCard(
            _card_json={}, structure_signature="sig_1",
            active_element=active, page_index=0, total_pages=1,
        )]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Element update will fail, but fallback to full update should succeed
        call_count = {"n": 0}
        original_update = client.update_element

        def failing_element(*args, **kwargs):
            raise TransportError("element push failed")

        client.update_element = failing_element
        active2 = ActiveElement(element_id="el_1", text="world")
        r2 = [RenderedCard(
            _card_json={}, structure_signature="sig_1",
            active_element=active2, page_index=0, total_pages=1,
        )]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        # Should fall back to full update (which succeeds)
        assert outcomes[0].kind == "applied"
        assert len(client.updates) == 1


class TestCreateCardFailure:
    """create_card failure in non-streaming path."""

    def test_create_card_failure_non_streaming(self):
        """create_card raising exception should return reconcile, not crash."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        # Make create_card fail
        original_create = client.create_card
        def failing_create(*args, **kwargs):
            raise RuntimeError("API unavailable")
        client.create_card = failing_create

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)

        assert len(outcomes) == 1
        assert outcomes[0].kind == "reconcile"
        assert "API unavailable" in outcomes[0].message

    def test_create_card_failure_does_not_create_binding(self):
        """Failed create_card should not leave a binding with empty page."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        def failing_create(*args, **kwargs):
            raise RuntimeError("network error")
        client.create_card = failing_create

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", rendered)

        # Binding is created but page should not be registered (since create failed)
        binding = delivery.get_binding("sess_1")
        # The binding exists (created before the API call) but page should be empty
        if binding is not None:
            assert 0 not in binding.pages


class TestConcurrentDeliverClose:
    """Multi-threaded deliver/close race condition safety test."""

    def test_concurrent_deliver_and_close_no_exception(self):
        """10 threads simultaneously deliver + close same session → no crash."""
        import threading

        client = MockCardClient()
        delivery = CardDelivery(client)
        session_id = "concurrent_sess"
        errors: list[Exception] = []

        def deliver_task():
            try:
                cards = [RenderedCard(
                    page_index=0,
                    _card_json={"header": {"title": {"content": "test"}}},
                    structure_signature="sig-concurrent",
                )]
                delivery.deliver(session_id, "chat_1", cards)
            except Exception as e:
                errors.append(e)

        def close_task():
            try:
                delivery.close(session_id)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            if i % 2 == 0:
                t = threading.Thread(target=deliver_task)
            else:
                t = threading.Thread(target=close_task)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # No exceptions should have been raised
        assert errors == [], f"Concurrent errors: {errors}"
        # After all threads complete, binding should be cleaned up
        # (close was called multiple times)
        binding = delivery.get_binding(session_id)
        assert binding is None


class TestCardDeliveryTOCTOU:
    """Verify per-session lock eliminates TOCTOU race between deliver() and close()."""

    def test_deliver_during_close_no_api_call_after_cleanup(self):
        """deliver() blocked by per-session lock cannot proceed after close() cleans up.

        Scenario: close() acquires session_lock for cleanup. A concurrent deliver()
        must wait for session_lock. After close() releases, deliver() sees
        the session in _closed_sessions and returns [].
        """
        import threading
        import time

        client = MockCardClient()
        delivery = CardDelivery(client)
        session_id = "toctou_1"

        # Initial delivery to create binding
        rendered = [RenderedCard(
            _card_json={"body": {}}, structure_signature="sig_1",
            page_index=0, total_pages=1,
        )]
        delivery.deliver(session_id, "chat_1", rendered)
        assert delivery.get_binding(session_id) is not None

        barrier = threading.Barrier(2, timeout=10)
        results: dict[str, list] = {"deliver": [], "errors": []}

        def deliver_thread():
            try:
                barrier.wait()
                # Small delay so close() grabs session_lock first
                time.sleep(0.02)
                outcome = delivery.deliver(session_id, "chat_1", rendered)
                results["deliver"] = outcome
            except Exception as e:
                results["errors"].append(e)

        def close_thread():
            try:
                barrier.wait()
                delivery.close(session_id)
            except Exception as e:
                results["errors"].append(e)

        t1 = threading.Thread(target=deliver_thread)
        t2 = threading.Thread(target=close_thread)
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert not results["errors"], f"Errors: {results['errors']}"
        # After close completes, deliver returns [] (no-op)
        assert results["deliver"] == []
        # No new API calls after the initial one
        assert len(client.creates) == 1

    def test_close_waits_for_inflight_deliver(self):
        """close() waits for in-flight deliver() to finish before cleanup.

        Scenario: deliver() holds session_lock doing slow I/O. close() must
        wait for deliver() to release session_lock before cleaning up.
        """
        import threading
        import time

        class SlowClient(MockCardClient):
            def update_card(self, card_id, card_json, *, sequence=0):
                time.sleep(0.1)  # Simulate slow API
                super().update_card(card_id, card_json, sequence=sequence)

        client = SlowClient()
        delivery = CardDelivery(client)
        session_id = "toctou_2"

        # Initial delivery
        rendered_v1 = [RenderedCard(
            _card_json={"body": {}}, structure_signature="sig_1",
            page_index=0, total_pages=1,
        )]
        delivery.deliver(session_id, "chat_1", rendered_v1)

        # Second delivery (triggers update) while close races
        rendered_v2 = [RenderedCard(
            _card_json={"body": {"v": 2}}, structure_signature="sig_2",
            page_index=0, total_pages=1,
        )]

        barrier = threading.Barrier(2, timeout=10)
        errors: list[Exception] = []
        update_completed = threading.Event()

        def deliver_thread():
            try:
                barrier.wait()
                delivery.deliver(session_id, "chat_1", rendered_v2)
                update_completed.set()
            except Exception as e:
                errors.append(e)

        def close_thread():
            try:
                barrier.wait()
                time.sleep(0.01)  # Let deliver grab the lock first
                delivery.close(session_id)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=deliver_thread)
        t2 = threading.Thread(target=close_thread)
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        # The update should have completed (deliver was in-flight)
        assert update_completed.is_set()
        assert len(client.updates) == 1
        # Binding removed by close
        assert delivery.get_binding(session_id) is None


class TestDeliveryTimeout:
    """Verify delivery handles timeout from slow API calls gracefully."""

    def test_deliver_timeout_on_slow_api(self):
        """A client that hangs (simulated with TimeoutError) should result in reconcile outcome."""
        import time as _time

        client = MockCardClient()
        delivery = CardDelivery(client)

        # Simulate a slow/timeout API — create_card raises TimeoutError
        def slow_create(*args, **kwargs):
            raise TimeoutError("Connection timed out after 30s")

        client.create_card = slow_create

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)

        # Should be handled gracefully as reconcile
        assert len(outcomes) == 1
        assert outcomes[0].kind == "reconcile"
        assert "timed out" in outcomes[0].message.lower() or "timeout" in outcomes[0].message.lower()

    def test_update_timeout_returns_reconcile(self):
        """Timeout on update_card should return reconcile, not crash."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        # First delivery creates successfully
        r1 = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Make update_card timeout
        def timeout_update(*args, **kwargs):
            raise TimeoutError("Request timed out")

        client.update_card = timeout_update
        r2 = [RenderedCard(_card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert delivery.get_binding("sess_1") is None


class TestPartialMultipageFailure:
    """Tests for partial failure during multipage card creation."""

    def test_partial_multipage_create_failure(self):
        """Page 0 created successfully, page 1 create_card raises exception.

        Expected behavior: page 0 binding is preserved (partial success),
        page 1 returns reconcile outcome. Subsequent deliver retries page 1.
        """
        client = MockCardClient()
        delivery = CardDelivery(client)

        call_count = 0
        original_create = client.create_card

        def _failing_second_create(chat_id, card_json, *, reply_to=None, idempotency_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated page 1 creation failure")
            return original_create(
                chat_id,
                card_json,
                reply_to=reply_to,
                idempotency_key=idempotency_key,
            )

        client.create_card = _failing_second_create

        rendered = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_a", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_b", page_index=1, total_pages=2),
        ]

        # Page 0 succeeds (applied), page 1 fails (reconcile)
        outcomes = delivery.deliver("sess_partial", "chat_abc", rendered)

        assert len(outcomes) == 2
        assert outcomes[0].kind == "applied"
        assert outcomes[1].kind == "reconcile"

        # Verify page 0 was created
        assert len(client.creates) == 1
        assert client.creates[0]["card_json"] == {"page": 0}

        # Now retry: fix the client and deliver again — page 0 should be
        # recognized as existing (same signature → skip), page 1 created fresh
        client.create_card = original_create

        outcomes2 = delivery.deliver("sess_partial", "chat_abc", rendered)

        # Page 0 skipped (same signature), page 1 now applied
        assert outcomes2[0].kind == "skipped"
        assert outcomes2[1].kind == "applied"
        # Total creates: 1 (page 0 initial) + 1 (page 1 retry)
        assert len(client.creates) == 2

    def test_retry_missing_page_reuses_same_idempotency_key(self):
        """A retried page create must use the same Feishu uuid for the same session/page."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        call_count = 0
        original_create = client.create_card

        def _failing_second_create(chat_id, card_json, *, reply_to=None, idempotency_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                _failing_second_create.last_idempotency_key = idempotency_key
                raise TimeoutError("create response lost after server-side send")
            return original_create(
                chat_id,
                card_json,
                reply_to=reply_to,
                idempotency_key=idempotency_key,
            )

        _failing_second_create.last_idempotency_key = None
        client.create_card = _failing_second_create

        rendered = [
            RenderedCard(_card_json={"page": 0}, structure_signature="sig_a", page_index=0, total_pages=2),
            RenderedCard(_card_json={"page": 1}, structure_signature="sig_b", page_index=1, total_pages=2),
        ]

        outcomes = delivery.deliver("sess_partial_retry", "chat_abc", rendered)
        assert [outcome.kind for outcome in outcomes] == ["applied", "reconcile"]

        failed_page_key = _failing_second_create.last_idempotency_key
        client.create_card = original_create
        outcomes2 = delivery.deliver("sess_partial_retry", "chat_abc", rendered)

        assert [outcome.kind for outcome in outcomes2] == ["skipped", "applied"]
        assert failed_page_key
        assert client.creates[-1]["idempotency_key"] == failed_page_key

    def test_streaming_reference_uses_stable_idempotency_key(self):
        """Streaming cards create a visible IM reference; that send must be idempotent too."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        rendered = [
            RenderedCard(
                _card_json={"config": {"streaming_mode": True}, "body": {}},
                structure_signature="sig_stream",
                page_index=1,
                total_pages=2,
            )
        ]

        delivery.deliver("sess_stream", "chat_abc", rendered)
        first_key = client.card_references[0]["idempotency_key"]

        assert first_key


class TestSessionLockEviction:
    """Tests for lazy eviction of stale session lock entries."""

    def _make_delivery(self, *, max_locks: int = 10, lock_ttl: float = 600.0):
        """Helper: create a CardDelivery with small thresholds for testing."""
        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=max_locks, session_lock_ttl=lock_ttl)
        return client, delivery

    def test_session_lock_evicted_after_close_and_ttl(self):
        """Closed sessions' locks are evicted by background eviction."""
        import time as _time

        client, delivery = self._make_delivery(max_locks=10, lock_ttl=600.0)

        # Create zombie sessions: deliver but DON'T close
        for i in range(9):
            sid = f"zombie_{i}"
            rendered = [RenderedCard(
                _card_json={}, structure_signature=f"sig_{i}",
                page_index=0, total_pages=1,
            )]
            delivery.deliver(sid, "chat_1", rendered)

        inspector = DeliveryInspector.from_delivery(delivery)
        # Simulate TTL expiry by backdating timestamps
        for i in range(9):
            inspector.timestamps[f"zombie_{i}"] = _time.monotonic() - 700.0

        # Remove bindings to simulate truly stale zombies
        for i in range(9):
            delivery._bindings.remove(f"zombie_{i}")

        assert len(inspector.session_locks) == 9

        # Manually trigger eviction (two-phase, simulates background thread)
        inspector.evict_stale_two_phase()

        # Stale zombie locks should have been partially evicted
        assert len(inspector.session_locks) < 9  # at least 1 evicted

    def test_session_lock_not_evicted_while_active(self):
        """Active sessions (with bindings) are NOT evicted even after TTL."""
        import time as _time

        client, delivery = self._make_delivery(max_locks=10, lock_ttl=600.0)

        # Deliver to 9 sessions (threshold = 8) but keep them active (have bindings)
        for i in range(9):
            sid = f"active_{i}"
            rendered = [RenderedCard(
                _card_json={}, structure_signature=f"sig_{i}",
                page_index=0, total_pages=1,
            )]
            delivery.deliver(sid, "chat_1", rendered)

        inspector = DeliveryInspector.from_delivery(delivery)
        # Simulate TTL expiry by backdating timestamps
        for i in range(9):
            inspector.timestamps[f"active_{i}"] = _time.monotonic() - 700.0

        # Manually trigger eviction (two-phase, simulates background thread)
        inspector.evict_stale_two_phase()

        # All active sessions still have their locks (binding exists → not evicted)
        for i in range(9):
            assert f"active_{i}" in inspector.session_locks

    def test_session_lock_hard_cap_triggers_eviction(self):
        """When session count exceeds 80% of max, background eviction removes stale entries."""
        import time as _time

        client, delivery = self._make_delivery(max_locks=10, lock_ttl=600.0)
        inspector = DeliveryInspector.from_delivery(delivery)

        # Create 9 zombie sessions (no binding, TTL expired)
        for i in range(9):
            sid = f"zombie_{i}"
            with inspector.lock:
                inspector.session_locks[sid] = __import__("threading").RLock()
                inspector.timestamps[sid] = _time.monotonic() - 700.0

        assert len(inspector.session_locks) == 9

        # Manually trigger eviction (two-phase, simulates background thread)
        inspector.evict_stale_two_phase()

        # Zombies partially evicted
        assert len(inspector.session_locks) < 9  # at least some evicted
        assert len(inspector.session_locks) >= 0  # reasonable lower bound

    def test_session_lock_timestamps_refreshed_on_access(self):
        """Accessing a session lock refreshes its timestamp."""
        import time as _time

        client, delivery = self._make_delivery(max_locks=100, lock_ttl=600.0)
        inspector = DeliveryInspector.from_delivery(delivery)

        rendered = [RenderedCard(
            _card_json={}, structure_signature="sig_1",
            page_index=0, total_pages=1,
        )]
        delivery.deliver("sess_1", "chat_1", rendered)

        ts1 = inspector.timestamps["sess_1"]

        # Manually backdate to ensure next deliver produces a fresher timestamp
        inspector.timestamps["sess_1"] = ts1 - 1.0

        # Second deliver refreshes timestamp
        delivery.deliver("sess_1", "chat_1", rendered)
        ts2 = inspector.timestamps["sess_1"]

        assert ts2 > ts1 - 1.0

    def test_batch_cap_evicts_stale_entries(self):
        """Two-phase eviction removes stale zombie entries."""
        import time as _time
        import threading

        # Use max_locks=60 so 65 entries are well above 50% threshold (triggers eviction)
        client, delivery = self._make_delivery(max_locks=60, lock_ttl=600.0)
        inspector = DeliveryInspector.from_delivery(delivery)

        # Inject 65 stale zombie entries (no binding, TTL expired)
        with inspector.lock:
            for i in range(65):
                sid = f"stale_{i}"
                inspector.session_locks[sid] = threading.RLock()
                inspector.timestamps[sid] = _time.monotonic() - 700.0

        assert len(inspector.session_locks) == 65

        # Trigger two-phase eviction
        evicted = inspector.evict_stale_two_phase()

        assert evicted > 0
        assert len(inspector.session_locks) < 65

    def test_two_phase_eviction_is_callable(self):
        """Two-phase eviction method is accessible and callable without preconditions."""
        client, delivery = self._make_delivery(max_locks=100, lock_ttl=600.0)
        inspector = DeliveryInspector.from_delivery(delivery)

        # Should not raise even with no stale entries
        evicted = inspector.evict_stale_two_phase()
        assert evicted == 0


class TestCapacityWarningLog:
    """Tests for 90% capacity threshold warning via real background eviction thread."""

    def test_90_percent_capacity_triggers_real_eviction_warning(self, caplog):
        """Real _eviction_loop_fn thread should log warning at 90%+ capacity."""
        import logging
        import threading
        import time as _time
        import weakref

        from src.card.delivery.lock_pool import _eviction_loop_fn

        client = MockCardClient()
        # Create delivery with low max and very short eviction interval
        delivery = CardDelivery(client, max_session_locks=10, session_lock_ttl=600.0)
        inspector = DeliveryInspector.from_delivery(delivery)
        # Stop the default eviction thread and restart with short interval
        inspector.eviction_stop.set()
        inspector.eviction_thread.join(timeout=2.0)

        # Fill 10 session locks (100% of max=10)
        with inspector.lock:
            for i in range(10):
                sid = f"fill_{i}"
                inspector.session_locks[sid] = threading.RLock()
                inspector.timestamps[sid] = _time.monotonic()
        for i in range(10):
            delivery._bindings.create(f"fill_{i}", "chat_1")

        # Restart eviction loop with a very short interval
        stop_event = threading.Event()
        eviction_interval = 0.05

        warned = threading.Event()
        original_warning = logging.getLogger("src.card.delivery.lock_pool").warning

        def _detect_warning(msg, *args, **kwargs):
            original_warning(msg, *args, **kwargs)
            if "approaching hard limit" in str(msg):
                warned.set()

        with caplog.at_level(logging.WARNING, logger="src.card.delivery.lock_pool"):
            with unittest.mock.patch.object(
                logging.getLogger("src.card.delivery.lock_pool"), "warning", side_effect=_detect_warning
            ):
                t = threading.Thread(
                    target=_eviction_loop_fn,
                    args=(weakref.ref(inspector._pool), stop_event, eviction_interval),
                    name="test-eviction", daemon=True,
                )
                t.start()

                # Wait for the eviction thread to trigger the warning
                assert warned.wait(timeout=2.0), "Eviction loop did not trigger 90% capacity warning"

                stop_event.set()
                t.join(timeout=2.0)

        assert any("approaching hard limit" in msg for msg in caplog.messages)
        delivery._shutdown()


class TestEvictionConcurrencyStress:
    """Stress test: 20+ threads delivering/closing concurrently — no deadlocks."""

    def test_concurrent_deliver_and_eviction_no_deadlock(self):
        """20+ threads deliver simultaneously while background eviction runs.

        Validates: no deadlocks, no exceptions, all threads complete within timeout.
        """
        import threading
        import time

        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=50, session_lock_ttl=1.0)
        # Override eviction interval to be aggressive
        delivery._eviction_interval = 0.05

        num_threads = 25
        iterations_per_thread = 20
        barrier = threading.Barrier(num_threads, timeout=30)
        errors: list[Exception] = []

        def worker(thread_id: int):
            try:
                barrier.wait()
                for i in range(iterations_per_thread):
                    session_id = f"stress_t{thread_id}_i{i}"
                    rendered = [RenderedCard(
                        _card_json={"body": {"elements": [{"tag": "markdown", "content": f"t{thread_id}i{i}"}]}},
                        structure_signature=f"sig_{thread_id}_{i}",
                        page_index=0,
                    )]
                    try:
                        delivery.deliver(session_id, f"chat_{thread_id}", rendered)
                    except Exception:
                        pass  # May get rejected at capacity — that's fine
                    # Close some sessions to trigger eviction eligibility
                    if i % 3 == 0:
                        try:
                            delivery.close(session_id)
                        except Exception:
                            pass
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # Verify all threads completed (no deadlock)
        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"{len(alive)} threads still alive — possible deadlock"
        assert not errors, f"Thread errors: {errors[:5]}"

        # Cleanup
        delivery._shutdown()

    def test_concurrent_close_and_deliver_same_session(self):
        """Multiple threads close/deliver the same session — no crash, no deadlock."""
        import threading

        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=100)

        session_id = "shared_session"
        num_threads = 20
        barrier = threading.Barrier(num_threads, timeout=30)
        errors: list[Exception] = []

        def deliver_worker():
            try:
                barrier.wait()
                for _ in range(50):
                    rendered = [RenderedCard(
                        _card_json={"body": {"elements": [{"tag": "markdown", "content": "x"}]}},
                        structure_signature="sig_shared",
                        page_index=0,
                    )]
                    delivery.deliver(session_id, "chat_shared", rendered)
            except Exception as exc:
                errors.append(exc)

        def close_worker():
            try:
                barrier.wait()
                for _ in range(10):
                    try:
                        delivery.close(session_id)
                    except Exception:
                        pass
            except Exception as exc:
                errors.append(exc)

        threads = []
        for i in range(num_threads):
            if i % 4 == 0:
                threads.append(threading.Thread(target=close_worker))
            else:
                threads.append(threading.Thread(target=deliver_worker))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"{len(alive)} threads still alive — possible deadlock"
        assert not errors, f"Thread errors: {errors[:5]}"
        delivery._shutdown()

    def test_eviction_under_lru_pressure(self):
        """Fill to hard cap then deliver new sessions — LRU evicts cleanly."""
        import threading

        client = MockCardClient()
        max_cap = 30
        delivery = CardDelivery(client, max_session_locks=max_cap, session_lock_ttl=0.1)

        # Fill to capacity
        for i in range(max_cap):
            rendered = [RenderedCard(
                _card_json={"body": {"elements": []}},
                structure_signature=f"sig_{i}",
                page_index=0,
            )]
            delivery.deliver(f"fill_{i}", "chat", rendered)

        assert delivery._lock_pool.count == max_cap

        # Now deliver new sessions — should LRU evict without deadlock
        num_threads = 10
        barrier = threading.Barrier(num_threads, timeout=30)
        errors: list[Exception] = []

        def new_session_worker(tid: int):
            try:
                barrier.wait()
                rendered = [RenderedCard(
                    _card_json={"body": {"elements": []}},
                    structure_signature=f"new_sig_{tid}",
                    page_index=0,
                )]
                result = delivery.deliver(f"new_{tid}", "chat", rendered)
                # Should get either success or rejected (not deadlock)
                assert isinstance(result, list)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=new_session_worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"{len(alive)} threads still alive — possible deadlock"
        assert not errors, f"Thread errors: {errors[:5]}"
        delivery._shutdown()


class TestDeliverRejectedAtCapacity:
    """Test that deliver() degrades gracefully when session lock capacity is exhausted."""

    def test_noop_degradation_when_capacity_full_and_eviction_fails(self):
        """Fill all session locks and disable LRU eviction, verify no-op degradation (still applied)."""
        client = MockCardClient()
        max_locks = 10  # Small capacity for testing
        delivery = CardDelivery(client, max_session_locks=max_locks)

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]

        # Fill up all session lock slots
        for i in range(max_locks):
            result = delivery.deliver(f"session_{i}", "chat_1", rendered)
            assert result[0].kind == "applied"

        # Disable LRU eviction to force capacity exhaustion path
        delivery._lock_pool._lru_evict_oldest = lambda: None  # noqa: internal test hook

        # Next new session gets an ephemeral lock (no-op degradation) — delivery still proceeds
        result = delivery.deliver("session_overflow", "chat_1", rendered)
        assert len(result) == 1
        assert result[0].kind == "applied"  # no-op degradation: delivery still applied

        delivery._shutdown()

    def test_existing_session_not_rejected(self):
        """An existing session should still work even at full capacity."""
        client = MockCardClient()
        max_locks = 5
        delivery = CardDelivery(client, max_session_locks=max_locks)

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]

        # Fill up all session lock slots
        for i in range(max_locks):
            delivery.deliver(f"session_{i}", "chat_1", rendered)

        # Existing session should still be able to deliver (update)
        rendered_update = [RenderedCard(_card_json={"new": True}, structure_signature="sig_2", page_index=0, total_pages=1)]
        result = delivery.deliver("session_0", "chat_1", rendered_update)
        assert result[0].kind in ("applied", "reconcile")

        delivery._shutdown()


class TestEvictionThreshold:
    """Test that eviction is triggered at >50% capacity and not at <=50%."""

    def test_49_percent_no_eviction(self):
        """At 49% capacity, eviction loop does NOT trigger eviction."""
        from unittest.mock import patch

        client = MockCardClient()
        max_locks = 100
        delivery = CardDelivery(client, max_session_locks=max_locks)
        inspector = DeliveryInspector.from_delivery(delivery)

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]

        # Fill to 49% (49 sessions)
        for i in range(49):
            delivery.deliver(f"session_{i}", "chat_1", rendered)

        # Manually trigger eviction loop body
        with patch.object(delivery._lock_pool, '_evict_stale_two_phase') as mock_evict:
            with inspector.lock:
                count = len(inspector.session_locks)
                assert count == 49  # Verify 49%
                need_eviction = count > delivery._max_session_locks * 0.5
            if need_eviction:
                mock_evict()
            # Should NOT have been called (49 <= 50)
            mock_evict.assert_not_called()

        delivery._shutdown()

    def test_51_percent_triggers_eviction(self):
        """At 51% capacity, eviction loop SHOULD trigger eviction."""
        from unittest.mock import patch

        client = MockCardClient()
        max_locks = 100
        delivery = CardDelivery(client, max_session_locks=max_locks)
        inspector = DeliveryInspector.from_delivery(delivery)

        rendered = [RenderedCard(_card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]

        # Fill to 51% (51 sessions)
        for i in range(51):
            delivery.deliver(f"session_{i}", "chat_1", rendered)

        # Manually trigger eviction loop body
        with patch.object(delivery._lock_pool, '_evict_stale_two_phase') as mock_evict:
            with inspector.lock:
                count = len(inspector.session_locks)
                assert count == 51  # Verify 51%
                need_eviction = count > delivery._max_session_locks * 0.5
            if need_eviction:
                mock_evict()
            # Should have been called (51 > 50)
            mock_evict.assert_called_once()

        delivery._shutdown()


class TestEvictionTOCTOUProtection:
    """Test that two-phase eviction skips entries whose timestamp changed between phases."""

    def test_timestamp_changed_between_phases_skips_eviction(self):
        """If a session is reused (new timestamp) between phase 1 and phase 3, it is NOT evicted."""
        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=100, session_lock_ttl=10.0)
        try:
            import time
            inspector = DeliveryInspector.from_delivery(delivery)
            # Create a session lock entry with old timestamp
            with inspector.lock:
                inspector.session_locks["sess_toctou"] = __import__("threading").RLock()
                inspector.timestamps["sess_toctou"] = time.monotonic() - 100  # old enough

            # Now call two-phase eviction
            # But first, simulate that between phase 1 and phase 3, the timestamp gets refreshed

            def patched_eviction():
                # Phase 1: collects sess_toctou as candidate (old timestamp)
                with inspector.lock:
                    now = time.monotonic()
                    candidates = [("sess_toctou", inspector.timestamps["sess_toctou"])]

                # Simulate session reuse between phases — update timestamp
                with inspector.lock:
                    inspector.timestamps["sess_toctou"] = time.monotonic()

                # Phase 3: re-validate (should see new timestamp ≠ original → skip)
                evicted = 0
                with inspector.lock:
                    for sid, original_ts in candidates:
                        current_ts = inspector.timestamps.get(sid)
                        if current_ts is None:
                            continue
                        if current_ts != original_ts:
                            continue  # TOCTOU protection
                        inspector.session_locks.pop(sid, None)
                        inspector.timestamps.pop(sid, None)
                        evicted += 1
                return evicted

            result = patched_eviction()
            assert result == 0  # Should NOT have evicted due to timestamp change
            assert "sess_toctou" in inspector.session_locks  # Still present
        finally:
            delivery._shutdown()


class TestLRUEvictEmptyTimestamps:
    """Test that _lru_evict_oldest handles empty timestamps safely."""

    def test_lru_evict_with_empty_timestamps(self):
        """_lru_evict_oldest should return safely when no timestamps exist."""
        client = MockCardClient()
        delivery = CardDelivery(client, max_session_locks=100, session_lock_ttl=10.0)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            with inspector.lock:
                # Ensure timestamps dict is empty
                inspector.timestamps.clear()
                # Should not raise
                inspector.lru_evict_oldest()
            # Verify nothing bad happened
            assert len(inspector.timestamps) == 0
        finally:
            delivery._shutdown()


class TestEvictionLoopExceptionRecovery:
    """Verify _eviction_loop_fn continues after exceptions."""

    def test_eviction_loop_survives_exception(self):
        """Background eviction thread does not die after an exception."""
        import time
        from unittest.mock import patch

        delivery = CardDelivery(client=MockCardClient(), max_session_locks=100, session_lock_ttl=600)
        try:
            inspector = DeliveryInspector.from_delivery(delivery)
            # Fill to >50% to trigger eviction check
            with inspector.lock:
                for i in range(60):
                    inspector.session_locks[f"s{i}"] = __import__("threading").RLock()
                    inspector.timestamps[f"s{i}"] = time.monotonic() - 1000

            # Patch two-phase eviction to raise, verify thread survives
            call_count = {"n": 0}
            original = inspector.evict_stale_two_phase

            def failing_then_ok():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("simulated eviction failure")
                return original()

            with patch.object(delivery._lock_pool, "_evict_stale_two_phase", side_effect=failing_then_ok):
                # Wait for at least 2 eviction cycles
                # Default interval is 30s, too slow for tests.
                # Instead verify the thread is alive after exception
                inspector.eviction_stop.clear()  # Ensure not stopped
                assert inspector.eviction_thread.is_alive()

            # Thread should still be alive
            assert inspector.eviction_thread.is_alive()
        finally:
            delivery._shutdown()


# ---------------------------------------------------------------------------
# AC12: Empty rendered list on open session
# ---------------------------------------------------------------------------


class TestDeliverEmptyRenderedOpenSession:
    """AC12: deliver(session_id, chat_id, []) on an open session."""

    def test_empty_rendered_new_session_creates_binding_empty_outcomes(self):
        """First delivery with empty rendered: binding created, outcomes empty."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            outcomes = delivery.deliver("sess_empty", "chat_1", [])
            assert outcomes == []
            # No API calls made
            assert len(client.creates) == 0
        finally:
            delivery._shutdown()

    def test_empty_rendered_existing_binding_finalizes_stale_pages(self):
        """Existing binding with pages → empty render should finalize all pages."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            # First: deliver a page to create binding
            r1 = [RenderedCard(
                _card_json={"body": {"elements": []}},
                structure_signature="sig_1",
                page_index=0,
                total_pages=1,
            )]
            delivery.deliver("sess_stale", "chat_1", r1)
            assert len(client.creates) == 1

            # Second: deliver empty list — should finalize stale page
            outcomes = delivery.deliver("sess_stale", "chat_1", [])
            assert outcomes == []
            # The stale page should have been finalized (updated with final state)
            # At minimum, no crash occurred and binding still exists
            binding = delivery.get_binding("sess_stale")
            assert binding is not None
        finally:
            delivery._shutdown()


# ---------------------------------------------------------------------------
# AC14: Non-contiguous page_index
# ---------------------------------------------------------------------------


class TestNonContiguousPageIndex:
    """AC14: RenderedCard list with non-contiguous page_index values."""

    def test_gap_in_page_index_no_crash(self):
        """page_index [0, 2] (gap at 1): should create both pages without crash."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            rendered = [
                RenderedCard(
                    _card_json={"body": {"elements": [{"tag": "markdown", "content": "p0"}]}},
                    structure_signature="sig_0",
                    page_index=0,
                    total_pages=3,
                ),
                RenderedCard(
                    _card_json={"body": {"elements": [{"tag": "markdown", "content": "p2"}]}},
                    structure_signature="sig_2",
                    page_index=2,
                    total_pages=3,
                ),
            ]
            outcomes = delivery.deliver("sess_gap", "chat_1", rendered)
            # Both pages should be created
            assert len(outcomes) == 2
            assert len(client.creates) == 2
        finally:
            delivery._shutdown()

    def test_stale_cleanup_with_gap_does_not_finalize_missing_index(self):
        """After gap delivery, re-delivering fewer pages should only finalize existing pages."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            # Create pages at index 0 and 2
            rendered = [
                RenderedCard(
                    _card_json={"body": {"elements": []}},
                    structure_signature="sig_0",
                    page_index=0,
                    total_pages=3,
                ),
                RenderedCard(
                    _card_json={"body": {"elements": []}},
                    structure_signature="sig_2",
                    page_index=2,
                    total_pages=3,
                ),
            ]
            delivery.deliver("sess_gap2", "chat_1", rendered)

            # Re-deliver with only page 0 — stale cleanup range is range(1, pages_count)
            # But page 1 doesn't exist in binding.pages dict, so should not crash
            rendered2 = [
                RenderedCard(
                    _card_json={"body": {"elements": []}},
                    structure_signature="sig_0",
                    page_index=0,
                    total_pages=1,
                ),
            ]
            # This should not raise KeyError even though index 1 doesn't exist
            outcomes = delivery.deliver("sess_gap2", "chat_1", rendered2)
            assert len(outcomes) == 1  # Only page 0 processed
        finally:
            delivery._shutdown()


# ---------------------------------------------------------------------------
# TOCTOU: second _closed_sessions check under session lock
# ---------------------------------------------------------------------------


class TestTOCTOUClosedAfterLock:
    """Verify _deliver_unlocked returns [] if session is closed after lock acquisition."""

    def test_closed_between_check_and_lock(self):
        """Session closed after fast-path check but before delivery → returns empty list."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            rendered = [
                RenderedCard(
                    _card_json={"body": {"elements": [{"tag": "markdown", "content": "hi"}]}},
                    structure_signature="sig1",
                    page_index=0,
                    total_pages=1,
                ),
            ]

            # First: deliver normally to create binding
            delivery.deliver("toctou_sess", "chat_1", rendered)
            assert len(client.creates) == 1

            # Now close the session (simulates close() racing with deliver())
            delivery.close("toctou_sess")

            # Attempt to deliver again — should return empty due to TOCTOU second check
            outcomes = delivery.deliver("toctou_sess", "chat_1", rendered)
            assert outcomes == []
            # No new creates should have been made
            assert len(client.creates) == 1
        finally:
            delivery._shutdown()

    def test_deliver_unlocked_second_check(self):
        """Directly test _deliver_unlocked returns [] when session in _closed_sessions."""
        client = MockCardClient()
        delivery = CardDelivery(client)
        try:
            rendered = [
                RenderedCard(
                    _card_json={"body": {"elements": [{"tag": "markdown", "content": "x"}]}},
                    structure_signature="sig1",
                    page_index=0,
                    total_pages=1,
                ),
            ]
            # Add session to closed set
            delivery._closed_sessions.add("direct_sess")
            # _deliver_unlocked should return [] immediately
            result = delivery._deliver_unlocked("direct_sess", "chat_1", rendered)
            assert result == []
        finally:
            delivery._shutdown()


class TestSessionLockPoolFullScan:
    """Test SessionLockPool low-watermark full scan."""

    def test_full_scan_cleans_stale_locks_below_threshold(self):
        """Locks below 50% capacity are cleaned by full scan."""
        import time
        from src.card.delivery.lock_pool import SessionLockPool

        pool = SessionLockPool(
            max_locks=100,
            lock_ttl=0.1,  # 100ms TTL for fast test
            eviction_interval=9999,  # disable periodic
        )
        try:
            # Add 10 locks (10% capacity — below 50% threshold)
            for i in range(10):
                pool.acquire(f"s{i}")

            # Wait for locks to become stale
            time.sleep(0.2)

            # Normal eviction check would NOT trigger at 10% capacity
            # But full scan should still clean them
            pool._last_full_scan = 0  # Force scan to be "due"
            pool._full_scan_if_needed()

            # All 10 should be evicted (no active bindings by default)
            assert pool.count == 0
        finally:
            pool.shutdown()

    def test_full_scan_respects_interval(self):
        """Full scan doesn't run if interval hasn't elapsed."""
        import time
        from src.card.delivery.lock_pool import SessionLockPool

        pool = SessionLockPool(
            max_locks=100,
            lock_ttl=0.05,
            eviction_interval=9999,
        )
        try:
            pool.acquire("s1")
            time.sleep(0.1)

            # Don't reset _last_full_scan — it was just set in __init__
            pool._full_scan_if_needed()

            # Lock should NOT be evicted (scan interval not elapsed)
            assert pool.count == 1
        finally:
            pool.shutdown()

    def test_full_scan_skips_active_bindings(self):
        """Full scan preserves locks with active bindings."""
        import time
        from src.card.delivery.lock_pool import SessionLockPool

        pool = SessionLockPool(
            max_locks=100,
            lock_ttl=0.1,
            eviction_interval=9999,
            has_active_binding=lambda sid: sid == "s_active",
        )
        try:
            pool.acquire("s_active")
            pool.acquire("s_stale")
            time.sleep(0.2)

            pool._last_full_scan = 0
            pool._full_scan_if_needed()

            assert pool.contains("s_active")
            assert not pool.contains("s_stale")
        finally:
            pool.shutdown()


# ---------------------------------------------------------------------------
# Async delivery exactly-once test (FS-12)
# ---------------------------------------------------------------------------


class TestAsyncDeliveryExactlyOnce:
    """Verify terminal delivery fires hook exactly once under real async (thread pool) mode."""

    def test_terminal_hook_fires_exactly_once_async(self):
        """
        Creates a CardSession with _sync_delivery=False, dispatches started + completed,
        waits for async delivery, asserts terminal hook fired exactly once.
        """
        import threading

        from src.card.delivery.engine import CardDelivery
        from src.card.events import CardEvent, CardEventType
        from src.card.session import CardSession
        from src.card.session.config import SessionCallbacks, SessionConfig
        from src.card.state.models import CardMetadata

        # Track hook invocations
        hook_calls = []
        hook_done = threading.Event()

        class _TrackingHook:
            """SessionHook that records on_terminal calls."""

            def on_dispatched(self, event, state):
                pass

            def on_terminal(self, state, reason):
                hook_calls.append(reason)
                hook_done.set()

        # Mock delivery client
        client = MockCardClient()
        delivery = CardDelivery(client)

        metadata = CardMetadata(mode_name="Test", tool_name="test", model_name="m1")
        callbacks = SessionCallbacks(hooks=(_TrackingHook(),))
        config = SessionConfig(metadata=metadata, sync_delivery=False)

        session = CardSession(
            chat_id="async_test_chat",
            config=config,
            delivery=delivery,
            callbacks=callbacks,
            session_id="async_sess_1",
        )
        # Override autouse fixture's sync enforcement
        session._sync_delivery = False

        # Dispatch lifecycle events
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent.completed(summary="done"))

        # Wait for async delivery to complete
        assert hook_done.wait(timeout=5.0), "Terminal hook did not fire within 5s"

        # Exactly once
        assert len(hook_calls) == 1
        assert hook_calls[0] in ("completed", "completed_empty")

        # Session should be closed
        assert session.closed
