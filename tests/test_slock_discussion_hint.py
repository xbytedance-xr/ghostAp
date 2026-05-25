"""Unit tests for the Discussion Hint mechanism.

Tests cover:
- Hint storage in DiscussionThread.pending_hints
- Hint injection into discussion prompt
- Hint rejection for inactive discussions
- Hint button presence in discussion card
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional

import pytest

from src.slock_engine.card_templates import build_discussion_card
from src.slock_engine.discussion_manager import DiscussionManager
from src.slock_engine.models import (
    AgentIdentity,
    DiscussionConfig,
    DiscussionMessage,
    DiscussionStatus,
    DiscussionThread,
)

# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "agent-001",
    name: str = "TestAgent",
    role: str = "coder",
) -> AgentIdentity:
    """Create a test AgentIdentity."""
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        role=role,
    )


def _make_thread(
    participants: Optional[list[str]] = None,
    config: Optional[DiscussionConfig] = None,
    messages: Optional[list[DiscussionMessage]] = None,
    status: DiscussionStatus = DiscussionStatus.ACTIVE,
    total_tokens_used: int = 0,
    pending_hints: Optional[list[str]] = None,
) -> DiscussionThread:
    """Create a test DiscussionThread."""
    return DiscussionThread(
        thread_id=str(uuid.uuid4()),
        channel_id="channel-test",
        participants=participants or ["agent-001", "agent-002"],
        messages=messages or [],
        status=status,
        config=config or DiscussionConfig(),
        trigger_reason="test",
        total_tokens_used=total_tokens_used,
        pending_hints=pending_hints or [],
    )


def _make_message(
    sender: str = "agent-001",
    receiver: str = "agent-002",
    content: str = "test message",
    round_num: int = 1,
    token_count: int = 10,
) -> DiscussionMessage:
    """Create a test DiscussionMessage."""
    return DiscussionMessage(
        message_id=str(uuid.uuid4()),
        sender_agent_id=sender,
        receiver_agent_id=receiver,
        content=content,
        round_num=round_num,
        timestamp=time.time(),
        token_count=token_count,
    )


@pytest.fixture
def default_config() -> DiscussionConfig:
    """Default discussion configuration for tests."""
    return DiscussionConfig(
        max_rounds=3,
        token_budget=50000,
        trigger_rules=["coder->reviewer"],
        convergence_threshold=0.85,
    )


@pytest.fixture
def manager(default_config: DiscussionConfig) -> DiscussionManager:
    """Discussion manager with default config and no engine."""
    return DiscussionManager(config=default_config)


# ===========================================================================
# Test Class: Hint Storage
# ===========================================================================


class TestHintStorage:
    """Tests for pending_hints storage and add_hint method."""

    def test_add_hint_appends_to_pending_hints(self):
        """Hint is successfully added to pending_hints list."""
        thread = _make_thread()

        assert thread.pending_hints == []

        thread.add_hint("Consider using a cache layer for performance")

        assert len(thread.pending_hints) == 1
        assert thread.pending_hints[0] == "Consider using a cache layer for performance"

    def test_add_multiple_hints(self):
        """Multiple hints are accumulated in pending_hints."""
        thread = _make_thread()

        thread.add_hint("Hint 1: Focus on security")
        thread.add_hint("Hint 2: Consider edge cases")
        thread.add_hint("Hint 3: Check performance")

        assert len(thread.pending_hints) == 3
        assert thread.pending_hints[0] == "Hint 1: Focus on security"
        assert thread.pending_hints[1] == "Hint 2: Consider edge cases"
        assert thread.pending_hints[2] == "Hint 3: Check performance"

    def test_consume_hints_returns_and_clears(self):
        """consume_hints returns all hints and clears the list."""
        thread = _make_thread(pending_hints=["Hint A", "Hint B"])

        hints = thread.consume_hints()

        assert hints == ["Hint A", "Hint B"]
        assert thread.pending_hints == []

    def test_consume_hints_empty_list(self):
        """consume_hints on empty list returns empty list."""
        thread = _make_thread()

        hints = thread.consume_hints()

        assert hints == []
        assert thread.pending_hints == []

    def test_add_hint_thread_safety(self):
        """add_hint is thread-safe (uses _data_lock)."""
        import threading

        thread = _make_thread()
        barrier = threading.Barrier(10)
        errors = []

        def worker(idx: int):
            try:
                barrier.wait(timeout=5)
                thread.add_hint(f"Hint from worker {idx}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert len(thread.pending_hints) == 10


# ===========================================================================
# Test Class: Hint Injection
# ===========================================================================


class TestHintInjection:
    """Tests for hint injection into discussion prompt."""

    def test_hint_injected_into_round_prompt(self, manager: DiscussionManager):
        """Pending hints are injected into the next round's prompt."""
        thread = _make_thread()
        thread.messages = [
            _make_message(content="Initial proposal", round_num=0),
        ]
        thread.add_hint("Please consider the security implications")
        thread.add_hint("Also check for performance bottlenecks")

        prompt = manager._build_round_prompt(thread, "agent-002")

        assert "User Hints" in prompt
        assert "Please consider the security implications" in prompt
        assert "Also check for performance bottlenecks" in prompt
        assert "Please carefully consider the above user hints" in prompt

    def test_hints_cleared_after_injection(self, manager: DiscussionManager):
        """Hints are cleared (consumed) after being injected into prompt."""
        thread = _make_thread()
        thread.messages = [
            _make_message(content="Initial message", round_num=0),
        ]
        thread.add_hint("This is a one-time hint")

        assert len(thread.pending_hints) == 1

        # First call consumes the hints
        manager._build_round_prompt(thread, "agent-002")

        assert thread.pending_hints == []

        # Second call should not have the hint
        prompt2 = manager._build_round_prompt(thread, "agent-001")
        assert "This is a one-time hint" not in prompt2

    def test_no_hints_no_injection_section(self, manager: DiscussionManager):
        """Without pending hints, no hint section appears in prompt."""
        thread = _make_thread()
        thread.messages = [
            _make_message(content="Initial message", round_num=0),
        ]

        prompt = manager._build_round_prompt(thread, "agent-002")

        assert "User Hints" not in prompt

    def test_hint_numbered_correctly(self, manager: DiscussionManager):
        """Multiple hints are numbered correctly in the prompt."""
        thread = _make_thread()
        thread.messages = [
            _make_message(content="Initial", round_num=0),
        ]
        thread.add_hint("First hint")
        thread.add_hint("Second hint")
        thread.add_hint("Third hint")

        prompt = manager._build_round_prompt(thread, "agent-002")

        assert "[Hint 1] First hint" in prompt
        assert "[Hint 2] Second hint" in prompt
        assert "[Hint 3] Third hint" in prompt


# ===========================================================================
# Test Class: inject_hint API
# ===========================================================================


class TestInjectHintAPI:
    """Tests for DiscussionManager.inject_hint method."""

    def test_inject_hint_active_thread(self, manager: DiscussionManager):
        """inject_hint returns True and adds hint for active thread."""
        thread = _make_thread(status=DiscussionStatus.ACTIVE)

        result = manager.inject_hint(thread, "Please review the error handling")

        assert result is True
        assert len(thread.pending_hints) == 1
        assert thread.pending_hints[0] == "Please review the error handling"

    def test_inject_hint_inactive_thread_rejected(self, manager: DiscussionManager):
        """inject_hint returns False for converged (inactive) thread."""
        thread = _make_thread(status=DiscussionStatus.CONVERGED)

        result = manager.inject_hint(thread, "This should be rejected")

        assert result is False
        assert thread.pending_hints == []

    def test_inject_hint_timeout_thread_rejected(self, manager: DiscussionManager):
        """inject_hint returns False for timeout thread."""
        thread = _make_thread(status=DiscussionStatus.TIMEOUT)

        result = manager.inject_hint(thread, "Rejected hint")

        assert result is False
        assert thread.pending_hints == []

    def test_inject_hint_manually_stopped_rejected(self, manager: DiscussionManager):
        """inject_hint returns False for manually stopped thread."""
        thread = _make_thread(status=DiscussionStatus.MANUALLY_STOPPED)

        result = manager.inject_hint(thread, "Rejected hint")

        assert result is False
        assert thread.pending_hints == []

    def test_inject_hint_budget_exhausted_rejected(self, manager: DiscussionManager):
        """inject_hint returns False for budget-exhausted thread."""
        thread = _make_thread(status=DiscussionStatus.BUDGET_EXHAUSTED)

        result = manager.inject_hint(thread, "Rejected hint")

        assert result is False
        assert thread.pending_hints == []

    def test_inject_hint_empty_hint_rejected(self, manager: DiscussionManager):
        """inject_hint returns False for empty or whitespace-only hint."""
        thread = _make_thread(status=DiscussionStatus.ACTIVE)

        result1 = manager.inject_hint(thread, "")
        result2 = manager.inject_hint(thread, "   ")
        result3 = manager.inject_hint(thread, "\n\t")

        assert result1 is False
        assert result2 is False
        assert result3 is False
        assert thread.pending_hints == []

    def test_inject_hint_whitespace_trimmed(self, manager: DiscussionManager):
        """Hint text is trimmed of leading/trailing whitespace."""
        thread = _make_thread(status=DiscussionStatus.ACTIVE)

        result = manager.inject_hint(thread, "   Trimmed hint   ")

        assert result is True
        assert thread.pending_hints[0] == "Trimmed hint"

    def test_inject_hint_multiple_calls(self, manager: DiscussionManager):
        """Multiple inject_hint calls accumulate hints."""
        thread = _make_thread(status=DiscussionStatus.ACTIVE)

        manager.inject_hint(thread, "First hint")
        manager.inject_hint(thread, "Second hint")

        assert len(thread.pending_hints) == 2
        assert thread.pending_hints == ["First hint", "Second hint"]


# ===========================================================================
# Test Class: Discussion Card Hint Button
# ===========================================================================


class TestDiscussionCardHintButton:
    """Tests for the hint injection button in discussion card."""

    def test_card_contains_hint_button(self):
        """Discussion card contains the '人工干预' button with correct action."""
        card = build_discussion_card(
            thread_id="test-thread-123",
            participants=["Agent A", "Agent B"],
            messages=[],
            current_round=1,
            max_rounds=5,
            trigger_reason="test",
            channel_id="channel-abc",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Check button text exists
        assert "人工干预" in card_str

        # Check action value exists
        assert "inject_discussion_hint" in card_str

    def test_card_hint_button_has_thread_id(self):
        """Hint button value contains the correct thread_id."""
        thread_id = "thread-hint-test-456"
        card = build_discussion_card(
            thread_id=thread_id,
            participants=["Coder", "Reviewer"],
            messages=[{"sender": "Coder", "content": "My proposal", "round_num": 1}],
            current_round=1,
            max_rounds=3,
            trigger_reason="uncertainty:needs review",
            channel_id="ch-test",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        assert thread_id in card_str

    def test_card_has_three_action_buttons(self):
        """Discussion card has 3 action buttons: expand, hint, stop."""
        card = build_discussion_card(
            thread_id="t1",
            participants=["A", "B"],
            messages=[],
            current_round=0,
            max_rounds=3,
            channel_id="ch",
        )

        # Find all buttons in the card
        card_str = json.dumps(card, ensure_ascii=False)

        # Check all three buttons exist
        assert "展开全部" in card_str
        assert "人工干预" in card_str
        assert "停止讨论" in card_str

    def test_card_button_action_values(self):
        """Each button has the correct action value."""
        card = build_discussion_card(
            thread_id="thread-xyz",
            participants=["P1", "P2"],
            messages=[],
            current_round=1,
            max_rounds=5,
            channel_id="ch-123",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Check action values
        assert "slock_discussion_expand" in card_str
        assert "inject_discussion_hint" in card_str
        assert "slock_discussion_stop" in card_str

    def test_hint_button_is_primary_type(self):
        """Hint button has primary type (blue/emphasized)."""
        card = build_discussion_card(
            thread_id="t1",
            participants=["A", "B"],
            messages=[],
            current_round=0,
            max_rounds=3,
            channel_id="ch",
        )

        # Parse the card to find the hint button
        def find_buttons(elem):
            buttons = []
            if isinstance(elem, dict):
                if elem.get("tag") == "button":
                    buttons.append(elem)
                for v in elem.values():
                    buttons.extend(find_buttons(v))
            elif isinstance(elem, list):
                for item in elem:
                    buttons.extend(find_buttons(item))
            return buttons

        buttons = find_buttons(card)

        # Find the hint button
        hint_button = None
        for btn in buttons:
            text = btn.get("text", {}).get("content", "")
            if "人工干预" in text:
                hint_button = btn
                break

        assert hint_button is not None
        assert hint_button.get("type") == "primary"


# ===========================================================================
# Test Class: Serialization Roundtrip
# ===========================================================================


class TestHintSerialization:
    """Tests for pending_hints in serialization/deserialization."""

    def test_to_dict_includes_pending_hints(self):
        """to_dict includes pending_hints field."""
        thread = _make_thread(pending_hints=["Hint 1", "Hint 2"])

        data = thread.to_dict()

        assert "pending_hints" in data
        assert data["pending_hints"] == ["Hint 1", "Hint 2"]

    def test_from_dict_restores_pending_hints(self):
        """from_dict restores pending_hints from serialized data."""
        data = {
            "thread_id": "test-thread",
            "channel_id": "ch-test",
            "participants": ["a1", "a2"],
            "messages": [],
            "status": "active",
            "pending_hints": ["Serialized hint 1", "Serialized hint 2"],
        }

        thread = DiscussionThread.from_dict(data)

        assert thread.pending_hints == ["Serialized hint 1", "Serialized hint 2"]

    def test_manager_serialize_includes_hints(self, manager: DiscussionManager):
        """DiscussionManager.serialize_thread includes pending_hints."""
        thread = _make_thread(pending_hints=["Manager hint"])

        data = manager.serialize_thread(thread)

        assert "pending_hints" in data
        assert data["pending_hints"] == ["Manager hint"]

    def test_manager_deserialize_restores_hints(self, manager: DiscussionManager):
        """DiscussionManager.deserialize_thread restores pending_hints."""
        data = {
            "thread_id": "t1",
            "channel_id": "ch",
            "participants": ["a1", "a2"],
            "messages": [],
            "status": "active",
            "pending_hints": ["Deserialized hint"],
        }

        thread = manager.deserialize_thread(data, channel_id="ch")

        assert thread.pending_hints == ["Deserialized hint"]
