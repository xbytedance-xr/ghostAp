"""Tests for AC02: role command missing-argument error cards with suggestion buttons.

Validates:
- build_role_arg_error_card produces valid Feishu card structure
- remove_role / show_role_info / move_role return error card (not text) when arg missing
- slock_cmd_fix callback re-routes corrected command
"""

from __future__ import annotations

import sys
import json
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock external dependencies that are not installed in the test environment.
# This must happen before any import of src.feishu.* modules.
# ---------------------------------------------------------------------------

_EXTERNAL_MODULES = [
    "lark_oapi", "lark_oapi.event", "lark_oapi.event.callback",
    "lark_oapi.event.callback.model", "lark_oapi.event.callback.model.p2_card_action_trigger",
    "lark_oapi.event.callback.model.p2_im_message_receive_v1",
    "lark_oapi.api", "lark_oapi.api.core", "lark_oapi.api.core.request",
    "lark_oapi.api.im", "lark_oapi.api.im.v1",
    "lark_oapi.ws", "lark_oapi.ws.const", "lark_oapi.ws.enum",
    "lark_oapi.ws.client",
    "acp", "acp.client", "acp.interfaces", "acp.schema", "acp.helpers",
    "acp.stdio",
]


class _FakeModule(MagicMock):
    """MagicMock subclass accepted by importlib machinery."""
    __spec__ = None
    __path__ = []
    __all__ = []


for _mod_name in _EXTERNAL_MODULES:
    sys.modules.setdefault(_mod_name, _FakeModule(name=_mod_name))

# ---------------------------------------------------------------------------

from src.slock_engine.card_templates import build_role_arg_error_card


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_buttons(node: object) -> list[dict]:
    """Recursively collect all button elements from a card dict."""
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


def _make_agent(name: str = "Alice", agent_id: str = "agent-001"):
    """Create a minimal agent-like mock for registry.list_agents()."""
    agent = MagicMock()
    agent.name = name
    agent.agent_id = agent_id
    agent.emoji = "🤖"
    agent.display_name = name
    return agent


def _make_handler():
    """Create a SlockHandler with mocked dependencies for unit testing."""
    from src.feishu.handlers.slock import SlockHandler

    handler = MagicMock(spec=SlockHandler)
    # Bind real methods under test (including internal helpers they call)
    handler.remove_role = SlockHandler.remove_role.__get__(handler, SlockHandler)
    handler.show_role_info = SlockHandler.show_role_info.__get__(handler, SlockHandler)
    handler.move_role = SlockHandler.move_role.__get__(handler, SlockHandler)
    handler.handle_card_action = SlockHandler.handle_card_action.__get__(handler, SlockHandler)
    handler._reply_role_arg_hint = SlockHandler._reply_role_arg_hint.__get__(handler, SlockHandler)
    handler.handle_slock_command = MagicMock()

    # Mock engine manager with registry
    engine = MagicMock()
    engine.channel = MagicMock()
    engine.channel.channel_id = "chat1"
    engine.registry.list_agents.return_value = [
        _make_agent("Alice", "agent-001"),
        _make_agent("Bob", "agent-002"),
    ]

    manager = MagicMock()
    manager.get_activated_engine.return_value = engine
    handler._get_engine_manager.return_value = manager

    # project_manager for handle_card_action callback routing
    handler.project_manager = MagicMock()
    handler.project_manager.get_project_for_chat.return_value = None

    return handler


# ===========================================================================
# Test: build_role_arg_error_card structure
# ===========================================================================


class TestBuildRoleArgErrorCard:
    def test_schema_version(self):
        card = build_role_arg_error_card("/role remove", "用法: `/role remove <名称>`", ["/role remove Alice"])
        assert card["schema"] == "2.0"

    def test_header_template_orange(self):
        card = build_role_arg_error_card("/role remove", "用法...", [])
        assert card["header"]["template"] == "orange"

    def test_header_title_contains_missing(self):
        card = build_role_arg_error_card("/role remove", "用法...", [])
        assert "参数缺失" in card["header"]["title"]["content"]

    def test_buttons_contain_slock_cmd_fix_action(self):
        suggestions = ["/role remove Alice", "/role remove Bob"]
        card = build_role_arg_error_card("/role remove", "用法...", suggestions, channel_id="ch1")
        buttons = _collect_buttons(card)
        assert len(buttons) >= 2
        for btn in buttons:
            assert btn["value"]["action"] == "slock_cmd_fix"

    def test_buttons_carry_fix_command(self):
        suggestions = ["/role remove Alice"]
        card = build_role_arg_error_card("/role remove", "用法...", suggestions)
        buttons = _collect_buttons(card)
        assert buttons[0]["value"]["fix_command"] == "/role remove Alice"

    def test_no_suggestions_still_valid(self):
        card = build_role_arg_error_card("/role remove", "用法...", [])
        assert card["body"]["elements"]  # has at least the markdown + hr + note
        buttons = _collect_buttons(card)
        assert buttons == []

    def test_max_five_suggestions(self):
        suggestions = [f"/role remove Agent{i}" for i in range(10)]
        card = build_role_arg_error_card("/role remove", "用法...", suggestions)
        buttons = _collect_buttons(card)
        assert len(buttons) <= 5

    def test_channel_id_in_button_value(self):
        card = build_role_arg_error_card("/role info", "...", ["/role info X"], channel_id="ch99")
        buttons = _collect_buttons(card)
        assert buttons[0]["value"]["channel_id"] == "ch99"


# ===========================================================================
# Test: remove_role empty name → error card
# ===========================================================================


class TestRemoveRoleErrorCard:
    def test_reply_card_called_with_suggestions(self):
        handler = _make_handler()
        handler.remove_role("msg1", "chat1", "", None)
        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        buttons = _collect_buttons(card)
        # remove_role now uses build_error_suggestion_card with static ["/role list"]
        assert len(buttons) == 1
        assert buttons[0]["value"]["fix_command"] == "/role list"

    def test_card_with_no_suggestions_when_no_engine(self):
        handler = _make_handler()
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None
        handler.remove_role("msg1", "chat1", "", None)
        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        buttons = _collect_buttons(card)
        # Static suggestions are always shown regardless of engine state
        assert len(buttons) == 1
        assert buttons[0]["value"]["fix_command"] == "/role list"

    def test_card_with_no_suggestions_when_no_agents(self):
        handler = _make_handler()
        engine = handler._get_engine_manager.return_value.get_activated_engine.return_value
        engine.registry.list_agents.return_value = []
        handler.remove_role("msg1", "chat1", "", None)
        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        buttons = _collect_buttons(card)
        # Static suggestions are always shown regardless of agent list
        assert len(buttons) == 1
        assert buttons[0]["value"]["fix_command"] == "/role list"


# ===========================================================================
# Test: show_role_info empty name → error card
# ===========================================================================


class TestShowRoleInfoErrorCard:
    def test_reply_card_called_with_suggestions(self):
        handler = _make_handler()
        handler.show_role_info("msg1", "chat1", "", None)
        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        buttons = _collect_buttons(card)
        assert any("/role info Alice" in btn["value"]["fix_command"] for btn in buttons)

    def test_card_with_no_suggestions_when_no_engine(self):
        handler = _make_handler()
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None
        handler.show_role_info("msg1", "chat1", "", None)
        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        buttons = _collect_buttons(card)
        assert buttons == []


# ===========================================================================
# Test: move_role empty name → error card
# ===========================================================================


class TestMoveRoleErrorCard:
    def test_reply_card_called_with_suggestions(self):
        handler = _make_handler()
        handler.move_role("msg1", "chat1", "", "", None)
        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        buttons = _collect_buttons(card)
        # move_role with empty name uses build_error_suggestion_card with
        # ["/role list", "/role move <角色名> <目标团队名>"]
        assert len(buttons) == 2
        assert buttons[0]["value"]["fix_command"] == "/role list"
        assert buttons[1]["value"]["fix_command"] == "/role move <角色名> <目标团队名>"

    def test_card_with_no_suggestions_when_no_engine(self):
        handler = _make_handler()
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None
        handler.move_role("msg1", "chat1", "", "", None)
        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        buttons = _collect_buttons(card)
        # Static suggestions are always shown regardless of engine state
        assert len(buttons) == 2
        assert buttons[0]["value"]["fix_command"] == "/role list"

    def test_card_when_name_provided_but_no_team(self):
        """If name is provided but target_team is missing, shows card with suggestion buttons."""
        handler = _make_handler()
        handler.move_role("msg1", "chat1", "Alice", "", None)
        handler.reply_card.assert_called_once()
        card = json.loads(handler.reply_card.call_args[0][1])
        buttons = _collect_buttons(card)
        # Should suggest "/role move Alice <目标团队名>" or similar
        assert len(buttons) >= 1


# ===========================================================================
# Test: slock_cmd_fix callback routing
# ===========================================================================


class TestSlockCmdFixCallback:
    def test_routes_valid_role_command(self):
        handler = _make_handler()
        handler.handle_card_action(
            "msg1", "chat1", "slock_cmd_fix", {"fix_command": "/role remove Alice"}
        )
        handler.handle_slock_command.assert_called_once_with(
            "msg1", "chat1", "/role remove Alice", None
        )

    def test_routes_task_command(self):
        handler = _make_handler()
        handler.handle_card_action(
            "msg1", "chat1", "slock_cmd_fix", {"fix_command": "/task list"}
        )
        handler.handle_slock_command.assert_called_once_with(
            "msg1", "chat1", "/task list", None
        )

    def test_rejects_non_whitelisted_command(self):
        handler = _make_handler()
        handler.handle_card_action(
            "msg1", "chat1", "slock_cmd_fix", {"fix_command": "/evil inject"}
        )
        handler.handle_slock_command.assert_not_called()
        handler.send_text_to_chat.assert_called_once()

    def test_rejects_empty_fix_command(self):
        handler = _make_handler()
        handler.handle_card_action(
            "msg1", "chat1", "slock_cmd_fix", {"fix_command": ""}
        )
        handler.handle_slock_command.assert_not_called()
        handler.send_text_to_chat.assert_called_once()


# ===========================================================================
# Test: AC-R04 — /new-team and /new-role without name return MISSING_NAME action
# ===========================================================================


class TestMissingNameAction:
    """AC-R04: /new-team and /new-role without name return MISSING_NAME action."""

    def test_new_team_missing_name(self):
        from src.slock_engine.slash_commands import parse_slock_command, SlockCommandAction
        cmd = parse_slock_command("/new-team")
        assert cmd.action == SlockCommandAction.NEW_TEAM_MISSING_NAME

    def test_new_role_missing_name(self):
        from src.slock_engine.slash_commands import parse_slock_command, SlockCommandAction
        cmd = parse_slock_command("/new-role")
        assert cmd.action == SlockCommandAction.NEW_ROLE_MISSING_NAME

    def test_new_team_with_name_still_works(self):
        from src.slock_engine.slash_commands import parse_slock_command, SlockCommandAction
        cmd = parse_slock_command("/new-team my-team")
        assert cmd.action == SlockCommandAction.NEW_TEAM
        assert cmd.args == "my-team"

    def test_new_role_with_name_still_works(self):
        from src.slock_engine.slash_commands import parse_slock_command, SlockCommandAction
        cmd = parse_slock_command("/new-role reviewer")
        assert cmd.action == SlockCommandAction.NEW_ROLE
        assert cmd.args == "reviewer"


# ===========================================================================
# Test: AC-R03 — _parse_assign_args correctly handles multi-word tasks
# ===========================================================================


class TestParseAssignArgs:
    """AC-R03: _parse_assign_args correctly handles multi-word tasks."""

    def test_multiword_task_last_word_is_role(self):
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args("fix the login bug coder")
        assert content == "fix the login bug"
        assert role == "coder"

    def test_at_role_syntax(self):
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args("fix the login bug @coder")
        assert content == "fix the login bug"
        assert role == "coder"

    def test_quoted_multiword_task(self):
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args('"fix the login bug" reviewer')
        assert content == "fix the login bug"
        assert role == "reviewer"

    def test_at_role_in_middle(self):
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args("@reviewer fix the login bug")
        assert content == "fix the login bug"
        assert role == "reviewer"

    def test_empty_input(self):
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args("")
        assert content == ""
        assert role == ""

    def test_single_word(self):
        from src.slock_engine.slash_commands import _parse_assign_args
        content, role = _parse_assign_args("task")
        assert content == "task"
        assert role == ""
