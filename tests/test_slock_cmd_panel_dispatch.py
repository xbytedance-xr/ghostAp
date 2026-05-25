"""Tests for slock_cmd_* button action routing (AC01: /slock interactive panel dispatch).

Covers:
- Missing action routing (slock_cmd_task_status, slock_cmd_role_info, slock_cmd_role_remove, slock_cmd_team_status, slock_cmd_memory, slock_cmd_panel_extended)
- Parameter hint branches (task_assign, role_info, role_remove, team_status, memory, discuss, council)
- Consistency with TestBuildCommandPanelCard (button count >= 4, action prefix slock_, channel_id propagation)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


def _collect_buttons(card: dict) -> list[dict]:
    buttons: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                buttons.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return buttons


class TestSlockCmdPanelDispatch:
    """AC01 dispatch tests for slock_cmd_* button routing."""

    # ------------------------------------------------------------------
    # Consistency: build_command_panel_card invariants
    # ------------------------------------------------------------------

    def test_panel_card_has_at_least_4_buttons(self):
        from src.slock_engine.card_templates import build_command_panel_card

        card = build_command_panel_card(channel_id="test_chat")
        buttons = _collect_buttons(card)
        assert len(buttons) >= 4, f"Expected >=4 buttons, got {len(buttons)}"

    def test_panel_buttons_have_slock_prefix(self):
        from src.slock_engine.card_templates import build_command_panel_card

        card = build_command_panel_card(channel_id="test_chat")
        buttons = _collect_buttons(card)
        for btn in buttons:
            action = btn.get("value", {}).get("action", "")
            assert action.startswith("slock_"), f"Button action {action!r} missing slock_ prefix"

    def test_panel_channel_id_propagated(self):
        from src.slock_engine.card_templates import build_command_panel_card

        card = build_command_panel_card(channel_id="my_channel")
        buttons = _collect_buttons(card)
        for btn in buttons:
            assert btn["value"]["channel_id"] == "my_channel"

    def test_panel_schema_and_header(self):
        from src.slock_engine.card_templates import build_command_panel_card

        card = build_command_panel_card()
        assert card["schema"] == "2.0"
        assert card["config"]["wide_screen_mode"] is True
        assert "Slock" in card["header"]["title"]["content"]

    # ------------------------------------------------------------------
    # Routing: no-param actions hit handlers
    # ------------------------------------------------------------------

    def test_dispatch_task_status(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.show_task_status = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_task_status",
            value={"channel_id": "chat_1"},
        )

        handler.show_task_status.assert_called_once_with("msg_1", "chat_1", None)

    def test_dispatch_role_list(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.list_roles = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_role_list",
            value={"channel_id": "chat_1"},
        )

        handler.list_roles.assert_called_once_with("msg_1", "chat_1", None)

    def test_dispatch_task_list(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.list_tasks = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_task_list",
            value={"channel_id": "chat_1"},
        )

        handler.list_tasks.assert_called_once_with("msg_1", "chat_1", None)

    def test_dispatch_team_list(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.list_teams = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_team_list",
            value={"channel_id": "chat_1"},
        )

        handler.list_teams.assert_called_once_with("msg_1", "chat_1", None)

    def test_dispatch_panel_extended_sends_card(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_card_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_panel_extended",
            value={"channel_id": "chat_1"},
        )

        handler.send_card_to_chat.assert_called_once()
        call_args = handler.send_card_to_chat.call_args
        assert call_args[0][0] == "chat_1"
        card_json = call_args[0][1]
        card = json.loads(card_json)
        assert card["schema"] == "2.0"
        assert "扩展" in card["header"]["title"]["content"]

    # ------------------------------------------------------------------
    # Routing: param-required actions send hints
    # ------------------------------------------------------------------

    def test_dispatch_task_assign_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_task_assign",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/task assign" in text

    def test_dispatch_role_info_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_role_info",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/role info" in text

    def test_dispatch_role_remove_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_role_remove",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/role remove" in text

    def test_dispatch_team_status_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_team_status",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/team status" in text or "/slock status" in text

    def test_dispatch_memory_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_memory",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/memory" in text

    def test_dispatch_discuss_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_discuss",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/slock discuss" in text or "/discuss" in text

    def test_dispatch_council_hint(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_council",
            value={"channel_id": "chat_1"},
        )

        handler.send_text_to_chat.assert_called_once()
        text = handler.send_text_to_chat.call_args[0][1]
        assert "/council" in text

    # ------------------------------------------------------------------
    # Routing: memory with target delegates to handle_slock_command
    # ------------------------------------------------------------------

    def test_dispatch_memory_with_target_delegates(self):
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.handle_slock_command = MagicMock()

        handler._dispatch_cmd_panel_action(
            message_id="msg_1",
            chat_id="chat_1",
            action_type="slock_cmd_memory",
            value={"channel_id": "chat_1", "target": "coder"},
        )

        handler.handle_slock_command.assert_called_once()
        call_args = handler.handle_slock_command.call_args
        assert call_args[0][0] == "msg_1"
        assert call_args[0][1] == "chat_1"
        assert "/memory coder" in call_args[0][2]
