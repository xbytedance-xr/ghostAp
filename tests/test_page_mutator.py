"""Tests for src.card.delivery.page_mutator — PageMutator unit tests.

Covers:
- create_page streaming success path
- create_page streaming fallback to IM API
- update_page sequence conflict reconcile
- stream_element fallback to update_page
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.engine import MutationOutcome, SequenceConflictError
from src.card.delivery.page_mutator import PageMutator
from src.card.delivery.sequence import SequenceManager


@dataclass
class FakeActiveElement:
    element_id: str = "elm_1"
    text: str = "hello world"


@dataclass
class FakeRenderedCard:
    page_index: int = 0
    structure_signature: str = "sig_abc"
    active_element: FakeActiveElement | None = None

    def to_feishu_json(self):
        return {"config": {"streaming_mode": False}, "body": "fake"}


def _make_streaming_card():
    card = FakeRenderedCard(active_element=FakeActiveElement())
    card.to_feishu_json = lambda: {"config": {"streaming_mode": True}, "body": "stream"}
    return card


class TestCreatePageStreamingSuccess:
    """create_page: streaming path succeeds."""

    def test_streaming_card_creates_via_streaming_api(self):
        client = MagicMock()
        client.create_streaming_card.return_value = "card_id_1"
        client.send_card_reference.return_value = "msg_id_1"

        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        card = _make_streaming_card()
        result = mutator.create_page("session_1", "chat_1", card)

        assert result.kind == "applied"
        assert "msg_id_1" in result.message
        client.create_streaming_card.assert_called_once()
        client.send_card_reference.assert_called_once_with("chat_1", "card_id_1", reply_to=None)


class TestCreatePageStreamingFallback:
    """create_page: streaming fails, falls back to IM API."""

    def test_streaming_failure_falls_back_to_create_card(self):
        client = MagicMock()
        client.create_streaming_card.side_effect = RuntimeError("streaming unavailable")
        client.create_card.return_value = ("msg_id_2", "card_id_2")

        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        card = _make_streaming_card()
        result = mutator.create_page("session_1", "chat_1", card)

        assert result.kind == "applied"
        assert "msg_id_2" in result.message
        client.create_card.assert_called_once()


class TestUpdatePageSequenceConflict:
    """update_page: SequenceConflictError triggers reconcile outcome."""

    def test_sequence_conflict_raises_floor_and_returns_reconcile(self):
        client = MagicMock()
        conflict = SequenceConflictError(next_floor=10)
        client.update_card.side_effect = conflict

        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        page = PageBinding(page_index=0, message_id="msg_1", card_id="card_1", signature="old_sig", last_text="")
        card = FakeRenderedCard(active_element=FakeActiveElement())

        result = mutator.update_page("session_1", page, card)

        assert result.kind == "reconcile"
        assert "sequence_conflict" in result.message
        # Floor should have been raised
        assert sequences.next_sequence("card_1") >= 10


class TestStreamElementFallbackToUpdate:
    """stream_element: on failure, falls back to update_page."""

    def test_element_update_error_triggers_full_update_fallback(self):
        client = MagicMock()
        # First call (update_element) fails, second call (update_card in fallback) succeeds
        client.update_element.side_effect = RuntimeError("element API unavailable")
        client.update_card.return_value = None

        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        page = PageBinding(page_index=0, message_id="msg_1", card_id="card_1", signature="old_sig", last_text="")
        card = FakeRenderedCard(active_element=FakeActiveElement())

        result = mutator.stream_element("session_1", page, card)

        # Should fall back to update_page which succeeds
        assert result.kind == "applied"
        client.update_element.assert_called_once()
        client.update_card.assert_called_once()

    def test_sequence_conflict_in_element_falls_back_to_update(self):
        client = MagicMock()
        conflict = SequenceConflictError(next_floor=5)
        client.update_element.side_effect = conflict
        client.update_card.return_value = None

        bindings = BindingStore()
        sequences = SequenceManager()
        mutator = PageMutator(client, bindings, sequences)

        page = PageBinding(page_index=0, message_id="msg_1", card_id="card_1", signature="old_sig", last_text="")
        card = FakeRenderedCard(active_element=FakeActiveElement())

        result = mutator.stream_element("session_1", page, card)

        assert result.kind == "applied"
        client.update_card.assert_called_once()
