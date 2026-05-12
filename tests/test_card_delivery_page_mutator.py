"""Unit tests for PageMutator: card create/update/stream/finalize operations."""

from unittest.mock import MagicMock, patch

import pytest

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.page_mutator import PageMutator
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.types import MutationOutcome, SequenceConflictError, TransportError
from src.card.types import ActiveElement, RenderedCard


class MockClient:
    """Minimal CardAPIClient mock."""

    def __init__(self):
        self.creates = []
        self.updates = []
        self.elements = []
        self.streaming_creates = []
        self.card_references = []
        self._counter = 0
        self._raise_on_create: Exception | None = None
        self._raise_on_update: Exception | None = None
        self._raise_on_element: Exception | None = None
        self._raise_on_streaming: Exception | None = None

    def create_card(self, chat_id, card_json, *, reply_to=None):
        if self._raise_on_create:
            raise self._raise_on_create
        self._counter += 1
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        if self._raise_on_update:
            raise self._raise_on_update
        self.updates.append({"card_id": card_id, "sequence": sequence})

    def update_element(self, card_id, element_id, content, *, sequence=0):
        if self._raise_on_element:
            raise self._raise_on_element
        self.elements.append({"card_id": card_id, "element_id": element_id})

    def create_streaming_card(self, card_json):
        if self._raise_on_streaming:
            raise self._raise_on_streaming
        self._counter += 1
        return f"stream_card_{self._counter}"

    def send_card_reference(self, chat_id, card_id, *, reply_to=None):
        self._counter += 1
        return f"ref_msg_{self._counter}"


def _make_card(*, page_index=0, signature="sig_1", text="hello", streaming=False):
    card_json = {"body": {"elements": []}}
    if streaming:
        card_json["config"] = {"streaming_mode": True}
    active = ActiveElement(element_id="el_1", text=text) if text else None
    return RenderedCard(
        _card_json=card_json,
        structure_signature=signature,
        page_index=page_index,
        total_pages=1,
        active_element=active,
    )


class TestCreatePage:
    def test_create_page_success(self):
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        # Pre-create binding (as CardDelivery.deliver() does before calling create_page)
        bindings.create("sess_1", "chat_1")

        card = _make_card()
        outcome = mutator.create_page("sess_1", "chat_1", card)

        assert outcome.kind == "applied"
        assert "created:" in outcome.message
        # Binding should be set
        binding = bindings.get("sess_1")
        assert binding is not None
        assert 0 in binding.pages

    def test_create_page_streaming_success(self):
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        card = _make_card(streaming=True)
        outcome = mutator.create_page("sess_1", "chat_1", card)

        assert outcome.kind == "applied"

    def test_create_page_streaming_fallback_to_im(self):
        """When streaming creation fails, should fall back to IM create API."""
        client = MockClient()
        client._raise_on_streaming = RuntimeError("streaming failed")
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        card = _make_card(streaming=True)
        outcome = mutator.create_page("sess_1", "chat_1", card)

        assert outcome.kind == "applied"
        # Binding should still be set via fallback
        binding = bindings.get("sess_1")
        assert binding is not None
        assert 0 in binding.pages

    def test_create_page_total_failure(self):
        """When all creation attempts fail, returns reconcile."""
        client = MockClient()
        client._raise_on_create = RuntimeError("API down")
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        card = _make_card()
        outcome = mutator.create_page("sess_1", "chat_1", card)

        assert outcome.kind == "reconcile"
        assert "API down" in outcome.message


class TestUpdatePage:
    def test_update_page_success(self):
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        # Set up existing binding
        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "old_sig", "old_text")
        page = bindings.get("sess_1").pages[0]

        card = _make_card(signature="new_sig")
        outcome = mutator.update_page("sess_1", page, card)

        assert outcome.kind == "applied"
        assert "updated:" in outcome.message

    def test_update_page_sequence_conflict_raises_floor(self):
        """On SequenceConflictError, should raise floor and return reconcile."""
        client = MockClient()
        client._raise_on_update = SequenceConflictError(next_floor=10)
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "sig", "text")
        page = bindings.get("sess_1").pages[0]

        card = _make_card(signature="new_sig")
        outcome = mutator.update_page("sess_1", page, card)

        assert outcome.kind == "reconcile"
        assert "sequence_conflict" in outcome.message

    def test_update_page_transport_error(self):
        """Transport errors return reconcile outcome."""
        client = MockClient()
        client._raise_on_update = TransportError("timeout")
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "sig", "text")
        page = bindings.get("sess_1").pages[0]

        card = _make_card(signature="new_sig")
        outcome = mutator.update_page("sess_1", page, card)

        assert outcome.kind == "reconcile"

    def test_update_page_content_invalid_patches_fallback_without_removing_binding(self):
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "old_sig", "old_text")
        page = bindings.get("sess_1").pages[0]

        calls = []

        def update_card(card_id, card_json, *, sequence=0):
            calls.append({"card_id": card_id, "card_json": card_json, "sequence": sequence})
            if len(calls) == 1:
                raise TransportError(
                    "Patch failed: code=230099, msg=ErrCode: 200621 content parse failed",
                    code=230099,
                )

        client.update_card = update_card
        card = _make_card(signature="new_sig")
        outcome = mutator.update_page("sess_1", page, card)

        assert outcome.kind == "applied"
        assert outcome.message == "fallback_content_invalid:230099"
        binding = bindings.get("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert binding.pages[0].signature == "new_sig"
        assert len(calls) == 2
        assert calls[1]["card_json"]["header"]["title"]["content"] == "⚠️ 卡片渲染失败"

    def test_update_page_missing_message_removes_binding_for_recreate(self):
        client = MockClient()
        client._raise_on_update = TransportError("message not found", code=99992354)
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "old_sig", "old_text")
        page = bindings.get("sess_1").pages[0]

        outcome = mutator.update_page("sess_1", page, _make_card(signature="new_sig"))

        assert outcome.kind == "reconcile"
        assert outcome.message == "recreate:99992354"
        assert 0 not in bindings.get("sess_1").pages


class TestStreamElement:
    def test_stream_element_success(self):
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "sig", "old_text")
        page = bindings.get("sess_1").pages[0]

        card = _make_card(text="new_text")
        outcome = mutator.stream_element("sess_1", page, card)

        assert outcome.kind == "applied"
        assert "element:" in outcome.message

    def test_stream_element_no_active_element(self):
        """Without active_element, returns skipped."""
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "sig", "text")
        page = bindings.get("sess_1").pages[0]

        card = _make_card(text="")  # No active element
        # Need to set active_element to None explicitly
        card = RenderedCard(_card_json={"body": {}}, structure_signature="sig", page_index=0, total_pages=1, active_element=None)
        outcome = mutator.stream_element("sess_1", page, card)

        assert outcome.kind == "skipped"

    def test_stream_element_failure_falls_back_to_update(self):
        """When element update fails, should fall back to full update."""
        client = MockClient()
        client._raise_on_element = RuntimeError("element API broken")
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "sig", "old_text")
        page = bindings.get("sess_1").pages[0]

        card = _make_card(text="new_text", signature="new_sig")
        outcome = mutator.stream_element("sess_1", page, card)

        # Falls back to update_page, which should succeed
        assert outcome.kind == "applied"
        assert "updated:" in outcome.message


class TestFinalizePage:
    def test_finalize_page_resets_sequence_and_removes(self):
        client = MockClient()
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        bindings.create("sess_1", "chat_1")
        bindings.set_page("sess_1", 0, "msg_1", "card_1", "sig", "text")
        page = bindings.get("sess_1").pages[0]

        mutator.finalize_page("sess_1", page)

        # Page should be removed from bindings
        binding = bindings.get("sess_1")
        assert 0 not in binding.pages
