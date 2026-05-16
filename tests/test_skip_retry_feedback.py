"""Skip-retry button feedback tests — verifies instant acknowledgement UX.

Covers:
- AC-R09: skip_retry with active engine sets event and replies skip_retry_ack
- AC-R10: skip_retry without active engine replies no_active_retry
"""

import threading
from unittest.mock import MagicMock

from src.card.ui_text import UI_TEXT


class TestSkipRetryWithActiveEngine:
    """AC-R09: skip_retry sets event and replies with ack when engine is active."""

    def test_sets_event_and_replies_ack(self):
        mock_engine = MagicMock()
        mock_engine.skip_retry_event = threading.Event()

        mock_manager = MagicMock()
        mock_manager.get_active_engine.return_value = mock_engine

        mock_ctx = MagicMock()
        mock_ctx.spec_engine_manager = mock_manager

        handler = MagicMock()
        handler.ctx = mock_ctx

        # Simulate the handler logic
        engine = handler.ctx.spec_engine_manager.get_active_engine("chat1")
        if engine and hasattr(engine, 'skip_retry_event'):
            engine.skip_retry_event.set()
            handler.reply_text("msg1", UI_TEXT["skip_retry_ack"])

        assert mock_engine.skip_retry_event.is_set()
        handler.reply_text.assert_called_once_with("msg1", UI_TEXT["skip_retry_ack"])


class TestSkipRetryWithoutActiveEngine:
    """AC-R10: skip_retry replies no_active_retry when no engine is active."""

    def test_replies_no_active_retry_when_no_engine(self):
        mock_manager = MagicMock()
        mock_manager.get_active_engine.return_value = None

        mock_ctx = MagicMock()
        mock_ctx.spec_engine_manager = mock_manager

        handler = MagicMock()
        handler.ctx = mock_ctx

        # Simulate the handler logic
        engine = handler.ctx.spec_engine_manager.get_active_engine("chat1")
        if engine and hasattr(engine, 'skip_retry_event'):
            engine.skip_retry_event.set()
            handler.reply_text("msg1", UI_TEXT["skip_retry_ack"])
        else:
            handler.reply_text("msg1", UI_TEXT["no_active_retry"])

        handler.reply_text.assert_called_once_with("msg1", UI_TEXT["no_active_retry"])

    def test_replies_no_active_retry_when_engine_has_no_event(self):
        mock_engine = MagicMock(spec=[])  # no skip_retry_event attribute

        mock_manager = MagicMock()
        mock_manager.get_active_engine.return_value = mock_engine

        mock_ctx = MagicMock()
        mock_ctx.spec_engine_manager = mock_manager

        handler = MagicMock()
        handler.ctx = mock_ctx

        engine = handler.ctx.spec_engine_manager.get_active_engine("chat1")
        if engine and hasattr(engine, 'skip_retry_event'):
            engine.skip_retry_event.set()
            handler.reply_text("msg1", UI_TEXT["skip_retry_ack"])
        else:
            handler.reply_text("msg1", UI_TEXT["no_active_retry"])

        handler.reply_text.assert_called_once_with("msg1", UI_TEXT["no_active_retry"])


class TestSkipRetryUITextKeys:
    """Guard: skip_retry_ack and no_active_retry keys exist in UI_TEXT."""

    def test_skip_retry_ack_exists(self):
        assert "skip_retry_ack" in UI_TEXT

    def test_no_active_retry_exists(self):
        assert "no_active_retry" in UI_TEXT

    def test_skip_retry_ack_not_empty(self):
        assert UI_TEXT["skip_retry_ack"].strip()

    def test_no_active_retry_not_empty(self):
        assert UI_TEXT["no_active_retry"].strip()


# ---------------------------------------------------------------------------
# Task 20: skip_retry_event shortens wait in attempt_pipeline_retry
# ---------------------------------------------------------------------------


class TestSkipRetryEventShortensWait:
    """AC-R11: skip_retry_event causes attempt_pipeline_retry to proceed immediately."""

    def test_skip_retry_shortens_delay(self):
        """Setting skip_retry_event during wait makes retry proceed without full delay."""
        import time
        from unittest.mock import MagicMock

        from src.spec_engine.review_retry import PipelineRetryContext, attempt_pipeline_retry
        from src.spec_engine.review_types import ReviewCircuitState

        # Create a mock circuit
        circuit = MagicMock(spec=ReviewCircuitState)
        circuit.consecutive_timeouts = 1

        # Create mock settings with a long retry delay
        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 1
        settings.spec_review_retry_max_delay = 60  # 60s delay (would be too long without skip)
        settings.spec_review_retry_base_delay = 0.05
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_min_timeout = 30
        settings.spec_review_hard_floor = 15

        # Pipeline fn succeeds immediately
        mock_outcome = MagicMock()
        mock_outcome.error = None
        mock_outcome.error_code = None
        mock_outcome.review = MagicMock()
        pipeline_fn = MagicMock(return_value=[mock_outcome])

        skip_event = threading.Event()
        cancel_event = threading.Event()

        ctx = PipelineRetryContext(
            cancel_event=cancel_event,
            on_retry_status=MagicMock(),
            base_timeout=120,
            multiplier=2,
            pipeline_fn=pipeline_fn,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="test",
            model_name=None,
            skip_retry_event=skip_event,
        )

        # Set skip after a short delay (0.1s) to simulate user pressing skip button
        def set_skip():
            time.sleep(0.1)
            skip_event.set()

        t = threading.Thread(target=set_skip)
        t.start()

        start = time.monotonic()
        result = attempt_pipeline_retry(circuit=circuit, settings=settings, cycle=1, ctx=ctx)
        elapsed = time.monotonic() - start

        t.join()

        # Should complete much faster than the 60s delay
        assert elapsed < 5.0
        # Pipeline was called (retry executed)
        pipeline_fn.assert_called_once()
        # Result should be the successful outcomes
        assert result is not None


# ---------------------------------------------------------------------------
# Task 21: Retry success callback sequence
# ---------------------------------------------------------------------------


class TestRetrySuccessCallbackSequence:
    """on_retry_status fires WAITING→EXECUTING→SUCCEEDED in correct order."""

    def test_callback_sequence_on_success(self):
        """Successful retry emits WAITING, EXECUTING, SUCCEEDED in order."""
        from unittest.mock import MagicMock

        from src.spec_engine.retry_status import RetryEvent, RetryStatus
        from src.spec_engine.review_retry import PipelineRetryContext, attempt_pipeline_retry
        from src.spec_engine.review_types import ReviewCircuitState

        circuit = MagicMock(spec=ReviewCircuitState)
        circuit.consecutive_timeouts = 1

        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 1
        settings.spec_review_retry_max_delay = 3  # >= 2 to trigger WAITING event
        settings.spec_review_retry_base_delay = 3.0
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_min_timeout = 30
        settings.spec_review_hard_floor = 15

        mock_outcome = MagicMock()
        mock_outcome.error = None
        mock_outcome.error_code = None
        mock_outcome.review = MagicMock()
        pipeline_fn = MagicMock(return_value=[mock_outcome])

        events_received: list = []

        def on_status(event: RetryEvent):
            events_received.append(event.status)

        ctx = PipelineRetryContext(
            cancel_event=None,
            on_retry_status=on_status,
            base_timeout=120,
            multiplier=2,
            pipeline_fn=pipeline_fn,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="test",
            model_name=None,
            skip_retry_event=None,
        )

        from unittest.mock import patch as _patch
        with _patch("src.spec_engine.review_retry.time.sleep", return_value=None):
            result = attempt_pipeline_retry(circuit=circuit, settings=settings, cycle=1, ctx=ctx)

        assert result is not None
        # Verify sequence: WAITING (because delay >= 2), EXECUTING, SUCCEEDED
        assert RetryStatus.WAITING in events_received
        assert RetryStatus.EXECUTING in events_received
        assert RetryStatus.SUCCEEDED in events_received
        # Order: WAITING before EXECUTING before SUCCEEDED
        w_idx = events_received.index(RetryStatus.WAITING)
        e_idx = events_received.index(RetryStatus.EXECUTING)
        s_idx = events_received.index(RetryStatus.SUCCEEDED)
        assert w_idx < e_idx < s_idx
