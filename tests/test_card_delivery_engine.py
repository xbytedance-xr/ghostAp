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
        self._create_counter = 0
        self._raise_on_update: Exception | None = None

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
        assert len(client.elements) == 1
        assert client.elements[0]["content"] == "hello world"
        assert client.elements[0]["element_id"] == "el_1"

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
