"""Unit tests for _call_with_timeout using ThreadPoolExecutor.

Verifies:
- future.result raises TimeoutError after _IO_CALL_TIMEOUT_S seconds
- Worker thread is not permanently blocked (pool has capacity for new tasks)
- Normal calls complete successfully
- Exceptions from fn are properly propagated
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.slock_engine.escalation_manager import EscalationManager
from src.slock_engine.models import (
    EscalationRequest,
)


def _make_manager(
    *,
    escalation_timeout_s: int = 30 * 60,
    update_card_fn=None,
    send_text_fn=None,
) -> tuple[EscalationManager, dict]:
    """Create a minimal EscalationManager with mock callbacks."""
    from src.slock_engine.task_router import TaskRouter

    lock = threading.RLock()
    escalations: list[EscalationRequest] = []
    retry_counts: dict[str, int] = {}
    mocks = {
        "dirty_setter": MagicMock(),
        "transition_agent": MagicMock(),
        "flush_if_dirty": MagicMock(),
        "execute_task_fn": MagicMock(return_value=None),
        "rollback_task_fn": MagicMock(),
        "force_complete_task_fn": MagicMock(),
    }

    router = TaskRouter()

    mgr = EscalationManager(
        lock=lock,
        escalations=escalations,
        retry_counts=retry_counts,
        channel_getter=lambda: None,
        chat_id_getter=lambda: "test_chat_id",
        task_list_getter=lambda: [],
        dirty_setter=mocks["dirty_setter"],
        router=router,
        transition_agent=mocks["transition_agent"],
        flush_if_dirty=mocks["flush_if_dirty"],
        execute_task_fn=mocks["execute_task_fn"],
        rollback_task_fn=mocks["rollback_task_fn"],
        force_complete_task_fn=mocks["force_complete_task_fn"],
        update_card_fn=update_card_fn,
        send_text_fn=send_text_fn,
        escalation_timeout_s=escalation_timeout_s,
    )
    return mgr, mocks


class TestFutureResultTimeout:
    """AC-5: future.result raises TimeoutError after configured seconds."""

    def test_timeout_raised_for_slow_call(self):
        """A function that sleeps 15s triggers TimeoutError at 2s."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 2  # Override for fast testing

        def slow_fn():
            time.sleep(15)

        start = time.perf_counter()
        with pytest.raises(TimeoutError):
            mgr._call_with_timeout(slow_fn, label="slow_test")
        elapsed = time.perf_counter() - start

        # Should timeout around 2s (not 15s)
        assert 1.5 < elapsed < 3.5, f"Timeout took {elapsed:.2f}s, expected ~2s"

        mgr.shutdown_timers()

    def test_normal_call_succeeds(self):
        """A fast function completes normally without timeout."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 5

        results = []

        def fast_fn(x, y):
            results.append(x + y)

        mgr._call_with_timeout(fast_fn, 3, 4, label="fast_test")
        assert results == [7]

        mgr.shutdown_timers()

    def test_exception_propagated(self):
        """Exceptions from fn are raised to the caller."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 5

        def failing_fn():
            raise ValueError("something went wrong")

        with pytest.raises(ValueError, match="something went wrong"):
            mgr._call_with_timeout(failing_fn, label="fail_test")

        mgr.shutdown_timers()


class TestWorkerAvailableAfterTimeout:
    """After timeout, the pool still has capacity for new tasks."""

    def test_new_task_runs_after_timeout(self):
        """After a timeout, the next call still succeeds (pool has 4 workers)."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 1  # Short timeout

        def slow_fn():
            time.sleep(10)

        # First call times out
        with pytest.raises(TimeoutError):
            mgr._call_with_timeout(slow_fn, label="slow")

        # Second call should work fine (pool has 4 workers, only 1 is stuck)
        results = []

        def fast_fn():
            results.append("done")

        mgr._call_with_timeout(fast_fn, label="fast_after_timeout")
        assert results == ["done"]

        mgr.shutdown_timers()

    def test_multiple_timeouts_dont_exhaust_pool(self):
        """3 consecutive timeouts don't prevent a 4th call from succeeding."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 1

        def slow_fn():
            time.sleep(10)

        # Timeout 3 times (consumes 3 of 4 workers)
        for _ in range(3):
            with pytest.raises(TimeoutError):
                mgr._call_with_timeout(slow_fn, label="slow")

        # 4th worker should still be available
        results = []

        def fast_fn():
            results.append("ok")

        mgr._call_with_timeout(fast_fn, label="final")
        assert results == ["ok"]

        mgr.shutdown_timers()
