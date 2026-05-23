"""Tests for TaskChainManager.on_task_done logic.

Verifies that task chain propagation works correctly when a task completes:
- No-op when chain_next_agent_id is empty
- Creates downstream task for the target agent
- Downstream task inherits relevant content/context from parent
- Target agent is properly notified (via claimed_by assignment + logging)
- Non-existent agent IDs are handled gracefully without raising
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import patch

import pytest

from src.slock_engine.models import SlockTask, TaskStatus
from src.slock_engine.task_chain_manager import TaskChainManager


@pytest.fixture
def chain_manager() -> TaskChainManager:
    """Create a TaskChainManager with a simple chain config."""
    return TaskChainManager(chain_config="coder->reviewer->tester")


@pytest.fixture
def completed_task_no_chain() -> SlockTask:
    """A completed task with no chain_next_agent_id set."""
    return SlockTask(
        task_id=str(uuid.uuid4()),
        content="Implement feature X",
        status=TaskStatus.DONE,
        claimed_by="agent-coder-001",
        created_in="channel-abc",
        chain_next_agent_id="",
    )


@pytest.fixture
def completed_task_with_chain() -> SlockTask:
    """A completed task with chain_next_agent_id pointing to a reviewer agent."""
    return SlockTask(
        task_id=str(uuid.uuid4()),
        content="Implement feature X",
        status=TaskStatus.DONE,
        claimed_by="agent-coder-001",
        created_in="channel-abc",
        chain_next_agent_id="agent-reviewer-002",
    )


class TestOnTaskDoneNoChain:
    """Test that on_task_done is a no-op when chain_next_agent_id is absent."""

    def test_on_task_done_no_chain(
        self, chain_manager: TaskChainManager, completed_task_no_chain: SlockTask
    ) -> None:
        """Task without chain_next_agent_id should return None and do nothing."""
        result = chain_manager.on_task_done(completed_task_no_chain)
        assert result is None

    def test_on_task_done_empty_string_chain(self, chain_manager: TaskChainManager) -> None:
        """Explicitly empty string chain_next_agent_id returns None."""
        task = SlockTask(
            task_id="task-123",
            content="Some work",
            status=TaskStatus.DONE,
            chain_next_agent_id="",
        )
        result = chain_manager.on_task_done(task)
        assert result is None

    def test_on_task_done_none_like_chain(self, chain_manager: TaskChainManager) -> None:
        """Default chain_next_agent_id (empty string) returns None."""
        task = SlockTask(
            task_id="task-456",
            content="Other work",
            status=TaskStatus.DONE,
        )
        # chain_next_agent_id defaults to ""
        assert task.chain_next_agent_id == ""
        result = chain_manager.on_task_done(task)
        assert result is None


class TestOnTaskDoneCreatesDownstreamTask:
    """Test that on_task_done creates a downstream task when chain is configured."""

    def test_on_task_done_creates_downstream_task(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Task with chain_next_agent_id should create a new SlockTask."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert isinstance(result, SlockTask)

    def test_downstream_task_has_unique_id(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task should have a new unique task_id different from parent."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert result.task_id != completed_task_with_chain.task_id
        # Verify it's a valid UUID
        uuid.UUID(result.task_id)

    def test_downstream_task_status_is_todo(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task should be in TODO status, ready for the next agent."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert result.status == TaskStatus.TODO

    def test_downstream_task_claimed_by_target_agent(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task should be claimed by the chain_next_agent_id."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert result.claimed_by == "agent-reviewer-002"

    def test_downstream_task_does_not_propagate_chain(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task should not propagate the chain further by default."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert result.chain_next_agent_id == ""


class TestOnTaskDoneDownstreamTaskContent:
    """Test that downstream task inherits relevant content/context from parent."""

    def test_downstream_task_content_references_parent(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task content should reference the parent task's content."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert "Implement feature X" in result.content

    def test_downstream_task_content_format(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task content should follow 'Review: {original_content}' format."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert result.content == "Review: Implement feature X"

    def test_downstream_task_inherits_channel(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Downstream task should inherit created_in (channel) from parent."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        assert result is not None
        assert result.created_in == completed_task_with_chain.created_in
        assert result.created_in == "channel-abc"

    def test_downstream_task_content_with_long_parent_content(
        self, chain_manager: TaskChainManager
    ) -> None:
        """Downstream task should handle long parent content correctly."""
        long_content = "Fix bug in module " + "A" * 1000
        task = SlockTask(
            task_id="task-long",
            content=long_content,
            status=TaskStatus.DONE,
            created_in="channel-xyz",
            chain_next_agent_id="agent-reviewer-002",
        )
        result = chain_manager.on_task_done(task)

        assert result is not None
        assert result.content == f"Review: {long_content}"


class TestOnTaskDoneNotifiesTargetAgent:
    """Test that the target agent is properly notified via task assignment and logging."""

    def test_on_task_done_notifies_target_agent(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Verify notification is sent: downstream task claimed_by is set to target agent."""
        result = chain_manager.on_task_done(completed_task_with_chain)

        # The primary notification mechanism is assigning claimed_by to the target agent
        assert result is not None
        assert result.claimed_by == completed_task_with_chain.chain_next_agent_id

    def test_on_task_done_logs_chain_trigger(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Verify that chain trigger is logged for observability."""
        with patch(
            "src.slock_engine.task_chain_manager.logger"
        ) as mock_logger:
            result = chain_manager.on_task_done(completed_task_with_chain)

            assert result is not None
            # Verify info-level log was emitted about the chain trigger
            mock_logger.info.assert_called()
            log_call_args = mock_logger.info.call_args
            log_message = log_call_args[0][0]
            assert "Chain triggered" in log_message

    def test_on_task_done_log_contains_task_ids(
        self, chain_manager: TaskChainManager, completed_task_with_chain: SlockTask
    ) -> None:
        """Verify log message includes source and downstream task IDs."""
        with patch(
            "src.slock_engine.task_chain_manager.logger"
        ) as mock_logger:
            result = chain_manager.on_task_done(completed_task_with_chain)

            assert result is not None
            mock_logger.info.assert_called()
            log_call_args = mock_logger.info.call_args[0]
            # The log format string args should contain the original task_id
            # and the target agent_id
            full_log = log_call_args[0] % log_call_args[1:]
            assert completed_task_with_chain.task_id in full_log
            assert "agent-reviewer-002" in full_log

    def test_on_task_done_no_log_when_no_chain(
        self, chain_manager: TaskChainManager, completed_task_no_chain: SlockTask
    ) -> None:
        """No chain trigger log should be emitted when chain_next_agent_id is empty."""
        with patch(
            "src.slock_engine.task_chain_manager.logger"
        ) as mock_logger:
            chain_manager.on_task_done(completed_task_no_chain)
            # info should not be called for chain trigger (may be called during init)
            for call in mock_logger.info.call_args_list:
                assert "Chain triggered" not in call[0][0]


class TestOnTaskDoneInvalidAgentId:
    """Test that chain_next_agent_id referencing a non-existent agent is handled gracefully."""

    def test_on_task_done_invalid_agent_id(self, chain_manager: TaskChainManager) -> None:
        """Non-existent agent_id should not raise; task is still created."""
        task = SlockTask(
            task_id="task-invalid",
            content="Do something",
            status=TaskStatus.DONE,
            created_in="channel-abc",
            chain_next_agent_id="non-existent-agent-999",
        )
        # Should not raise any exception
        result = chain_manager.on_task_done(task)

        assert result is not None
        assert isinstance(result, SlockTask)
        assert result.claimed_by == "non-existent-agent-999"

    def test_on_task_done_whitespace_agent_id(self, chain_manager: TaskChainManager) -> None:
        """Whitespace-only agent_id is truthy; task still created (edge case)."""
        task = SlockTask(
            task_id="task-whitespace",
            content="Do something",
            status=TaskStatus.DONE,
            created_in="channel-abc",
            chain_next_agent_id="   ",
        )
        # "   " is truthy so on_task_done will proceed
        result = chain_manager.on_task_done(task)

        assert result is not None
        assert result.claimed_by == "   "

    def test_on_task_done_special_chars_agent_id(self, chain_manager: TaskChainManager) -> None:
        """Agent ID with special characters should not cause errors."""
        task = SlockTask(
            task_id="task-special",
            content="Process data",
            status=TaskStatus.DONE,
            created_in="channel-def",
            chain_next_agent_id="agent/with:special@chars!",
        )
        result = chain_manager.on_task_done(task)

        assert result is not None
        assert result.claimed_by == "agent/with:special@chars!"
        assert result.status == TaskStatus.TODO

    def test_on_task_done_uuid_format_agent_id(self, chain_manager: TaskChainManager) -> None:
        """Standard UUID-format agent_id (common pattern) works correctly."""
        agent_id = str(uuid.uuid4())
        task = SlockTask(
            task_id="task-uuid",
            content="Deploy service",
            status=TaskStatus.DONE,
            created_in="channel-ghi",
            chain_next_agent_id=agent_id,
        )
        result = chain_manager.on_task_done(task)

        assert result is not None
        assert result.claimed_by == agent_id
        assert result.content == "Review: Deploy service"
        assert result.created_in == "channel-ghi"
