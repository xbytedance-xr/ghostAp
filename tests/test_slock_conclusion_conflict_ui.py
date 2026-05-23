"""Tests for conclusion notification card conflict UI.

Verifies that:
1. When skipped_agents is provided, the card shows conflict warning
2. When detection_timed_out is True, the card shows timeout hint
3. Action buttons (查看冲突, 强制覆盖) are present when there are conflicts
"""

import json

from src.slock_engine.card_templates import build_conclusion_notification_card


class TestConclusionConflictUI:
    """Test suite for conclusion notification card conflict UI."""

    def test_no_conflict_shows_success(self):
        """Without skipped_agents, card shows success state."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test conclusion",
            participants=["AgentA", "AgentB"],
            affected_agents=["AgentA", "AgentB"],
            channel_id="test-ch",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Should show success
        assert "讨论结论已持久化" in card_str
        assert "已同步" in card_str
        # Should NOT show conflict warning
        assert "知识冲突" not in card_str
        assert "查看冲突" not in card_str
        assert "强制覆盖" not in card_str
        # Header should be green (success)
        assert card["header"]["template"] == "green"

    def test_skipped_agents_shows_conflict_warning(self):
        """With skipped_agents, card shows conflict warning section."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test conclusion",
            participants=["AgentA", "AgentB"],
            affected_agents=["AgentA"],
            skipped_agents=["AgentB"],
            channel_id="test-ch",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Should show conflict warning
        assert "知识冲突" in card_str
        assert "部分同步完成" in card_str
        assert "AgentB" in card_str
        # Should have action buttons
        assert "查看冲突" in card_str
        assert "强制覆盖" in card_str
        # Header should be orange (warning)
        assert card["header"]["template"] == "orange"
        assert "存在冲突" in card["header"]["title"]["content"]

    def test_skipped_agents_button_types(self):
        """Conflict buttons have correct types (primary and danger)."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test",
            participants=["A"],
            affected_agents=[],
            skipped_agents=["AgentB"],
            channel_id="test-ch",
        )

        # Find the conflict buttons by checking button text and type
        card_str = json.dumps(card, ensure_ascii=False)
        # Button action values are "view_conflict" and "force_override"
        assert "view_conflict" in card_str
        assert "force_override" in card_str
        # Check button types exist in the card
        assert '"type": "primary"' in card_str
        assert '"type": "danger"' in card_str

    def test_detection_timed_out_shows_hint(self):
        """When detection_timed_out=True, card shows timeout hint."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test conclusion",
            participants=["AgentA"],
            affected_agents=["AgentA"],
            detection_timed_out=True,
            channel_id="test-ch",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Should show timeout hint
        assert "检测超时" in card_str or "语义冲突检测超时" in card_str
        assert "跳过 LLM 校验" in card_str

    def test_detection_timed_out_without_conflict(self):
        """Timeout hint shows even without skipped agents."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test",
            participants=["A"],
            affected_agents=["A"],
            detection_timed_out=True,
            skipped_agents=None,
            channel_id="test-ch",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Should have timeout hint but not conflict warning buttons
        assert "跳过 LLM 校验" in card_str
        assert "view_conflict" not in card_str
        # Header should still be green (no actual conflict)
        assert card["header"]["template"] == "green"

    def test_both_conflict_and_timeout(self):
        """Card shows both conflict warning and timeout hint."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test",
            participants=["A", "B"],
            affected_agents=["A"],
            skipped_agents=["B"],
            detection_timed_out=True,
            channel_id="test-ch",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Should show both
        assert "知识冲突" in card_str
        assert "view_conflict" in card_str
        assert "跳过 LLM 校验" in card_str
        # Header should be orange (conflict takes precedence)
        assert card["header"]["template"] == "orange"

    def test_empty_skipped_agents_no_warning(self):
        """Empty skipped_agents list should not trigger warning."""
        card = build_conclusion_notification_card(
            conclusion_preview="Test",
            participants=["A"],
            affected_agents=["A"],
            skipped_agents=[],
            channel_id="test-ch",
        )

        card_str = json.dumps(card, ensure_ascii=False)

        # Empty list is falsy, should not show warning
        assert "知识冲突" not in card_str
        assert "view_conflict" not in card_str

    def test_backward_compatibility_default_params(self):
        """New parameters have defaults that maintain backward compatibility."""
        # Call with original signature (no new params)
        card = build_conclusion_notification_card(
            "Test conclusion",
            ["AgentA"],
            affected_agents=["AgentA"],
            channel_id="test-ch",
        )

        # Should work without errors
        assert card["schema"] == "2.0"
        assert "讨论结论已持久化" in card["header"]["title"]["content"]
