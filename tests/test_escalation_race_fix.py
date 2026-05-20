"""Unit tests for escalation race condition fix.

Verifies:
- card_message_id is written before timeout timer fires
- timer starts even if on_escalation callback raises
- concurrent escalations don't leak threads beyond max_workers bound
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from src.slock_engine.escalation_manager import EscalationManager
from src.slock_engine.models import (
    AgentIdentity,
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


def _make_agent(name: str = "TestAgent") -> AgentIdentity:
    return AgentIdentity(
        agent_id="agent-001",
        name=name,
        emoji="🔧",
        agent_type="codex",
        model_name="o3-pro",
        role="coder",
        system_prompt="test",
        permissions={},
        owner_group="test_chat_id",
    )


class _FakeCallbacks:
    """Minimal callback object mimicking SlockEngineCallbacks."""

    def __init__(self, on_escalation=None, on_error=None):
        self.on_escalation = on_escalation
        self.on_error = on_error


class TestCardMessageIdAvailableBeforeTimeout:
    """AC-1: With very short timeout, card_message_id is set before timeout fires."""

    def test_update_card_receives_card_message_id(self):
        """on_escalation writes card_message_id, timeout fires, update_card gets it."""
        received_ids = []

        def mock_update_card(msg_id, card_json):
            received_ids.append(msg_id)
            return True

        mgr, mocks = _make_manager(
            escalation_timeout_s=1,  # 1 second - short but safe
            update_card_fn=mock_update_card,
            send_text_fn=MagicMock(),
        )
        # Override IO call timeout to be short for testing
        mgr._IO_CALL_TIMEOUT_S = 5

        agent = _make_agent()

        def on_escalation(esc):
            # Simulate card send - write card_message_id
            esc.card_message_id = "msg_written_by_callback"

        callbacks = _FakeCallbacks(on_escalation=on_escalation)
        mgr.escalate(agent, "test reason", callbacks=callbacks)

        # Wait for timeout to fire + IO to complete
        time.sleep(2.5)
        mgr._io_executor.shutdown(wait=True)

        # Verify update_card was called with the correct message_id
        assert len(received_ids) > 0, "update_card should have been called"
        assert received_ids[0] == "msg_written_by_callback"

    def test_card_message_id_none_when_callback_doesnt_set_it(self):
        """If on_escalation doesn't set card_message_id, update_card is skipped."""
        update_card_fn = MagicMock()
        mgr, mocks = _make_manager(
            escalation_timeout_s=1,
            update_card_fn=update_card_fn,
            send_text_fn=MagicMock(),
        )

        agent = _make_agent()

        def on_escalation(esc):
            pass  # Don't set card_message_id

        callbacks = _FakeCallbacks(on_escalation=on_escalation)
        mgr.escalate(agent, "test reason", callbacks=callbacks)

        time.sleep(2.5)
        mgr._io_executor.shutdown(wait=True)

        # update_card should NOT have been called (card_message_id is None)
        update_card_fn.assert_not_called()


class TestTimerStartsEvenIfCallbackRaises:
    """Timer always starts via finally block even when on_escalation raises."""

    def test_timer_fires_after_callback_exception(self):
        send_text_fn = MagicMock()
        mgr, mocks = _make_manager(
            escalation_timeout_s=1,
            send_text_fn=send_text_fn,
        )

        agent = _make_agent()

        def on_escalation(esc):
            raise RuntimeError("callback exploded")

        callbacks = _FakeCallbacks(on_escalation=on_escalation)
        esc = mgr.escalate(agent, "test reason", callbacks=callbacks)

        # Wait for timeout
        time.sleep(2.5)
        mgr._io_executor.shutdown(wait=True)

        # Escalation should be resolved (timer fired)
        assert esc.resolved is True
        assert esc.resolution == "中止"
        # Text notification should have been sent (may include half-time reminder + timeout)
        assert send_text_fn.call_count >= 1
        # At least one call should be the timeout notification
        all_texts = [str(c) for c in send_text_fn.call_args_list]
        assert any("超时" in t for t in all_texts)


class TestConcurrentEscalationsThreadBounded:
    """AC-2: 20 concurrent escalations don't leak threads beyond pool bounds."""

    def test_thread_count_bounded(self):
        mgr, mocks = _make_manager(
            escalation_timeout_s=1,
            update_card_fn=MagicMock(return_value=True),
            send_text_fn=MagicMock(),
        )
        mgr._IO_CALL_TIMEOUT_S = 5

        baseline_threads = threading.active_count()

        # Create 20 escalations that will all timeout
        for i in range(20):
            agent_i = AgentIdentity(
                agent_id=f"agent-{i:03d}",
                name=f"Agent-{i}",
                emoji="🔧",
                agent_type="codex",
                model_name="o3-pro",
                role="coder",
                system_prompt="test",
                permissions={},
                owner_group="test_chat_id",
            )

            def on_esc(esc, idx=i):
                esc.card_message_id = f"msg_{idx}"

            callbacks = _FakeCallbacks(on_escalation=on_esc)
            mgr.escalate(agent_i, f"reason {i}", callbacks=callbacks)

        # Wait for all timers to fire
        time.sleep(2.0)

        # Check thread growth is bounded
        # Expected: _timeout_call_executor(4) + _io_executor consumer(1) + timer threads
        # Timer threads are transient (fire and die quickly)
        peak_threads = threading.active_count()
        # Allow generous margin: baseline + pool workers(4) + io consumer(1) + some timers
        max_allowed = baseline_threads + 4 + 1 + 5  # generous headroom
        assert peak_threads <= max_allowed, (
            f"Thread count {peak_threads} exceeds max allowed {max_allowed} "
            f"(baseline={baseline_threads})"
        )

        # Cleanup
        mgr.shutdown_timers()
        time.sleep(1.0)
