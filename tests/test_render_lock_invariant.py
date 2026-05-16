"""Test that render_card() is called outside the session lock.

This test verifies the architectural invariant that Phase 1b (render) executes
outside self._lock to avoid holding the lock during CPU-bound work.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_render_called_outside_lock():
    """Spy on render_card to assert session._lock is NOT held during render."""
    from src.card.events import CardEvent
    from src.card.session import CardSession
    from src.card.session.config import SessionCallbacks, SessionConfig
    from src.card.state.models import CardMetadata

    lock_was_held_during_render = []

    mock_delivery = MagicMock()
    mock_delivery.deliver.return_value = []

    metadata = CardMetadata(engine_type="deep")
    config = SessionConfig(metadata=metadata)
    callbacks = SessionCallbacks(notify_callback=lambda _cid, _txt: None)

    session = CardSession(
        chat_id="test-chat",
        config=config,
        delivery=mock_delivery,
        callbacks=callbacks,
    )

    def _spy_render(state, budget=None):
        # Check if session lock is held at this moment
        locked = session._lock.locked()
        lock_was_held_during_render.append(locked)
        # Return a minimal rendered card
        from src.card.types import RenderedCard
        return [RenderedCard(_card_json={"schema": "2.0"}, structure_signature="s", content_hash="h")]

    with patch("src.card.session.core.render_card", side_effect=_spy_render):
        session.dispatch(CardEvent.started())

    assert len(lock_was_held_during_render) == 1
    assert lock_was_held_during_render[0] is False, "render_card() must NOT be called while session._lock is held"
