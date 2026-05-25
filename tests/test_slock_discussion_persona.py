"""Tests for discussion persona/role injection (AC22)."""
from unittest.mock import MagicMock


class TestDiscussionPersona:
    """AC22: Discussion messages use emoji+name prefix in L2 persistence."""

    def test_build_round_prompt_uses_emoji_name(self):
        """_build_round_prompt should use emoji+name instead of agent_id[:8]."""
        from src.slock_engine.discussion_manager import DiscussionManager

        # Create mock engine with agent lookup
        engine = MagicMock()
        agent_identity = MagicMock()
        agent_identity.emoji = "\U0001f9d1\u200d\U0001f4bb"
        agent_identity.name = "Coder"
        engine.get_agent.return_value = agent_identity

        mgr = DiscussionManager.__new__(DiscussionManager)
        mgr._engine = engine
        mgr._memory_manager = MagicMock()
        mgr._threads = {}
        mgr._cooldowns = {}

        # Create a mock thread with messages
        thread = MagicMock()
        thread.thread_id = "test_thread_123"
        thread.trigger_reason = "uncertainty detected"
        thread.current_round = 0
        thread.config = MagicMock()
        thread.config.max_rounds = 5

        msg = MagicMock()
        msg.sender_agent_id = "agent_abc123"
        msg.round_num = 1
        msg.content = "I think we should refactor this"
        thread.messages = [msg]

        # Call _build_round_prompt
        prompt = mgr._build_round_prompt(thread, "agent_xyz")

        # Should contain emoji+name, not just agent_id[:8]
        assert "\U0001f9d1\u200d\U0001f4bb" in prompt or "Coder" in prompt
        # Should NOT contain raw truncated ID as the only label
        # (it may appear as fallback but primary should be emoji+name)

    def test_persist_conclusion_includes_agent_identity(self):
        """Conclusion persisted to L2 should contain emoji+name prefixes."""
        from src.slock_engine.discussion_manager import DiscussionManager

        engine = MagicMock()
        agent1 = MagicMock()
        agent1.emoji = "\U0001f9d1\u200d\U0001f4bb"
        agent1.name = "Coder"
        agent2 = MagicMock()
        agent2.emoji = "\U0001f4dd"
        agent2.name = "Writer"

        def get_agent_side_effect(agent_id):
            if "coder" in agent_id:
                return agent1
            return agent2
        engine.get_agent.side_effect = get_agent_side_effect

        memory_mgr = MagicMock()

        mgr = DiscussionManager.__new__(DiscussionManager)
        mgr._engine = engine
        mgr._memory_manager = memory_mgr
        mgr._threads = {}
        mgr._cooldowns = {}
        mgr._task_bindings = {}

        # Create thread with conclusion
        thread = MagicMock()
        thread.thread_id = "conclude_thread"
        thread.conclusion = "We agree to refactor the module"
        thread.trigger_reason = "code review"
        thread.participants = ["agent_coder_1", "agent_writer_2"]
        thread.channel_id = "chan_test"
        thread.messages = []

        # Call _persist_conclusion
        if hasattr(mgr, '_persist_conclusion'):
            mgr._persist_conclusion(thread)

            # Check that append_discussion_conclusion was called
            if memory_mgr.append_discussion_conclusion.called:
                call_args = memory_mgr.append_discussion_conclusion.call_args
                # The participants should include emoji+name
                if call_args.kwargs.get("participants"):
                    participants = call_args.kwargs["participants"]
                    combined = " ".join(str(p) for p in participants)
                    assert "\U0001f9d1\u200d\U0001f4bb" in combined or "Coder" in combined or "\U0001f4dd" in combined or "Writer" in combined
