"""Tests for slock_cmd_* command panel button routing in handle_card_action.

Validates AC01: clicking command panel buttons triggers the corresponding
sub-command handler methods.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestSlockCmdPanelRouting:
    """Verify handle_card_action routes slock_cmd_* actions to correct methods."""

    def _make_handler(self):
        """Create a SlockHandler mock with _dispatch_cmd_panel_action bound."""
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        # Bind real _dispatch_cmd_panel_action
        handler._dispatch_cmd_panel_action = (
            SlockHandler._dispatch_cmd_panel_action.__get__(handler, SlockHandler)
        )
        # Mock project_manager
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        return handler

    def test_team_list_routes_to_list_teams(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_team_list", {"channel_id": "chat1"}
        )
        handler.list_teams.assert_called_once_with("msg1", "chat1", None)

    def test_new_team_routes_to_create_team(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_new_team", {"channel_id": "chat1"}
        )
        handler.create_team.assert_called_once_with("msg1", "chat1", "", None)

    def test_role_list_routes_to_list_roles(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_role_list", {"channel_id": "chat1"}
        )
        handler.list_roles.assert_called_once_with("msg1", "chat1", None)

    def test_new_role_routes_to_create_role(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_new_role", {"channel_id": "chat1"}
        )
        handler.create_role.assert_called_once_with("msg1", "chat1", "", None)

    def test_task_list_routes_to_list_tasks(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_task_list", {"channel_id": "chat1"}
        )
        handler.list_tasks.assert_called_once_with("msg1", "chat1", None)

    def test_council_routes_to_run_council(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_council", {"channel_id": "chat1"}
        )
        handler.run_council.assert_called_once_with("msg1", "chat1", "", None)

    def test_unknown_cmd_sends_error_text(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_nonexistent", {"channel_id": "chat1"}
        )
        handler.send_text_to_chat.assert_called_once()
        call_args = handler.send_text_to_chat.call_args[0]
        assert "slock_cmd_nonexistent" in call_args[1]

    def test_project_id_resolved_when_present(self):
        handler = self._make_handler()
        mock_project = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = mock_project

        handler._dispatch_cmd_panel_action(
            "msg1", "chat1", "slock_cmd_team_list", {"channel_id": "chat1", "project_id": "proj1"}
        )
        handler.project_manager.get_project_for_chat.assert_called_once_with("proj1", "chat1")
        handler.list_teams.assert_called_once_with("msg1", "chat1", mock_project)


class TestHandleCardActionCmdPrefix:
    """Verify handle_card_action dispatches slock_cmd_* to _dispatch_cmd_panel_action."""

    def test_slock_cmd_prefix_dispatched(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler.handle_card_action = SlockHandler.handle_card_action.__get__(handler, SlockHandler)
        handler._dispatch_cmd_panel_action = MagicMock()

        handler.handle_card_action("msg1", "chat1", "slock_cmd_task_list", {"channel_id": "chat1"})

        handler._dispatch_cmd_panel_action.assert_called_once_with(
            "msg1", "chat1", "slock_cmd_task_list", {"channel_id": "chat1"}
        )


class TestHandleCardActionNewHandlers:
    """Tests for show_memory, switch_role, confirm_switch_role, form actions."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler.handle_card_action = SlockHandler.handle_card_action.__get__(handler, SlockHandler)
        handler._get_engine_manager = MagicMock()
        handler.send_text_to_chat = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        return handler

    def test_show_memory_no_engine_sends_fallback(self):
        handler = self._make_handler()
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None
        handler.handle_card_action("msg1", "chat1", "slock_agent_show_memory", {"agent_id": "a1"})
        handler.send_text_to_chat.assert_called_once()
        assert "暂无记忆" in handler.send_text_to_chat.call_args[0][1]

    def test_show_memory_with_engine_sends_card(self):
        handler = self._make_handler()
        engine = MagicMock()
        memory = MagicMock()
        memory.role = "coder role"
        memory.key_knowledge = "knows python"
        memory.active_context = "working on X"
        engine.memory.read_agent_memory.return_value = memory
        agent = MagicMock()
        agent.display_name = "TestAgent"
        engine.registry.get.return_value = agent
        handler._get_engine_manager.return_value.get_activated_engine.return_value = engine

        handler.handle_card_action("msg1", "chat1", "slock_agent_show_memory", {"agent_id": "a1"})
        handler.send_card_to_chat.assert_called_once()

    def test_switch_role_no_engine_sends_error(self):
        handler = self._make_handler()
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None
        handler.handle_card_action("msg1", "chat1", "slock_agent_switch_role", {"agent_id": "a1"})
        handler.send_text_to_chat.assert_called_once()
        assert "未找到" in handler.send_text_to_chat.call_args[0][1]

    def test_form_new_team_empty_name_error(self):
        handler = self._make_handler()
        handler.handle_card_action("msg1", "chat1", "slock_form_new_team", {"team_name": ""})
        handler.send_text_to_chat.assert_called_once()
        assert "团队名称" in handler.send_text_to_chat.call_args[0][1]

    def test_form_new_team_routes_to_create(self):
        handler = self._make_handler()
        handler.create_team = MagicMock()
        handler.handle_card_action("msg1", "chat1", "slock_form_new_team", {"team_name": "alpha"})
        handler.create_team.assert_called_once_with("msg1", "chat1", "alpha", None)

    def test_form_discuss_empty_topic_error(self):
        handler = self._make_handler()
        handler.handle_card_action("msg1", "chat1", "slock_form_discuss", {"topic": ""})
        handler.send_text_to_chat.assert_called_once()
        assert "讨论主题" in handler.send_text_to_chat.call_args[0][1]

    def test_form_discuss_routes_to_trigger(self):
        handler = self._make_handler()
        handler._trigger_nli_discussion = MagicMock()
        handler.handle_card_action("msg1", "chat1", "slock_form_discuss", {"topic": "设计方案"})
        handler._trigger_nli_discussion.assert_called_once()

    def test_confirm_dissolve_no_engine(self):
        handler = self._make_handler()
        handler._get_engine_manager.return_value.find_team.return_value = None
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None
        handler.handle_card_action("msg1", "chat1", "slock_confirm_dissolve", {"team_name": "x"})
        handler.send_text_to_chat.assert_called_once()
        assert "未找到" in handler.send_text_to_chat.call_args[0][1]


class TestDispatchCmdPanelDiscuss:
    """Tests for slock_cmd_discuss routing in _dispatch_cmd_panel_action."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler._dispatch_cmd_panel_action = (
            SlockHandler._dispatch_cmd_panel_action.__get__(handler, SlockHandler)
        )
        handler.project_manager = MagicMock()
        handler.project_manager.get_project_for_chat.return_value = None
        handler._get_engine_manager = MagicMock()
        handler._check_slock_permission = MagicMock(return_value=True)
        return handler

    def test_discuss_empty_topic_sends_hint(self):
        handler = self._make_handler()
        handler._dispatch_cmd_panel_action("msg1", "chat1", "slock_cmd_discuss", {"topic": ""})
        handler.send_text_to_chat.assert_called_once()
        assert "讨论主题" in handler.send_text_to_chat.call_args[0][1]

    def test_discuss_with_topic_triggers(self):
        handler = self._make_handler()
        handler._trigger_nli_discussion = MagicMock()
        handler._dispatch_cmd_panel_action("msg1", "chat1", "slock_cmd_discuss", {"topic": "架构"})
        handler._trigger_nli_discussion.assert_called_once()
