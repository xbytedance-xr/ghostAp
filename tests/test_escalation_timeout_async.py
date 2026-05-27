"""Unit tests for escalation timeout async I/O offloading.

Verifies that _timeout_auto_abort offloads I/O to a dedicated worker thread
(slock-esc-io) so the Timer thread returns immediately. Also tests the 10s
per-call timeout wrapper and execution order guarantees.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.slock_engine.escalation_manager import EscalationManager
from src.slock_engine.models import (
    EscalationLevel,
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

    context = MagicMock()
    context.channel = None
    context.chat_id = "test_chat_id"
    context.dirty = False
    context.set_dirty = mocks["dirty_setter"]

    mgr = EscalationManager(
        lock=lock,
        escalations=escalations,
        retry_counts=retry_counts,
        context=context,
        router=router,
        transition_agent=mocks["transition_agent"],
        flush_if_dirty=mocks["flush_if_dirty"],
        update_card_fn=update_card_fn,
        send_text_fn=send_text_fn,
        escalation_timeout_s=escalation_timeout_s,
    )
    mgr.set_task_callbacks(
        execute_task_fn=mocks["execute_task_fn"],
        rollback_task_fn=mocks["rollback_task_fn"],
        force_complete_task_fn=mocks["force_complete_task_fn"],
    )
    return mgr, mocks


def _make_escalation(
    agent_name: str = "TestAgent",
    task_id: str = "task-001",
    card_message_id: str = "msg_abc123",
) -> EscalationRequest:
    return EscalationRequest(
        agent_id="agent-001",
        agent_name=agent_name,
        task_id=task_id,
        level=EscalationLevel.BLOCKED,
        reason="Cannot access resource",
        context="Error details here",
        options=["重试", "跳过", "中止"],
        card_message_id=card_message_id,
    )


class TestIORunsOnWorkerThread:
    """Verify I/O operations execute on the slock-esc-io thread, not Timer."""

    def test_update_card_runs_on_worker_thread(self):
        """update_card_fn is called from a worker thread, not Timer or MainThread."""
        thread_names: list[str] = []

        def capture_thread(*args):
            thread_names.append(threading.current_thread().name)
            return True

        mgr, mocks = _make_manager(update_card_fn=capture_thread)
        esc = _make_escalation()
        mgr._escalations.append(esc)

        mgr._timeout_auto_abort(esc.escalation_id)
        # Wait for IO executor to finish
        mgr._io_executor.shutdown(wait=True)

        assert len(thread_names) == 1
        assert "Timer" not in thread_names[0]
        assert thread_names[0] != "MainThread"

    def test_send_text_runs_on_worker_thread(self):
        """send_text_fn is called from a worker thread, not Timer or MainThread."""
        thread_names: list[str] = []

        def capture_thread(*args):
            thread_names.append(threading.current_thread().name)

        mgr, mocks = _make_manager(send_text_fn=capture_thread)
        esc = _make_escalation(card_message_id=None)  # skip card update
        mgr._escalations.append(esc)

        mgr._timeout_auto_abort(esc.escalation_id)
        mgr._io_executor.shutdown(wait=True)

        assert len(thread_names) == 1
        assert "Timer" not in thread_names[0]
        assert thread_names[0] != "MainThread"

    def test_resume_runs_on_worker_thread(self):
        """resume_after_escalation runs on worker thread (abort sets dirty there)."""
        thread_names: list[str] = []

        def track_set_dirty(val):
            thread_names.append(threading.current_thread().name)

        mgr, mocks = _make_manager()
        # Override set_dirty to track which thread calls it from _do_timeout_io
        mgr._context.set_dirty = track_set_dirty
        esc = _make_escalation(card_message_id=None)
        mgr._escalations.append(esc)

        mgr._timeout_auto_abort(esc.escalation_id)
        mgr._io_executor.shutdown(wait=True)

        # set_dirty is called from _timeout_auto_abort (main/timer thread) and
        # from resume_after_escalation (io thread). At least one should be on io thread.
        assert any("slock-esc-io" in name for name in thread_names)


class TestTimerReturnsImmediately:
    """Verify the Timer thread is not blocked by slow I/O."""

    def test_timer_returns_within_100ms_with_slow_card_update(self):
        """Even with a 3s-blocking update_card_fn, _timeout_auto_abort returns fast."""

        def slow_update(*args):
            time.sleep(3)
            return True

        mgr, mocks = _make_manager(update_card_fn=slow_update)
        esc = _make_escalation()
        mgr._escalations.append(esc)

        start = time.perf_counter()
        mgr._timeout_auto_abort(esc.escalation_id)
        elapsed = time.perf_counter() - start

        # Timer thread should return in < 100ms (only does state mutation + submit)
        assert elapsed < 0.1, f"_timeout_auto_abort took {elapsed:.3f}s, expected < 0.1s"

        # Cleanup: don't wait for the slow call to finish
        mgr._io_executor.shutdown(wait=False)

    def test_timer_returns_within_100ms_with_slow_send_text(self):
        """Even with a 3s-blocking send_text_fn, _timeout_auto_abort returns fast."""

        def slow_send(*args):
            time.sleep(3)

        mgr, mocks = _make_manager(send_text_fn=slow_send)
        esc = _make_escalation(card_message_id=None)
        mgr._escalations.append(esc)

        start = time.perf_counter()
        mgr._timeout_auto_abort(esc.escalation_id)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1, f"_timeout_auto_abort took {elapsed:.3f}s, expected < 0.1s"
        mgr._io_executor.shutdown(wait=False)


class TestSendTextTimeoutSkipped:
    """Verify that a permanently blocking send_text_fn is skipped after 10s."""

    def test_send_text_timeout_does_not_block_resume(self):
        """If send_text_fn blocks forever, resume_after_escalation still runs."""
        blocker = threading.Event()

        def blocking_send(*args):
            blocker.wait()  # block forever until test cleanup

        mgr, mocks = _make_manager(send_text_fn=blocking_send)
        # Reduce IO timeout for faster test
        mgr._IO_CALL_TIMEOUT_S = 0.5

        esc = _make_escalation(card_message_id=None)
        mgr._escalations.append(esc)

        mgr._timeout_auto_abort(esc.escalation_id)
        # Wait for IO executor to process (should take ~0.5s for timeout + resume)
        mgr._io_executor.shutdown(wait=True)

        # Abort branch sets dirty (resume was still reached despite send timeout)
        mocks["dirty_setter"].assert_called_with(True)

        # Cleanup: unblock the daemon thread
        blocker.set()

    def test_update_card_timeout_does_not_block_send_text(self):
        """If update_card_fn blocks forever, send_text_fn still runs."""
        blocker = threading.Event()
        send_text_calls: list[tuple] = []

        def blocking_update(*args):
            blocker.wait()
            return True

        def capture_send(*args):
            send_text_calls.append(args)

        mgr, mocks = _make_manager(
            update_card_fn=blocking_update,
            send_text_fn=capture_send,
        )
        mgr._IO_CALL_TIMEOUT_S = 0.5

        esc = _make_escalation()
        mgr._escalations.append(esc)

        mgr._timeout_auto_abort(esc.escalation_id)
        mgr._io_executor.shutdown(wait=True)

        # send_text should still have been called despite update_card timeout
        assert len(send_text_calls) == 1
        # Abort branch sets dirty (resume was reached)
        mocks["dirty_setter"].assert_called_with(True)

        blocker.set()


class TestIOOrderPreserved:
    """Verify I/O operations execute in order: update_card → send_text → resume."""

    def test_execution_order(self):
        """Operations are called in the defined sequence."""
        call_order: list[str] = []
        order_lock = threading.Lock()

        def mock_update(*args):
            with order_lock:
                call_order.append("update_card")
            return True

        def mock_send(*args):
            with order_lock:
                call_order.append("send_text")

        mgr, mocks = _make_manager(
            update_card_fn=mock_update,
            send_text_fn=mock_send,
        )

        esc = _make_escalation()
        mgr._escalations.append(esc)

        mgr._timeout_auto_abort(esc.escalation_id)
        mgr._io_executor.shutdown(wait=True)

        assert call_order == ["update_card", "send_text"]


class TestExecutorShutdownOnCleanup:
    """Verify shutdown_timers() also shuts down the IO executor."""

    def test_executor_shutdown_called(self):
        """After shutdown_timers(), _io_executor is shut down."""
        mgr, mocks = _make_manager()

        assert not mgr._io_executor._shutdown
        mgr.shutdown_timers()
        assert mgr._io_executor._shutdown

    def test_submit_after_shutdown_raises(self):
        """Cannot submit new IO work after shutdown."""
        mgr, mocks = _make_manager()
        mgr.shutdown_timers()

        with pytest.raises(RuntimeError):
            mgr._io_executor.submit(lambda: None)

    def test_pending_io_completes_before_shutdown_wait(self):
        """If wait=True, pending IO completes."""
        completed = threading.Event()

        def slow_fn():
            time.sleep(0.5)
            completed.set()

        mgr, mocks = _make_manager()
        mgr._io_executor.submit(slow_fn)

        # Use wait=True for this specific test
        mgr._io_executor.shutdown(wait=True)
        assert completed.is_set()
