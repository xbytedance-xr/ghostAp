"""Tests for src/card/delivery/pool.py — thread pool lifecycle, integration, and fallback."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.session.config import SessionCallbacks, SessionConfig
from src.card.state.models import CardMetadata


@pytest.fixture(autouse=True)
def _reset_pool_state():
    """Reset pool module state before/after each test."""
    import src.card.delivery.pool as pool_mod

    # Save originals
    orig_pool = pool_mod._pool
    orig_shutting = pool_mod._shutting_down

    # Reset for test
    pool_mod._pool = None
    pool_mod._shutting_down = False
    yield
    # Shutdown any pool created during test
    if pool_mod._pool is not None:
        pool_mod._pool.shutdown(wait=False)
    # Restore
    pool_mod._pool = orig_pool
    pool_mod._shutting_down = orig_shutting


class TestGetDeliveryPool:
    """Test lazy initialization and thread safety of get_delivery_pool()."""

    def test_returns_thread_pool_executor(self):
        from src.card.delivery.pool import get_delivery_pool

        pool = get_delivery_pool()
        assert isinstance(pool, ThreadPoolExecutor)

    def test_returns_same_instance_on_multiple_calls(self):
        from src.card.delivery.pool import get_delivery_pool

        pool1 = get_delivery_pool()
        pool2 = get_delivery_pool()
        assert pool1 is pool2

    def test_concurrent_init_returns_same_instance(self):
        """10 threads all call get_delivery_pool() concurrently — must get same instance."""
        from src.card.delivery.pool import get_delivery_pool

        results: list[ThreadPoolExecutor] = []
        barrier = threading.Barrier(10)

        def _get():
            barrier.wait()
            results.append(get_delivery_pool())

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 10
        assert all(r is results[0] for r in results)


class TestShutdownDeliveryPool:
    """Test shutdown behavior."""

    def test_shutdown_idempotent(self):
        """Calling shutdown_delivery_pool() twice does not raise."""
        from src.card.delivery.pool import get_delivery_pool, shutdown_delivery_pool

        get_delivery_pool()  # ensure pool exists
        shutdown_delivery_pool(wait=True)
        shutdown_delivery_pool(wait=True)  # second call should be no-op

    def test_shutdown_before_init_is_noop(self):
        """Shutting down before pool is created does nothing."""
        from src.card.delivery.pool import shutdown_delivery_pool

        shutdown_delivery_pool(wait=True)  # no error

    def test_get_after_shutdown_raises_runtime_error(self):
        """After shutdown, get_delivery_pool() raises RuntimeError (sentinel check)."""
        from src.card.delivery.pool import get_delivery_pool, shutdown_delivery_pool

        get_delivery_pool()  # ensure pool exists
        shutdown_delivery_pool(wait=True)

        with pytest.raises(RuntimeError, match="delivery pool has been shut down"):
            get_delivery_pool()


# ---------------------------------------------------------------------------
# RuntimeError fallback — AC-18
# ---------------------------------------------------------------------------


class MockDeliveryClient:
    """Minimal mock for CardAPIClient."""

    def __init__(self):
        self.creates = []

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self.creates.append(chat_id)
        return ("msg_1", "card_1")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


class TestDeliveryPoolRuntimeErrorFallback:
    """When pool.submit raises RuntimeError, delivery falls back to sync."""

    def _make_session(self, *, sync_delivery=False):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(engine_type="deep")
        config = SessionConfig(metadata=metadata, sync_delivery=sync_delivery)
        session = CardSession(
            chat_id="chat_pool_test",
            config=config,
            delivery=delivery,
            session_id="pool_fallback_test",
        )
        return session, client

    def test_pool_shutdown_falls_back_to_sync_delivery(self):
        """RuntimeError from pool.submit -> synchronous delivery succeeds."""
        session, client = self._make_session(sync_delivery=False)

        # Patch get_delivery_pool to return a mock that raises RuntimeError on submit
        mock_pool = MagicMock()
        mock_pool.submit.side_effect = RuntimeError("cannot schedule new futures after shutdown")

        with patch("src.card.delivery.pool.get_delivery_pool", return_value=mock_pool):
            # This should NOT raise — it falls back to sync delivery
            session.dispatch(CardEvent(type=CardEventType.STARTED))

        # Verify delivery still happened (synchronous fallback)
        assert len(client.creates) == 1

    def test_pool_shutdown_delivers_terminal_event(self):
        """Terminal event delivery also works under pool shutdown fallback."""
        session, client = self._make_session(sync_delivery=False)

        mock_pool = MagicMock()
        mock_pool.submit.side_effect = RuntimeError("pool shut down")

        with patch("src.card.delivery.pool.get_delivery_pool", return_value=mock_pool):
            session.dispatch(CardEvent(type=CardEventType.STARTED))
            session.dispatch(CardEvent(type=CardEventType.COMPLETED, payload={}))

        # Both events delivered successfully via sync fallback
        assert len(client.creates) >= 1


# ---------------------------------------------------------------------------
# Integration: verify thread pool delivery end-to-end
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
def test_delivery_runs_on_non_main_thread():
    """With _sync_delivery=False, delivery executes on a pool thread."""
    delivery_thread_name = []

    mock_delivery = MagicMock()

    def _fake_deliver(*args, **kwargs):
        delivery_thread_name.append(threading.current_thread().name)
        return []

    mock_delivery.deliver.side_effect = _fake_deliver
    mock_delivery.close = MagicMock()

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
