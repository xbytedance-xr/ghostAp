"""Tests for delivery throttle scheduling."""

import threading
import time

import pytest

from src.card.delivery.throttle import DeliveryThrottle, DELIVERY_INTERVAL_MS
from src.card.render.renderer import RenderedCard


class TestDeliveryThrottle:
    """DeliveryThrottle tests."""

    def _make_rendered(self, sig: str = "sig1") -> list[RenderedCard]:
        return [RenderedCard(card_json={"test": True}, structure_signature=sig)]

    def test_immediate_flush(self):
        """immediate=True → callback invoked synchronously."""
        results = []
        def callback(sid, rendered):
            results.append((sid, rendered))

        throttle = DeliveryThrottle(flush_callback=callback)
        rendered = self._make_rendered()
        throttle.schedule("sess_1", rendered, immediate=True)

        assert len(results) == 1
        assert results[0][0] == "sess_1"

    def test_throttled_flush_occurs(self):
        """Non-immediate schedule → callback after delay."""
        results = []
        event = threading.Event()

        def callback(sid, rendered):
            results.append((sid, rendered))
            event.set()

        throttle = DeliveryThrottle(flush_callback=callback)
        rendered = self._make_rendered()
        throttle.schedule("sess_1", rendered)

        # Should not fire immediately
        assert len(results) == 0
        # Wait for timer
        event.wait(timeout=1.0)
        assert len(results) == 1

    def test_pending_cancelled_on_new(self):
        """New schedule cancels previous pending."""
        results = []
        event = threading.Event()

        def callback(sid, rendered):
            results.append((sid, rendered))
            event.set()

        throttle = DeliveryThrottle(flush_callback=callback)
        r1 = self._make_rendered("old")
        r2 = self._make_rendered("new")

        throttle.schedule("sess_1", r1)
        time.sleep(0.05)
        throttle.schedule("sess_1", r2)  # Cancels old

        event.wait(timeout=1.0)
        # Should only have the latest
        assert len(results) == 1
        assert results[0][1][0].structure_signature == "new"

    def test_flush_now(self):
        """flush_now immediately fires pending."""
        results = []

        def callback(sid, rendered):
            results.append((sid, rendered))

        throttle = DeliveryThrottle(flush_callback=callback)
        rendered = self._make_rendered()
        throttle.schedule("sess_1", rendered)
        assert len(results) == 0

        throttle.flush_now("sess_1")
        assert len(results) == 1

    def test_cancel(self):
        """cancel removes pending without firing."""
        results = []

        def callback(sid, rendered):
            results.append((sid, rendered))

        throttle = DeliveryThrottle(flush_callback=callback)
        rendered = self._make_rendered()
        throttle.schedule("sess_1", rendered)
        throttle.cancel("sess_1")

        time.sleep(0.4)  # Wait past any timer
        assert len(results) == 0

    def test_has_pending(self):
        throttle = DeliveryThrottle(flush_callback=lambda s, r: None)
        rendered = self._make_rendered()

        assert not throttle.has_pending("sess_1")
        throttle.schedule("sess_1", rendered)
        assert throttle.has_pending("sess_1")

    def test_multiple_sessions_independent(self):
        """Different sessions are throttled independently."""
        results = []
        def callback(sid, rendered):
            results.append(sid)

        throttle = DeliveryThrottle(flush_callback=callback)
        throttle.schedule("sess_1", self._make_rendered(), immediate=True)
        throttle.schedule("sess_2", self._make_rendered(), immediate=True)

        assert "sess_1" in results
        assert "sess_2" in results
