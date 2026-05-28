"""Tests for CardSession weakref.finalize safety net (ResourceWarning on GC without close)."""

import warnings
import weakref
from unittest.mock import MagicMock

from src.card.delivery.engine import CardDelivery
from src.card.session.config import SessionConfig
from src.card.session.core import CardSession, _release_lock
from src.card.state.models import CardMetadata


class _MockClient:
    """Minimal CardAPIClient mock."""

    def create_card(self, chat_id, card_json, *, reply_to=None, reply_in_thread=None, idempotency_key=None):
        return ("msg_1", "card_1")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


class TestCardSessionDel:
    """Verify weakref.finalize safety net emits ResourceWarning and releases lock."""

    def _make_session(self) -> CardSession:
        client = _MockClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(mode_name="Test", mode_emoji="T")
        config = SessionConfig(metadata=metadata)
        session = CardSession(
            chat_id="chat_del_test",
            config=config,
            delivery=delivery,
            session_id="del_test_session",
        )
        return session

    def test_finalize_emits_resource_warning_when_not_closed(self):
        """GC of an unclosed session should emit ResourceWarning via weakref.finalize."""
        session = self._make_session()
        assert not session.closed

        # Directly invoke the _release_lock callback to test the safety net logic
        delivery_ref = weakref.ref(session._delivery)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _release_lock(delivery_ref, "del_test_session")

        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) >= 1
        assert "del_test_session" in str(resource_warnings[0].message)

    def test_finalize_calls_release_session_lock(self):
        """Finalizer of unclosed session should call delivery.release_session_lock."""
        session = self._make_session()
        session._delivery.release_session_lock = MagicMock()
        delivery_ref = weakref.ref(session._delivery)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            _release_lock(delivery_ref, "del_test_session")

        session._delivery.release_session_lock.assert_called_once_with("del_test_session")

    def test_finalize_no_warning_when_closed(self):
        """A properly closed session detaches its finalizer — no ResourceWarning on GC."""
        session = self._make_session()
        session.close()
        assert session.closed

        # After close(), the finalizer is detached, so _release_lock won't be called.
        # We verify by checking that the finalizer is no longer alive.
        assert not session._finalizer.alive

    def test_release_lock_handles_dead_weakref(self):
        """_release_lock should gracefully handle a dead delivery weakref (returns None)."""
        # Simulate a dead weakref by using a lambda that returns None
        dead_ref = lambda: None  # noqa: E731
        # Should not raise
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _release_lock(dead_ref, "dead_session")

        # No ResourceWarning emitted for dead reference
        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) == 0
