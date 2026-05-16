"""Tests for atexit/graceful shutdown behavior.

Validates:
- graceful_shutdown sets is_shutting_down flag
- Double-call is idempotent (second call returns early)
- delivery_registry.install_atexit() is idempotent
- shutdown sequence order: cleanup → hooks → drain → delivery shutdown → hook executor
"""

import unittest
from unittest.mock import MagicMock, patch

from src.utils.shutdown import (
    _reset_shutdown_state,
    graceful_shutdown,
    is_shutting_down,
)


class TestAtexitShutdown(unittest.TestCase):
    """Graceful shutdown behavior."""

    def setUp(self):
        _reset_shutdown_state()

    def tearDown(self):
        _reset_shutdown_state()

    def test_is_shutting_down_initially_false(self):
        self.assertFalse(is_shutting_down())

    @patch("src.utils.shutdown.sys.exit")
    @patch("src.utils.shutdown.shutdown_hook_executor", create=True)
    @patch("src.utils.shutdown.fire_hooks")
    @patch("src.utils.shutdown.run_all_cleanups")
    def test_graceful_shutdown_sets_flag(self, mock_cleanups, mock_fire, mock_hook_exec, mock_exit):
        mock_cleanups.return_value = MagicMock()  # async mock
        try:
            graceful_shutdown(reason="test")
        except SystemExit:
            pass
        self.assertTrue(is_shutting_down())

    @patch("src.utils.shutdown.sys.exit")
    @patch("src.utils.shutdown.fire_hooks")
    @patch("src.utils.shutdown.run_all_cleanups")
    def test_double_call_idempotent(self, mock_cleanups, mock_fire, mock_exit):
        mock_cleanups.return_value = MagicMock()
        try:
            graceful_shutdown(reason="first")
        except SystemExit:
            pass
        # Second call should return early
        call_count_before = mock_cleanups.call_count
        graceful_shutdown(reason="second")
        self.assertEqual(mock_cleanups.call_count, call_count_before)

    def test_install_atexit_idempotent(self):
        """delivery_registry.install_atexit() is idempotent — only registers once."""
        from src.card.delivery.registry import DeliveryRegistry
        reg = DeliveryRegistry()
        with patch("src.card.delivery.registry.atexit") as mock_atexit:
            reg.install_atexit()
            reg.install_atexit()  # second call should be no-op
            mock_atexit.register.assert_called_once()
            # The registered callback should drain then shutdown
            callback = mock_atexit.register.call_args[0][0]
            with patch.object(reg, "drain_in_flight") as mock_drain, \
                 patch.object(reg, "shutdown_all") as mock_shutdown:
                callback()
                mock_drain.assert_called_once_with(timeout=5)
                mock_shutdown.assert_called_once()

    def test_reset_clears_flag(self):
        """_reset_shutdown_state resets the flag for test isolation."""
        with patch("src.utils.shutdown.sys.exit"):
            with patch("src.utils.shutdown.fire_hooks"):
                with patch("src.utils.shutdown.run_all_cleanups", return_value=MagicMock()):
                    try:
                        graceful_shutdown(reason="test")
                    except SystemExit:
                        pass
        self.assertTrue(is_shutting_down())
        _reset_shutdown_state()
        self.assertFalse(is_shutting_down())

    def test_registry_no_module_level_atexit(self):
        """delivery_registry must NOT register atexit at module import time."""
        import inspect

        import src.card.delivery.registry as registry_module
        source = inspect.getsource(registry_module)
        # The old pattern was: atexit.register(delivery_registry.shutdown_all) at module level
        # Now it should only exist inside the install_atexit() method
        lines = source.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if "atexit.register(delivery_registry.shutdown_all)" in stripped:
                self.fail(
                    "Found module-level atexit.register(delivery_registry.shutdown_all). "
                    "Use delivery_registry.install_atexit() explicitly during bootstrap instead."
                )


if __name__ == "__main__":
    unittest.main()
