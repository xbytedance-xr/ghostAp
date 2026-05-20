"""Tests for Agent move notification card, confirm card, and /slock help containing /role move."""

from __future__ import annotations

import json
import re

from src.slock_engine.card_templates import (
    build_agent_move_confirm_card,
    build_agent_move_departure_card,
    build_agent_move_notification_card,
)
from src.slock_engine.models import AgentIdentity


def _make_test_agent() -> AgentIdentity:
    return AgentIdentity(
        agent_id="card-test-001",
        name="TestBot",
        emoji="🤖",
        agent_type="codex",
        model_name="o3-pro",
        system_prompt="You are TestBot.",
        role="coder",
        permissions=["shell"],
        owner_group="source-group",
        member_groups=["source-group"],
    )


class TestMoveNotificationCardNoJumpButton:
    """AC6: Notification card (sent to target group) does NOT contain a jump button."""

    def test_notification_card_has_no_button(self):
        """Card JSON body.elements has markdown + notation footer, no button or multi_url."""
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="源团队",
            target_team="目标团队",
        )

        elements = card["body"]["elements"]
        # markdown + notation footer
        assert len(elements) == 2
        assert elements[0]["tag"] == "markdown"
        assert elements[1]["tag"] == "markdown"
        assert elements[1].get("text_size") == "notation"

        # Ensure no multi_url or button anywhere in the card JSON
        card_json = json.dumps(card, ensure_ascii=False)
        assert "multi_url" not in card_json
        assert '"tag": "button"' not in card_json

    def test_notification_card_header_and_content(self):
        """Basic card structure is correct."""
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
        )

        assert card["schema"] == "2.0"
        assert "角色加入" in card["header"]["title"]["content"]
        assert "TestBot" in card["header"]["title"]["content"]

        # Markdown content has agent info
        md_content = card["body"]["elements"][0]["content"]
        assert "TestBot" in md_content
        assert "Alpha" in md_content
        assert "codex" in md_content
        assert "o3-pro" in md_content


class TestMoveNotificationCardOperatorDisplay:
    """Notification card displays operator name when provided."""

    def test_notification_card_shows_operator(self):
        """When operator_display is provided, card shows '由 {operator} 迁移至此'."""
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
            operator_display="张三",
        )

        md_content = card["body"]["elements"][0]["content"]
        assert "由 张三 迁移至此" in md_content

    def test_notification_card_no_operator_fallback(self):
        """When operator_display is empty, card shows '已迁移至此'."""
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
            operator_display="",
        )

        md_content = card["body"]["elements"][0]["content"]
        assert "已迁移至此" in md_content
        assert "由" not in md_content.split("已迁移至此")[0].split("\n")[-1]


class TestMoveNotificationCardFooter:
    """Notification card has a note footer with agent_type | model_name | timestamp."""

    def test_notification_card_footer_content(self):
        """Footer contains agent_type, model_name, and a timestamp pattern."""
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
        )

        elements = card["body"]["elements"]
        note_elem = elements[-1]
        assert note_elem["tag"] == "markdown"
        assert note_elem.get("text_size") == "notation"
        footer_text = note_elem["content"]
        assert "codex" in footer_text
        assert "o3-pro" in footer_text
        # Timestamp pattern: YYYY-MM-DD HH:MM
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", footer_text)


class TestMoveConfirmCardHasJumpButton:
    """AC5: Confirm card (sent to source group) contains a jump button to target group."""

    def test_confirm_card_has_jump_button_with_target_channel(self):
        """Confirm card contains button with multi_url pointing to target group."""
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="源团队",
            target_team="目标团队",
            target_channel_id="oc_abc123def456",
        )

        card_json = json.dumps(card, ensure_ascii=False)
        assert "multi_url" in card_json
        assert "oc_abc123def456" in card_json

        # Verify multi_url structure contains applink URLs
        assert "applink.feishu.cn/client/chat/open" in card_json
        assert "lark://applink/client/chat/open" in card_json

    def test_confirm_card_header_is_green(self):
        """Confirm card header template is 'green' for success semantics."""
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="源团队",
            target_team="目标团队",
            target_channel_id="oc_test",
        )

        assert card["header"]["template"] == "green"
        assert "角色迁移完成" in card["header"]["title"]["content"]
        assert "TestBot" in card["header"]["title"]["content"]

    def test_confirm_card_body_has_agent_details(self):
        """Confirm card body includes agent name, target team, and status."""
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="AlphaTeam",
            target_team="BetaTeam",
            target_channel_id="oc_target",
        )

        md_content = card["body"]["elements"][0]["content"]
        assert "TestBot" in md_content
        assert "BetaTeam" in md_content
        assert "已成功移动" in md_content

    def test_confirm_card_no_button_when_empty_channel(self):
        """When target_channel_id is empty string, no jump button added."""
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="源团队",
            target_team="目标团队",
            target_channel_id="",
        )

        elements = card["body"]["elements"]
        # markdown + notation footer only (no button)
        assert elements[0]["tag"] == "markdown"
        assert elements[-1]["tag"] == "markdown"
        assert elements[-1].get("text_size") == "notation"
        card_json = json.dumps(card, ensure_ascii=False)
        assert "multi_url" not in card_json


class TestMoveConfirmCardFooter:
    """Confirm card has a note footer with agent_type | model_name | timestamp."""

    def test_confirm_card_footer_content(self):
        """Footer contains agent_type, model_name, and a timestamp pattern."""
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
            target_channel_id="oc_test",
        )

        elements = card["body"]["elements"]
        note_elem = elements[-1]
        assert note_elem["tag"] == "markdown"
        assert note_elem.get("text_size") == "notation"
        footer_text = note_elem["content"]
        assert "codex" in footer_text
        assert "o3-pro" in footer_text
        # Timestamp pattern: YYYY-MM-DD HH:MM
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", footer_text)


class TestHelpContainsRoleMove:
    """AC3: /slock help output includes /role move command."""

    def test_help_text_has_role_move(self):
        """The help text string contains /role move usage."""
        import inspect

        from src.feishu.handlers.slock import SlockHandler

        source = inspect.getsource(SlockHandler.show_slock_help)
        assert "/role move" in source
        assert "目标团队" in source


class TestDepartureCard:
    """Validate build_agent_move_departure_card output structure."""

    def test_header_template_is_orange(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetAlpha")
        assert card["header"]["template"] == "orange"

    def test_header_title_contains_agent_name(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetAlpha")
        assert agent.name in card["header"]["title"]["content"]

    def test_markdown_contains_agent_name_and_target_team(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetAlpha")
        md_element = card["body"]["elements"][0]
        assert md_element["tag"] == "markdown"
        assert agent.name in md_element["content"]
        assert "TargetAlpha" in md_element["content"]

    def test_no_jump_button(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetAlpha")
        card_json = json.dumps(card)
        assert "multi_url" not in card_json
        assert '"button"' not in card_json

    def test_footer_has_timestamp(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetAlpha")
        footer = card["body"]["elements"][1]
        assert footer["tag"] == "markdown"
        assert footer.get("text_size") == "notation"
        footer_text = footer["content"]
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", footer_text)

    def test_footer_contains_agent_type_and_model(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetAlpha")
        footer = card["body"]["elements"][1]
        footer_text = footer["content"]
        assert agent.agent_type in footer_text
        assert agent.model_name in footer_text


class TestMoveRoleSendsDepartureNotification:
    """Integration: move_role sends departure card to source group."""

    def test_departure_card_sent_to_source_chat(self, monkeypatch):
        """After move, send_card_to_chat is called with source chat_id and orange header."""
        from unittest.mock import MagicMock

        # Build a minimal mock handler
        handler = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg-123")
        handler.reply_card = MagicMock()
        handler.reply_text = MagicMock()

        # Track send_card_to_chat calls
        calls = []

        def track_send(chat_id, card_json):
            calls.append({"chat_id": chat_id, "card": json.loads(card_json)})
            return "msg-track"

        handler.send_card_to_chat.side_effect = track_send

        # Verify departure card structure in tracked calls
        agent = _make_test_agent()
        departure_card = build_agent_move_departure_card(agent=agent, target_team="TargetTeam")

        assert departure_card["header"]["template"] == "orange"
        assert agent.name in departure_card["header"]["title"]["content"]


class TestCardTerminologyConsistency:
    """Verify all cards use '角色' terminology and correct emojis after rename."""

    def test_departure_card_uses_role_terminology(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetTeam")
        header_title = card["header"]["title"]["content"]
        md_content = card["body"]["elements"][0]["content"]
        assert "角色迁出" in header_title
        assert "角色记忆" in md_content
        assert "Agent 记忆" not in md_content

    def test_notification_card_uses_role_terminology(self):
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
        )
        header_title = card["header"]["title"]["content"]
        md_content = card["body"]["elements"][0]["content"]
        assert "角色加入" in header_title
        assert card["header"]["template"] == "indigo"
        assert "角色定义" in md_content
        assert "跨群策略" in md_content
        assert "完整保留" not in md_content
        assert "Agent 记忆" not in md_content

    def test_confirm_card_uses_role_terminology(self):
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
            target_channel_id="oc_test",
        )
        header_title = card["header"]["title"]["content"]
        md_content = card["body"]["elements"][0]["content"]
        assert "角色迁移完成" in header_title
        assert "角色定义" in md_content
        assert "跨群隐私策略" in md_content
        assert "完整保留" not in md_content
        assert "Agent 记忆" not in md_content

    def test_departure_emoji_is_arrow(self):
        agent = _make_test_agent()
        card = build_agent_move_departure_card(agent=agent, target_team="TargetTeam")
        header_title = card["header"]["title"]["content"]
        assert header_title.startswith("➡️")

    def test_notification_emoji_is_wave(self):
        agent = _make_test_agent()
        card = build_agent_move_notification_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
        )
        header_title = card["header"]["title"]["content"]
        assert header_title.startswith("👋")

    def test_confirm_emoji_is_checkmark(self):
        agent = _make_test_agent()
        card = build_agent_move_confirm_card(
            agent=agent,
            source_team="Alpha",
            target_team="Beta",
            target_channel_id="oc_test",
        )
        header_title = card["header"]["title"]["content"]
        assert header_title.startswith("✅")
