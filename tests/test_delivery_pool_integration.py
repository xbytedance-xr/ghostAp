"""Integration test: verify thread pool delivery end-to-end.

Marked @pytest.mark.integration — not run in default test suite.
Run with: uv run pytest -m integration tests/test_delivery_pool_integration.py -v
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_pool():
    """Reset pool state for integration test."""
    import src.card.delivery.pool as pool_mod

    pool_mod._pool = None
    pool_mod._shutting_down = False
    yield
    if pool_mod._pool is not None:
        pool_mod._pool.shutdown(wait=False)
    pool_mod._pool = None
    pool_mod._shutting_down = False


def test_delivery_runs_on_non_main_thread():
    """With _sync_delivery=False, delivery executes on a pool thread."""
    delivery_thread_name = []

    mock_delivery = MagicMock()

    def _fake_deliver(*args, **kwargs):
        delivery_thread_name.append(threading.current_thread().name)
        return []

    mock_delivery.deliver.side_effect = _fake_deliver
    mock_delivery.close = MagicMock()

    from src.card.events import CardEvent
    from src.card.session import CardSession
    from src.card.session.config import SessionCallbacks, SessionConfig
    from src.card.state.models import CardMetadata

    metadata = CardMetadata(engine_type="deep")
    config = SessionConfig(metadata=metadata)
    callbacks = SessionCallbacks(notify_callback=lambda _cid, _txt: None)

    with patch("src.card.session.core.render_card") as mock_render:
        mock_render.return_value = [MagicMock(_card_json={"schema": "2.0"}, structure_signature="sig", content_hash="h")]

        session = CardSession(
            chat_id="test-chat",
            config=config,
            delivery=mock_delivery,
            callbacks=callbacks,
        )
        # Override sync delivery
        session._sync_delivery = False

        session.dispatch(CardEvent.started())

        # Wait for pool thread to complete
        from src.card.delivery.pool import get_delivery_pool
        get_delivery_pool().shutdown(wait=True)

    # Delivery should have been called on a card-delivery thread
    assert len(delivery_thread_name) >= 1
    assert "card-delivery" in delivery_thread_name[0]
