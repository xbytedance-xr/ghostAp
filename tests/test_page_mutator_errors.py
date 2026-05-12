"""Tests for PageMutator handling of TimeoutError and ConnectionError.

Verifies that PageMutator.create_page and update_page gracefully handle
network errors (TimeoutError, ConnectionError) by returning
MutationOutcome(kind="reconcile") instead of propagating exceptions.
"""

import pytest

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.engine import MutationOutcome, SequenceConflictError, TransportError
from src.card.delivery.page_mutator import PageMutator
from src.card.delivery.sequence import SequenceManager
from src.card.types import ActiveElement, RenderedCard


class _ErrorClient:
    """Mock client that raises configurable errors."""

    def __init__(self, error: Exception | None = None):
        self._error = error
        self._create_counter = 0

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        if self._error:
            raise self._error
        self._create_counter += 1
        return (f"msg_{self._create_counter}", f"card_{self._create_counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        if self._error:
            raise self._error

    def update_element(self, card_id, element_id, content, *, sequence=0):
        if self._error:
            raise self._error

    def create_streaming_card(self, card_json):
        if self._error:
            raise self._error
        return "stream_card_1"

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, idempotency_key=None):
        if self._error:
            raise self._error
        return "ref_msg_1"


def _make_rendered(sig: str = "sig_1", page_index: int = 0) -> RenderedCard:
    return RenderedCard(
        _card_json={"body": {"elements": []}},
        structure_signature=sig,
        page_index=page_index,
        total_pages=1,
        active_element=ActiveElement(element_id="el_1", text="hello"),
    )


def _setup_with_binding():
    """Create mutator + bindings with a pre-created binding (simulates _deliver_unlocked)."""
    client = _ErrorClient(None)
    bindings = BindingStore()
    sequences = SequenceManager()
    mutator = PageMutator(client, bindings, sequences)
    # Simulate what CardDelivery._deliver_unlocked does before calling create_page
    bindings.create("sess_1", "chat_1")
    return client, bindings, sequences, mutator


class TestPageMutatorTimeoutError:
    """TimeoutError in API calls → reconcile outcome."""

    def test_create_page_timeout(self):
        client = _ErrorClient(TimeoutError("Connection timed out"))
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)
        bindings.create("sess_1", "chat_1")

        outcome = mutator.create_page("sess_1", "chat_1", _make_rendered())
        assert outcome.kind == "reconcile"
        assert "timed out" in outcome.message.lower()

    def test_update_page_timeout(self):
        client, bindings, sequences, mutator = _setup_with_binding()

        # First create a page to get a PageBinding
        outcome = mutator.create_page("sess_1", "chat_1", _make_rendered())
        assert outcome.kind == "applied"

        # Now make the client raise on update
        client._error = TimeoutError("Request timeout")
        page = bindings.get("sess_1").pages[0]
        outcome = mutator.update_page("sess_1", page, _make_rendered(sig="sig_2"))
        assert outcome.kind == "reconcile"

    def test_stream_element_timeout_falls_back_to_update(self):
        """TimeoutError in stream_element triggers fallback to update_page."""
        client, bindings, sequences, mutator = _setup_with_binding()

        # Create page first
        mutator.create_page("sess_1", "chat_1", _make_rendered())

        # Make element update raise TimeoutError
        client._error = TimeoutError("timeout")
        page = bindings.get("sess_1").pages[0]
        card = _make_rendered(sig="sig_1")  # same sig to trigger element path
        outcome = mutator.stream_element("sess_1", page, card)
        # Falls back to update_page which also raises → reconcile
        assert outcome.kind == "reconcile"


class TestPageMutatorConnectionError:
    """ConnectionError in API calls → reconcile outcome."""

    def test_create_page_connection_error(self):
        client = _ErrorClient(ConnectionError("Connection refused"))
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)
        bindings.create("sess_1", "chat_1")

        outcome = mutator.create_page("sess_1", "chat_1", _make_rendered())
        assert outcome.kind == "reconcile"
        assert "refused" in outcome.message.lower()

    def test_update_page_connection_error(self):
        client, bindings, sequences, mutator = _setup_with_binding()

        mutator.create_page("sess_1", "chat_1", _make_rendered())
        client._error = ConnectionError("Network unreachable")
        page = bindings.get("sess_1").pages[0]
        outcome = mutator.update_page("sess_1", page, _make_rendered(sig="sig_2"))
        assert outcome.kind == "reconcile"


class TestPageMutatorTransportError:
    """TransportError (5xx) → reconcile outcome with specific handling."""

    def test_update_page_transport_error(self):
        client, bindings, sequences, mutator = _setup_with_binding()

        mutator.create_page("sess_1", "chat_1", _make_rendered())
        client._error = TransportError("502 Bad Gateway")
        page = bindings.get("sess_1").pages[0]
        outcome = mutator.update_page("sess_1", page, _make_rendered(sig="sig_2"))
        assert outcome.kind == "reconcile"
        assert "502" in outcome.message


class TestPageMutatorSequenceConflict:
    """SequenceConflictError → reconcile with floor raised."""

    def test_update_page_sequence_conflict(self):
        client, bindings, sequences, mutator = _setup_with_binding()

        mutator.create_page("sess_1", "chat_1", _make_rendered())
        client._error = SequenceConflictError(next_floor=10)
        page = bindings.get("sess_1").pages[0]
        outcome = mutator.update_page("sess_1", page, _make_rendered(sig="sig_2"))
        assert outcome.kind == "reconcile"
        assert "sequence_conflict" in outcome.message

    def test_stream_element_sequence_conflict_falls_back(self):
        """SequenceConflictError in element update → falls back to update_page."""
        client, bindings, sequences, mutator = _setup_with_binding()

        mutator.create_page("sess_1", "chat_1", _make_rendered())
        client._error = SequenceConflictError(next_floor=5)
        page = bindings.get("sess_1").pages[0]
        card = _make_rendered(sig="sig_1")
        outcome = mutator.stream_element("sess_1", page, card)
        # Falls back to update_page which also raises SequenceConflictError → reconcile
        assert outcome.kind == "reconcile"


class TestPageMutatorCreatePageSuccess:
    """Verify successful create_page sets binding correctly."""

    def test_create_page_sets_binding(self):
        client, bindings, sequences, mutator = _setup_with_binding()

        card = _make_rendered()
        outcome = mutator.create_page("sess_1", "chat_1", card)
        assert outcome.kind == "applied"
        assert "created:" in outcome.message

        binding = bindings.get("sess_1")
        assert binding is not None
        assert 0 in binding.pages
        assert binding.pages[0].signature == "sig_1"


class TestSetPageWriteFailure:
    """When set_page() fails after card creation, outcome is reconcile and no ghost binding."""

    def test_set_page_raises_returns_reconcile(self):
        """API creates card successfully but set_page throws → reconcile, no binding page."""
        from unittest.mock import patch

        client = _ErrorClient(None)
        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)
        bindings.create("sess_1", "chat_1")

        # Patch set_page to fail after card is created
        with patch.object(bindings, "set_page", side_effect=RuntimeError("write failed")):
            outcome = mutator.create_page("sess_1", "chat_1", _make_rendered())

        assert outcome.kind == "reconcile"
        assert "write failed" in outcome.message

        # Verify no ghost page binding was left behind
        binding = bindings.get("sess_1")
        assert binding is not None
        assert len(binding.pages) == 0
