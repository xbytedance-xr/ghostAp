"""Unit tests for SlockHandler.handle_card_action dispatch logic.

Tests cover the card action routing for:
- slock_show_plan_detail: dispatches to collaboration_orchestrator.get_plan and sends card
- slock_pause_plan: calls pause_plan with permission check
- slock_resume_plan: calls resume_plan with permission check
- slock_role_info: looks up agent and calls show_role_info
- slock_new_task: calls assign_task
- slock_dispatch_tasks: dispatches pending tasks on engine
- slock_show_task_board: sends task board card
- slock_assign_task_to_agent: calls engine.assign_task_to_agent
- Unknown action type: logs warning and falls through to standard dispatch
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

from src.feishu.handlers.slock import SlockHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_ID = "oc_test_chat_001"
MSG_ID = "om_test_msg_001"


def _make_handler() -> MagicMock:
    """Create a MagicMock that proxies handle_card_action to the real method.

    We mock the handler instance, but bind `handle_card_action` to the real
    implementation so the dispatch logic executes against mocked collaborators.
    """
    handler = MagicMock(spec=SlockHandler)
    # Bind the real handle_card_action method to our mock instance
    handler.handle_card_action = lambda *args, **kwargs: SlockHandler.handle_card_action(handler, *args, **kwargs)
    return handler


def _make_engine(
    *,
    plan=None,
    agents=None,
    tasks=None,
    channel_id: str = "chan-001",
) -> MagicMock:
    """Create a mock engine with common attributes pre-configured."""
    engine = MagicMock()
    engine.collaboration_orchestrator = MagicMock()
    engine.collaboration_orchestrator.get_plan.return_value = plan
    engine.collaboration_orchestrator.pause_plan.return_value = True
    engine.collaboration_orchestrator.resume_plan.return_value = True

    engine.registry = MagicMock()
    engine.registry.list_agents.return_value = agents or []

    engine.channel = MagicMock()
    engine.channel.channel_id = channel_id

    engine.tasks = tasks or []
    engine.dispatch_pending_tasks = MagicMock()
    engine.assign_task_to_agent = MagicMock(return_value=True)

    return engine


def _setup_engine_manager(handler: MagicMock, engine) -> MagicMock:
    """Attach an engine manager mock that returns the given engine."""
    manager = MagicMock()
    manager.get_activated_engine.return_value = engine
    handler._get_engine_manager.return_value = manager
    return manager


# ---------------------------------------------------------------------------
# Tests: slock_show_plan_detail
# ---------------------------------------------------------------------------


class TestShowPlanDetail:
    """slock_show_plan_detail dispatches to collaboration_orchestrator.get_plan and sends card."""

    @patch("src.feishu.handlers.slock.json", wraps=json)
    def test_sends_plan_card_when_plan_exists(self, mock_json):
        handler = _make_handler()
        plan = MagicMock()
        agents = [MagicMock()]
        engine = _make_engine(plan=plan, agents=agents)
        _setup_engine_manager(handler, engine)

        with patch(
            "src.slock_engine.card_templates.progress.build_collaboration_plan_card",
            return_value={"card": "plan_detail"},
        ) as mock_build:
            handler.handle_card_action(MSG_ID, CHAT_ID, "slock_show_plan_detail", {"plan_id": "plan-123"})

        engine.collaboration_orchestrator.get_plan.assert_called_once_with("plan-123")
        mock_build.assert_called_once_with(plan, agents, channel_id="chan-001")
        handler.send_card_to_chat.assert_called_once()
        # Verify the card JSON was sent
        sent_card_json = handler.send_card_to_chat.call_args[0][1]
        assert "plan_detail" in sent_card_json

    def test_sends_warning_when_plan_not_found(self):
        handler = _make_handler()
        engine = _make_engine(plan=None)
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_show_plan_detail", {"plan_id": "nonexistent"})

        handler.send_text_to_chat.assert_called_once_with(CHAT_ID, "\u26a0\ufe0f \u672a\u627e\u5230\u8be5\u8ba1\u5212\u3002")

    def test_sends_warning_when_no_engine(self):
        handler = _make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_show_plan_detail", {"plan_id": "plan-123"})

        handler.send_text_to_chat.assert_called_once_with(CHAT_ID, "\u26a0\ufe0f \u672a\u627e\u5230\u8be5\u8ba1\u5212\u3002")


# ---------------------------------------------------------------------------
# Tests: slock_pause_plan
# ---------------------------------------------------------------------------


class TestPausePlan:
    """slock_pause_plan calls pause_plan with permission check."""

    def test_pause_success_with_permission(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)
        handler._check_slock_permission.return_value = True

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_pause_plan", {"plan_id": "plan-abc"})

        handler._check_slock_permission.assert_called_once_with(engine, MSG_ID, CHAT_ID)
        engine.collaboration_orchestrator.pause_plan.assert_called_once_with("plan-abc")
        handler.send_text_to_chat.assert_called_once()
        assert "plan-abc" in handler.send_text_to_chat.call_args[0][1][:20]

    def test_pause_denied_without_permission(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)
        handler._check_slock_permission.return_value = False

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_pause_plan", {"plan_id": "plan-abc"})

        handler._check_slock_permission.assert_called_once_with(engine, MSG_ID, CHAT_ID)
        engine.collaboration_orchestrator.pause_plan.assert_not_called()

    def test_pause_failure_returns_warning(self):
        handler = _make_handler()
        engine = _make_engine()
        engine.collaboration_orchestrator.pause_plan.return_value = False
        _setup_engine_manager(handler, engine)
        handler._check_slock_permission.return_value = True

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_pause_plan", {"plan_id": "plan-abc"})

        handler.send_text_to_chat.assert_called_once()
        assert "\u6682\u505c\u5931\u8d25" in handler.send_text_to_chat.call_args[0][1]


# ---------------------------------------------------------------------------
# Tests: slock_resume_plan
# ---------------------------------------------------------------------------


class TestResumePlan:
    """slock_resume_plan calls resume_plan with permission check."""

    def test_resume_success_with_permission(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)
        handler._check_slock_permission.return_value = True

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_resume_plan", {"plan_id": "plan-xyz"})

        handler._check_slock_permission.assert_called_once_with(engine, MSG_ID, CHAT_ID)
        engine.collaboration_orchestrator.resume_plan.assert_called_once_with("plan-xyz")
        handler.send_text_to_chat.assert_called_once()
        assert "plan-xyz" in handler.send_text_to_chat.call_args[0][1][:20]

    def test_resume_denied_without_permission(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)
        handler._check_slock_permission.return_value = False

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_resume_plan", {"plan_id": "plan-xyz"})

        handler._check_slock_permission.assert_called_once_with(engine, MSG_ID, CHAT_ID)
        engine.collaboration_orchestrator.resume_plan.assert_not_called()

    def test_resume_failure_returns_warning(self):
        handler = _make_handler()
        engine = _make_engine()
        engine.collaboration_orchestrator.resume_plan.return_value = False
        _setup_engine_manager(handler, engine)
        handler._check_slock_permission.return_value = True

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_resume_plan", {"plan_id": "plan-xyz"})

        handler.send_text_to_chat.assert_called_once()
        assert "\u6062\u590d\u5931\u8d25" in handler.send_text_to_chat.call_args[0][1]


# ---------------------------------------------------------------------------
# Tests: slock_role_info
# ---------------------------------------------------------------------------


class TestRoleInfo:
    """slock_role_info looks up agent and calls show_role_info."""

    def test_calls_show_role_info_when_agent_found(self):
        handler = _make_handler()
        engine = _make_engine()
        agent = MagicMock()
        agent.name = "CodeReviewer"
        engine.registry.get.return_value = agent
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_role_info", {"agent_id": "agent-001"})

        engine.registry.get.assert_called_once_with("agent-001")
        handler.show_role_info.assert_called_once_with(MSG_ID, CHAT_ID, "CodeReviewer")

    def test_sends_warning_when_agent_not_found(self):
        handler = _make_handler()
        engine = _make_engine()
        engine.registry.get.return_value = None
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_role_info", {"agent_id": "nonexistent"})

        handler.send_text_to_chat.assert_called_once_with(CHAT_ID, "\u26a0\ufe0f \u672a\u627e\u5230\u8be5\u89d2\u8272\u3002")

    def test_sends_warning_when_no_engine(self):
        handler = _make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_role_info", {"agent_id": "agent-001"})

        handler.send_text_to_chat.assert_called_once_with(CHAT_ID, "\u26a0\ufe0f \u672a\u627e\u5230\u8be5\u89d2\u8272\u3002")


# ---------------------------------------------------------------------------
# Tests: slock_new_task
# ---------------------------------------------------------------------------


class TestNewTask:
    """slock_new_task calls assign_task."""

    def test_calls_assign_task_with_content(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_new_task", {"content": "Fix login bug"})

        handler.assign_task.assert_called_once_with(MSG_ID, CHAT_ID, "Fix login bug")

    def test_sends_warning_when_content_empty(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_new_task", {"content": ""})

        handler.send_text_to_chat.assert_called_once_with(CHAT_ID, "\u26a0\ufe0f \u8bf7\u8f93\u5165\u4efb\u52a1\u5185\u5bb9\u3002")
        handler.assign_task.assert_not_called()

    def test_does_not_call_assign_task_without_engine(self):
        handler = _make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_new_task", {"content": "Some task"})

        handler.assign_task.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: slock_dispatch_tasks
# ---------------------------------------------------------------------------


class TestDispatchTasks:
    """slock_dispatch_tasks dispatches pending tasks on engine."""

    def test_dispatches_and_confirms(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_dispatch_tasks", {})

        engine.dispatch_pending_tasks.assert_called_once()
        handler.send_text_to_chat.assert_called_once()
        assert "\u5df2\u6d3e\u53d1" in handler.send_text_to_chat.call_args[0][1]

    def test_no_op_without_engine(self):
        handler = _make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_dispatch_tasks", {})

        handler.send_text_to_chat.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: slock_show_task_board
# ---------------------------------------------------------------------------


class TestShowTaskBoard:
    """slock_show_task_board sends task board card."""

    @patch("src.feishu.handlers.slock.json", wraps=json)
    def test_sends_task_board_card(self, mock_json):
        handler = _make_handler()
        tasks = [MagicMock(), MagicMock()]
        agents = [MagicMock()]
        engine = _make_engine(tasks=tasks, agents=agents)
        _setup_engine_manager(handler, engine)

        with patch(
            "src.slock_engine.card_templates.build_task_board_card",
            return_value={"card": "task_board"},
        ) as mock_build:
            handler.handle_card_action(MSG_ID, CHAT_ID, "slock_show_task_board", {})

        mock_build.assert_called_once_with(
            tasks=tasks,
            agents=agents,
            channel_id="chan-001",
        )
        handler.send_card_to_chat.assert_called_once()
        sent_card_json = handler.send_card_to_chat.call_args[0][1]
        assert "task_board" in sent_card_json

    def test_no_op_without_engine(self):
        handler = _make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.handle_card_action(MSG_ID, CHAT_ID, "slock_show_task_board", {})

        handler.send_card_to_chat.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: slock_assign_task_to_agent
# ---------------------------------------------------------------------------


class TestAssignTaskToAgent:
    """slock_assign_task_to_agent calls engine.assign_task_to_agent."""

    def test_assigns_successfully(self):
        handler = _make_handler()
        engine = _make_engine()
        engine.assign_task_to_agent.return_value = True
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_assign_task_to_agent",
            {"task_id": "task-99", "agent_id": "agent-007"},
        )

        engine.assign_task_to_agent.assert_called_once_with("task-99", "agent-007")
        handler.send_text_to_chat.assert_called_once()
        assert "agent-00" in handler.send_text_to_chat.call_args[0][1]

    def test_assignment_failure_sends_warning(self):
        handler = _make_handler()
        engine = _make_engine()
        engine.assign_task_to_agent.return_value = False
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_assign_task_to_agent",
            {"task_id": "task-99", "agent_id": "agent-007"},
        )

        engine.assign_task_to_agent.assert_called_once_with("task-99", "agent-007")
        handler.send_text_to_chat.assert_called_once()
        assert "\u5206\u914d\u5931\u8d25" in handler.send_text_to_chat.call_args[0][1]

    def test_does_nothing_without_task_id(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_assign_task_to_agent",
            {"task_id": "", "agent_id": "agent-007"},
        )

        engine.assign_task_to_agent.assert_not_called()

    def test_does_nothing_without_agent_id(self):
        handler = _make_handler()
        engine = _make_engine()
        _setup_engine_manager(handler, engine)

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_assign_task_to_agent",
            {"task_id": "task-99", "agent_id": ""},
        )

        engine.assign_task_to_agent.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Unknown action type
# ---------------------------------------------------------------------------


class TestUnknownActionType:
    """Unknown action type logs a warning and falls through to standard dispatch."""

    def test_logs_warning_for_unknown_action(self, caplog):
        handler = _make_handler()
        # For unknown actions, the code falls through to _dispatch_standard_card_action
        # which requires project_manager and slock_actions dict lookup.
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        handler._dispatch_standard_card_action = MagicMock()
        handler._toggle_log = MagicMock()
        handler._switch_card_mode = MagicMock()
        handler._toggle_ac = MagicMock()
        handler.stop_slock_engine = MagicMock()
        handler._refresh_status_card = MagicMock()
        handler._refresh_task_board_card = MagicMock()

        with caplog.at_level(logging.WARNING, logger="src.feishu.handlers.slock"):
            handler.handle_card_action(MSG_ID, CHAT_ID, "slock_unknown_xyz", {})

        assert any("Unhandled slock card action" in r.message for r in caplog.records)
        assert any("slock_unknown_xyz" in r.message for r in caplog.records)


# ===========================================================================
# Tests for handle_slock_command dispatch mappings
# ===========================================================================


def _make_command_handler() -> MagicMock:
    """Create a MagicMock that proxies handle_slock_command to the real method.

    We mock the handler instance, but bind `handle_slock_command` to the real
    implementation so the dispatch logic executes against mocked collaborators.
    """
    handler = MagicMock(spec=SlockHandler)
    handler.handle_slock_command = lambda *args, **kwargs: SlockHandler.handle_slock_command(handler, *args, **kwargs)
    return handler


# ---------------------------------------------------------------------------
# Tests: SlockCommandAction.MEMORY (/memory <name>)
# ---------------------------------------------------------------------------


class TestCommandMemory:
    """MEMORY action dispatches to show_agent_memory(message_id, chat_id, cmd.target, project)."""

    def test_dispatches_show_agent_memory_with_target(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory coder", None)

        handler.show_agent_memory.assert_called_once_with(MSG_ID, CHAT_ID, "coder", None)

    def test_dispatches_show_agent_memory_with_at_prefix_stripped(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory @reviewer", None)

        handler.show_agent_memory.assert_called_once_with(MSG_ID, CHAT_ID, "reviewer", None)

    def test_dispatches_show_agent_memory_with_empty_target(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory", None)

        handler.show_agent_memory.assert_called_once_with(MSG_ID, CHAT_ID, "", None)

    def test_passes_project_when_provided(self):
        handler = _make_command_handler()
        project = MagicMock()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory architect", project)

        handler.show_agent_memory.assert_called_once_with(MSG_ID, CHAT_ID, "architect", project)


# ---------------------------------------------------------------------------
# Tests: SlockCommandAction.MEMORY_LIST (/memory list)
# ---------------------------------------------------------------------------


class TestCommandMemoryList:
    """MEMORY_LIST action dispatches to show_memory_list(message_id, chat_id, project)."""

    def test_dispatches_show_memory_list(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory list", None)

        handler.show_memory_list.assert_called_once_with(MSG_ID, CHAT_ID, None)

    def test_passes_project_when_provided(self):
        handler = _make_command_handler()
        project = MagicMock()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory list", project)

        handler.show_memory_list.assert_called_once_with(MSG_ID, CHAT_ID, project)

    def test_does_not_trigger_show_agent_memory(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/memory list", None)

        handler.show_agent_memory.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: SlockCommandAction.STOP_DISCUSSION (/discuss stop)
# ---------------------------------------------------------------------------


class TestCommandStopDiscussion:
    """STOP_DISCUSSION action dispatches to stop_discussion(message_id, chat_id, project)."""

    def test_dispatches_stop_discussion(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss stop", None)

        handler.stop_discussion.assert_called_once_with(MSG_ID, CHAT_ID, None)

    def test_passes_project_when_provided(self):
        handler = _make_command_handler()
        project = MagicMock()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss stop", project)

        handler.stop_discussion.assert_called_once_with(MSG_ID, CHAT_ID, project)

    def test_does_not_trigger_discussion_or_list(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss stop", None)

        handler.list_discussions.assert_not_called()
        handler._trigger_nli_discussion.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: SlockCommandAction.DISCUSSION_HISTORY (/discuss history <id>)
# ---------------------------------------------------------------------------


class TestCommandDiscussionHistory:
    """DISCUSSION_HISTORY action dispatches to show_discussion_history(message_id, chat_id, cmd.target, project)."""

    def test_dispatches_show_discussion_history(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss history thread-42", None)

        handler.show_discussion_history.assert_called_once()

    def test_dispatches_with_empty_target_when_no_id(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss history", None)

        handler.show_discussion_history.assert_called_once()
        # Verify message_id and chat_id are passed correctly
        call_args = handler.show_discussion_history.call_args[0]
        assert call_args[0] == MSG_ID
        assert call_args[1] == CHAT_ID

    def test_passes_project_when_provided(self):
        handler = _make_command_handler()
        project = MagicMock()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss history abc123", project)

        handler.show_discussion_history.assert_called_once()
        call_args = handler.show_discussion_history.call_args[0]
        assert call_args[0] == MSG_ID
        assert call_args[1] == CHAT_ID
        assert call_args[3] == project

    def test_does_not_trigger_list_or_stop(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss history thread-42", None)

        handler.list_discussions.assert_not_called()
        handler.stop_discussion.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: SlockCommandAction.DISCUSSION_LIST (/discuss list)
# ---------------------------------------------------------------------------


class TestCommandDiscussionList:
    """DISCUSSION_LIST action dispatches to list_discussions(message_id, chat_id, project)."""

    def test_dispatches_list_discussions(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss list", None)

        handler.list_discussions.assert_called_once_with(MSG_ID, CHAT_ID, None)

    def test_dispatches_list_discussions_on_bare_discuss(self):
        """Bare /discuss (no subcommand) also routes to DISCUSSION_LIST."""
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss", None)

        handler.list_discussions.assert_called_once_with(MSG_ID, CHAT_ID, None)

    def test_passes_project_when_provided(self):
        handler = _make_command_handler()
        project = MagicMock()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss list", project)

        handler.list_discussions.assert_called_once_with(MSG_ID, CHAT_ID, project)

    def test_does_not_trigger_stop_or_history(self):
        handler = _make_command_handler()

        handler.handle_slock_command(MSG_ID, CHAT_ID, "/discuss list", None)

        handler.stop_discussion.assert_not_called()
        handler.show_discussion_history.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: slock_hub_cmd (hub card button routing)
# ---------------------------------------------------------------------------


class TestHubCmdRouting:
    """slock_hub_cmd routes value["cmd"] to handle_slock_command."""

    def test_routes_new_role_command(self):
        handler = _make_handler()
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_hub_cmd",
            {"cmd": "/new-role Alice", "channel_id": CHAT_ID},
        )

        handler.handle_slock_command.assert_called_once_with(
            MSG_ID, CHAT_ID, "/new-role Alice", None
        )

    def test_routes_role_list_command(self):
        handler = _make_handler()
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_hub_cmd",
            {"cmd": "/role list", "channel_id": CHAT_ID},
        )

        handler.handle_slock_command.assert_called_once_with(
            MSG_ID, CHAT_ID, "/role list", None
        )

    def test_routes_task_list_command(self):
        handler = _make_handler()
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_hub_cmd",
            {"cmd": "/task list", "channel_id": CHAT_ID},
        )

        handler.handle_slock_command.assert_called_once_with(
            MSG_ID, CHAT_ID, "/task list", None
        )

    def test_routes_with_project_id(self):
        handler = _make_handler()
        project = MagicMock()
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = project

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_hub_cmd",
            {"cmd": "/team list", "channel_id": CHAT_ID, "project_id": "proj-123"},
        )

        handler.project_manager.get_project_for_chat.assert_called_once_with("proj-123", CHAT_ID)
        handler.handle_slock_command.assert_called_once_with(
            MSG_ID, CHAT_ID, "/team list", project
        )

    def test_rejects_empty_cmd(self):
        handler = _make_handler()
        handler.project_manager = MagicMock()

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_hub_cmd",
            {"cmd": "", "channel_id": CHAT_ID},
        )

        handler.handle_slock_command.assert_not_called()
        handler.send_text_to_chat.assert_called_once()

    def test_rejects_missing_cmd(self):
        handler = _make_handler()
        handler.project_manager = MagicMock()

        handler.handle_card_action(
            MSG_ID, CHAT_ID, "slock_hub_cmd",
            {"channel_id": CHAT_ID},
        )

        handler.handle_slock_command.assert_not_called()
        handler.send_text_to_chat.assert_called_once()
