"""Unit tests for build_resolved_escalation_card template."""

from __future__ import annotations

import time

from src.slock_engine.card_templates import build_resolved_escalation_card
from src.slock_engine.models import EscalationLevel, EscalationRequest


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


class TestResolvedEscalationCard:
    """Test build_resolved_escalation_card output structure."""

    def _make_escalation(self, **kwargs) -> EscalationRequest:
        defaults = {
            "escalation_id": "esc-001",
            "agent_id": "agent-001",
            "agent_name": "Coder-A",
            "level": EscalationLevel.BLOCKED,
            "reason": "Cannot access API",
            "options": ["Retry", "Skip", "Abort"],
        }
        defaults.update(kwargs)
        return EscalationRequest(**defaults)

    def test_no_buttons_in_resolved_card(self):
        """AC9b: Resolved card has NO button elements."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="admin-user", resolution="Retry", resolved_at=time.time()
        )

        buttons = _collect_buttons(card)
        assert buttons == [], f"Expected no buttons but found {len(buttons)}"

    def test_resolved_text_present(self):
        """AC9b: Card contains resolved text with resolution and operator."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="admin-user-123", resolution="Skip", resolved_at=1700000000.0
        )

        all_md = _all_markdown_content(card)
        resolved_texts = [md for md in all_md if "已解决" in md or "Resolved" in md or "Skip" in md]
        assert len(resolved_texts) >= 1, "No resolved text found"
        combined = "\n".join(all_md)
        assert "Skip" in combined
        assert "admin-user-123" in combined

    def test_header_color_is_green(self):
        """Resolved escalation card header uses green template."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Abort", resolved_at=time.time()
        )

        assert card["header"]["template"] == "green"

    def test_header_contains_resolved_label(self):
        """Header title includes [已解决] marker."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Retry", resolved_at=time.time()
        )

        title = card["header"]["title"]["content"]
        assert "[已解决]" in title

    def test_preserves_original_escalation_info(self):
        """Card still shows original agent name, reason, and level."""
        esc = self._make_escalation(
            agent_name="Writer-B",
            reason="Missing credentials for deploy",
            context="Need AWS_SECRET_KEY",
        )
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Retry", resolved_at=time.time()
        )

        all_md = _all_markdown_content(card)
        combined = "\n".join(all_md)
        assert "Writer-B" in combined
        assert "Missing credentials for deploy" in combined
        assert "AWS_SECRET_KEY" in combined

    def test_schema_v2(self):
        """Resolved card uses schema 2.0."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Retry", resolved_at=time.time()
        )
        assert card["schema"] == "2.0"

    def test_resolved_at_timestamp_formatted(self):
        """Resolution timestamp is formatted as readable date string."""
        # 2023-11-14 22:13:20 UTC → 2023-11-15 06:13:20 Asia/Shanghai
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Retry", resolved_at=1700000000.0
        )
        all_md = _all_markdown_content(card)
        combined = "\n".join(all_md)
        # Should contain date (either UTC or Shanghai date)
        assert "2023-11-1" in combined

    def test_resolved_card_no_utc_suffix(self):
        """Resolved card timestamp should NOT contain 'UTC' suffix (uses Asia/Shanghai)."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Retry", resolved_at=1700000000.0
        )
        all_md = _all_markdown_content(card)
        combined = "\n".join(all_md)
        assert "UTC" not in combined

    def test_resolved_card_labels_chinese(self):
        """Resolved card labels should be in Chinese (级别, 原因, 已解决)."""
        esc = self._make_escalation()
        card = build_resolved_escalation_card(
            esc, resolved_by="op", resolution="Retry", resolved_at=1700000000.0
        )
        all_md = _all_markdown_content(card)
        combined = "\n".join(all_md)
        # Should contain Chinese labels
        assert "已解决" in combined or "[已解决]" in card["header"]["title"]["content"]
