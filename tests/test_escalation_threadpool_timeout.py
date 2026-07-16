"""Unit tests for _call_with_timeout using ThreadPoolExecutor.

Verifies:
- future.result raises TimeoutError after _IO_CALL_TIMEOUT_S seconds
- Worker thread is not permanently blocked (pool has capacity for new tasks)
- Normal calls complete successfully
- Exceptions from fn are properly propagated
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import MagicMock, patch

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
        """A function that sleeps 5s triggers TimeoutError at 0.5s."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 0.5  # Override for fast testing

        def slow_fn():
            time.sleep(5)

        start = time.perf_counter()
        with pytest.raises(TimeoutError):
            mgr._call_with_timeout(slow_fn, label="slow_test")
        elapsed = time.perf_counter() - start

        # Should timeout around 0.5s (not 5s)
        assert 0.3 < elapsed < 1.5, f"Timeout took {elapsed:.2f}s, expected ~0.5s"

        mgr.shutdown_timers()

    def test_normal_call_succeeds(self):
        """A fast function completes normally without timeout."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 2

        results = []

        def fast_fn(x, y):
            results.append(x + y)

        mgr._call_with_timeout(fast_fn, 3, 4, label="fast_test")
        assert results == [7]

        mgr.shutdown_timers()

    def test_exception_propagated(self):
        """Exceptions from fn are raised to the caller."""
        mgr, _ = _make_manager()
        mgr._IO_CALL_TIMEOUT_S = 2

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
        mgr._IO_CALL_TIMEOUT_S = 0.3  # Short timeout

        def slow_fn():
            time.sleep(5)

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
        mgr._IO_CALL_TIMEOUT_S = 0.3

        def slow_fn():
            time.sleep(5)

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


def test_timeout_io_rejects_fifth_call_while_four_callbacks_are_blocked():
    """A saturated timeout pool rejects instead of queueing another callback."""
    mgr, _ = _make_manager()
    mgr._IO_CALL_TIMEOUT_S = 0.2
    release = threading.Event()
    all_started = threading.Event()
    fifth_started = threading.Event()
    started_count = 0
    started_lock = threading.Lock()

    def blocked_callback() -> None:
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 4:
                all_started.set()
        release.wait()

    def fifth_callback() -> None:
        fifth_started.set()

    try:
        for _ in range(4):
            with pytest.raises(TimeoutError):
                mgr._call_with_timeout(blocked_callback, label="blocked")
        assert all_started.wait(timeout=1)

        started_at = time.perf_counter()
        with pytest.raises(TimeoutError):
            mgr._call_with_timeout(fifth_callback, label="saturated")
        rejection_elapsed = time.perf_counter() - started_at

        shutdown_started_at = time.perf_counter()
        mgr.shutdown_timers()
        shutdown_elapsed = time.perf_counter() - shutdown_started_at
    finally:
        release.set()

    assert rejection_elapsed < 0.1
    assert shutdown_elapsed < 0.1
    assert not fifth_started.wait(timeout=0.2)


def test_half_time_reminder_uses_bounded_timeout_submission():
    """The reminder's Feishu callback shares the bounded timeout capacity."""
    send_text = MagicMock()
    mgr, _ = _make_manager(send_text_fn=send_text)
    escalation = EscalationRequest(agent_id="agent", agent_name="Agent")
    mgr._escalations.append(escalation)

    try:
        with patch.object(mgr, "_call_with_timeout", wraps=mgr._call_with_timeout) as bounded:
            mgr._half_time_reminder(escalation.escalation_id)

        assert bounded.call_args.kwargs["label"] == "half_time_reminder"
    finally:
        mgr.shutdown_timers()


def test_resume_failure_alert_uses_bounded_timeout_submission():
    """The recovery alert cannot bypass timeout callback backpressure."""
    send_text = MagicMock()
    mgr, _ = _make_manager(send_text_fn=send_text)
    escalation = EscalationRequest(
        agent_id="agent",
        agent_name="Agent",
        task_id="task",
        reason="blocked",
    )

    try:
        with (
            patch.object(mgr, "resume_after_escalation", side_effect=RuntimeError("boom")),
            patch.object(mgr, "_call_with_timeout", wraps=mgr._call_with_timeout) as bounded,
        ):
            mgr._do_timeout_io(escalation)

        labels = [call.kwargs["label"] for call in bounded.call_args_list]
        assert labels == ["send_text", "resume_failure_alert"]
    finally:
        mgr.shutdown_timers()


def test_never_returning_timeout_callback_does_not_block_subprocess_exit():
    """Timeout callback workers cannot keep Python alive during interpreter shutdown."""
    script = textwrap.dedent(
        """
        import threading
        from tests.test_escalation_threadpool_timeout import _make_manager

        manager, _ = _make_manager()
        manager._IO_CALL_TIMEOUT_S = 0.05

        def never_returns():
            threading.Event().wait()

        try:
            manager._call_with_timeout(never_returns, label="never")
        except TimeoutError:
            pass
        manager.shutdown_timers()
        print("shutdown-returned", flush=True)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "shutdown-returned" in completed.stdout
