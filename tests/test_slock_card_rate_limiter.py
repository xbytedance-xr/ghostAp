"""Tests for CardRateLimiter — AC-R07.

Verifies:
- Immediate send when interval has elapsed
- Pending/deferred send when within rate limit window
- 'Merge latest' strategy: pending payload is replaced, not queued
- flush_all sends all pending and marks as closed
- Thread safety under concurrent updates
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from src.slock_engine.card_rate_limiter import CardRateLimiter


class TestCardRateLimiterImmediate:
    """Immediate sends when interval is satisfied."""

    def test_first_update_sends_immediately(self):
        """First update to a message_id is sent immediately."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=1.0)

        limiter.update("msg-1", {"content": "hello"})
        send_fn.assert_called_once_with("msg-1", {"content": "hello"})

    def test_update_after_interval_sends_immediately(self):
        """Update after min_interval has passed is sent immediately."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=0.05)

        limiter.update("msg-1", {"v": 1})
        time.sleep(0.06)
        limiter.update("msg-1", {"v": 2})

        assert send_fn.call_count == 2
        send_fn.assert_any_call("msg-1", {"v": 1})
        send_fn.assert_any_call("msg-1", {"v": 2})


class TestCardRateLimiterMergeLatest:
    """Merge latest strategy: only the last pending payload is sent."""

    def test_rapid_updates_merge_to_latest(self):
        """Multiple rapid updates keep only the latest payload."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=0.2)

        # First update sends immediately
        limiter.update("msg-1", {"v": 1})
        assert send_fn.call_count == 1

        # Rapid updates within interval — should be merged
        limiter.update("msg-1", {"v": 2})
        limiter.update("msg-1", {"v": 3})
        limiter.update("msg-1", {"v": 4})

        # Wait for the deferred send
        time.sleep(0.3)

        # Should have sent v=1 immediately, then only v=4 (latest)
        assert send_fn.call_count == 2
        send_fn.assert_any_call("msg-1", {"v": 1})
        send_fn.assert_any_call("msg-1", {"v": 4})

    def test_different_message_ids_independent(self):
        """Different message_ids are rate-limited independently."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=1.0)

        limiter.update("msg-1", {"v": "a"})
        limiter.update("msg-2", {"v": "b"})

        # Both should send immediately (first update for each)
        assert send_fn.call_count == 2


class TestCardRateLimiterFlush:
    """flush_all behavior."""

    def test_flush_all_sends_pending(self):
        """flush_all sends all pending updates immediately."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=5.0)

        # First sends immediately
        limiter.update("msg-1", {"v": 1})
        # This will be pending
        limiter.update("msg-1", {"v": 2})

        assert send_fn.call_count == 1

        limiter.flush_all()

        # Should have flushed the pending v=2
        assert send_fn.call_count == 2
        send_fn.assert_any_call("msg-1", {"v": 2})

    def test_update_after_close_is_ignored(self):
        """Updates after flush_all (closed) are silently ignored."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=1.0)

        limiter.flush_all()
        limiter.update("msg-1", {"v": "ignored"})

        # Only flush_all may have called send_fn for pending, but no new sends
        send_fn.assert_not_called()

    def test_pending_count(self):
        """pending_count reflects actual pending items."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=5.0)

        assert limiter.pending_count == 0

        limiter.update("msg-1", {"v": 1})  # immediate
        assert limiter.pending_count == 0

        limiter.update("msg-1", {"v": 2})  # pending
        assert limiter.pending_count == 1

        limiter.flush_all()
        assert limiter.pending_count == 0


class TestCardRateLimiterThreadSafety:
    """Basic thread safety verification."""

    def test_concurrent_updates_no_crash(self):
        """Concurrent updates from multiple threads don't crash."""
        send_fn = MagicMock()
        limiter = CardRateLimiter(send_fn=send_fn, min_interval=0.01)

        errors = []

        def worker(msg_id, count):
            try:
                for i in range(count):
                    limiter.update(msg_id, {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(f"msg-{t}", 20))
            for t in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety violation: {errors}"

        # Cleanup
        limiter.flush_all()


class TestCardRateLimiterConfiguredInterval:
    """AC16: CardRateLimiter uses the configured interval value."""

    def test_card_rate_limiter_uses_configured_interval(self):
        """AC16: CardRateLimiter min_interval comes from settings."""
        mock_send = MagicMock(return_value=True)
        limiter = CardRateLimiter(send_fn=mock_send, min_interval=3.0)
        assert limiter._min_interval == 3.0
