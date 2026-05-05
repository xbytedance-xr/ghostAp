"""Tests for CardDelivery.__del__ destructor behavior."""
from unittest.mock import MagicMock

from src.card.delivery.engine import CardDelivery


class TestCardDeliveryDestructor:
    def test_del_calls_lock_pool_shutdown(self):
        """When CardDelivery.__del__ is invoked, it should call _lock_pool.shutdown()."""
        client = MagicMock()
        client.create_card = MagicMock(return_value=("msg_1", "card_1"))
        client.update_card = MagicMock()

        delivery = CardDelivery(client)
        # Grab a reference to the lock pool and mock its shutdown
        mock_shutdown = MagicMock()
        delivery._lock_pool.shutdown = mock_shutdown

        # Explicitly call __del__ (simulates GC finalization)
        delivery.__del__()

        # Verify _lock_pool.shutdown() was called
        mock_shutdown.assert_called_once()

        # Cleanup
        delivery._shutdown()

    def test_del_suppresses_exceptions(self):
        """__del__ should suppress any exception from _lock_pool.shutdown()."""
        client = MagicMock()
        client.create_card = MagicMock(return_value=("msg_1", "card_1"))
        client.update_card = MagicMock()

        delivery = CardDelivery(client)
        # Make shutdown raise
        delivery._lock_pool.shutdown = MagicMock(side_effect=RuntimeError("already shut down"))

        # Should not raise
        delivery.__del__()

        # Reset mock for cleanup
        delivery._lock_pool.shutdown = MagicMock()
        delivery._shutdown()
