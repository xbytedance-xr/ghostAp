"""Tests for StreamThrottle: check_throttle and check_plan_throttle boundary cases."""
import time
from unittest.mock import patch

from src.card.render.throttle import StreamThrottle


class TestCheckThrottle:
    """Tests for check_throttle() behavior."""

    def test_force_always_returns_true(self):
        """force=True bypasses all throttle logic."""
        t = StreamThrottle(min_interval=10.0, min_chars=100)
        t.last_stream_ts = time.monotonic()
        t.last_stream_text_len = 50
        assert t.check_throttle(51, force=True) is True

    def test_zero_interval_never_throttles_by_time(self):
        """With zero interval, time condition is always False so AND short-circuits.

        The throttle logic is: throttle if (time < min_interval AND chars < min_chars).
        With min_interval=0, (now - last_ts) < 0 is always False, so the AND is
        always False → never throttled regardless of chars.
        """
        t = StreamThrottle(min_interval=0.0, min_chars=10)
        t.last_stream_ts = time.monotonic()
        t.last_stream_text_len = 100
        # Even with insufficient new chars, zero interval means no throttle
        assert t.check_throttle(105, force=False) is True
        # Enough new chars also passes
        assert t.check_throttle(111, force=False) is True

    def test_zero_interval_zero_chars_always_passes(self):
        """With zero interval and zero min_chars, always proceeds."""
        t = StreamThrottle(min_interval=0.0, min_chars=0)
        t.last_stream_ts = time.monotonic()
        t.last_stream_text_len = 100
        # Any text_len should pass since both conditions fail to throttle
        assert t.check_throttle(100, force=False) is True

    def test_text_shrink_still_throttled_by_time(self):
        """When text shrinks (edit scenario), time interval still gates."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        t.last_stream_ts = time.monotonic()
        t.last_stream_text_len = 200
        # Text shrank to 150 (negative delta, < min_chars)
        # But time hasn't elapsed either → throttled
        assert t.check_throttle(150, force=False) is False

    def test_text_shrink_passes_after_interval(self):
        """When text shrinks but enough time passed, update proceeds."""
        t = StreamThrottle(min_interval=0.5, min_chars=10)
        t.last_stream_ts = time.monotonic() - 1.0  # 1s ago
        t.last_stream_text_len = 200
        # Text shrank, but time elapsed → proceeds
        assert t.check_throttle(150, force=False) is True

    def test_exact_threshold_chars(self):
        """Exactly min_chars of new content should pass."""
        t = StreamThrottle(min_interval=10.0, min_chars=10)
        t.last_stream_ts = time.monotonic()
        t.last_stream_text_len = 100
        # Exactly at threshold (100 + 10 = 110)
        assert t.check_throttle(110, force=False) is True
        # One below threshold
        assert t.check_throttle(109, force=False) is False

    def test_exact_threshold_time(self):
        """Exactly at min_interval boundary."""
        t = StreamThrottle(min_interval=0.5, min_chars=100)
        t.last_stream_ts = time.monotonic() - 0.5  # exactly at boundary
        t.last_stream_text_len = 100
        # Time elapsed >= min_interval → passes regardless of chars
        assert t.check_throttle(101, force=False) is True

    def test_custom_min_interval_override(self):
        """min_interval parameter overrides instance default."""
        t = StreamThrottle(min_interval=10.0, min_chars=100)
        t.last_stream_ts = time.monotonic() - 0.3
        t.last_stream_text_len = 100
        # Default 10s interval → would throttle
        assert t.check_throttle(101, min_interval=0.2) is True

    def test_custom_min_new_chars_override(self):
        """min_new_chars parameter overrides instance default."""
        t = StreamThrottle(min_interval=10.0, min_chars=100)
        t.last_stream_ts = time.monotonic()
        t.last_stream_text_len = 100
        # Default 100 min_chars → would throttle at 105
        # But override to 5 → passes
        assert t.check_throttle(105, min_new_chars=5) is True


class TestUpdateStreamState:
    """Tests for update_stream_state."""

    def test_updates_timestamp_and_length(self):
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        before = time.monotonic()
        t.update_stream_state(250)
        after = time.monotonic()
        assert t.last_stream_text_len == 250
        assert before <= t.last_stream_ts <= after


class TestCheckPlanThrottle:
    """Tests for check_plan_throttle() behavior."""

    def test_force_always_returns_true(self):
        """force=True bypasses plan throttle."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        t.last_plan_content = "same content"
        t.last_plan_ts = time.monotonic()
        assert t.check_plan_throttle("same content", force=True) is True

    def test_same_content_within_interval_returns_false(self):
        """Same plan content within interval should be throttled."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        t.last_plan_content = "plan A"
        t.last_plan_ts = time.monotonic()
        assert t.check_plan_throttle("plan A", min_interval=1.5) is False

    def test_same_content_after_interval_returns_true(self):
        """Same plan content after interval elapsed should pass (re-render)."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        t.last_plan_content = "plan A"
        t.last_plan_ts = time.monotonic() - 2.0  # 2s ago
        assert t.check_plan_throttle("plan A", min_interval=1.5) is True

    def test_different_content_always_passes(self):
        """Different plan content always passes regardless of time."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        t.last_plan_content = "plan A"
        t.last_plan_ts = time.monotonic()
        assert t.check_plan_throttle("plan B") is True

    def test_empty_content_returns_false(self):
        """Empty plan content should not trigger update."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        assert t.check_plan_throttle("") is False

    def test_first_plan_with_content_returns_true(self):
        """First plan update (last_plan_content='') with content should pass."""
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        assert t.check_plan_throttle("new plan") is True


class TestUpdatePlanState:
    """Tests for update_plan_state."""

    def test_updates_timestamp_and_content(self):
        t = StreamThrottle(min_interval=1.0, min_chars=10)
        before = time.monotonic()
        t.update_plan_state("new plan content")
        after = time.monotonic()
        assert t.last_plan_content == "new plan content"
        assert before <= t.last_plan_ts <= after
