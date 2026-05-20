"""Tests for slock task execution: assign → claim → execute → complete/rollback.

Covers:
- AC5: assign success (claim→ACP→DONE)
- AC5: assign failure (rollback to TODO, lock released, error card)
- ACP timeout handling
"""

from __future__ import annotations

import pytest

pytest.importorskip("acp", reason="acp SDK not installed")

import unittest.mock
from unittest.mock import MagicMock, patch

from src.slock_engine.engine import SlockEngine
from src.slock_engine.models import AgentIdentity, TaskStatus

# ============================================================
# Engine.execute_task tests
# ============================================================


class TestExecuteTaskSuccess:
    """AC5: assign success chain — claim → execute → DONE."""

    def _make_engine(self, tmp_path):
        engine = SlockEngine(
            chat_id="chat_test",
            root_path=str(tmp_path),
            engine_name="Test",
            memory_base_path=str(tmp_path),
        )
        # Set up channel
        from src.slock_engine.models import SlockChannel
        channel = SlockChannel(channel_id="chat_test", name="TestChannel")
        engine.activate_channel(channel)
        return engine

    def test_execute_task_success_marks_done(self, tmp_path):
        """Successful execute_task sets status to DONE and releases claim."""
        engine = self._make_engine(tmp_path)

        # Register an agent
        agent = AgentIdentity(name="Coder", agent_type="coco", owner_group="chat_test")
        engine.registry.register(agent)

        # Add a task
        task = engine.add_task("Write unit tests")

        # Mock _execute_agent to return a result
        with patch.object(engine, "_execute_agent", return_value="Tests written successfully"):
            result = engine.execute_task(task.task_id, agent.agent_id)

        assert result == "Tests written successfully"
        assert task.status == TaskStatus.DONE
        assert not engine._router.task_claim.is_claimed(task.task_id)

    def test_execute_task_claims_if_not_already_claimed(self, tmp_path):
        """execute_task auto-claims if task is not yet claimed."""
        engine = self._make_engine(tmp_path)

        agent = AgentIdentity(name="Coder", agent_type="coco", owner_group="chat_test")
        engine.registry.register(agent)
        task = engine.add_task("Fix the bug")

        with patch.object(engine, "_execute_agent", return_value="Bug fixed"):
            result = engine.execute_task(task.task_id, agent.agent_id)

        assert result == "Bug fixed"
        assert task.status == TaskStatus.DONE


class TestExecuteTaskFailure:
    """AC5: assign failure — rollback to TODO, release lock, error visible."""

    def _make_engine(self, tmp_path):
        engine = SlockEngine(
            chat_id="chat_test",
            root_path=str(tmp_path),
            engine_name="Test",
            memory_base_path=str(tmp_path),
        )
        from src.slock_engine.models import SlockChannel
        channel = SlockChannel(channel_id="chat_test", name="TestChannel")
        engine.activate_channel(channel)
        return engine

    def test_execute_task_failure_rollback(self, tmp_path):
        """Failed execution rolls back task to TODO and releases claim."""
        engine = self._make_engine(tmp_path)

        agent = AgentIdentity(name="Coder", agent_type="coco", owner_group="chat_test")
        engine.registry.register(agent)
        task = engine.add_task("Implement feature")

        # Claim first
        engine.claim_task(task.task_id, agent.agent_id)
        assert task.status == TaskStatus.IN_PROGRESS

        # Mock _execute_agent to raise an exception
        with patch.object(engine, "_execute_agent", side_effect=RuntimeError("ACP session timeout")):
            with pytest.raises(RuntimeError, match="ACP session timeout"):
                engine.execute_task(task.task_id, agent.agent_id)

        # Verify rollback
        assert task.status == TaskStatus.TODO
        assert task.claimed_by is None
        assert not engine._router.task_claim.is_claimed(task.task_id)

    def test_execute_task_no_output_rollback(self, tmp_path):
        """If _execute_agent returns None, task is rolled back."""
        engine = self._make_engine(tmp_path)

        agent = AgentIdentity(name="Coder", agent_type="coco", owner_group="chat_test")
        engine.registry.register(agent)
        task = engine.add_task("Debug issue")

        with patch.object(engine, "_execute_agent", return_value=None):
            result = engine.execute_task(task.task_id, agent.agent_id)

        assert result is None
        assert task.status == TaskStatus.TODO
        assert task.claimed_by is None

    def test_execute_task_nonexistent_task(self, tmp_path):
        """Nonexistent task_id returns None."""
        engine = self._make_engine(tmp_path)

        agent = AgentIdentity(name="Coder", agent_type="coco", owner_group="chat_test")
        engine.registry.register(agent)

        result = engine.execute_task("nonexistent_id", agent.agent_id)
        assert result is None

    def test_execute_task_nonexistent_agent(self, tmp_path):
        """Nonexistent agent_id returns None."""
        engine = self._make_engine(tmp_path)
        task = engine.add_task("Some task")

        result = engine.execute_task(task.task_id, "nonexistent_agent")
        assert result is None


# ============================================================
# Handler assign_task integration
# ============================================================


class TestAssignTaskHandler:
    """Test SlockHandler.assign_task close-loop behavior."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        # Patch real inherited methods with mocks
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="card-msg-001")
        handler.update_card = MagicMock(return_value=True)
        return handler

    @staticmethod
    def _make_sync_executor():
        """Create a mock executor that runs submitted functions synchronously."""
        executor = MagicMock()

        def immediate_submit(fn, *args, **kwargs):
            fn(*args, **kwargs)
            return MagicMock()

        executor.submit = immediate_submit
        return executor

    def test_assign_with_role_executes_task(self):
        """assign_task with role triggers execute_task and sends card on success."""
        handler = self._make_handler()

        agent = MagicMock()
        agent.emoji = "🔧"
        agent.name = "Coder"
        agent.agent_id = "agent_123"
        agent.agent_type = "codex"

        engine = MagicMock()
        engine.add_task.return_value = MagicMock(task_id="task_abc")
        engine.registry.find_by_name.return_value = agent
        engine.claim_task.return_value = True
        engine.execute_task.return_value = "Feature implemented"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}}
        engine._get_executor.return_value = self._make_sync_executor()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.assign_task("msg_1", "chat_1", "implement feature X", "Coder", None)

        engine.execute_task.assert_called_once_with("task_abc", "agent_123", unittest.mock.ANY)

    def test_assign_without_role_creates_only(self):
        """assign_task without role creates task but doesn't execute."""
        handler = self._make_handler()

        engine = MagicMock()
        task = MagicMock(task_id="task_xyz")
        engine.add_task.return_value = task

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.assign_task("msg_1", "chat_1", "some task", "", None)

        engine.execute_task.assert_not_called()
        handler.reply_text.assert_called()

    def test_assign_execution_failure_shows_error(self):
        """When execute_task raises, error card is sent to user."""
        handler = self._make_handler()

        agent = MagicMock()
        agent.emoji = "🔧"
        agent.name = "Coder"
        agent.agent_id = "agent_123"

        engine = MagicMock()
        engine.add_task.return_value = MagicMock(task_id="task_fail")
        engine.registry.find_by_name.return_value = agent
        engine.claim_task.return_value = True
        engine.execute_task.side_effect = RuntimeError("timeout")
        engine._get_executor.return_value = self._make_sync_executor()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.assign_task("msg_1", "chat_1", "broken task", "Coder", None)

        # Error card should be sent via update_card (async pattern)
        handler.update_card.assert_called()
        update_args = handler.update_card.call_args[0]
        assert "❌" in update_args[1] or "失败" in update_args[1]
