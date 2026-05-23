"""Tests for Slock @mention pending queue and timeout behavior.

Covers:
- AC22: When a user @mentions a busy agent, the message is stored in
  _mention_pending_queue. After slock_queue_wait_timeout elapses, a timeout
  notification card is sent to the user.
"""

from __future__ import annotations

import collections
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus

# ============================================================
# Helpers
# ============================================================


def _make_handler():
    """Create a SlockHandler with fully mocked context."""
    from src.feishu.handlers.slock import SlockHandler

    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.admin_user_ids = frozenset(["admin_001"])
    ctx.settings.slock_queue_wait_timeout = 60
    ctx.slock_engine_manager = MagicMock()
    ctx.api_client_factory = MagicMock()

    handler = SlockHandler(ctx)
    handler.reply_text = MagicMock(return_value=True)
    handler.reply_card = MagicMock()
    handler.send_card_to_chat = MagicMock(return_value="card-msg-001")
    handler.update_card = MagicMock(return_value=True)
    handler.send_text_to_chat = MagicMock()
    handler.add_reaction = MagicMock()
    handler.get_working_dir = MagicMock(return_value="/tmp/test")
    return handler


def _make_engine_mock():
    """Create a mock engine with channel and registry."""
    engine = MagicMock()
    engine.is_active = True
    engine.channel = MagicMock()
    engine.channel.channel_id = "chat-001"
    engine.channel.team_name = "TestTeam"
    engine.channel.owner_id = "owner_001"
    engine.engine_name = "Slock"
    engine.root_path = "/tmp/test"
    return engine


def _make_busy_agent():
    """Create an AgentIdentity for a busy agent."""
    return AgentIdentity(
        agent_id="agent-busy-001",
        name="BusyCoder",
        emoji="\U0001f4bb",
        agent_type="coco",
        owner_group="chat-001",
    )


# ============================================================
# AC22: @mention busy agent → queue + timeout notification
# ============================================================


class TestMentionPendingQueueEnqueue:
    """Verify that @mentioning a busy agent stores the message in the queue."""

    def test_message_enqueued_when_agent_is_busy(self):
        """A message directed at a RUNNING agent is stored in _mention_pending_queue."""
        handler = _make_handler()
        engine = _make_engine_mock()
        agent = _make_busy_agent()

        # Configure engine: agent is RUNNING (busy)
        engine.get_agent_status = MagicMock(return_value=AgentStatus.RUNNING)
        engine.router.route_mention = MagicMock(return_value=agent)
        engine.registry.list_agents = MagicMock(return_value=[agent])

        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(
            return_value=engine
        )

        # Simulate handle_message with an @mention text
        with patch("time.time", return_value=1000.0):
            handler.handle_message(
                message_id="msg-001",
                chat_id="chat-001",
                text="@BusyCoder please fix the bug",
                project=None,
            )

        # Assert message is in the pending queue under the agent's ID
        assert agent.agent_id in handler._mention_pending_queue
        queue = handler._mention_pending_queue[agent.agent_id]
        assert len(queue) == 1

        entry = queue[0]
        # Entry format: (text, chat_id, channel_id, message_id, enqueue_time)
        assert entry[0] == "@BusyCoder please fix the bug"
        assert entry[1] == "chat-001"
        assert entry[3] == "msg-001"
        assert entry[4] == 1000.0


class TestMentionQueueTimeout:
    """Verify timeout notifications are sent for expired queued messages."""

    def test_timeout_sends_notification_card(self):
        """After slock_queue_wait_timeout, _check_mention_queue_timeouts sends a card."""
        handler = _make_handler()
        agent_id = "agent-busy-001"

        # Manually populate the queue with a message enqueued at t=1000
        handler._mention_pending_queue[agent_id] = collections.deque(
            [("@BusyCoder fix the bug", "chat-001", "chat-001", "msg-001", 1000.0)],
            maxlen=8,
        )

        # Patch time.time() to be past the timeout (1000 + 60 + 1 = 1061)
        with patch("time.time", return_value=1061.0):
            handler._check_mention_queue_timeouts()

        # Assert notification card was sent
        handler.send_card_to_chat.assert_called_once()
        call_args = handler.send_card_to_chat.call_args[0]

        # First arg is chat_id
        assert call_args[0] == "chat-001"

        # Second arg is the card JSON
        card_data = json.loads(call_args[1])
        assert card_data["header"]["title"]["content"] == "\u23f0 \u6392\u961f\u8d85\u65f6"
        assert "\u6392\u961f\u8d85\u65f6" in card_data["body"]["elements"][0]["content"]
        assert "@BusyCoder fix the bug" in card_data["body"]["elements"][0]["content"]

        # Keyword arg: origin_message_id
        call_kwargs = handler.send_card_to_chat.call_args[1]
        assert call_kwargs.get("origin_message_id") == "msg-001"

        # Queue should be cleaned up
        assert agent_id not in handler._mention_pending_queue

    def test_no_timeout_within_threshold(self):
        """Messages within slock_queue_wait_timeout remain queued, no notification."""
        handler = _make_handler()
        agent_id = "agent-busy-001"

        # Enqueue at t=1000; check at t=1050 (only 50s elapsed, timeout=60s)
        handler._mention_pending_queue[agent_id] = collections.deque(
            [("@BusyCoder fix the bug", "chat-001", "chat-001", "msg-001", 1000.0)],
            maxlen=8,
        )

        with patch("time.time", return_value=1050.0):
            handler._check_mention_queue_timeouts()

        # No notification sent
        handler.send_card_to_chat.assert_not_called()

        # Message still in queue
        assert agent_id in handler._mention_pending_queue
        assert len(handler._mention_pending_queue[agent_id]) == 1

    def test_multiple_messages_partial_timeout(self):
        """Only expired messages are removed; fresh messages remain queued."""
        handler = _make_handler()
        agent_id = "agent-busy-001"

        handler._mention_pending_queue[agent_id] = collections.deque(
            [
                # Enqueued at t=900 → expired at t=1001 (elapsed=101s > 60s)
                ("@BusyCoder old msg", "chat-001", "chat-001", "msg-old", 900.0),
                # Enqueued at t=990 → still valid at t=1001 (elapsed=11s < 60s)
                ("@BusyCoder new msg", "chat-001", "chat-001", "msg-new", 990.0),
            ],
            maxlen=8,
        )

        with patch("time.time", return_value=1001.0):
            handler._check_mention_queue_timeouts()

        # One notification sent (for the old message)
        handler.send_card_to_chat.assert_called_once()
        card_json = handler.send_card_to_chat.call_args[0][1]
        assert "@BusyCoder old msg" in card_json

        # The new message remains in queue
        assert agent_id in handler._mention_pending_queue
        remaining = handler._mention_pending_queue[agent_id]
        assert len(remaining) == 1
        assert remaining[0][0] == "@BusyCoder new msg"


# ============================================================
# @mention routing: detection, filtering, and queue delivery
# ============================================================


def _make_engine_for_mention_tests():
    """Create a SlockEngine with mocked internals for _route_at_mentions testing.

    Patches __init__ to bypass heavy constructor dependencies and manually
    sets up only the state required by the mention routing logic.
    """
    import threading

    from src.slock_engine.engine import SlockEngine

    with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
        engine = SlockEngine.__new__(SlockEngine)

    # Minimal attributes required by _route_at_mentions / _process_mention_queue
    engine._mention_queue = {}
    engine._mention_queue_lock = threading.Lock()
    engine._channel = MagicMock()
    engine._channel.channel_id = "ch-mention-001"
    engine._registry = MagicMock()
    engine._memory = MagicMock()
    engine._agent_statuses = {}
    engine._lock = threading.RLock()
    engine._settings = MagicMock()
    engine._settings.slock_agent_execution_timeout = 120
    engine._router = MagicMock()
    return engine


def _agent(agent_id: str, name: str) -> AgentIdentity:
    """Shorthand to create an AgentIdentity for mention tests."""
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        emoji="\U0001f916",
        agent_type="coco",
        owner_group="ch-mention-001",
    )


class TestMentionDetectedInAgentOutput:
    """Verify that @AgentB in agent output triggers mention queue entry."""

    def test_mention_detected_in_agent_output(self):
        """Output containing '@AgentB' enqueues a mention for AgentB."""
        engine = _make_engine_for_mention_tests()

        agent_a = _agent("agent-a-001", "AgentA")
        agent_b = _agent("agent-b-001", "AgentB")

        # Registry resolves AgentB by name, returns None for others
        def _find_by_name(name, channel_id=None):
            if name == "AgentB":
                return agent_b
            return None

        engine._registry.find_by_name = MagicMock(side_effect=_find_by_name)
        engine._registry.get = MagicMock(return_value=agent_a)

        # AgentB is RUNNING (not IDLE), so mention stays in queue
        engine._agent_statuses[agent_b.agent_id] = AgentStatus.RUNNING

        routed = engine._route_at_mentions(
            "I think @AgentB should review this code.",
            source_agent_id=agent_a.agent_id,
            callbacks=None,
        )

        # Verify AgentB was routed
        assert agent_b.agent_id in routed

        # Verify mention_queue has an entry for AgentB
        assert agent_b.agent_id in engine._mention_queue
        queue = engine._mention_queue[agent_b.agent_id]
        assert len(queue) == 1
        entry = queue[0]
        # Entry format: (mention_message, source_agent_id, callbacks, enqueue_time)
        assert "AgentA" in entry[0]  # source name in message
        assert entry[1] == agent_a.agent_id


class TestMultipleMentionsInSingleOutput:
    """Verify that multiple @mentions in a single output are all enqueued."""

    def test_multiple_mentions_in_single_output(self):
        """'@AgentA @AgentB' creates mention queue entries for both."""
        engine = _make_engine_for_mention_tests()

        source = _agent("agent-src-001", "SourceAgent")
        agent_a = _agent("agent-a-001", "AgentA")
        agent_b = _agent("agent-b-001", "AgentB")

        def _find_by_name(name, channel_id=None):
            if name == "AgentA":
                return agent_a
            if name == "AgentB":
                return agent_b
            return None

        engine._registry.find_by_name = MagicMock(side_effect=_find_by_name)
        engine._registry.get = MagicMock(return_value=source)

        # Both agents are busy → mentions stay in queue
        engine._agent_statuses[agent_a.agent_id] = AgentStatus.RUNNING
        engine._agent_statuses[agent_b.agent_id] = AgentStatus.RUNNING

        routed = engine._route_at_mentions(
            "cc @AgentA and @AgentB for input on this design.",
            source_agent_id=source.agent_id,
            callbacks=None,
        )

        assert agent_a.agent_id in routed
        assert agent_b.agent_id in routed

        # Both agents have entries in the mention queue
        assert agent_a.agent_id in engine._mention_queue
        assert agent_b.agent_id in engine._mention_queue
        assert len(engine._mention_queue[agent_a.agent_id]) == 1
        assert len(engine._mention_queue[agent_b.agent_id]) == 1


class TestMentionSelfIgnored:
    """Verify that an agent mentioning itself is filtered out."""

    def test_mention_self_ignored(self):
        """Agent output containing its own @name does not enqueue a self-mention."""
        engine = _make_engine_for_mention_tests()

        agent_a = _agent("agent-a-001", "AgentA")

        def _find_by_name(name, channel_id=None):
            if name == "AgentA":
                return agent_a
            return None

        engine._registry.find_by_name = MagicMock(side_effect=_find_by_name)
        engine._registry.get = MagicMock(return_value=agent_a)

        routed = engine._route_at_mentions(
            "I (@AgentA) have completed the task.",
            source_agent_id=agent_a.agent_id,  # source IS AgentA
            callbacks=None,
        )

        # Self-mention should be filtered out
        assert routed == []
        assert agent_a.agent_id not in engine._mention_queue


class TestMentionNonexistentAgentLogged:
    """Verify that mentioning a non-registered agent logs warning but doesn't crash."""

    def test_mention_nonexistent_agent_logged(self):
        """@NonExistentBot does not crash and returns empty routed list."""
        engine = _make_engine_for_mention_tests()

        agent_a = _agent("agent-a-001", "AgentA")

        # Registry cannot resolve any name → returns None
        engine._registry.find_by_name = MagicMock(return_value=None)
        engine._registry.get = MagicMock(return_value=agent_a)

        # Should not raise any exception
        routed = engine._route_at_mentions(
            "Hey @NonExistentBot can you help?",
            source_agent_id=agent_a.agent_id,
            callbacks=None,
        )

        # No agents routed, no mention_queue entries
        assert routed == []
        assert "NonExistentBot" not in [
            aid for aid in engine._mention_queue.keys()
        ]
        # Verify the engine state is still clean (no crash side-effects)
        assert engine._mention_queue == {}


class TestMentionQueueRoutesToTarget:
    """Verify queued mentions are consumed and routed to the target agent."""

    def test_mention_queue_routes_to_target(self):
        """A queued mention is drained and submitted to executor for the target agent."""
        engine = _make_engine_for_mention_tests()

        agent_b = _agent("agent-b-001", "AgentB")

        # Pre-populate the mention_queue with a pending message for AgentB
        engine._mention_queue[agent_b.agent_id] = collections.deque(
            [
                (
                    "[来自 AgentA 的 @mention][hop:1]\nPlease review this.",
                    "agent-a-001",
                    None,
                    time.time(),  # enqueued just now
                )
            ],
            maxlen=16,
        )

        # Registry resolves AgentB
        engine._registry.get = MagicMock(return_value=agent_b)

        # Mock executor to capture submitted work
        mock_executor = MagicMock()
        engine._get_executor = MagicMock(return_value=mock_executor)
        engine._execute_agent = MagicMock()

        # Process the mention queue for AgentB
        engine._process_mention_queue(agent_b.agent_id, callbacks=None)

        # Verify the queue was drained
        assert agent_b.agent_id not in engine._mention_queue

        # Verify executor.submit was called with _execute_agent for the target agent
        mock_executor.submit.assert_called_once()
        submit_args = mock_executor.submit.call_args[0]
        assert submit_args[0] == engine._execute_agent  # function
        assert submit_args[1] == agent_b  # target agent
        assert "Please review this." in submit_args[2]  # mention message content
