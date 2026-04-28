"""AC-22: Verify skip_retry_event.set() actually shortens attempt_pipeline_retry wait time.

Creates a PipelineRetryContext with a long delay (60s), sets skip_retry_event
from a background thread after 0.1s, and asserts pipeline_fn was called
within <5s total elapsed time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from src.spec_engine.retry_status import RetryEvent, RetryStatus
from src.spec_engine.review_retry import PipelineRetryContext, attempt_pipeline_retry
from src.spec_engine.review_types import ReviewCircuitState


@dataclass
class _FakeOutcome:
    error: str = ""
    error_code: object = None


class TestSkipRetryEventShortensWait:
    """skip_retry_event.set() must cause attempt_pipeline_retry to skip the delay."""

    def test_skip_event_shortens_wait(self):
        """Configure 60s delay, set skip_retry_event after 0.1s,
        assert pipeline_fn is called and total time < 5s."""
        skip_event = threading.Event()
        cancel_event = threading.Event()
        called = threading.Event()

        # pipeline_fn succeeds immediately (returns no-error outcomes)
        def fake_pipeline(artifacts, budget, *, agent_type="", model_name=None):
            called.set()
            return [_FakeOutcome(error="")]

        # Mock settings: long delay to prove skip works
        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 1
        settings.spec_review_retry_max_delay = 60  # Would normally wait ~60s
        settings.spec_review_retry_base_delay = 5.0
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_min_timeout = 20
        settings.spec_review_hard_floor = 10
        settings.spec_review_timeout = 180

        circuit = ReviewCircuitState(consecutive_timeouts=0)

        ctx = PipelineRetryContext(
            cancel_event=cancel_event,
            on_retry_status=None,
            base_timeout=60,
            multiplier=2,
            pipeline_fn=fake_pipeline,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="test",
            model_name=None,
            skip_retry_event=skip_event,
        )

        # Background thread: set skip event after 0.1s
        def _trigger_skip():
            time.sleep(0.1)
            skip_event.set()

        t = threading.Thread(target=_trigger_skip, daemon=True)
        t.start()

        start = time.monotonic()
        result = attempt_pipeline_retry(
            circuit=circuit, settings=settings, cycle=1, ctx=ctx,
        )
        elapsed = time.monotonic() - start

        t.join(timeout=2)

        assert called.is_set(), "pipeline_fn was never called"
        assert result is not None, "attempt_pipeline_retry returned None (expected success)"
        assert elapsed < 5.0, f"Elapsed {elapsed:.1f}s — skip_retry_event did not short-circuit the delay"

    def test_skip_event_without_cancel_event(self):
        """skip_retry_event works even when cancel_event is None."""
        skip_event = threading.Event()
        called = threading.Event()

        def fake_pipeline(artifacts, budget, *, agent_type="", model_name=None):
            called.set()
            return [_FakeOutcome(error="")]

        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 1
        settings.spec_review_retry_max_delay = 60
        settings.spec_review_retry_base_delay = 5.0
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_min_timeout = 20
        settings.spec_review_hard_floor = 10
        settings.spec_review_timeout = 180

        circuit = ReviewCircuitState(consecutive_timeouts=0)

        ctx = PipelineRetryContext(
            cancel_event=None,  # No cancel event
            on_retry_status=None,
            base_timeout=60,
            multiplier=2,
            pipeline_fn=fake_pipeline,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="test",
            model_name=None,
            skip_retry_event=skip_event,
        )

        def _trigger_skip():
            time.sleep(0.1)
            skip_event.set()

        t = threading.Thread(target=_trigger_skip, daemon=True)
        t.start()

        start = time.monotonic()
        result = attempt_pipeline_retry(
            circuit=circuit, settings=settings, cycle=1, ctx=ctx,
        )
        elapsed = time.monotonic() - start

        t.join(timeout=2)

        assert called.is_set(), "pipeline_fn was never called"
        assert elapsed < 5.0, f"Elapsed {elapsed:.1f}s — skip without cancel_event failed"

    def test_retry_status_callbacks_emitted(self):
        """Verify WAITING and EXECUTING status callbacks are emitted even with skip."""
        skip_event = threading.Event()
        statuses: list[RetryStatus] = []

        def fake_pipeline(artifacts, budget, *, agent_type="", model_name=None):
            return [_FakeOutcome(error="")]

        def on_status(event: RetryEvent):
            statuses.append(event.status)

        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 1
        settings.spec_review_retry_max_delay = 60
        settings.spec_review_retry_base_delay = 5.0
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_min_timeout = 20
        settings.spec_review_hard_floor = 10
        settings.spec_review_timeout = 180

        circuit = ReviewCircuitState(consecutive_timeouts=0)

        ctx = PipelineRetryContext(
            cancel_event=threading.Event(),
            on_retry_status=on_status,
            base_timeout=60,
            multiplier=2,
            pipeline_fn=fake_pipeline,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="test",
            model_name=None,
            skip_retry_event=skip_event,
        )

        def _trigger_skip():
            time.sleep(0.1)
            skip_event.set()

        t = threading.Thread(target=_trigger_skip, daemon=True)
        t.start()

        attempt_pipeline_retry(
            circuit=circuit, settings=settings, cycle=1, ctx=ctx,
        )
        t.join(timeout=2)

        assert RetryStatus.WAITING in statuses, f"WAITING not in {statuses}"
        assert RetryStatus.EXECUTING in statuses, f"EXECUTING not in {statuses}"
        assert RetryStatus.SUCCEEDED in statuses, f"SUCCEEDED not in {statuses}"
