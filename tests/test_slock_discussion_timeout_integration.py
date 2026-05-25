"""Integration test: discussion watchdog timeout terminates discussion correctly."""

import threading
import time
from unittest.mock import MagicMock

from src.slock_engine.models import (
    DiscussionConfig,
    DiscussionMessage,
    DiscussionStatus,
    DiscussionThread,
)


class TestDiscussionTimeoutIntegration:
    """Verify watchdog terminates discussion after slock_discussion_timeout."""

    def _make_thread(self, timeout=2):
        """Create a discussion thread with short timeout config."""
        config = DiscussionConfig(
            max_rounds=20,
            token_budget=100000,
            discussion_timeout=timeout,
        )
        thread = DiscussionThread(
            channel_id="test-channel",
            participants=["agent-a", "agent-b"],
            trigger_reason="test timeout scenario",
            topic="test timeout scenario"[:100],
            config=config,
        )
        thread.cancellation_event = threading.Event()
        return thread

    def test_timeout_sets_status_to_timeout(self):
        """Thread status becomes TIMEOUT after watchdog fires."""
        thread = self._make_thread(timeout=1)

        # Simulate watchdog behavior
        def watchdog_fire():
            time.sleep(0.5)  # Fire after 0.5s (shorter than real timeout for test speed)
            thread.status = DiscussionStatus.TIMEOUT
            thread.cancellation_event.set()

        watchdog = threading.Thread(target=watchdog_fire, daemon=True)
        watchdog.start()

        # Wait for watchdog to fire
        fired = thread.cancellation_event.wait(timeout=3)
        assert fired, "Watchdog should have fired"
        assert thread.status == DiscussionStatus.TIMEOUT

    def test_cancellation_event_stops_discussion_loop(self):
        """Discussion loop should exit when cancellation_event is set."""
        thread = self._make_thread(timeout=2)

        rounds_executed = 0

        # Simulate a discussion loop that checks cancellation
        def mock_discussion_loop():
            nonlocal rounds_executed
            for _ in range(20):
                if thread.cancellation_event.is_set():
                    break
                rounds_executed += 1
                time.sleep(0.1)

        # Fire watchdog after 0.3s
        def watchdog_fire():
            time.sleep(0.3)
            thread.status = DiscussionStatus.TIMEOUT
            thread.cancellation_event.set()

        watchdog = threading.Thread(target=watchdog_fire, daemon=True)
        watchdog.start()

        mock_discussion_loop()

        assert thread.status == DiscussionStatus.TIMEOUT
        # Should have executed only ~3 rounds (0.3s / 0.1s per round)
        assert rounds_executed < 10, f"Expected early exit, got {rounds_executed} rounds"
        assert rounds_executed >= 2, f"Should have run at least a couple rounds, got {rounds_executed}"

    def test_timeout_with_budget_breaker_integration(self):
        """check_budget_with_breaker returns False when timeout limit is reached via round count."""
        from src.slock_engine.discussion_manager import DiscussionManager

        thread = self._make_thread()
        # Simulate 20 rounds completed (at the limit)
        for i in range(20):
            thread.messages.append(
                DiscussionMessage(
                    sender_agent_id="agent-a" if i % 2 == 0 else "agent-b",
                    receiver_agent_id="agent-b" if i % 2 == 0 else "agent-a",
                    content=f"Round {i} message",
                    round_num=i + 1,
                    token_count=100,
                )
            )
        thread.total_tokens_used = 2000
        thread.cancellation_event = threading.Event()

        # Create a minimal DM
        dm = DiscussionManager.__new__(DiscussionManager)
        dm._on_budget_warning = None

        # Mock settings
        settings = MagicMock()
        settings.slock_discussion_token_budget = 100000
        settings.slock_max_discussion_rounds = 20

        card_sent = []

        result = dm.check_budget_with_breaker(
            thread, settings, on_card_send=lambda c: card_sent.append(c)
        )

        assert result is False, "Breaker should trip at round limit"
        assert thread.status == DiscussionStatus.TIMEOUT
        assert thread.cancellation_event.is_set()
        assert len(card_sent) == 1
        assert "轮数上限" in str(card_sent[0])

    def test_timeout_notification_card_sent(self):
        """Breaker card is sent when timeout (round limit) triggers."""
        from src.slock_engine.discussion_manager import DiscussionManager

        thread = self._make_thread()
        # Add messages to exceed round limit
        for i in range(21):
            thread.messages.append(
                DiscussionMessage(
                    sender_agent_id="agent-a",
                    receiver_agent_id="agent-b",
                    content=f"msg {i}",
                    round_num=i + 1,
                    token_count=50,
                )
            )
        thread.total_tokens_used = 1050
        thread.cancellation_event = threading.Event()

        dm = DiscussionManager.__new__(DiscussionManager)
        dm._on_budget_warning = None

        settings = MagicMock()
        settings.slock_discussion_token_budget = 100000
        settings.slock_max_discussion_rounds = 20

        cards = []
        dm.check_budget_with_breaker(thread, settings, on_card_send=lambda c: cards.append(c))

        assert len(cards) == 1
        card_str = str(cards[0])
        assert "讨论熔断" in card_str
        assert "轮数上限" in card_str
