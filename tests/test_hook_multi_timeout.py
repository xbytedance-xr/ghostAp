"""Tests for HookFirer with multiple hooks and timeout scenarios.

Validates:
- Multiple hooks fire concurrently
- Slow hooks time out without blocking other hooks
- fire_terminal exactly-once semantics
- Timeout is wall-clock for all hooks, not per-hook
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from src.card.hooks import HookFirer, HOOK_TIMEOUT_SECONDS


class SlowHook:
    """A hook that sleeps for a configurable duration."""

    def __init__(self, delay: float, name: str = "slow"):
        self.delay = delay
        self.name = name
        self.terminal_called = threading.Event()
        self.dispatched_called = threading.Event()

    def on_dispatched(self, event, state):
        self.dispatched_called.set()
        time.sleep(self.delay)

    def on_terminal(self, state, reason):
        self.terminal_called.set()
        time.sleep(self.delay)


class FastHook:
    """A hook that completes immediately and records calls."""

    def __init__(self):
        self.terminal_reasons = []
        self.dispatched_count = 0

    def on_dispatched(self, event, state):
        self.dispatched_count += 1

    def on_terminal(self, state, reason):
        self.terminal_reasons.append(reason)


class TestHookMultiTimeout(unittest.TestCase):
    """HookFirer behavior with multiple hooks and timeouts."""

    def test_fast_hooks_all_complete(self):
        """Multiple fast hooks all fire successfully."""
        h1 = FastHook()
        h2 = FastHook()
        firer = HookFirer(hooks=(h1, h2), session_id="test_sess")
        state = MagicMock()
        firer.fire_terminal(state, "completed")
        self.assertEqual(h1.terminal_reasons, ["completed"])
        self.assertEqual(h2.terminal_reasons, ["completed"])

    def test_fire_terminal_exactly_once(self):
        """Calling fire_terminal twice only fires hooks once."""
        h = FastHook()
        firer = HookFirer(hooks=(h,), session_id="test_sess")
        state = MagicMock()
        firer.fire_terminal(state, "completed")
        firer.fire_terminal(state, "failed")
        # Only the first call should have executed
        self.assertEqual(len(h.terminal_reasons), 1)
        self.assertEqual(h.terminal_reasons[0], "completed")

    def test_slow_hook_does_not_block_fast_hook(self):
        """A slow hook timing out doesn't prevent fast hooks from completing."""
        slow = SlowHook(delay=HOOK_TIMEOUT_SECONDS + 2, name="blocker")
        fast = FastHook()
        firer = HookFirer(hooks=(slow, fast), session_id="test_sess")
        state = MagicMock()

        start = time.monotonic()
        firer.fire_terminal(state, "completed")
        elapsed = time.monotonic() - start

        # Fast hook should have completed
        self.assertEqual(fast.terminal_reasons, ["completed"])
        # Total time should be close to HOOK_TIMEOUT_SECONDS, not much more
        self.assertLess(elapsed, HOOK_TIMEOUT_SECONDS + 3)

    def test_fire_terminal_concurrent_calls_safe(self):
        """Calling fire_terminal from multiple threads is safe (exactly-once)."""
        h = FastHook()
        firer = HookFirer(hooks=(h,), session_id="test_sess")
        state = MagicMock()
        barrier = threading.Barrier(3)

        def fire():
            barrier.wait()
            firer.fire_terminal(state, "completed")

        threads = [threading.Thread(target=fire) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one call should have gone through
        self.assertEqual(len(h.terminal_reasons), 1)

    def test_dispatched_hooks_fire_for_all(self):
        """fire_dispatched fires on_dispatched for all hooks."""
        h1 = FastHook()
        h2 = FastHook()
        firer = HookFirer(hooks=(h1, h2), session_id="test_sess")
        event = MagicMock()
        state = MagicMock()
        firer.fire_dispatched(event, state)
        # fire_dispatched is fire-and-forget; give executor time to run hooks
        import time
        time.sleep(0.15)
        self.assertEqual(h1.dispatched_count, 1)
        self.assertEqual(h2.dispatched_count, 1)

    def test_empty_hooks_tuple_noop(self):
        """HookFirer with no hooks completes without error."""
        firer = HookFirer(hooks=(), session_id="test_sess")
        state = MagicMock()
        event = MagicMock()
        # Should not raise
        firer.fire_terminal(state, "completed")
        firer.fire_dispatched(event, state)


if __name__ == "__main__":
    unittest.main()
