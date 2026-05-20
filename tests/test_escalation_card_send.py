"""Unit and integration tests for escalation card sending via on_escalation callback.

Covers:
- on_escalation callback invocation when escalate() is called
- card_message_id is set from send_card_to_chat return value
- timeout_auto_abort uses card_message_id to update the card
- Graceful handling when send_card_to_chat returns None
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from src.slock_engine.engine import SlockEngineCallbacks
from src.slock_engine.escalation_manager import EscalationManager
from src.slock_engine.models import (
    AgentIdentity,
    EscalationRequest,
)
from src.slock_engine.task_router import TaskRouter


def _make_manager(
    *,
    escalation_timeout_s: int = 30 * 60,
    update_card_fn=None,
    send_text_fn=None,
) -> tuple[EscalationManager, dict]:
    """Create a minimal EscalationManager with mock callbacks."""
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


def _make_agent() -> AgentIdentity:
    return AgentIdentity(agent_id="agent-001", name="TestAgent")


class TestOnEscalationCallbackInvocation:
    """Tests that escalate() triggers the on_escalation callback."""

    def test_escalate_triggers_on_escalation_callback(self):
        """on_escalation is called with the EscalationRequest after escalate()."""
        mgr, _ = _make_manager()
        agent = _make_agent()

        on_escalation = MagicMock()
        callbacks = SlockEngineCallbacks(on_escalation=on_escalation)

        esc = mgr.escalate(agent, "test reason", callbacks=callbacks)

        on_escalation.assert_called_once_with(esc)

    def test_escalate_without_on_escalation_does_not_crash(self):
        """escalate() works fine when on_escalation is None."""
        mgr, _ = _make_manager()
        agent = _make_agent()

        callbacks = SlockEngineCallbacks()  # on_escalation=None

        esc = mgr.escalate(agent, "test reason", callbacks=callbacks)

        assert esc is not None
        assert esc.reason == "test reason"

    def test_escalate_without_callbacks_does_not_crash(self):
        """escalate() works fine when callbacks is None."""
        mgr, _ = _make_manager()
        agent = _make_agent()

        esc = mgr.escalate(agent, "test reason", callbacks=None)

        assert esc is not None
        assert esc.reason == "test reason"

    def test_on_escalation_exception_does_not_propagate(self):
        """If on_escalation raises, escalate() still returns the request."""
        mgr, _ = _make_manager()
        agent = _make_agent()

        def bad_callback(esc):
            raise RuntimeError("callback failed")

        callbacks = SlockEngineCallbacks(on_escalation=bad_callback)

        esc = mgr.escalate(agent, "test reason", callbacks=callbacks)

        # Should still return without raising
        assert esc is not None
        assert esc.reason == "test reason"


class TestCardMessageIdWriteback:
    """Tests that on_escalation callback correctly writes back card_message_id."""

    def test_card_message_id_set_from_send_card(self):
        """Simulate the handler's on_escalation setting card_message_id."""
        mgr, _ = _make_manager()
        agent = _make_agent()

        fake_message_id = "om_test_msg_12345"

        def on_escalation(esc):
            # Simulate what the handler does: send card and write back id
            esc.card_message_id = fake_message_id

        callbacks = SlockEngineCallbacks(on_escalation=on_escalation)

        esc = mgr.escalate(agent, "need help", callbacks=callbacks)

        assert esc.card_message_id == fake_message_id

    def test_card_message_id_remains_none_on_send_failure(self):
        """If send_card_to_chat returns None, card_message_id stays None."""
        mgr, _ = _make_manager()
        agent = _make_agent()

        def on_escalation(esc):
            # Simulate send failure: don't set card_message_id
            pass

        callbacks = SlockEngineCallbacks(on_escalation=on_escalation)

        esc = mgr.escalate(agent, "need help", callbacks=callbacks)

        assert esc.card_message_id is None


class TestTimeoutWithCardMessageId:
    """Integration: timeout_auto_abort uses card_message_id set by on_escalation."""

    def test_timeout_updates_card_when_message_id_set_by_callback(self):
        """Full flow: escalate → on_escalation sets card_message_id → timeout updates card."""
        update_card_fn = MagicMock(return_value=True)
        mgr, _ = _make_manager(
            update_card_fn=update_card_fn,
            escalation_timeout_s=1,  # short timeout for testing
        )
        agent = _make_agent()

        fake_message_id = "om_esc_card_001"

        def on_escalation(esc):
            esc.card_message_id = fake_message_id

        callbacks = SlockEngineCallbacks(on_escalation=on_escalation)

        esc = mgr.escalate(agent, "blocked on resource", callbacks=callbacks)

        # Verify card_message_id is set
        assert esc.card_message_id == fake_message_id

        # Manually trigger timeout (normally fires via timer)
        mgr._timeout_auto_abort(esc.escalation_id)
        mgr._io_executor.shutdown(wait=True)

        # Verify update_card_fn was called with the correct message_id
        update_card_fn.assert_called_once()
        call_args = update_card_fn.call_args
        assert call_args[0][0] == fake_message_id

    def test_timeout_skips_card_update_when_no_message_id(self):
        """If card_message_id is None (send failed), timeout doesn't try to update."""
        update_card_fn = MagicMock(return_value=True)
        mgr, _ = _make_manager(update_card_fn=update_card_fn)
        agent = _make_agent()

        # Don't set card_message_id in the callback
        callbacks = SlockEngineCallbacks(on_escalation=lambda esc: None)

        esc = mgr.escalate(agent, "blocked", callbacks=callbacks)
        assert esc.card_message_id is None

        mgr._timeout_auto_abort(esc.escalation_id)

        # update_card_fn should NOT be called since no message_id
        update_card_fn.assert_not_called()
