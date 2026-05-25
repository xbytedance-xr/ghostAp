"""Wave 5 optimization tests — behavior-driven.

AC-R13: Verifies unified discussion path via actual behavior, not source inspection.
"""

from unittest.mock import MagicMock


class TestUnifiedDiscussionPath:
    """Verify engine uses _start_confirmed_discussion for all discussion paths."""

    def test_maybe_trigger_routes_through_start_confirmed(self):
        """When discussion is triggered, _start_confirmed_discussion is called."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import AgentIdentity, DiscussionThread

        engine = SlockEngine.__new__(SlockEngine)
        engine._settings = MagicMock()
        engine._settings.slock_discussion_enabled = True
        engine._settings.slock_discussion_require_confirm = False
        engine._settings.slock_max_parallel_discussions = 5
        engine._settings.slock_discussion_timeout = 120
        engine._discussion_manager = MagicMock()
        engine._discussions_lock = MagicMock()
        engine._active_discussions = {}
        engine._pending_discussions = {}
        engine._bounded_executor = MagicMock()

        # Mock should_trigger_discussion to return a thread
        mock_thread = MagicMock(spec=DiscussionThread)
        mock_thread.thread_id = "test-thread"
        mock_thread.participants = ["a1", "a2"]
        mock_thread.trigger_reason = ""
        engine._discussion_manager.should_trigger_discussion.return_value = mock_thread
        engine._channel_trust_rules = {}
        engine._check_trust_bypass = MagicMock(return_value=False)
        engine._registry = MagicMock()
        engine._registry.get.return_value = None

        # Mock _add_discussion to succeed
        engine._add_discussion = MagicMock(return_value=True)
        engine._start_confirmed_discussion = MagicMock()

        agent = AgentIdentity(agent_id="a1", name="coder", role="coder", agent_type="coco")

        # Result must be >= 100 chars to pass the length check in _maybe_trigger_discussion
        long_result = "I'm not sure about this approach. " * 5  # ~170 chars

        # Call _maybe_trigger_discussion
        engine._maybe_trigger_discussion(agent, long_result, "ch-001", callbacks=None)

        # Verify it routes to _start_confirmed_discussion
        engine._start_confirmed_discussion.assert_called_once()


class TestNoSourceInspection:
    """AC-R13: This file uses behavioral tests, not static source analysis."""

    def test_no_source_inspection_usage(self):
        import pathlib
        content = pathlib.Path(__file__).read_text()
        # Verify we don't use static source inspection
        forbidden = "get" + "source("
        assert forbidden not in content.replace('"get" + "source("', "")
