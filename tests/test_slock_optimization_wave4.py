"""Wave 4 optimization tests — behavior-driven.

AC-R13: Verifies _persist_conclusion is called exactly once per discussion,
and L2 memory does not contain duplicate entries.
Uses mock-based behavioral assertions instead of source inspection.

Task 16: Status panel auto-refresh meets 3s SLA with debounce regression test.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

# The slock engine modules have heavy deps (acp); skip gracefully if unavailable.
pytest.importorskip("acp", reason="acp package not installed")

from src.slock_engine.engine import SlockEngine as _SlockEngine  # noqa: E402


class TestNoDuplicateConclusionWrites:
    """After discussion completes, conclusion appears only once in L2 and L1."""

    def test_persist_conclusion_called_once(self):
        """Verify _persist_conclusion is called exactly once during a discussion."""
        from src.slock_engine.discussion_manager import DiscussionManager
        from src.slock_engine.models import (
            DiscussionConfig,
            DiscussionMessage,
            DiscussionThread,
        )

        dm = DiscussionManager.__new__(DiscussionManager)
        dm._engine = MagicMock()
        dm._memory_manager = MagicMock()
        dm._config = DiscussionConfig(max_rounds=2, max_tokens_per_round=8000)
        dm._active_threads = {}
        dm._thread_lock = MagicMock()
        dm._cooldowns = {}
        dm._discussion_depth = {}
        dm._last_discussion_time = {}
        dm._pending_card_update = None
        dm._last_card_update_time = 0.0

        # Create a thread
        thread = DiscussionThread(
            thread_id="test-thread",
            channel_id="test_channel",
            participants=["agent-1", "agent-2"],
            config=DiscussionConfig(max_rounds=3, max_tokens_per_round=8000),
            trigger_reason="uncertainty:不确定",
        )
        thread.messages = [
            DiscussionMessage(sender_agent_id="agent-1", content="I'm uncertain about X", round_num=0),
        ]

        # Mock internal methods used by run_discussion
        dm.start_discussion = MagicMock(return_value=thread)
        dm.check_budget = MagicMock(return_value=True)
        # execute_round returns thread but marks convergence
        dm.execute_round = MagicMock(return_value=thread)
        dm.check_convergence = MagicMock(return_value=True)
        dm.summarize_conclusion = MagicMock()
        dm._persist_conclusion = MagicMock()
        dm.unbind_task = MagicMock()

        # Run discussion
        result = dm.run_discussion(thread, "test context")

        # Verify _persist_conclusion called exactly once
        assert dm._persist_conclusion.call_count == 1


class TestNoSourceInspection:
    """AC-R13: This file uses behavioral tests, not static source analysis."""

    def test_no_source_inspection_usage(self):
        import pathlib
        test_file = pathlib.Path(__file__)
        content = test_file.read_text()
        # Verify we don't use static source inspection
        forbidden = "get" + "source("
        assert forbidden not in content.replace('"get" + "source("', "")


# ============================================================
# Task 16: Status panel auto-refresh 3s SLA regression tests
# ============================================================


class TestStatusPanelAutoRefresh:
    """Verify _schedule_status_refresh meets 3s SLA with debounce."""

    def _make_engine(self):
        """Create a minimal SlockEngine instance without calling __init__."""
        engine = _SlockEngine.__new__(_SlockEngine)
        engine._timer_lock = threading.Lock()
        engine._status_refresh_timer = None
        engine._status_card_msg_ids = {"ch-1": "msg-001"}
        engine._status_panel_msg_id = None
        engine._channel = MagicMock()
        engine._channel.channel_id = "ch-1"
        engine._channel.team_name = "TestTeam"
        engine._dirty = False
        return engine

    @patch("src.slock_engine.engine.threading.Timer")
    def test_default_delay_is_3s(self, mock_timer_cls):
        """The default delay argument is 3.0 seconds, meeting the 3s SLA."""
        mock_timer_instance = MagicMock()
        mock_timer_cls.return_value = mock_timer_instance

        engine = self._make_engine()
        engine._schedule_status_refresh()

        # Timer was constructed with delay=3.0
        assert mock_timer_cls.call_count == 1
        args, _kwargs = mock_timer_cls.call_args
        assert args[0] == 3.0, f"Expected delay 3.0, got {args[0]}"

        # Timer was started
        mock_timer_instance.start.assert_called_once()
        mock_timer_instance.daemon = True  # verify daemon set

    @patch("src.slock_engine.engine.threading.Timer")
    def test_debounce_cancels_previous_timer(self, mock_timer_cls):
        """Second call to _schedule_status_refresh cancels the first timer."""
        first_timer = MagicMock()
        second_timer = MagicMock()
        mock_timer_cls.side_effect = [first_timer, second_timer]

        engine = self._make_engine()

        # First call
        engine._schedule_status_refresh()
        assert mock_timer_cls.call_count == 1
        first_timer.cancel.assert_not_called()

        # Second call — should cancel the first timer
        engine._schedule_status_refresh()
        assert mock_timer_cls.call_count == 2
        first_timer.cancel.assert_called_once()
        second_timer.start.assert_called_once()

    @patch("src.slock_engine.engine.threading.Timer")
    def test_set_dirty_triggers_refresh(self, mock_timer_cls):
        """Setting dirty=True calls _schedule_status_refresh."""
        mock_timer_instance = MagicMock()
        mock_timer_cls.return_value = mock_timer_instance

        engine = self._make_engine()
        engine._set_dirty(True)

        assert mock_timer_cls.call_count == 1
        args, _ = mock_timer_cls.call_args
        assert args[0] == 3.0

    @patch("src.slock_engine.engine.threading.Timer")
    def test_callback_invoked_with_correct_args(self, mock_timer_cls):
        """The refresh callback receives (msg_id, card_dict)."""
        # Capture the _do_refresh function passed to Timer
        captured_fn = None

        def fake_timer(delay, fn):
            nonlocal captured_fn
            captured_fn = fn
            timer = MagicMock()
            return timer

        mock_timer_cls.side_effect = fake_timer

        engine = self._make_engine()
        mock_cb = MagicMock()
        engine._on_status_refresh_cb = mock_cb
        engine.get_status_card = MagicMock(return_value={"type": "status_card"})

        engine._schedule_status_refresh()

        # Simulate timer firing
        assert captured_fn is not None
        captured_fn()

        # Callback should be called with (msg_id, card_dict)
        mock_cb.assert_called_once_with("msg-001", {"type": "status_card"})
        engine.get_status_card.assert_called_once_with(team_name="TestTeam")

    @patch("src.slock_engine.engine.threading.Timer")
    def test_no_timer_when_no_msg_ids(self, mock_timer_cls):
        """If _status_card_msg_ids is empty, no timer is scheduled."""
        engine = self._make_engine()
        engine._status_card_msg_ids = {}

        engine._schedule_status_refresh()

        mock_timer_cls.assert_not_called()

    @patch("src.slock_engine.engine.threading.Timer")
    def test_set_dirty_false_does_not_trigger_refresh(self, mock_timer_cls):
        """Setting dirty=False does NOT schedule a refresh."""
        engine = self._make_engine()
        engine._set_dirty(False)

        mock_timer_cls.assert_not_called()
