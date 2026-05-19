"""Unit tests for deactivate() escalation timer cleanup.

Verifies that deactivate() calls shutdown_timers() before observer_queue.shutdown(),
ensuring no ghost messages from orphaned Timer threads after engine deactivation.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from src.slock_engine.escalation_manager import EscalationManager
from src.slock_engine.models import (
    EscalationLevel,
    EscalationRequest,
)


def _make_manager(
    *,
    escalation_timeout_s: int = 30 * 60,
    send_text_fn=None,
    update_card_fn=None,
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


def _make_escalation() -> EscalationRequest:
    return EscalationRequest(
        agent_id="agent-001",
        agent_name="TestAgent",
        task_id="task-001",
        level=EscalationLevel.BLOCKED,
        reason="Cannot access resource",
        context="Error details here",
        options=["重试", "跳过", "中止"],
        card_message_id="msg_abc123",
    )


class TestDeactivateCallsShutdownTimers:
    """TEST-1: Verify deactivate() calls shutdown_timers on the escalation manager."""

    def test_deactivate_calls_shutdown_timers(self, tmp_path):
        """deactivate() must invoke _escalation_mgr.shutdown_timers()."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine(
            chat_id="test_chat",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path / "slock"),
        )
        engine._escalation_mgr.shutdown_timers = MagicMock()

        engine.deactivate()

        engine._escalation_mgr.shutdown_timers.assert_called_once()


class TestDeactivateShutdownTimersOrder:
    """TEST-2: Verify shutdown_timers is called before observer_queue.shutdown."""

    def test_shutdown_timers_before_observer_shutdown(self, tmp_path):
        """shutdown_timers() must be called before observer_queue.shutdown()."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine(
            chat_id="test_chat",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path / "slock"),
        )

        call_order: list[str] = []
        original_shutdown_timers = engine._escalation_mgr.shutdown_timers
        original_observer_shutdown = engine._observer_queue.shutdown

        def mock_shutdown_timers():
            call_order.append("shutdown_timers")
            original_shutdown_timers()

        def mock_observer_shutdown():
            call_order.append("observer_shutdown")
            original_observer_shutdown()

        engine._escalation_mgr.shutdown_timers = mock_shutdown_timers
        engine._observer_queue.shutdown = mock_observer_shutdown

        engine.deactivate()

        assert call_order.index("shutdown_timers") < call_order.index("observer_shutdown")


class TestDeactivateCancelsActiveTimer:
    """TEST-3: Verify active Timer threads are cancelled by deactivate()."""

    def test_active_timer_cancelled(self, tmp_path):
        """A running Timer started by escalation is cancelled after deactivate()."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine(
            chat_id="test_chat",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path / "slock"),
        )

        # Start an escalation timer with long timeout (won't fire naturally)
        esc = _make_escalation()
        engine._escalation_mgr._escalations.append(esc)
        engine._escalation_mgr._escalation_timeout_s = 60  # 60s, won't fire in test
        engine._escalation_mgr._start_timeout_timer(esc)

        # Verify timer is alive
        timer = engine._escalation_mgr._timeout_timers.get(esc.escalation_id)
        assert timer is not None
        assert timer.is_alive()

        # Deactivate should cancel it
        engine.deactivate()

        assert timer.finished.is_set()


class TestDeactivateIdempotent:
    """TEST-4: Verify deactivate() can be called multiple times without error."""

    def test_double_deactivate_no_exception(self, tmp_path):
        """Calling deactivate() twice must not raise."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine(
            chat_id="test_chat",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path / "slock"),
        )

        # Start a timer to have something to cancel
        esc = _make_escalation()
        engine._escalation_mgr._escalations.append(esc)
        engine._escalation_mgr._escalation_timeout_s = 60
        engine._escalation_mgr._start_timeout_timer(esc)

        # First deactivate
        engine.deactivate()
        # Second deactivate — should not raise
        engine.deactivate()


class TestDeactivatePreventsGhostMessage:
    """TEST-5: Verify deactivate() prevents ghost messages from timer callbacks."""

    def test_no_ghost_message_after_deactivate(self, tmp_path):
        """After deactivate(), timer callback must not send messages."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine(
            chat_id="test_chat",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path / "slock"),
        )

        send_text_fn = MagicMock()
        update_card_fn = MagicMock(return_value=True)
        engine._escalation_mgr.set_ui_callbacks(update_card_fn, send_text_fn)

        # Start an escalation timer with very short timeout
        esc = _make_escalation()
        engine._escalation_mgr._escalations.append(esc)
        engine._escalation_mgr._escalation_timeout_s = 1  # 1 second
        engine._escalation_mgr._start_timeout_timer(esc)

        # Immediately deactivate — timer should be cancelled before firing
        engine.deactivate()

        # Wait longer than the timeout to verify callback doesn't fire
        time.sleep(1.5)

        # Neither update_card nor send_text should have been called
        update_card_fn.assert_not_called()
        send_text_fn.assert_not_called()


class TestShutdownTimersConcurrency:
    """TEST-6: Verify shutdown_timers is thread-safe with concurrent timer fire."""

    def test_shutdown_timers_concurrent_with_timeout_fire(self):
        """shutdown_timers and _timeout_auto_abort can run concurrently without error.

        Uses a Barrier to synchronize two threads so they execute simultaneously.
        Timeout: 2s to prevent CI hang.
        """
        barrier = threading.Barrier(2, timeout=2)
        errors: list[Exception] = []

        mgr, mocks = _make_manager(
            escalation_timeout_s=60,
            send_text_fn=MagicMock(),
            update_card_fn=MagicMock(return_value=True),
        )

        # Create multiple escalations with timers
        escalations = []
        for i in range(5):
            esc = _make_escalation()
            # Override escalation_id to make unique
            esc.escalation_id = f"esc-concurrent-{i}"
            esc.card_message_id = f"msg-{i}"
            mgr._escalations.append(esc)
            mgr._start_timeout_timer(esc)
            escalations.append(esc)

        def run_shutdown():
            try:
                barrier.wait(timeout=2)
                mgr.shutdown_timers()
            except Exception as e:
                errors.append(e)

        def run_timeout_fire():
            try:
                barrier.wait(timeout=2)
                # Simulate a timer firing for the first escalation
                mgr._timeout_auto_abort(escalations[0].escalation_id)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run_shutdown, daemon=True)
        t2 = threading.Thread(target=run_timeout_fire, daemon=True)

        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        # No RuntimeError or deadlock
        assert not errors, f"Concurrent execution raised errors: {errors}"
        # Neither thread should still be alive (no deadlock)
        assert not t1.is_alive(), "shutdown thread deadlocked"
        assert not t2.is_alive(), "timeout_fire thread deadlocked"
