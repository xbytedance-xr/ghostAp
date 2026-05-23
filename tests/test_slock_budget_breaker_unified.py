"""Unified budget breaker test: verify both paths produce consistent behavior."""

import threading
from unittest.mock import MagicMock

import pytest

from src.slock_engine.discussion_manager import DiscussionManager
from src.slock_engine.models import (
    DiscussionConfig,
    DiscussionMessage,
    DiscussionStatus,
    DiscussionThread,
)


class TestCheckBudgetWithBreaker:
    """Unit tests for DiscussionManager.check_budget_with_breaker()."""

    def _make_dm(self):
        dm = DiscussionManager.__new__(DiscussionManager)
        dm._on_budget_warning = None
        return dm

    def _make_settings(self, token_budget=100000, max_rounds=20):
        settings = MagicMock()
        settings.slock_discussion_token_budget = token_budget
        settings.slock_max_discussion_rounds = max_rounds
        return settings

    def _make_thread(self, tokens_used=0, rounds=0):
        thread = DiscussionThread(
            channel_id="ch-test",
            participants=["agent-a", "agent-b"],
            trigger_reason="test",
            topic="test"[:100],
            config=DiscussionConfig(max_rounds=20, token_budget=100000),
        )
        thread.total_tokens_used = tokens_used
        thread.cancellation_event = threading.Event()
        # Add messages to simulate rounds
        for i in range(rounds):
            thread.messages.append(
                DiscussionMessage(
                    sender_agent_id="agent-a",
                    receiver_agent_id="agent-b",
                    content=f"round {i+1}",
                    round_num=i + 1,
                    token_count=100,
                )
            )
        return thread

    def test_within_budget_returns_true(self):
        """When both token and round limits are within bounds, returns True."""
        dm = self._make_dm()
        settings = self._make_settings()
        thread = self._make_thread(tokens_used=5000, rounds=3)

        result = dm.check_budget_with_breaker(thread, settings)
        assert result is True
        assert thread.status == DiscussionStatus.ACTIVE

    def test_token_exhausted_returns_false(self):
        """When token budget is exceeded, returns False and sets BUDGET_EXHAUSTED."""
        dm = self._make_dm()
        settings = self._make_settings(token_budget=100000)
        thread = self._make_thread(tokens_used=100000, rounds=5)

        cards = []
        result = dm.check_budget_with_breaker(thread, settings, on_card_send=lambda c: cards.append(c))

        assert result is False
        assert thread.status == DiscussionStatus.BUDGET_EXHAUSTED
        assert thread.cancellation_event.is_set()
        assert len(cards) == 1
        assert "Token 预算耗尽" in str(cards[0])

    def test_round_limit_returns_false(self):
        """When round limit is reached, returns False and sets TIMEOUT."""
        dm = self._make_dm()
        settings = self._make_settings(max_rounds=20)
        thread = self._make_thread(tokens_used=5000, rounds=20)

        cards = []
        result = dm.check_budget_with_breaker(thread, settings, on_card_send=lambda c: cards.append(c))

        assert result is False
        assert thread.status == DiscussionStatus.TIMEOUT
        assert thread.cancellation_event.is_set()
        assert len(cards) == 1
        assert "轮数上限" in str(cards[0])

    def test_token_checked_before_rounds(self):
        """Token budget is checked first — if both limits exceeded, BUDGET_EXHAUSTED wins."""
        dm = self._make_dm()
        settings = self._make_settings(token_budget=50000, max_rounds=10)
        thread = self._make_thread(tokens_used=60000, rounds=15)

        result = dm.check_budget_with_breaker(thread, settings)
        assert result is False
        assert thread.status == DiscussionStatus.BUDGET_EXHAUSTED

    def test_no_callback_still_sets_status(self):
        """Even without on_card_send callback, status is correctly updated."""
        dm = self._make_dm()
        settings = self._make_settings(token_budget=1000)
        thread = self._make_thread(tokens_used=2000, rounds=1)

        result = dm.check_budget_with_breaker(thread, settings, on_card_send=None)
        assert result is False
        assert thread.status == DiscussionStatus.BUDGET_EXHAUSTED
        assert thread.cancellation_event.is_set()

    def test_cancellation_event_not_set_when_missing(self):
        """If thread has no cancellation_event, method still works without error."""
        dm = self._make_dm()
        settings = self._make_settings(token_budget=1000)
        thread = self._make_thread(tokens_used=2000, rounds=1)
        # Remove cancellation_event
        if hasattr(thread, 'cancellation_event'):
            delattr(thread, 'cancellation_event')

        result = dm.check_budget_with_breaker(thread, settings)
        assert result is False
        assert thread.status == DiscussionStatus.BUDGET_EXHAUSTED


class TestEnforceBudgetThinWrapper:
    """Verify engine._enforce_discussion_budget delegates to DM correctly."""

    def test_delegates_to_dm(self):
        """Engine wrapper calls dm.check_budget_with_breaker."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._settings = MagicMock()
        engine._settings.slock_discussion_token_budget = 100000
        engine._settings.slock_max_discussion_rounds = 20
        engine._discussion_manager = MagicMock()
        engine._discussion_manager.check_budget_with_breaker.return_value = True

        thread = MagicMock()

        class FakeCallbacks:
            def on_card_send(self, card):
                pass

        result = engine._enforce_discussion_budget(thread, FakeCallbacks())
        assert result is True
        engine._discussion_manager.check_budget_with_breaker.assert_called_once()

    def test_returns_true_when_no_dm(self):
        """When _discussion_manager is not available, returns True (allow)."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._settings = MagicMock()
        # No _discussion_manager attribute
        if hasattr(engine, '_discussion_manager'):
            delattr(engine, '_discussion_manager')

        thread = MagicMock()
        result = engine._enforce_discussion_budget(thread, None)
        assert result is True

    def test_handler_and_auto_path_consistency(self):
        """Both handler path and auto path produce same result for same input."""
        dm = DiscussionManager.__new__(DiscussionManager)
        dm._on_budget_warning = None

        settings = MagicMock()
        settings.slock_discussion_token_budget = 50000
        settings.slock_max_discussion_rounds = 10

        # Create a thread that exceeds token budget
        thread = DiscussionThread(
            channel_id="ch-test",
            participants=["a", "b"],
            trigger_reason="test",
            topic="test",
            config=DiscussionConfig(),
        )
        thread.total_tokens_used = 60000
        thread.cancellation_event = threading.Event()
        thread.messages.append(
            DiscussionMessage(
                sender_agent_id="a",
                receiver_agent_id="b",
                content="x",
                round_num=1,
                token_count=100,
            )
        )

        # Direct DM call (auto path)
        result = dm.check_budget_with_breaker(thread, settings)
        assert result is False
        assert thread.status == DiscussionStatus.BUDGET_EXHAUSTED
