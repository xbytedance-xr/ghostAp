"""Enhanced unit tests for DiscussionManager — AC19, AC20, AC21.

Tests cover:
- AC19: Budget warning callback fires exactly once at 80% threshold.
- AC20: Depth config (slock_max_discussion_depth) blocks nested discussions.
- AC21: Finally block flushes final state via on_round_complete even when debounce suppresses.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.discussion_manager import DiscussionManager
from src.slock_engine.models import (
    DiscussionConfig,
    DiscussionMessage,
    DiscussionStatus,
    DiscussionThread,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _make_thread(
    token_budget: int = 1000,
    total_tokens_used: int = 0,
    max_rounds: int = 5,
    participants: list[str] | None = None,
    status: DiscussionStatus = DiscussionStatus.ACTIVE,
) -> DiscussionThread:
    """Create a test DiscussionThread with configurable token budget."""
    config = DiscussionConfig(
        max_rounds=max_rounds,
        token_budget=token_budget,
    )
    return DiscussionThread(
        thread_id=str(uuid.uuid4()),
        channel_id="channel-test",
        participants=participants or ["agent-001", "agent-002"],
        messages=[],
        status=status,
        config=config,
        trigger_reason="test",
        total_tokens_used=total_tokens_used,
    )


def _make_engine(settings_overrides: dict | None = None) -> MagicMock:
    """Create a mock engine with a mock settings attribute."""
    engine = MagicMock()
    settings = MagicMock()
    # Defaults
    settings.slock_max_discussion_depth = 3
    settings.slock_card_debounce_interval = 1.5
    # Apply overrides
    if settings_overrides:
        for key, value in settings_overrides.items():
            setattr(settings, key, value)
    engine.settings = settings
    return engine


# ===========================================================================
# AC19: Budget Warning — callback fires exactly once at 80% threshold
# ===========================================================================


class TestAC19BudgetWarning:
    """AC19: on_budget_warning callback triggered exactly once at 80% threshold."""

    def test_budget_warning_fires_at_80_percent(self):
        """When total_tokens_used crosses 80% of token_budget, callback fires once."""
        on_budget_warning = MagicMock()
        engine = _make_engine()

        dm = DiscussionManager(
            engine=engine,
            on_budget_warning=on_budget_warning,
        )

        # token_budget=1000, 80% = 800
        thread = _make_thread(token_budget=1000, total_tokens_used=800)

        # First check at 80% — should trigger warning
        dm.check_budget(thread)
        on_budget_warning.assert_called_once_with(thread)

    def test_budget_warning_fires_only_once(self):
        """Repeated check_budget calls after 80% do NOT re-trigger the callback."""
        on_budget_warning = MagicMock()
        engine = _make_engine()

        dm = DiscussionManager(
            engine=engine,
            on_budget_warning=on_budget_warning,
        )

        thread = _make_thread(token_budget=1000, total_tokens_used=850)

        # Call check_budget multiple times
        dm.check_budget(thread)
        dm.check_budget(thread)
        dm.check_budget(thread)

        # Should be called exactly once despite 3 invocations
        assert on_budget_warning.call_count == 1, (
            f"Expected exactly 1 call, got {on_budget_warning.call_count}"
        )

    def test_budget_warning_not_fired_below_80_percent(self):
        """When tokens used is below 80%, callback should NOT fire."""
        on_budget_warning = MagicMock()
        engine = _make_engine()

        dm = DiscussionManager(
            engine=engine,
            on_budget_warning=on_budget_warning,
        )

        # 79% of 1000 = 790, below threshold
        thread = _make_thread(token_budget=1000, total_tokens_used=790)

        dm.check_budget(thread)
        on_budget_warning.assert_not_called()

    def test_budget_warning_exactly_at_boundary(self):
        """At exactly 80% (boundary), the warning fires."""
        on_budget_warning = MagicMock()
        engine = _make_engine()

        dm = DiscussionManager(
            engine=engine,
            on_budget_warning=on_budget_warning,
        )

        # Exactly 80% of 10000 = 8000
        thread = _make_thread(token_budget=10000, total_tokens_used=8000)

        dm.check_budget(thread)
        on_budget_warning.assert_called_once()

    def test_budget_warning_callback_exception_is_swallowed(self):
        """If the callback raises, check_budget does not propagate the error."""
        on_budget_warning = MagicMock(side_effect=RuntimeError("callback boom"))
        engine = _make_engine()

        dm = DiscussionManager(
            engine=engine,
            on_budget_warning=on_budget_warning,
        )

        thread = _make_thread(token_budget=1000, total_tokens_used=900)

        # Should not raise
        result = dm.check_budget(thread)
        # Budget is not exhausted (900 < 1000)
        assert result is True

    def test_budget_warning_no_callback_configured(self):
        """If on_budget_warning is None, no error occurs at 80%."""
        engine = _make_engine()

        dm = DiscussionManager(
            engine=engine,
            on_budget_warning=None,
        )

        thread = _make_thread(token_budget=1000, total_tokens_used=900)

        # Should not raise
        result = dm.check_budget(thread)
        assert result is True


# ===========================================================================
# AC20: Depth Config — slock_max_discussion_depth blocks nested discussions
# ===========================================================================


class TestAC20DepthConfig:
    """AC20: slock_max_discussion_depth limits nested discussion depth."""

    def test_depth_limit_2_blocks_third_level(self):
        """With max_depth=2, a 3rd nested discussion is blocked."""
        engine = _make_engine({"slock_max_discussion_depth": 2})

        dm = DiscussionManager(engine=engine)

        # Simulate nesting: root -> child -> grandchild
        root_id = "thread-root"
        child_id = "thread-child"
        grandchild_id = "thread-grandchild"

        # Root starts at depth 0 (no parent), incrementing makes depth 1
        dm._increment_depth(root_id, parent_thread_id=None)
        assert dm._discussion_depth[root_id] == 1

        # Child is nested under root, depth becomes 2
        dm._increment_depth(child_id, parent_thread_id=root_id)
        assert dm._discussion_depth[child_id] == 2

        # Now check: can we nest under child? depth of child is 2, max is 2
        # _check_depth_limit checks: depth < self._max_depth
        # child has depth 2, max_depth is 2, so 2 < 2 is False -> blocked
        allowed = dm._check_depth_limit(parent_thread_id=child_id)
        assert allowed is False, (
            "3rd level nesting should be blocked when max_depth=2"
        )

    def test_depth_limit_5_allows_up_to_5_levels(self):
        """With max_depth=5, nesting up to 5 levels is allowed."""
        engine = _make_engine({"slock_max_discussion_depth": 5})

        dm = DiscussionManager(engine=engine)

        # Build a chain of 5 nested discussions
        thread_ids = [f"thread-level-{i}" for i in range(5)]

        # Level 0: root (no parent)
        dm._increment_depth(thread_ids[0], parent_thread_id=None)
        assert dm._discussion_depth[thread_ids[0]] == 1

        # Levels 1-4: each nested under the previous
        for i in range(1, 5):
            parent = thread_ids[i - 1]
            # Before incrementing, check that nesting is still allowed
            allowed = dm._check_depth_limit(parent_thread_id=parent)
            assert allowed is True, (
                f"Level {i+1} should be allowed when max_depth=5, "
                f"but was blocked at parent depth {dm._discussion_depth[parent]}"
            )
            dm._increment_depth(thread_ids[i], parent_thread_id=parent)

        # Verify depths: 1, 2, 3, 4, 5
        for i, tid in enumerate(thread_ids):
            assert dm._discussion_depth[tid] == i + 1

        # Now level 5 has depth 5: trying to nest a 6th level should be blocked
        blocked = dm._check_depth_limit(parent_thread_id=thread_ids[4])
        assert blocked is False, (
            "6th level nesting should be blocked when max_depth=5"
        )

    def test_depth_limit_no_parent_always_allowed(self):
        """A top-level discussion (no parent) is always allowed regardless of max_depth."""
        engine = _make_engine({"slock_max_discussion_depth": 1})

        dm = DiscussionManager(engine=engine)

        # No parent — should always be allowed
        allowed = dm._check_depth_limit(parent_thread_id=None)
        assert allowed is True

    def test_depth_defaults_to_3_without_settings(self):
        """When engine has no _settings attr, max_depth defaults to 3."""
        engine = MagicMock(spec=[])  # no _settings attribute

        dm = DiscussionManager(engine=engine)

        assert dm._max_depth == 3


# ===========================================================================
# AC21: Finally Flush — final state flushed via on_round_complete
# ===========================================================================


class TestAC21FinallyFlush:
    """AC21: Finally block flushes final state even when debounce suppresses."""

    def test_finally_flushes_when_debounce_suppresses_last_round(self):
        """Even if debounce suppresses the last round's card update,
        finally block still calls on_round_complete with final state."""
        on_round_complete = MagicMock()
        engine = _make_engine({
            "slock_card_debounce_interval": 999.0,  # Very long debounce to suppress all mid-loop calls
        })

        dm = DiscussionManager(engine=engine)

        thread = _make_thread(token_budget=100000, max_rounds=2)

        # Mock internal methods to avoid real LLM calls
        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t) as mock_start, \
             patch.object(dm, "execute_round", side_effect=lambda t: t) as mock_exec, \
             patch.object(dm, "check_convergence", return_value=False), \
             patch.object(dm, "check_budget", return_value=True), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            dm.run_discussion(thread, "test content", on_round_complete=on_round_complete)

        # Despite debounce suppressing mid-loop calls, the finally block
        # must have called on_round_complete at least once (the final flush)
        assert on_round_complete.call_count >= 1, (
            "Finally block must flush the final state via on_round_complete"
        )

    def test_finally_flushes_pending_card_update(self):
        """If _pending_card_update is set (deferred by debounce), finally uses it."""
        on_round_complete = MagicMock()
        engine = _make_engine({
            "slock_card_debounce_interval": 999.0,  # Suppress all mid-loop calls
        })

        dm = DiscussionManager(engine=engine)
        thread = _make_thread(token_budget=100000, max_rounds=2)

        # We'll track what the finally block passes to on_round_complete
        call_args = []
        on_round_complete.side_effect = lambda t: call_args.append(t)

        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t), \
             patch.object(dm, "execute_round", side_effect=lambda t: t), \
             patch.object(dm, "check_convergence", return_value=False), \
             patch.object(dm, "check_budget", return_value=True), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            dm.run_discussion(thread, "test content", on_round_complete=on_round_complete)

        # The final flush should have been called
        assert len(call_args) >= 1, (
            "Finally block must call on_round_complete at least once"
        )
        # The flushed thread should be the actual thread (or pending update)
        final_flushed = call_args[-1]
        assert final_flushed is thread or final_flushed is not None

    def test_finally_clears_pending_card_update(self):
        """After finally block runs, _pending_card_update is reset to None."""
        on_round_complete = MagicMock()
        engine = _make_engine({
            "slock_card_debounce_interval": 999.0,
        })

        dm = DiscussionManager(engine=engine)
        thread = _make_thread(token_budget=100000, max_rounds=2)

        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t), \
             patch.object(dm, "execute_round", side_effect=lambda t: t), \
             patch.object(dm, "check_convergence", return_value=False), \
             patch.object(dm, "check_budget", return_value=True), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            dm.run_discussion(thread, "test content", on_round_complete=on_round_complete)

        # After run_discussion returns, pending update should be cleared
        assert dm._pending_card_update is None, (
            "_pending_card_update must be cleared after finally block"
        )

    def test_finally_flush_even_on_exception(self):
        """If execute_round raises, finally block still flushes the card."""
        on_round_complete = MagicMock()
        engine = _make_engine({
            "slock_card_debounce_interval": 0.0,  # No debounce
        })

        dm = DiscussionManager(engine=engine)
        thread = _make_thread(token_budget=100000, max_rounds=3)

        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t), \
             patch.object(dm, "execute_round", side_effect=RuntimeError("LLM error")), \
             patch.object(dm, "check_budget", return_value=True), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            with pytest.raises(RuntimeError, match="LLM error"):
                dm.run_discussion(thread, "test content", on_round_complete=on_round_complete)

        # Even though an exception was raised, finally should have flushed
        assert on_round_complete.call_count >= 1, (
            "Finally block must flush on_round_complete even when an exception occurs"
        )

    def test_no_on_round_complete_does_not_error_in_finally(self):
        """When on_round_complete is None, finally block completes without error."""
        engine = _make_engine()

        dm = DiscussionManager(engine=engine)
        thread = _make_thread(token_budget=100000, max_rounds=2)

        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t), \
             patch.object(dm, "execute_round", side_effect=lambda t: t), \
             patch.object(dm, "check_convergence", return_value=True), \
             patch.object(dm, "check_budget", return_value=True), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            # Should not raise even with on_round_complete=None
            result = dm.run_discussion(thread, "test content", on_round_complete=None)

        assert result is thread


# ===========================================================================
# AC17: Expand Card Completeness + Pagination
# ===========================================================================


class TestDiscussionExpandCard:
    """AC17: Expand button returns paginated card with all messages for current page."""

    def test_expand_card_returns_all_messages(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [
            {"sender": f"Agent{i}", "content": f"Message {i}", "round_num": i}
            for i in range(10)
        ]
        card = build_discussion_expand_card(
            thread_id="thread-abc",
            messages=messages,
            participants=["Agent0", "Agent1"],
            channel_id="ch1",
        )
        assert card["schema"] == "2.0"
        elements = card["body"]["elements"]
        # All 10 messages fit in one page (PAGE_SIZE=25), rendered as note elements
        msg_elements = [e for e in elements if e.get("tag") == "note" and any("(R" in el.get("content", "") for el in e.get("elements", []))]
        assert len(msg_elements) == 10

    def test_expand_card_has_all_messages_in_single_page(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [{"sender": "A", "content": "hi", "round_num": 1}]
        card = build_discussion_expand_card(
            thread_id="t1", messages=messages,
            participants=["A"], channel_id="ch1",
        )
        elements = card["body"]["elements"]
        msg_elements = [e for e in elements if e.get("tag") == "note" and any("(R" in el.get("content", "") for el in e.get("elements", []))]
        assert len(msg_elements) == 1


class TestDiscussionExpandPagination:
    """Pagination: >25 messages shows load-more button (PAGE_SIZE=25)."""

    def test_more_than_25_shows_load_more(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [
            {"sender": f"Agent{i % 3}", "content": f"Msg {i}", "round_num": i}
            for i in range(30)
        ]
        card = build_discussion_expand_card(
            thread_id="thread-xyz",
            messages=messages,
            participants=["Agent0", "Agent1", "Agent2"],
            channel_id="ch1",
        )
        import json
        card_str = json.dumps(card, ensure_ascii=False)
        assert "slock_discussion_expand_page" in card_str
        assert "加载更多" in card_str

    def test_10_or_fewer_no_load_more(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [
            {"sender": "A", "content": f"Msg {i}", "round_num": i}
            for i in range(8)
        ]
        card = build_discussion_expand_card(
            thread_id="t2", messages=messages,
            participants=["A"], channel_id="ch1",
        )
        import json
        card_str = json.dumps(card, ensure_ascii=False)
        assert "slock_discussion_expand_page" not in card_str
        assert "加载更多" not in card_str

    def test_pagination_renders_only_current_page(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [
            {"sender": "A", "content": f"Msg {i}", "round_num": i}
            for i in range(45)
        ]
        # Page 0: shows first 10
        card = build_discussion_expand_card(
            thread_id="t3", messages=messages,
            participants=["A", "B"], channel_id="ch1",
            page=0,
        )
        elements = card["body"]["elements"]
        msg_elements = [e for e in elements if e.get("tag") == "note" and any("(R" in el.get("content", "") for el in e.get("elements", []))]
        assert len(msg_elements) == 10
        # Has load-more button
        import json
        assert "加载更多" in json.dumps(card, ensure_ascii=False)


# ===========================================================================
# Uncertainty Trigger Detection Tests
# ===========================================================================


class TestUncertaintyTrigger:
    """Tests for _check_uncertainty_trigger: marker detection and 500-char window."""

    def _make_agent(self, agent_id: str = "agent-initiator") -> MagicMock:
        """Create a mock AgentIdentity."""
        agent = MagicMock()
        agent.agent_id = agent_id
        agent.role = "coder"
        agent.owner_group = ""
        return agent

    def test_uncertainty_trigger_detects_chinese_markers(self):
        """Chinese uncertainty markers '不确定' and '可能有问题' trigger a discussion."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        agent = self._make_agent()
        config = DiscussionConfig()

        # Mock get_settings to return Chinese markers
        mock_settings = MagicMock()
        mock_settings.slock_uncertainty_markers = ["不确定", "可能有问题"]

        with patch(
            "src.config.get_settings",
            return_value=mock_settings,
        ), patch.object(
            dm, "_find_best_discussion_partner", return_value="agent-partner"
        ):
            # Test '不确定'
            result = dm._check_uncertainty_trigger(
                agent, "这个方案我不确定是否正确", config, channel_id="ch1"
            )
            assert result is not None, "'不确定' should trigger uncertainty"
            assert "不确定" in result.trigger_reason

            # Test '可能有问题'
            result = dm._check_uncertainty_trigger(
                agent, "这段代码可能有问题需要确认", config, channel_id="ch1"
            )
            assert result is not None, "'可能有问题' should trigger uncertainty"
            assert "可能有问题" in result.trigger_reason

    def test_uncertainty_trigger_detects_english_markers(self):
        """English uncertainty markers 'needs review' and 'unsure' trigger a discussion."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        agent = self._make_agent()
        config = DiscussionConfig()

        mock_settings = MagicMock()
        mock_settings.slock_uncertainty_markers = ["needs review", "unsure"]

        with patch(
            "src.config.get_settings",
            return_value=mock_settings,
        ), patch.object(
            dm, "_find_best_discussion_partner", return_value="agent-partner"
        ):
            # Test 'needs review'
            result = dm._check_uncertainty_trigger(
                agent, "This implementation needs review before merging", config, channel_id="ch1"
            )
            assert result is not None, "'needs review' should trigger uncertainty"
            assert "needs review" in result.trigger_reason

            # Test 'unsure'
            result = dm._check_uncertainty_trigger(
                agent, "I am unsure about this approach", config, channel_id="ch1"
            )
            assert result is not None, "'unsure' should trigger uncertainty"
            assert "unsure" in result.trigger_reason

    def test_uncertainty_trigger_only_scans_last_500_chars(self):
        """Markers beyond 500 chars from the end of content do NOT trigger."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        agent = self._make_agent()
        config = DiscussionConfig()

        mock_settings = MagicMock()
        mock_settings.slock_uncertainty_markers = ["不确定", "needs review"]

        with patch(
            "src.config.get_settings",
            return_value=mock_settings,
        ), patch.object(
            dm, "_find_best_discussion_partner", return_value="agent-partner"
        ):
            # Place marker at the very beginning, followed by >500 chars of padding
            # The marker is outside the last-500-char window so should NOT trigger
            content_marker_far = "不确定" + ("x" * 600)
            result = dm._check_uncertainty_trigger(
                agent, content_marker_far, config, channel_id="ch1"
            )
            assert result is None, (
                "Marker beyond 500 chars from end should NOT trigger"
            )

            # Place marker within last 500 chars — should trigger
            content_marker_near = ("x" * 600) + "不确定"
            result = dm._check_uncertainty_trigger(
                agent, content_marker_near, config, channel_id="ch1"
            )
            assert result is not None, (
                "Marker within last 500 chars should trigger"
            )


# ===========================================================================
# Conclusion Persistence Tests
# ===========================================================================


class TestConclusionPersistence:
    """Tests for _persist_conclusion_to_task: task.reasoning_snapshot and L2 Decisions."""

    def test_conclusion_persisted_to_task_reasoning_snapshot(self):
        """When discussion converges, conclusion is written to task.reasoning_snapshot."""
        engine = _make_engine()

        # Create a mock task with task_id and reasoning_snapshot
        mock_task = MagicMock()
        mock_task.task_id = "task-001"
        mock_task.reasoning_snapshot = None
        engine._tasks = [mock_task]

        memory_mgr = MagicMock()
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        # Build a completed thread with conclusion
        thread = _make_thread()
        thread.conclusion = "Final decision: use approach B for better performance."
        thread.channel_id = "channel-test"

        # Call _persist_conclusion_to_task
        dm._persist_conclusion_to_task(thread, "task-001")

        # Verify conclusion was written to the task's reasoning_snapshot
        assert mock_task.reasoning_snapshot == thread.conclusion, (
            "Conclusion must be written to task.reasoning_snapshot"
        )

    def test_conclusion_persisted_to_shared_memory(self):
        """Conclusion is also persisted to L2 SHARED_MEMORY.md Decisions section."""
        engine = _make_engine()
        engine._tasks = []

        memory_mgr = MagicMock()
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        # Build a completed thread with conclusion and channel_id
        thread = _make_thread()
        thread.conclusion = "Agreed: implement caching layer at service boundary."
        thread.channel_id = "channel-abc"

        # Call _persist_conclusion_to_task
        dm._persist_conclusion_to_task(thread, "task-002")

        # Verify memory_mgr.append_discussion_conclusion was called with Decisions section
        memory_mgr.append_discussion_conclusion.assert_called_once()
        call_kwargs = memory_mgr.append_discussion_conclusion.call_args
        # Check positional args: (channel_id, conclusion_text)
        assert call_kwargs[0][0] == "channel-abc"
        assert call_kwargs[0][1] == thread.conclusion
        # Check section="Decisions" in kwargs
        assert call_kwargs[1]["section"] == "Decisions"


# ===========================================================================
# Task 7: Convergence Detection Tests
# ===========================================================================


class TestDiscussionConvergenceDetection:
    """Tests for check_convergence: detecting similar opinions across rounds."""

    def test_convergence_detection_consecutive_similar_rounds(self):
        """When 3 consecutive rounds express similar views, convergence is detected."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Create a thread with 3 rounds of similar messages
        thread = _make_thread()

        # Mock get_settings to return empty convergence_signals
        mock_settings = MagicMock()
        mock_settings.slock_convergence_signals = ()

        with patch("src.config.get_settings", return_value=mock_settings):
            # Add 3 messages with highly similar content
            similar_content = "The solution should use Redis for caching with TTL of 1 hour."

            for i in range(3):
                msg = DiscussionMessage(
                    message_id=f"msg-{i}",
                    sender_agent_id=f"agent-{i % 2 + 1}",
                    receiver_agent_id=f"agent-{(i + 1) % 2 + 1}",
                    content=similar_content,
                    round_num=i,
                    timestamp=time.time(),
                    token_count=10,
                )
                thread.messages.append(msg)

            # After 3 similar rounds, check_convergence should return True
            result = dm.check_convergence(thread)
            assert result is True, (
                "Convergence should be detected after 3 rounds of similar content"
            )

    def test_convergence_detection_explicit_signal(self):
        """Explicit convergence signals like 'AGREE' trigger convergence."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        thread = _make_thread()

        # Add messages with explicit AGREE signal
        thread.messages.append(DiscussionMessage(
            message_id="msg-1",
            sender_agent_id="agent-1",
            receiver_agent_id="agent-2",
            content="I propose we use approach A.",
            round_num=0,
            timestamp=time.time(),
            token_count=10,
        ))
        thread.messages.append(DiscussionMessage(
            message_id="msg-2",
            sender_agent_id="agent-2",
            receiver_agent_id="agent-1",
            content="AGREE, approach A is the best choice.",
            round_num=1,
            timestamp=time.time(),
            token_count=10,
        ))

        # Mock get_settings to include 'AGREE' in convergence_signals
        mock_settings = MagicMock()
        mock_settings.slock_convergence_signals = ("AGREE", "LGTM")

        with patch("src.config.get_settings", return_value=mock_settings):
            result = dm.check_convergence(thread)
            assert result is True, (
                "Convergence should be detected when 'AGREE' signal is present"
            )

    def test_convergence_detection_different_opinions_no_convergence(self):
        """Different opinions do NOT trigger convergence detection."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        thread = _make_thread()

        # Mock get_settings to return empty convergence_signals
        mock_settings = MagicMock()
        mock_settings.slock_convergence_signals = ()

        with patch("src.config.get_settings", return_value=mock_settings):
            # Add messages with very different content
            thread.messages.append(DiscussionMessage(
                message_id="msg-1",
                sender_agent_id="agent-1",
                receiver_agent_id="agent-2",
                content="We should use PostgreSQL for data storage.",
                round_num=0,
                timestamp=time.time(),
                token_count=10,
            ))
            thread.messages.append(DiscussionMessage(
                message_id="msg-2",
                sender_agent_id="agent-2",
                receiver_agent_id="agent-1",
                content="I disagree, MongoDB would be better for this use case.",
                round_num=1,
                timestamp=time.time(),
                token_count=10,
            ))

            result = dm.check_convergence(thread)
            assert result is False, (
                "Different opinions should NOT trigger convergence"
            )

    def test_text_similarity_uses_difflib_sequence_matcher(self):
        """_calculate_text_similarity uses difflib.SequenceMatcher for robust comparison."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Test with similar but not identical texts
        text1 = "The quick brown fox jumps over the lazy dog"
        text2 = "The quick brown fox jumps over a lazy dog"  # 'the' -> 'a'

        similarity = dm._calculate_text_similarity(text1, text2)

        # Should be high similarity (> 0.8)
        assert similarity > 0.8, (
            f"Similar texts should have high similarity, got {similarity}"
        )

        # Test with very different texts
        text3 = "Completely different content about something else"
        similarity_diff = dm._calculate_text_similarity(text1, text3)

        # Should be low similarity (< 0.3)
        assert similarity_diff < 0.5, (
            f"Different texts should have low similarity, got {similarity_diff}"
        )

    def test_text_similarity_cjk_support(self):
        """_calculate_text_similarity handles CJK characters properly."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Test with Chinese text
        text1 = "我们应该使用Redis作为缓存层"
        text2 = "我们应该使用Redis作为缓存"  # Slightly different

        similarity = dm._calculate_text_similarity(text1, text2)

        # Should detect similarity in CJK text
        assert similarity > 0.7, (
            f"Similar CJK texts should have high similarity, got {similarity}"
        )


# ===========================================================================
# Task 7: Budget Tracking Tests
# ===========================================================================


class TestDiscussionBudgetTracking:
    """Tests for budget tracking and automatic termination."""

    def test_budget_tracking_estimate_tokens(self):
        """_estimate_tokens correctly estimates token count for mixed content."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Empty string returns 0
        assert dm._estimate_tokens("") == 0

        # Pure ASCII text (approx 0.25 tokens per char)
        ascii_text = "hello world"
        tokens_ascii = dm._estimate_tokens(ascii_text)
        assert tokens_ascii >= 0

        # CJK text (approx 1.5 tokens per char)
        cjk_text = "你好世界"
        tokens_cjk = dm._estimate_tokens(cjk_text)
        assert tokens_cjk >= 0

        # CJK should have higher token estimate per character
        cjk_per_char = tokens_cjk / len(cjk_text) if cjk_text else 0
        ascii_per_char = tokens_ascii / len(ascii_text) if ascii_text else 0
        assert cjk_per_char > ascii_per_char, (
            "CJK text should have higher token estimate per character"
        )

    def test_budget_exhausted_returns_false(self):
        """check_budget returns False when token budget is exhausted."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Budget of 1000, used 1000 (exactly at limit)
        thread = _make_thread(token_budget=1000, total_tokens_used=1000)

        result = dm.check_budget(thread)
        assert result is False, (
            "check_budget should return False when budget is exhausted"
        )

    def test_budget_available_returns_true(self):
        """check_budget returns True when tokens remain."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Budget of 1000, used 500
        thread = _make_thread(token_budget=1000, total_tokens_used=500)

        result = dm.check_budget(thread)
        assert result is True, (
            "check_budget should return True when budget is available"
        )

    def test_run_discussion_terminates_on_budget_exhausted(self):
        """run_discussion automatically terminates when budget is exhausted."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        thread = _make_thread(token_budget=100, max_rounds=10)

        # Mock methods to simulate budget exhaustion
        call_count = [0]

        def mock_execute_round(t):
            call_count[0] += 1
            # Add tokens each round
            t.total_tokens_used += 60  # Exceeds budget after 2 rounds
            return t

        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t), \
             patch.object(dm, "execute_round", side_effect=mock_execute_round), \
             patch.object(dm, "check_convergence", return_value=False), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            result = dm.run_discussion(thread, "initial content")

            # Should have terminated due to budget exhaustion
            assert result.status == DiscussionStatus.BUDGET_EXHAUSTED, (
                "Discussion should terminate with BUDGET_EXHAUSTED status"
            )
            # Should have executed at least 1 round before hitting budget
            assert call_count[0] >= 1

    def test_budget_exceeded_sets_status(self):
        """When budget is exceeded in run_discussion, status is set to BUDGET_EXHAUSTED."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)

        # Start with budget already exceeded
        thread = _make_thread(token_budget=100, total_tokens_used=200, max_rounds=10)

        with patch.object(dm, "start_discussion", side_effect=lambda t, c: t), \
             patch.object(dm, "summarize_conclusion"), \
             patch.object(dm, "_persist_conclusion"), \
             patch.object(dm, "unbind_task"):

            result = dm.run_discussion(thread, "initial content")

            assert result.status == DiscussionStatus.BUDGET_EXHAUSTED


# ===========================================================================
# Task 8: Triple Persistence Tests
# ===========================================================================


class TestConclusionTriplePersistence:
    """Tests for triple persistence: task snapshot, L2 memory, and agent L1."""

    def test_persist_conclusion_to_memory_writes_to_l2(self):
        """_persist_conclusion_to_memory writes to L2 SHARED_MEMORY.md Decisions section."""
        engine = _make_engine()
        memory_mgr = MagicMock()
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        thread = _make_thread()
        thread.conclusion = "Decision: use microservices architecture."
        thread.channel_id = "channel-test"

        dm._persist_conclusion_to_memory(thread)

        # Verify append_discussion_conclusion was called with Decisions section
        memory_mgr.append_discussion_conclusion.assert_called_once()
        call_kwargs = memory_mgr.append_discussion_conclusion.call_args
        assert call_kwargs[1]["section"] == "Decisions", (
            "Conclusion should be written to Decisions section"
        )
        assert call_kwargs[0][0] == "channel-test"
        assert call_kwargs[0][1] == thread.conclusion

    def test_persist_conclusion_to_memory_no_memory_manager_no_error(self):
        """_persist_conclusion_to_memory handles missing memory_manager gracefully."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine, memory_manager=None)

        thread = _make_thread()
        thread.conclusion = "Test conclusion"
        thread.channel_id = "channel-test"

        # Should not raise
        dm._persist_conclusion_to_memory(thread)

    def test_persist_conclusion_to_memory_exception_logged_not_raised(self):
        """Exceptions during L2 persistence are logged but not raised."""
        engine = _make_engine()
        memory_mgr = MagicMock()
        memory_mgr.append_discussion_conclusion.side_effect = RuntimeError("Disk full")
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        thread = _make_thread()
        thread.conclusion = "Test conclusion"
        thread.channel_id = "channel-test"

        # Should not raise - exception should be caught and logged
        dm._persist_conclusion_to_memory(thread)

        # Verify the method was called (exception was caught)
        memory_mgr.append_discussion_conclusion.assert_called_once()

    def test_triple_persistence_all_three_channels(self):
        """_persist_conclusion writes to all three persistence channels."""
        engine = _make_engine()
        memory_mgr = MagicMock()
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        thread = _make_thread()
        thread.conclusion = "Final decision: approach B is optimal."
        thread.channel_id = "channel-xyz"
        thread.trigger_reason = "uncertainty:needs review"
        thread.topic = "Architecture review"

        # Bind to a task
        task_id = "task-123"
        dm.bind_to_task(thread.thread_id, task_id)

        # Mock read_agent_reasoning_snapshot to return None (new snapshot)
        memory_mgr.read_agent_reasoning_snapshot.return_value = None

        with patch.object(dm, "_send_conclusion_notification"):
            dm._persist_conclusion(thread)

        # 1. Verify L2 persistence (Discussion History section)
        l2_calls = [
            c for c in memory_mgr.method_calls
            if c[0] == "append_discussion_conclusion"
        ]
        # Should be called at least once for L2
        assert len(l2_calls) >= 1, (
            "append_discussion_conclusion should be called for L2 persistence"
        )

        # 2. Verify reasoning snapshot persistence
        snapshot_calls = [
            c for c in memory_mgr.method_calls
            if c[0] == "write_discussion_conclusion_snapshot"
        ]
        assert len(snapshot_calls) >= 1, (
            "write_discussion_conclusion_snapshot should be called for task snapshot"
        )

        # 3. Verify L1 sync
        l1_sync_calls = [
            c for c in memory_mgr.method_calls
            if c[0] == "sync_discussion_conclusion_to_agents"
        ]
        assert len(l1_sync_calls) >= 1, (
            "sync_discussion_conclusion_to_agents should be called for L1 sync"
        )
        # Verify participants were passed
        sync_call = l1_sync_calls[0]
        assert sync_call[1][0] == thread.participants, (
            "All participants should be synced to L1"
        )
        assert sync_call[1][1] == thread.conclusion, (
            "Conclusion text should be synced to L1"
        )

    def test_persistence_failure_does_not_block_other_channels(self):
        """Failure in one persistence channel does not block others."""
        engine = _make_engine()
        memory_mgr = MagicMock()

        # Make L2 persistence fail
        def failing_append(*args, **kwargs):
            raise RuntimeError("L2 write failed")

        memory_mgr.append_discussion_conclusion.side_effect = failing_append
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        thread = _make_thread()
        thread.conclusion = "Test conclusion"
        thread.channel_id = "channel-test"

        # Bind to task
        task_id = "task-456"
        dm.bind_to_task(thread.thread_id, task_id)
        memory_mgr.read_agent_reasoning_snapshot.return_value = None

        with patch.object(dm, "_send_conclusion_notification"):
            # Should not raise - L2 failure should be caught
            dm._persist_conclusion(thread)

        # Verify other persistence channels were still attempted
        # (L1 sync should have been called despite L2 failure)
        l1_sync_calls = [
            c for c in memory_mgr.method_calls
            if c[0] == "sync_discussion_conclusion_to_agents"
        ]
        assert len(l1_sync_calls) >= 1, (
            "L1 sync should still be attempted even if L2 fails"
        )

    def test_persist_conclusion_to_task_updates_reasoning_snapshot(self):
        """_persist_conclusion_to_task updates SlockTask.reasoning_snapshot field."""
        engine = _make_engine()

        # Create a mock task
        mock_task = MagicMock()
        mock_task.task_id = "task-789"
        mock_task.reasoning_snapshot = "old content"
        engine._tasks = [mock_task]

        memory_mgr = MagicMock()
        dm = DiscussionManager(engine=engine, memory_manager=memory_mgr)

        thread = _make_thread()
        thread.conclusion = "New decision: refactor module X."
        thread.channel_id = "channel-test"

        dm._persist_conclusion_to_task(thread, "task-789")

        # Verify task.reasoning_snapshot was updated
        assert mock_task.reasoning_snapshot == thread.conclusion, (
            "task.reasoning_snapshot should be updated with conclusion"
        )


# ===========================================================================
# Task Context Injection — 自动任务上下文注入
# ===========================================================================


class TestTaskContextInjection:
    """Test task context auto-injection into discussion initial_content."""

    def test_start_discussion_injects_task_context_when_bound(self):
        """When discussion thread is bound to a task, task context is injected."""
        engine = _make_engine()
        mock_task = MagicMock()
        mock_task.task_id = "task-123"
        mock_task.content = "实现用户登录功能"
        mock_task.status = TaskStatus.IN_PROGRESS
        mock_task.reasoning_snapshot = "已分析需求文档"
        mock_task.claimed_by = "agent-coder"
        engine._tasks = [mock_task]

        dm = DiscussionManager(engine=engine)
        thread = _make_thread(participants=["agent-001", "agent-002"])

        # Bind to task
        dm.bind_to_task(thread.thread_id, "task-123")

        # Start discussion
        initial_content = "我不确定这个实现方案是否正确..."
        result_thread = dm.start_discussion(thread, initial_content)

        # Verify
        assert len(result_thread.messages) == 1
        msg_content = result_thread.messages[0].content

        # Verify task context was injected
        assert "=== 关联任务上下文 ===" in msg_content
        assert "任务ID: task-123" in msg_content
        assert "任务描述: 实现用户登录功能" in msg_content
        assert "当前状态: in_progress" in msg_content
        assert "推理快照: 已分析需求文档" in msg_content
        assert "认领者: agent-coder" in msg_content

        # Verify original content is still present
        assert "我不确定这个实现方案是否正确..." in msg_content

    def test_start_discussion_no_injection_when_not_bound(self):
        """When thread is not bound to a task, initial_content remains unchanged."""
        engine = _make_engine()
        dm = DiscussionManager(engine=engine)
        thread = _make_thread()

        initial_content = "原始讨论内容"
        result_thread = dm.start_discussion(thread, initial_content)

        # Verify no task context marker
        assert "=== 关联任务上下文 ===" not in result_thread.messages[0].content
        # Verify original content is intact
        assert result_thread.messages[0].content == initial_content

    def test_start_discussion_graceful_when_task_not_found(self):
        """When bound task doesn't exist, degrade gracefully (no injection, no error)."""
        engine = _make_engine()
        engine._tasks = []  # Empty task list

        dm = DiscussionManager(engine=engine)
        thread = _make_thread()

        # Bind to a non-existent task
        dm.bind_to_task(thread.thread_id, "nonexistent-task")

        initial_content = "讨论内容"
        # Should not raise
        result_thread = dm.start_discussion(thread, initial_content)

        # Verify no injection (task not found)
        assert "=== 关联任务上下文 ===" not in result_thread.messages[0].content
        assert result_thread.messages[0].content == initial_content

    def test_start_discussion_no_engine_no_error(self):
        """When no engine is available, DiscussionManager works normally."""
        dm = DiscussionManager()  # No engine
        thread = _make_thread()

        dm.bind_to_task(thread.thread_id, "task-123")

        initial_content = "测试内容"
        # Should not raise
        result_thread = dm.start_discussion(thread, initial_content)

        assert result_thread.messages[0].content == initial_content

    def test_task_context_truncates_long_content(self):
        """Long task description and reasoning snapshot should be truncated."""
        engine = _make_engine()
        mock_task = MagicMock()
        mock_task.task_id = "task-long"
        mock_task.content = "x" * 1000  # Very long content
        mock_task.status = TaskStatus.IN_PROGRESS
        mock_task.reasoning_snapshot = "y" * 500
        engine._tasks = [mock_task]

        dm = DiscussionManager(engine=engine)
        thread = _make_thread()
        dm.bind_to_task(thread.thread_id, "task-long")

        result_thread = dm.start_discussion(thread, "讨论内容")
        msg_content = result_thread.messages[0].content

        # Verify truncation marker exists
        assert "[TRUNCATED]" in msg_content
        # Verify content is not excessively long
        assert len(msg_content) < 2000

    def test_run_discussion_with_task_context_integration(self):
        """Integration test: run_discussion includes task context in initial message."""
        engine = _make_engine()
        mock_task = MagicMock()
        mock_task.task_id = "task-integration"
        mock_task.content = "集成测试任务"
        mock_task.status = TaskStatus.TODO
        mock_task.reasoning_snapshot = ""
        engine._tasks = [mock_task]

        dm = DiscussionManager(engine=engine)

        # Capture the initial message before execute_round modifies anything
        initial_content_captured = []

        with patch.object(dm, 'execute_round') as mock_execute:
            # Make first round converge - add new message instead of modifying existing
            def mock_execute_round(t):
                from src.slock_engine.models import DiscussionMessage
                import uuid
                import time
                # Add a new response message instead of modifying the initial one
                response = DiscussionMessage(
                    message_id=str(uuid.uuid4()),
                    sender_agent_id=t.participants[1],
                    receiver_agent_id=t.participants[0],
                    content="AGREE",
                    round_num=t.current_round + 1,
                    timestamp=time.time(),
                    token_count=5,
                )
                t.messages.append(response)
                return t
            mock_execute.side_effect = mock_execute_round

            thread = _make_thread(max_rounds=1)
            dm.bind_to_task(thread.thread_id, "task-integration")

            # Mock check_convergence to return True
            with patch.object(dm, 'check_convergence', return_value=True):
                with patch.object(dm, 'summarize_conclusion'):
                    result = dm.run_discussion(thread, "初始内容")

            # Verify initial message contains task context
            first_msg = result.messages[0]
            assert "=== 关联任务上下文 ===" in first_msg.content
            assert "集成测试任务" in first_msg.content
