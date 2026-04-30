"""Tests for CardDelivery engine."""

import pytest

from src.card.delivery.engine import (
    CardDelivery,
    CardAPIClient,
    MutationOutcome,
    SequenceConflictError,
    TransportError,
)
from src.card.render.renderer import ActiveElement, RenderedCard


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

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        card_id = f"card_{self._create_counter}"
        self.creates.append({
            "chat_id": chat_id,
            "card_json": card_json,
            "reply_to": reply_to,
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

    def send_card_reference(self, chat_id, card_id, *, reply_to=None):
        self._create_counter += 1
        msg_id = f"msg_{self._create_counter}"
        self.card_references.append({"chat_id": chat_id, "card_id": card_id, "reply_to": reply_to})
        return msg_id


class TestCardDeliveryCreate:
    """First delivery creates cards."""

    def test_first_deliver_creates_card(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [RenderedCard(
            card_json={"body": {"elements": []}},
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
            card_json={},
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
        r1 = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Second delivery with different signature
        r2 = [RenderedCard(card_json={"updated": True}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "applied"
        assert len(client.updates) == 1
        assert client.updates[0]["card_id"] == "card_1"

    def test_text_only_triggers_element_content(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="hello")
        r1 = [RenderedCard(
            card_json={},
            structure_signature="sig_1",
            active_element=active,
            page_index=0,
            total_pages=1,
        )]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Same signature, different text
        active2 = ActiveElement(element_id="el_1", text="hello world")
        r2 = [RenderedCard(
            card_json={},
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
            card_json={},
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

        r1 = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Set up conflict on next update
        client._raise_on_update = SequenceConflictError(next_floor=10)
        r2 = [RenderedCard(card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert "sequence_conflict" in outcomes[0].message


class TestMultiPage:
    """Multi-page delivery."""

    def test_multi_page_delivery(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        rendered = [
            RenderedCard(card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", rendered)

        assert len(outcomes) == 2
        assert all(o.kind == "applied" for o in outcomes)
        assert len(client.creates) == 2

    def test_new_page_created_on_growth(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        # Start with 1 page
        r1 = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Grow to 2 pages
        r2 = [
            RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(card_json={}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        # Page 0 same sig → skip, Page 1 new → create
        assert outcomes[1].kind == "applied"
        assert len(client.creates) == 2  # 1 initial + 1 new page


class TestPageShrink:
    """Page shrink: stale pages cleaned up when page count decreases."""

    def test_shrink_removes_stale_page_from_binding(self):
        client = MockCardClient()
        delivery = CardDelivery(client)

        # Deliver 2 pages
        r2 = [
            RenderedCard(card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_1", "chat_abc", r2)

        binding = delivery.get_binding("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert 1 in binding.pages
        assert len(binding.pages) == 2

        # Shrink to 1 page (different signature to trigger update)
        r1 = [
            RenderedCard(card_json={"page": 0}, structure_signature="sig_2", page_index=0, total_pages=1),
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
            RenderedCard(card_json={"page": 0}, structure_signature="sig_1", page_index=0, total_pages=2),
            RenderedCard(card_json={"page": 1}, structure_signature="sig_1", page_index=1, total_pages=2),
        ]
        delivery.deliver("sess_1", "chat_abc", r2)

        # The stale page card_id is "card_2" (second create)
        stale_card_id = "card_2"
        # Bump sequence so we can verify reset
        delivery._sequences.next_sequence(stale_card_id)
        assert delivery._sequences.current(stale_card_id) > 0

        # Shrink to 1 page
        r1 = [
            RenderedCard(card_json={"page": 0}, structure_signature="sig_2", page_index=0, total_pages=1),
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
            card_json={"config": {"streaming_mode": True}, "body": {}},
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
            card_json={"config": {"streaming_mode": True}, "body": {}},
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
            card_json={"config": {"streaming_mode": True}, "body": {}},
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

        r1 = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)
        assert delivery.get_binding("sess_1") is not None

        delivery.close("sess_1")
        assert delivery.get_binding("sess_1") is None


class TestTransportError:
    """TransportError handling in update path."""

    def test_transport_error_returns_reconcile(self):
        """update_card raising TransportError should return reconcile outcome."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        # First delivery creates the card
        r1 = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
        delivery.deliver("sess_1", "chat_abc", r1)

        # Second delivery triggers update — TransportError
        client._raise_on_update = TransportError("connection timeout")
        r2 = [RenderedCard(card_json={}, structure_signature="sig_2", page_index=0, total_pages=1)]
        outcomes = delivery.deliver("sess_1", "chat_abc", r2)

        assert outcomes[0].kind == "reconcile"
        assert "connection timeout" in outcomes[0].message

    def test_transport_error_on_element_falls_back_to_update(self):
        """update_element raising TransportError falls back to _update_page."""
        client = MockCardClient()
        delivery = CardDelivery(client)

        active = ActiveElement(element_id="el_1", text="hello")
        r1 = [RenderedCard(
            card_json={}, structure_signature="sig_1",
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
            card_json={}, structure_signature="sig_1",
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

        rendered = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
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

        rendered = [RenderedCard(card_json={}, structure_signature="sig_1", page_index=0, total_pages=1)]
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
                    card_json={"header": {"title": {"content": "test"}}},
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
