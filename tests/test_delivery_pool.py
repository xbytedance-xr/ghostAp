"""Tests for src/card/delivery/pool.py — thread pool lifecycle."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

import pytest


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


class TestRuntimeErrorFallback:
    """Test _submit_delivery fallback when pool raises RuntimeError."""

    def test_sync_fallback_on_pool_runtime_error(self):
        """When pool.submit raises RuntimeError, delivery runs synchronously."""
        from src.card.session import CardSession

        # Create a session with mocked delivery
        mock_delivery = MagicMock()
        mock_delivery.deliver.return_value = []

        with patch("src.card.delivery.pool.get_delivery_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.submit.side_effect = RuntimeError("pool shut down")
            mock_get_pool.return_value = mock_pool

            # Create a minimal session
            session = MagicMock(spec=CardSession)
            session._sync_delivery = False
            session._session_id = "test-123"
            session._coordinator = MagicMock()
            session._coordinator.deliver.return_value = []

            # Call _submit_delivery directly
            rendered = [MagicMock()]
            from src.card.events import CardEvent

            event = CardEvent.started()
            # Use the real method on the mock
            CardSession._submit_delivery(session, rendered, False, event)

            # Should have fallen back to synchronous _deliver_and_track
            session._deliver_and_track.assert_called_once_with(rendered, False, event=event)
