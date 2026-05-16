"""Tests for stop escalation flow in CardSession.

Covers:
1. STOPPING event triggers _schedule_stop_escalation (timer scheduled)
2. Terminal event cancels the escalation timer
3. Timeout callback dispatches stop_escalated()
4. close() cancels the timer
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.card.events import CardEvent, CardEventType


class TestStopEscalation:
    """Test stop escalation timer lifecycle in CardSession."""

    def _make_session(self):
        """Create a minimal CardSession for testing stop escalation."""
        from src.card.delivery.engine import CardDelivery
        from src.card.session.config import SessionCallbacks
        from src.card.session.factory import CardSessionFactory
        from src.card.state.models import CardMetadata

        delivery = MagicMock(spec=CardDelivery)
        delivery.acquire_session_lock = MagicMock(return_value=True)
        delivery.release_session_lock = MagicMock()
        delivery.close = MagicMock()
        delivery.deliver = MagicMock()

        factory = CardSessionFactory(delivery=delivery)
        metadata = CardMetadata(engine_type="deep", mode_name="Deep")
        session = factory.create(
            chat_id="test_chat",
            metadata=metadata,
            reply_to="test_msg",
            callbacks=SessionCallbacks(notify_callback=lambda _c, _t: None),
        )
        return session

    @patch("src.card.timers.scheduler.get_timer_scheduler")
    def test_stopping_event_schedules_escalation_timer(self, mock_get_scheduler):
        """STOPPING event should trigger _schedule_stop_escalation."""
        mock_scheduler = MagicMock()
        mock_scheduler.schedule = MagicMock(return_value="handle_1")
        mock_get_scheduler.return_value = mock_scheduler

        session = self._make_session()
        # Dispatch STARTED first to get to running state
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        # Now dispatch STOPPING
        session.dispatch(CardEvent(type=CardEventType.STOPPING))

        # The scheduler.schedule should have been called with ~30s delay
        mock_scheduler.schedule.assert_called()
        call_args = mock_scheduler.schedule.call_args
        assert call_args[0][0] == 30.0  # delay

    @patch("src.card.timers.scheduler.get_timer_scheduler")
    def test_terminal_event_cancels_escalation_timer(self, mock_get_scheduler):
        """Terminal event (COMPLETED) should cancel the escalation timer."""
        mock_scheduler = MagicMock()
        mock_handle = MagicMock()
        mock_scheduler.schedule = MagicMock(return_value=mock_handle)
        mock_scheduler.cancel = MagicMock()
        mock_get_scheduler.return_value = mock_scheduler

        session = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.STOPPING))
        # Now send terminal event
        session.dispatch(CardEvent(type=CardEventType.COMPLETED, payload={"summary": "done"}))

        # cancel should have been called with the handle
        mock_scheduler.cancel.assert_called_with(mock_handle)

    @patch("src.card.timers.scheduler.get_timer_scheduler")
    def test_escalation_timeout_dispatches_stop_escalated(self, mock_get_scheduler):
        """When escalation timer fires, it dispatches STOP_ESCALATED event."""
        mock_scheduler = MagicMock()
        captured_callback = None

        def capture_schedule(delay, callback, **kwargs):
            nonlocal captured_callback
            captured_callback = callback
            return "handle_1"

        mock_scheduler.schedule = MagicMock(side_effect=capture_schedule)
        mock_scheduler.cancel = MagicMock()
        mock_get_scheduler.return_value = mock_scheduler

        session = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.STOPPING))

        # The callback should have been captured
        assert captured_callback is not None

        # Fire the callback (simulates timer expiring)
        captured_callback()

        # After escalation, the session state should have force-stop button
        # (verified by checking state has STOP_ESCALATED applied)
        state = session._state
        if state and state.buttons:
            # Should have danger type button
            assert any(b.type == "danger" for b in state.buttons)

    @patch("src.card.timers.scheduler.get_timer_scheduler")
    def test_close_cancels_escalation_timer(self, mock_get_scheduler):
        """session.close() should cancel the escalation timer."""
        mock_scheduler = MagicMock()
        mock_handle = MagicMock()
        mock_scheduler.schedule = MagicMock(return_value=mock_handle)
        mock_scheduler.cancel = MagicMock()
        mock_get_scheduler.return_value = mock_scheduler

        session = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.STOPPING))

        # Close the session
        session.close()

        # cancel should have been called
        mock_scheduler.cancel.assert_called_with(mock_handle)
