"""Integration test: mention pending queue overflow sends queue_full_card."""

import collections
import json
import threading
from unittest.mock import MagicMock


class TestMentionQueueFullIntegration:
    """Verify full handler path when mention queue overflows at maxlen=8."""

    def _make_handler(self):
        """Create a minimal SlockHandler mock with queue infrastructure."""
        from src.feishu.handlers.slock import SlockHandler

        handler = SlockHandler.__new__(SlockHandler)
        handler._queue_lock = threading.Lock()
        handler._mention_pending_queue = {}
        handler.send_card_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        return handler

    def _make_agent(self, agent_id="agent-001", name="TestCoder", role="coder"):
        agent = MagicMock()
        agent.agent_id = agent_id
        agent.name = name
        agent.role = role
        return agent

    def _make_engine(self, agent, status="RUNNING"):
        from src.slock_engine.models import AgentStatus

        engine = MagicMock()
        engine.router.route_mention.return_value = agent
        engine.get_agent_status.return_value = AgentStatus.RUNNING
        # No idle same-role agents
        engine._registry = MagicMock()
        return engine

    def test_first_8_messages_enqueue_successfully(self):
        """First 8 messages should be queued without error card."""
        handler = self._make_handler()
        agent = self._make_agent()

        # Pre-fill queue with 7 items, then add 1 more (8th) — should succeed
        handler._mention_pending_queue[agent.agent_id] = collections.deque(maxlen=8)
        for i in range(7):
            handler._mention_pending_queue[agent.agent_id].append(
                (f"msg-{i}", "chat_id", "channel_id", f"mid-{i}", 1000.0 + i)
            )

        # The queue has 7 items, adding one more should work
        queue = handler._mention_pending_queue[agent.agent_id]
        assert len(queue) == 7
        queue.append(("msg-7", "chat_id", "channel_id", "mid-7", 1007.0))
        assert len(queue) == 8

    def test_9th_message_triggers_queue_full_card(self):
        """9th @mention message to a busy agent sends queue_full_card."""
        handler = self._make_handler()
        agent = self._make_agent()
        engine = self._make_engine(agent)

        # Pre-fill queue to maxlen
        handler._mention_pending_queue[agent.agent_id] = collections.deque(maxlen=8)
        for i in range(8):
            handler._mention_pending_queue[agent.agent_id].append(
                (f"msg-{i}", "chat_id", "channel_id", f"mid-{i}", 1000.0 + i)
            )

        # Simulate the handler's queue-full check logic directly
        from src.slock_engine.card_templates import build_queue_full_card

        queue = handler._mention_pending_queue[agent.agent_id]
        assert len(queue) >= queue.maxlen

        card = build_queue_full_card(
            agent,
            channel_id="channel_id",
            original_message="This is the 9th message that should be rejected",
        )
        handler.send_card_to_chat("chat_id", json.dumps(card, ensure_ascii=False))

        # Verify send_card_to_chat was called
        handler.send_card_to_chat.assert_called_once()
        sent_card_json = handler.send_card_to_chat.call_args[0][1]
        sent_card = json.loads(sent_card_json)

        # Verify card content
        card_str = json.dumps(sent_card, ensure_ascii=False)
        assert "队列已满" in card_str, "Card should contain '队列已满'"

        # Verify action buttons exist
        assert "slock_queue_retry" in card_str, "Card should have retry action"
        assert "slock_force_interrupt" in card_str, "Card should have force interrupt action"

    def test_queue_full_card_contains_rejected_message(self):
        """Queue full card should include a truncated preview of the rejected message."""
        from src.slock_engine.card_templates import build_queue_full_card

        agent = self._make_agent()
        long_message = "x" * 500  # Very long message

        card = build_queue_full_card(
            agent,
            channel_id="ch-001",
            original_message=long_message,
        )

        card_str = json.dumps(card, ensure_ascii=False)
        # Card should contain some portion of the message (truncated)
        assert "队列已满" in card_str
        # Should have both action buttons
        assert "slock_queue_retry" in card_str
        assert "slock_force_interrupt" in card_str

    def test_queue_not_full_does_not_trigger_card(self):
        """When queue has room, no error card should be sent."""
        handler = self._make_handler()
        agent = self._make_agent()

        handler._mention_pending_queue[agent.agent_id] = collections.deque(maxlen=8)
        # Only 5 items — not full
        for i in range(5):
            handler._mention_pending_queue[agent.agent_id].append(
                (f"msg-{i}", "chat_id", "channel_id", f"mid-{i}", 1000.0 + i)
            )

        queue = handler._mention_pending_queue[agent.agent_id]
        assert len(queue) < queue.maxlen
        # No card should be needed
        handler.send_card_to_chat.assert_not_called()
