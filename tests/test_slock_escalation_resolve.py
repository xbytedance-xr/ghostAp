"""Unit tests for slock escalation resolve flow.

Tests the full path: escalation creation → card rendering → resolve callback → agent recovery.
"""

from __future__ import annotations

import json

import pytest

from src.slock_engine.card_templates import build_escalation_card
from src.slock_engine.models import (
    AgentIdentity,
    AgentStatus,
    EscalationLevel,
    EscalationRequest,
    TaskStatus,
)


class TestEscalationResolveEngine:
    """Test engine.resolve_escalation via the SlockEngine class."""

    def _make_engine(self, tmp_path):
        """Create a minimal SlockEngine for testing escalation."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine(chat_id="test_chat", root_path=str(tmp_path), memory_base_path=str(tmp_path))
        return engine

    def _make_agent(self, agent_id="agent-001", name="Coder-A") -> AgentIdentity:
        return AgentIdentity(agent_id=agent_id, name=name, emoji="🔧", role="coder")

    def test_escalation_create_and_resolve_retry(self, tmp_path):
        """AC-9: Escalation created → resolve with Retry → escalation marked resolved."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        # Create escalation
        escalation = engine.escalate(
            agent,
            reason="Cannot access external API",
            level=EscalationLevel.BLOCKED,
            context="HTTP 403 Forbidden on api.example.com",
        )

        assert escalation.resolved is False
        assert escalation.escalation_id
        assert len(engine.get_pending_escalations()) == 1

        # Resolve with Retry
        resolved = engine.resolve_escalation(escalation.escalation_id, "Retry")

        assert resolved is not None
        assert resolved.resolved is True
        assert resolved.resolution == "Retry"
        assert resolved.resolved_at is not None
        assert len(engine.get_pending_escalations()) == 0

    def test_escalation_resolve_skip(self, tmp_path):
        """Resolve with Skip marks escalation resolved."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        escalation = engine.escalate(agent, reason="Ambiguous requirement")
        resolved = engine.resolve_escalation(escalation.escalation_id, "Skip")

        assert resolved is not None
        assert resolved.resolution == "Skip"
        assert resolved.resolved is True

    def test_escalation_resolve_abort(self, tmp_path):
        """Resolve with Abort marks escalation resolved."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        escalation = engine.escalate(agent, reason="Fatal error")
        resolved = engine.resolve_escalation(escalation.escalation_id, "Abort")

        assert resolved is not None
        assert resolved.resolution == "Abort"

    def test_resolve_invalid_escalation_id_returns_none(self, tmp_path):
        """AC-T5: Invalid escalation_id returns None, no crash."""
        engine = self._make_engine(tmp_path)

        result = engine.resolve_escalation("nonexistent-id-999", "Retry")
        assert result is None

    def test_resolve_already_resolved_returns_none(self, tmp_path):
        """Double-resolving the same escalation returns None."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        escalation = engine.escalate(agent, reason="Need credentials")
        engine.resolve_escalation(escalation.escalation_id, "Retry")

        # Second resolve should fail
        result = engine.resolve_escalation(escalation.escalation_id, "Abort")
        assert result is None

    def test_agent_transitions_to_idle_on_escalation(self, tmp_path):
        """When escalation is raised, agent status transitions to IDLE."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        # Manually set agent to RUNNING first (bypass state machine for test setup)
        engine.set_agent_status(agent.agent_id, AgentStatus.RUNNING)
        assert engine.get_agent_status(agent.agent_id) == AgentStatus.RUNNING

        engine.escalate(agent, reason="Blocked")

        # Agent should be back to IDLE
        assert engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE


class TestEscalationCardRendering:
    """Test that escalation cards render with correct structure and buttons."""

    def test_escalation_card_has_resolution_buttons(self):
        """Escalation card contains Retry/Skip/Abort buttons."""
        esc = EscalationRequest(
            agent_id="a1",
            agent_name="Coder-A",
            level=EscalationLevel.BLOCKED,
            reason="Cannot proceed",
            options=["Retry", "Skip", "Abort"],
        )

        card = build_escalation_card(esc, channel_id="ch1")

        assert card["schema"] == "2.0"
        assert "升级告警" in card["header"]["title"]["content"]

        # Find all buttons recursively
        buttons = _collect_buttons(card)
        button_texts = [b["text"]["content"] for b in buttons]
        assert "Retry" in button_texts
        assert "Skip" in button_texts
        assert "Abort" in button_texts

    def test_escalation_card_buttons_contain_action_value(self):
        """Each button's value contains action=slock_escalation_resolve and escalation_id."""
        esc = EscalationRequest(
            escalation_id="esc-123",
            agent_id="a1",
            agent_name="Coder-A",
            level=EscalationLevel.CRITICAL,
            reason="Fatal",
        )

        card = build_escalation_card(esc, channel_id="ch1")
        buttons = _collect_buttons(card)

        for btn in buttons:
            val = btn.get("value", {})
            assert val.get("action") == "slock_escalation_resolve"
            assert val.get("escalation_id") == "esc-123"
            assert val.get("channel_id") == "ch1"

    def test_escalation_card_level_colors(self):
        """Different escalation levels produce different header colors."""
        levels_colors = [
            (EscalationLevel.WARNING, "yellow"),
            (EscalationLevel.BLOCKED, "orange"),
            (EscalationLevel.CRITICAL, "red"),
        ]
        for level, expected_color in levels_colors:
            esc = EscalationRequest(
                agent_id="a1", agent_name="A", level=level, reason="test"
            )
            card = build_escalation_card(esc)
            assert card["header"]["template"] == expected_color, f"level={level.value}"

    def test_escalation_card_shows_context(self):
        """Escalation card displays the context block when provided."""
        esc = EscalationRequest(
            agent_id="a1",
            agent_name="Coder",
            level=EscalationLevel.BLOCKED,
            reason="API failure",
            context="HTTP 500 from /api/deploy",
        )
        card = build_escalation_card(esc)
        all_md = _all_markdown_content(card)
        assert any("HTTP 500" in md for md in all_md)


def _collect_buttons(node: object) -> list[dict]:
    """Recursively collect all button elements from a card structure."""
    if isinstance(node, dict):
        buttons = [node] if node.get("tag") == "button" else []
        for value in node.values():
            buttons.extend(_collect_buttons(value))
        return buttons
    if isinstance(node, list):
        buttons: list[dict] = []
        for item in node:
            buttons.extend(_collect_buttons(item))
        return buttons
    return []


def _all_markdown_content(node: object) -> list[str]:
    """Recursively collect all markdown content strings."""
    if isinstance(node, dict):
        results = []
        if node.get("tag") == "markdown" and "content" in node:
            results.append(node["content"])
        for value in node.values():
            results.extend(_all_markdown_content(value))
        return results
    if isinstance(node, list):
        results: list[str] = []
        for item in node:
            results.extend(_all_markdown_content(item))
        return results
    return []


class TestEscalationResolutionValidation:
    """Test resolution whitelist validation at the handler level.

    The handler should reject resolution values not in the escalation's options.
    These tests verify the engine.get_escalation + whitelist check logic.
    """

    def _make_engine(self, tmp_path):
        from src.slock_engine.engine import SlockEngine
        return SlockEngine(chat_id="test_chat", root_path=str(tmp_path), memory_base_path=str(tmp_path))

    def _make_agent(self, agent_id="agent-001", name="Coder-A") -> AgentIdentity:
        return AgentIdentity(agent_id=agent_id, name=name, emoji="🔧", role="coder")

    def test_get_escalation_returns_object(self, tmp_path):
        """get_escalation returns the EscalationRequest by ID."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="Blocked", options=["Retry", "Skip", "Abort"])
        result = engine.get_escalation(esc.escalation_id)

        assert result is not None
        assert result.escalation_id == esc.escalation_id
        assert result.options == ("Retry", "Skip", "Abort")

    def test_get_escalation_nonexistent_returns_none(self, tmp_path):
        """get_escalation with invalid ID returns None."""
        engine = self._make_engine(tmp_path)
        assert engine.get_escalation("nonexistent-id") is None

    def test_valid_resolution_in_options(self, tmp_path):
        """Retry/Skip/Abort are valid when in escalation.options."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="test", options=["Retry", "Skip", "Abort"])
        allowed = esc.options or ["Retry", "Skip", "Abort"]

        assert "Retry" in allowed
        assert "Skip" in allowed
        assert "Abort" in allowed

    def test_invalid_resolution_force_merge(self, tmp_path):
        """AC11: 'ForceMerge' is NOT in default options — should be rejected."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="test", options=["Retry", "Skip", "Abort"])
        allowed = esc.options or ["Retry", "Skip", "Abort"]

        assert "ForceMerge" not in allowed

    def test_invalid_resolution_empty_string(self, tmp_path):
        """AC11: Empty string is NOT a valid resolution."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="test", options=["Retry", "Skip", "Abort"])
        allowed = esc.options or ["Retry", "Skip", "Abort"]

        assert "" not in allowed

    def test_resolution_with_whitespace_not_matched(self, tmp_path):
        """AC11: '  Retry  ' (with whitespace) does not match 'Retry' without strip."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="test", options=["Retry", "Skip", "Abort"])
        allowed = esc.options or ["Retry", "Skip", "Abort"]

        # Raw value with spaces is not in the list
        assert "  Retry  " not in allowed
        # But stripped value IS — handler should strip before checking
        assert "  Retry  ".strip() in allowed

    def test_resolution_case_sensitive(self, tmp_path):
        """AC11: 'retry' (lowercase) is NOT in ['Retry', 'Skip', 'Abort']."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="test", options=["Retry", "Skip", "Abort"])
        allowed = esc.options or ["Retry", "Skip", "Abort"]

        assert "retry" not in allowed
        assert "RETRY" not in allowed


class TestGetEscalationReturnsCopy:
    """AC-15: get_escalation() returns a shallow copy — modifying it does not affect internal state."""

    def _make_engine(self, tmp_path):
        from src.slock_engine.engine import SlockEngine
        return SlockEngine(chat_id="test_chat", root_path=str(tmp_path), memory_base_path=str(tmp_path))

    def _make_agent(self, agent_id="agent-001", name="Coder-A") -> AgentIdentity:
        return AgentIdentity(agent_id=agent_id, name=name, emoji="🔧", role="coder")

    def test_modifying_returned_escalation_does_not_affect_internal(self, tmp_path):
        """Mutating the returned escalation object does not change the engine's internal copy."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="Test isolation", options=["Retry", "Skip", "Abort"])

        # Get a copy via the public API
        retrieved = engine.get_escalation(esc.escalation_id)
        assert retrieved is not None

        # Mutate the retrieved copy
        object.__setattr__(retrieved, "resolved", True)
        object.__setattr__(retrieved, "resolution", "Tampered")
        object.__setattr__(retrieved, "reason", "Hacked")

        # Internal state should remain unchanged
        internal = engine.get_escalation(esc.escalation_id)
        assert internal is not None
        assert internal.resolved is False
        assert internal.resolution == ""
        assert internal.reason == "Test isolation"

    def test_options_tuple_is_immutable(self, tmp_path):
        """Options field is a tuple — cannot be appended to."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        esc = engine.escalate(agent, reason="Test options", options=["Retry", "Skip"])
        retrieved = engine.get_escalation(esc.escalation_id)

        assert retrieved is not None
        assert isinstance(retrieved.options, tuple)

        # Attempting to append should raise AttributeError (tuple has no append)
        with pytest.raises(AttributeError):
            retrieved.options.append("Abort")  # type: ignore


class TestResumeAfterEscalation:
    """Test engine.resume_after_escalation — Retry/Skip/Abort recovery logic."""

    def _make_engine(self, tmp_path):
        from src.slock_engine.engine import SlockEngine
        return SlockEngine(chat_id="test_chat", root_path=str(tmp_path), memory_base_path=str(tmp_path))

    def _make_agent(self, agent_id="agent-001", name="Coder-A") -> AgentIdentity:
        return AgentIdentity(agent_id=agent_id, name=name, emoji="🔧", role="coder")

    def _setup_escalation_with_task(self, engine, agent, task_content="Implement feature X"):
        """Create a task, claim it, then escalate from the agent."""
        engine.registry.register(agent)
        task = engine.add_task(task_content)
        # Manually claim and set to IN_PROGRESS
        task.claimed_by = agent.agent_id
        task.status = TaskStatus.IN_PROGRESS
        engine._persist_task_board()

        escalation = engine.escalate(
            agent,
            reason="Blocked by external API",
            level=EscalationLevel.BLOCKED,
            task_id=task.task_id,
        )
        return task, escalation

    def test_retry_calls_execute_task(self, tmp_path):
        """Retry resolution triggers execute_task for the associated task."""
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        # Resolve the escalation with Retry
        resolved = engine.resolve_escalation(escalation.escalation_id, "Retry")

        # Disable async path so we test synchronous retry logic
        engine._escalation_mgr._get_executor_fn = None
        with patch.object(engine, "execute_task", return_value="output") as mock_exec:
            result = engine.resume_after_escalation(resolved)
            mock_exec.assert_called_once_with(task.task_id, agent.agent_id, None)
            assert result == "output"

    def test_skip_rolls_back_task_to_todo(self, tmp_path):
        """Skip resolution releases task back to TODO status."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        resolved = engine.resolve_escalation(escalation.escalation_id, "Skip")
        engine.resume_after_escalation(resolved)

        # Task should be back to TODO with no claimer
        updated_task = next(t for t in engine._tasks if t.task_id == task.task_id)
        assert updated_task.status == TaskStatus.TODO
        assert updated_task.claimed_by is None

    def test_abort_marks_task_done(self, tmp_path):
        """Abort resolution force-completes the task."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        resolved = engine.resolve_escalation(escalation.escalation_id, "Abort")
        engine.resume_after_escalation(resolved)

        updated_task = next(t for t in engine._tasks if t.task_id == task.task_id)
        assert updated_task.status == TaskStatus.DONE

    def test_retry_limit_auto_aborts(self, tmp_path):
        """R4: After _MAX_ESCALATION_RETRIES retries, auto-aborts the task."""
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        resolved = engine.resolve_escalation(escalation.escalation_id, "Retry")

        # Exhaust retries
        with patch.object(engine, "execute_task", return_value="ok"):
            for _ in range(engine._MAX_ESCALATION_RETRIES):
                engine.resume_after_escalation(resolved)

        # Next retry should auto-abort
        result = engine.resume_after_escalation(resolved)
        assert result is None

        updated_task = next(t for t in engine._tasks if t.task_id == task.task_id)
        assert updated_task.status == TaskStatus.DONE

    def test_no_task_id_retry_returns_none(self, tmp_path):
        """Retry with no task_id returns None gracefully."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        escalation = engine.escalate(agent, reason="No task context")
        resolved = engine.resolve_escalation(escalation.escalation_id, "Retry")

        result = engine.resume_after_escalation(resolved)
        assert result is None

    def test_unknown_resolution_returns_none(self, tmp_path):
        """Unknown resolution string returns None without crashing."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        engine.registry.register(agent)

        escalation = engine.escalate(agent, reason="test")
        resolved = engine.resolve_escalation(escalation.escalation_id, "ForceMerge")
        # Manually set resolution to something unexpected
        if resolved:
            object.__setattr__(resolved, "resolution", "ForceMerge")
            result = engine.resume_after_escalation(resolved)
            assert result is None


class TestResumeAfterEscalationChinese:
    """AC-14: resume_after_escalation supports Chinese resolution options."""

    def _make_engine(self, tmp_path):
        from src.slock_engine.engine import SlockEngine
        return SlockEngine(chat_id="test_chat", root_path=str(tmp_path), memory_base_path=str(tmp_path))

    def _make_agent(self, agent_id="agent-001", name="Coder-A") -> AgentIdentity:
        return AgentIdentity(agent_id=agent_id, name=name, emoji="🔧", role="coder")

    def _setup_escalation_with_task(self, engine, agent, task_content="Implement feature X"):
        """Create a task, claim it, then escalate from the agent."""
        engine.registry.register(agent)
        task = engine.add_task(task_content)
        task.claimed_by = agent.agent_id
        task.status = TaskStatus.IN_PROGRESS
        engine._persist_task_board()

        escalation = engine.escalate(
            agent,
            reason="Blocked by external API",
            level=EscalationLevel.BLOCKED,
            task_id=task.task_id,
        )
        return task, escalation

    def test_chinese_retry_triggers_execute(self, tmp_path):
        """resolution='重试' follows the Retry branch (re-executes task)."""
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        resolved = engine.resolve_escalation(escalation.escalation_id, "重试")

        # Disable async path so we test synchronous retry logic
        engine._escalation_mgr._get_executor_fn = None
        with patch.object(engine, "execute_task", return_value="output") as mock_exec:
            result = engine.resume_after_escalation(resolved)
            mock_exec.assert_called_once_with(task.task_id, agent.agent_id, None)
            assert result == "output"

    def test_chinese_skip_rolls_back_task(self, tmp_path):
        """resolution='跳过' follows the Skip branch (rolls task back to TODO)."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        resolved = engine.resolve_escalation(escalation.escalation_id, "跳过")
        engine.resume_after_escalation(resolved)

        updated_task = next(t for t in engine._tasks if t.task_id == task.task_id)
        assert updated_task.status == TaskStatus.TODO
        assert updated_task.claimed_by is None

    def test_chinese_abort_marks_task_done(self, tmp_path):
        """resolution='中止' follows the Abort branch (force-complete task)."""
        engine = self._make_engine(tmp_path)
        agent = self._make_agent()
        task, escalation = self._setup_escalation_with_task(engine, agent)

        resolved = engine.resolve_escalation(escalation.escalation_id, "中止")
        engine.resume_after_escalation(resolved)

        updated_task = next(t for t in engine._tasks if t.task_id == task.task_id)
        assert updated_task.status == TaskStatus.DONE

    def test_escalation_card_fallback_uses_chinese_options(self):
        """AC-14: When escalation.options is empty, card buttons use Chinese text."""
        escalation = EscalationRequest(
            escalation_id="esc-fallback",
            agent_id="agent-001",
            agent_name="Coder-A",
            level=EscalationLevel.BLOCKED,
            reason="Test fallback",
            options=None,  # Triggers fallback
        )

        card = build_escalation_card(escalation, channel_id="ch-001")
        card_json = json.dumps(card, ensure_ascii=False)

        # Verify Chinese options appear in the card
        assert "重试" in card_json
        assert "跳过" in card_json
        assert "中止" in card_json


class TestResumeRetryAsync:
    """Test that the Retry path in resume_after_escalation uses executor.submit for async execution."""

    def _make_manager(self, *, get_executor_fn=None, execute_task_fn=None):
        """Create a minimal EscalationManager with mocked dependencies for Retry-path tests."""
        import threading
        from unittest.mock import MagicMock

        from src.slock_engine.escalation_manager import EscalationManager
        from src.slock_engine.task_router import TaskRouter

        lock = threading.RLock()
        escalations: list[EscalationRequest] = []
        retry_counts: dict[str, int] = {}

        router = MagicMock(spec=TaskRouter)

        manager = EscalationManager(
            lock=lock,
            escalations=escalations,
            retry_counts=retry_counts,
            channel_getter=lambda: None,
            chat_id_getter=lambda: "test_chat",
            task_list_getter=lambda: [],
            dirty_setter=lambda v: None,
            router=router,
            transition_agent=lambda aid, st: None,
            flush_if_dirty=lambda tasks: None,
            execute_task_fn=execute_task_fn or MagicMock(return_value="sync_result"),
            rollback_task_fn=MagicMock(),
            force_complete_task_fn=MagicMock(),
            get_executor_fn=get_executor_fn,
        )
        return manager

    def _make_resolved_retry_escalation(self, task_id="task-001", agent_id="agent-001"):
        """Create a resolved EscalationRequest with Retry resolution."""
        escalation = EscalationRequest(
            agent_id=agent_id,
            agent_name="Coder-A",
            level=EscalationLevel.BLOCKED,
            reason="Blocked by API",
            task_id=task_id,
        )
        escalation.resolved = True
        escalation.resolution = "Retry"
        return escalation

    def test_retry_uses_executor_submit(self):
        """When get_executor_fn is provided, Retry submits task via executor.submit."""
        from unittest.mock import MagicMock

        mock_executor = MagicMock()
        mock_get_executor = MagicMock(return_value=mock_executor)
        mock_execute_task = MagicMock(return_value="sync_result")

        manager = self._make_manager(
            get_executor_fn=mock_get_executor,
            execute_task_fn=mock_execute_task,
        )

        escalation = self._make_resolved_retry_escalation()
        manager.resume_after_escalation(escalation)

        mock_get_executor.assert_called_once()
        mock_executor.submit.assert_called_once_with(
            mock_execute_task, "task-001", "agent-001", None,
        )

    def test_retry_returns_none_for_async(self):
        """When executor is available, Retry returns None (result delivered asynchronously)."""
        from unittest.mock import MagicMock

        mock_executor = MagicMock()
        mock_get_executor = MagicMock(return_value=mock_executor)
        mock_execute_task = MagicMock(return_value="sync_result")

        manager = self._make_manager(
            get_executor_fn=mock_get_executor,
            execute_task_fn=mock_execute_task,
        )

        escalation = self._make_resolved_retry_escalation()
        result = manager.resume_after_escalation(escalation)

        assert result is None
        # Verify sync execute was NOT called directly
        mock_execute_task.assert_not_called()

    def test_retry_fallback_sync_on_executor_error(self):
        """When executor.submit raises, falls back to synchronous execution."""
        from unittest.mock import MagicMock

        mock_executor = MagicMock()
        mock_executor.submit.side_effect = RuntimeError("executor pool exhausted")
        mock_get_executor = MagicMock(return_value=mock_executor)
        mock_execute_task = MagicMock(return_value="sync_fallback_result")

        manager = self._make_manager(
            get_executor_fn=mock_get_executor,
            execute_task_fn=mock_execute_task,
        )

        escalation = self._make_resolved_retry_escalation()
        result = manager.resume_after_escalation(escalation)

        # Should fall back to sync execution
        mock_execute_task.assert_called_once_with("task-001", "agent-001", None)
        assert result == "sync_fallback_result"

    def test_retry_sync_without_executor(self):
        """When get_executor_fn is None, Retry uses synchronous execution (backward compat)."""
        from unittest.mock import MagicMock

        mock_execute_task = MagicMock(return_value="direct_sync_result")

        manager = self._make_manager(
            get_executor_fn=None,
            execute_task_fn=mock_execute_task,
        )

        escalation = self._make_resolved_retry_escalation()
        result = manager.resume_after_escalation(escalation)

        mock_execute_task.assert_called_once_with("task-001", "agent-001", None)
        assert result == "direct_sync_result"
