"""Tests for ContextPersistenceHook injection and failure handling.

Validates:
- Persistence is called only on completed/completed_empty reasons
- Non-success reasons skip persistence
- Exception in update_fn is caught and optionally notifies user
- Exception in notify_callback is swallowed
- Missing chat_id skips notification
"""

import unittest
from unittest.mock import MagicMock, patch

from src.card.hooks import ContextPersistenceHook


class TestContextPersistenceHookInjection(unittest.TestCase):
    """ContextPersistenceHook behavior across terminal reasons."""

    def _make_hook(self, update_fn=None, notify_callback=None, chat_id=None, engine_type=None):
        if update_fn is None:
            update_fn = MagicMock()
        return ContextPersistenceHook(
            update_fn=update_fn,
            notify_callback=notify_callback,
            chat_id=chat_id,
            engine_type=engine_type,
        ), update_fn

    def test_completed_calls_update_fn(self):
        hook, update_fn = self._make_hook()
        state = MagicMock()
        hook.on_terminal(state, "completed")
        update_fn.assert_called_once_with(state)

    def test_completed_empty_calls_update_fn(self):
        hook, update_fn = self._make_hook()
        state = MagicMock()
        hook.on_terminal(state, "completed_empty")
        update_fn.assert_called_once_with(state)

    def test_failed_skips_update(self):
        hook, update_fn = self._make_hook()
        state = MagicMock()
        hook.on_terminal(state, "failed")
        update_fn.assert_not_called()

    def test_cancelled_skips_update(self):
        hook, update_fn = self._make_hook()
        state = MagicMock()
        hook.on_terminal(state, "cancelled")
        update_fn.assert_not_called()

    def test_ttl_expired_skips_update(self):
        hook, update_fn = self._make_hook()
        state = MagicMock()
        hook.on_terminal(state, "ttl_expired")
        update_fn.assert_not_called()

    def test_archived_skips_update(self):
        hook, update_fn = self._make_hook()
        state = MagicMock()
        hook.on_terminal(state, "archived")
        update_fn.assert_not_called()

    def test_update_fn_exception_caught(self):
        """Exception in update_fn should not propagate."""
        update_fn = MagicMock(side_effect=RuntimeError("db error"))
        hook, _ = self._make_hook(update_fn=update_fn)
        state = MagicMock()
        # Should not raise
        hook.on_terminal(state, "completed")

    def test_update_fn_exception_triggers_notify(self):
        """When update_fn fails, notify_callback is called with chat_id."""
        update_fn = MagicMock(side_effect=RuntimeError("db error"))
        notify_cb = MagicMock()
        hook, _ = self._make_hook(
            update_fn=update_fn,
            notify_callback=notify_cb,
            chat_id="chat_999",
            engine_type="spec",
        )
        state = MagicMock()
        hook.on_terminal(state, "completed")
        notify_cb.assert_called_once()
        call_args = notify_cb.call_args[0]
        self.assertEqual(call_args[0], "chat_999")
        # Message should be a non-empty string
        self.assertIsInstance(call_args[1], str)
        self.assertTrue(len(call_args[1]) > 0)

    def test_update_fn_exception_no_notify_when_chat_id_missing(self):
        """Without chat_id, notify_callback is skipped."""
        update_fn = MagicMock(side_effect=RuntimeError("db error"))
        notify_cb = MagicMock()
        hook, _ = self._make_hook(
            update_fn=update_fn,
            notify_callback=notify_cb,
            chat_id=None,
        )
        state = MagicMock()
        hook.on_terminal(state, "completed")
        notify_cb.assert_not_called()

    def test_update_fn_exception_no_notify_when_callback_missing(self):
        """Without notify_callback, no notification attempted."""
        update_fn = MagicMock(side_effect=RuntimeError("db error"))
        hook, _ = self._make_hook(
            update_fn=update_fn,
            notify_callback=None,
            chat_id="chat_123",
        )
        state = MagicMock()
        # Should not raise
        hook.on_terminal(state, "completed")

    def test_notify_callback_exception_swallowed(self):
        """Exception in notify_callback itself should not propagate."""
        update_fn = MagicMock(side_effect=RuntimeError("db error"))
        notify_cb = MagicMock(side_effect=RuntimeError("notify failed"))
        hook, _ = self._make_hook(
            update_fn=update_fn,
            notify_callback=notify_cb,
            chat_id="chat_123",
            engine_type="deep",
        )
        state = MagicMock()
        # Should not raise
        hook.on_terminal(state, "completed")

    def test_on_dispatched_is_noop(self):
        """on_dispatched should do nothing."""
        hook, update_fn = self._make_hook()
        event = MagicMock()
        state = MagicMock()
        hook.on_dispatched(event, state)
        update_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
