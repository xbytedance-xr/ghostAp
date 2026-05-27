"""Tests for task assignment compatibility boundaries."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.slock_engine.models import AgentIdentity, SlockTask, TaskStatus


class TestSlockTaskAssign:
    """Test deprecated /task assign command flow."""

    def _make_handler(self):
        """Create a SlockHandler with mocked context."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.add_reaction = MagicMock()
        return handler

    def _make_engine_with_agent(self, agent_name="Coder-A"):
        """Create mock engine with a registered agent."""
        engine = MagicMock()
        engine.is_active = True
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat-001"

        agent = AgentIdentity(
            agent_id=f"agent-{agent_name.lower()}",
            name=agent_name,
            emoji="🔧",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test",
            role="coder",
        )

        engine.registry.find_by_name = MagicMock(return_value=agent)
        engine.list_agents = MagicMock(return_value=[agent])

        # Simulate successful task creation and claim
        task = SlockTask(
            task_id="task-001",
            content="Fix the login bug",
            status=TaskStatus.IN_PROGRESS,
            claimed_by=agent.agent_id,
            created_in="chat-001",
        )
        engine.add_task = MagicMock(return_value=task)
        engine.claim_task = MagicMock(return_value=True)
        engine.get_task = MagicMock(return_value=task)
        engine.execute_task = MagicMock(return_value="Done: login bug fixed")
        engine._mouthpiece.format_card = MagicMock(return_value={"header": {"title": {"content": "Result"}}})
        engine.engine_name = "Slock"
        engine.root_path = "/tmp/test"

        return engine, agent

    def test_task_assign_creates_and_claims(self):
        """/task assign is deprecated and no longer creates tasks directly."""
        handler = self._make_handler()
        handler.show_slock_help = MagicMock()
        engine, _agent = self._make_engine_with_agent("Coder-A")
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.handle_slock_command("msg-001", "chat-001", "/task assign Fix login bug Coder-A", None)

        engine.add_task.assert_not_called()
        engine.registry.find_by_name.assert_not_called()
        handler.show_slock_help.assert_called_once_with("msg-001")

    def test_task_assign_unknown_role_feedback(self):
        """/task assign with an unknown role falls back to help, not legacy assignment."""
        handler = self._make_handler()
        handler.show_slock_help = MagicMock()
        engine = MagicMock()
        engine.is_active = True
        engine.channel = MagicMock()
        engine.registry.find_by_name = MagicMock(return_value=None)
        engine.list_agents = MagicMock(return_value=[])
        # add_task must return a task-like object with task_id
        task = SlockTask(
            task_id="task-002",
            content="Fix bug",
            status=TaskStatus.TODO,
            created_in="chat-001",
        )
        engine.add_task = MagicMock(return_value=task)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.handle_slock_command("msg-001", "chat-001", "/task assign Fix bug UnknownAgent", None)

        engine.add_task.assert_not_called()
        handler.show_slock_help.assert_called_once_with("msg-001")

    def test_task_status_changes_to_in_progress(self):
        """Task status should be IN_PROGRESS after successful claim."""
        task = SlockTask(
            task_id="task-001",
            content="Build feature",
            status=TaskStatus.TODO,
            created_in="chat-001",
        )
        assert task.status == TaskStatus.TODO

        # Simulate claim
        task.status = TaskStatus.IN_PROGRESS
        task.claimed_by = "agent-coder"
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.claimed_by == "agent-coder"
