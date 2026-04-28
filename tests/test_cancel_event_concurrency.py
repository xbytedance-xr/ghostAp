"""Concurrency tests for _reset_cancel_event vs stop()/signal_stop() races.

AC-R02: After stop() is called, cancel_event must remain set even if
reset_cancel_event is called concurrently from another thread.

AC-R17: cancel_event set before attempt_pipeline_retry → immediate return None.

Uses threading.Barrier for synchronization to avoid flaky tests.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.engine_base import EngineRunState
from src.spec_engine.review import ReviewOrchestrator


class TestResetCancelEventConcurrency:
    """AC-R02: _reset_cancel_event and stop()/signal_stop() concurrent race."""

    def test_stop_then_reset_preserves_set(self):
        """After signal_stop(), reset_cancel_event with is_running=False keeps event set."""
        orch = ReviewOrchestrator()

        # Signal stop first
        orch.signal_stop()
        assert orch.cancel_event.is_set()

        # Reset with is_running=False (STOPPING state) — should keep it set
        result = orch.reset_cancel_event(is_running=False)
        assert result is False
        assert orch.cancel_event.is_set()

    def test_concurrent_signal_stop_and_reset(self):
        """Concurrent stop and reset: cancel_event invariant holds.

        Invariant: after signal_stop() completes, if run_state is STOPPING,
        then cancel_event.is_set() must be True.
        """
        ITERATIONS = 100
        failures = []

        for i in range(ITERATIONS):
            orch = ReviewOrchestrator()
            lock = threading.RLock()
            barrier = threading.Barrier(2, timeout=5)
            is_running_box = [True]

            def stopper():
                barrier.wait()
                is_running_box[0] = False
                orch.signal_stop()

            def resetter():
                barrier.wait()
                orch.reset_cancel_event(is_running=is_running_box[0])

            t1 = threading.Thread(target=stopper)
            t2 = threading.Thread(target=resetter)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            # After both threads finish, signal_stop always sets the event
            # Even if resetter cleared it first, signal_stop re-sets it
            # The invariant is: signal_stop() guarantees event is_set() after return
            if not is_running_box[0]:
                if not orch.cancel_event.is_set():
                    failures.append(f"Iteration {i}: cancel_event not set after stop")

        assert not failures, f"Race condition detected:\n" + "\n".join(failures)

    def test_concurrent_reset_with_running_state(self):
        """When state remains RUNNING, reset_cancel_event clears the event."""
        orch = ReviewOrchestrator()

        # Start with event set
        orch.signal_stop()
        assert orch.cancel_event.is_set()

        # Reset with is_running=True — should clear
        result = orch.reset_cancel_event(is_running=True)
        assert result is True
        assert not orch.cancel_event.is_set()

    def test_rapid_stop_reset_interleave(self):
        """Rapid interleaving of signal_stop and reset_cancel_event.

        Final state: since last signal_stop wins, event should be set.
        """
        orch = ReviewOrchestrator()
        barrier = threading.Barrier(2, timeout=5)

        def stopper():
            barrier.wait()
            for _ in range(100):
                orch.signal_stop()

        def resetter():
            barrier.wait()
            for _ in range(100):
                orch.reset_cancel_event(is_running=False)

        t1 = threading.Thread(target=stopper)
        t2 = threading.Thread(target=resetter)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # After rapid stopper + STOPPING resetter, event must be set
        assert orch.cancel_event.is_set(), (
            "After rapid stop + STOPPING reset, cancel_event must be set"
        )


class TestCancelEventLoopTopBoundary:
    """AC-R17: cancel_event pre-set before attempt_pipeline_retry → immediate None."""

    def test_cancel_event_set_before_retry_returns_none(self):
        """When cancel_event is already set, attempt_pipeline_retry returns None immediately."""
        from src.spec_engine.review_retry import PipelineRetryContext, attempt_pipeline_retry
        from src.spec_engine.review_types import ReviewCircuitState

        cancel_event = threading.Event()
        cancel_event.set()  # Pre-set before calling retry

        pipeline_called = []

        def mock_pipeline(*args, **kwargs):
            pipeline_called.append(True)
            return []

        settings = SimpleNamespace(
            spec_review_retry_max_attempts=3,
            spec_review_retry_max_delay=5,
            spec_review_min_timeout=30,
            spec_review_hard_floor=20,
        )
        circuit = ReviewCircuitState(consecutive_timeouts=1)

        ctx = PipelineRetryContext(
            cancel_event=cancel_event,
            on_retry_status=None,
            base_timeout=120,
            multiplier=2,
            pipeline_fn=mock_pipeline,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="coco",
            model_name=None,
        )

        result = attempt_pipeline_retry(
            circuit=circuit,
            settings=settings,
            cycle=1,
            ctx=ctx,
        )

        assert result is None, "Should return None when cancel_event is already set"
        assert len(pipeline_called) == 0, "pipeline_fn should never be called"

    def test_cancel_event_set_during_wait_returns_none(self):
        """When cancel_event gets set during wait, retry exits after wait."""
        from src.spec_engine.review_retry import PipelineRetryContext, attempt_pipeline_retry
        from src.spec_engine.review_types import ReviewCircuitState

        cancel_event = threading.Event()

        # Set event after a very short delay
        def set_after_delay():
            time.sleep(0.05)
            cancel_event.set()

        threading.Thread(target=set_after_delay, daemon=True).start()

        settings = SimpleNamespace(
            spec_review_retry_max_attempts=3,
            spec_review_retry_max_delay=10,  # Long delay to ensure wait is interruptible
            spec_review_min_timeout=30,
            spec_review_hard_floor=20,
        )
        circuit = ReviewCircuitState(consecutive_timeouts=1)

        pipeline_called = []

        def mock_pipeline(*args, **kwargs):
            pipeline_called.append(True)
            return []

        ctx = PipelineRetryContext(
            cancel_event=cancel_event,
            on_retry_status=None,
            base_timeout=120,
            multiplier=2,
            pipeline_fn=mock_pipeline,
            budget_cls=MagicMock(),
            artifacts=MagicMock(),
            agent_type="coco",
            model_name=None,
        )

        result = attempt_pipeline_retry(
            circuit=circuit,
            settings=settings,
            cycle=1,
            ctx=ctx,
        )

        assert result is None, "Should return None when cancel_event is set during wait"
