"""Tests for role naming syntax guidance in UX.

Verifies that the command panel and error messages include guidance
for using @role or "Role Name" syntax to avoid parsing ambiguity.
"""

import json

from src.slock_engine.card_templates import (
    build_command_panel_card,
    build_error_suggestion_card,
)
from src.slock_engine.slash_commands import (
    SlockCommandAction,
    parse_slock_command,
)


class TestRoleHelpUX:
    """Test suite for role naming UX guidance."""

    def test_command_panel_includes_role_guidance(self) -> None:
        """Command panel should include role naming syntax guidance."""
        card = build_command_panel_card()

        # Convert card to string for searching
        card_str = str(card)

        # Should mention @role syntax or quoted role name syntax
        has_at_role = "@role" in card_str or "`@role`" in card_str
        has_quoted = "Role Name" in card_str or '"Role Name"' in card_str

        assert has_at_role or has_quoted, "Command panel should include role naming guidance"

    def test_command_panel_has_markdown_guidance(self) -> None:
        """Command panel should have a markdown element with guidance."""
        card = build_command_panel_card()
        body = card.get("body", {})
        elements = body.get("elements", [])

        # Find markdown elements containing guidance
        guidance_found = False
        for elem in elements:
            if elem.get("tag") == "markdown":
                content = elem.get("content", "")
                if "@role" in content or "Role Name" in content:
                    guidance_found = True
                    break
            # Also check inside collapsible_panel elements
            if elem.get("tag") == "collapsible_panel":
                panel_elems = elem.get("elements", [])
                for pelem in panel_elems:
                    if pelem.get("tag") == "markdown":
                        content = pelem.get("content", "")
                        if "@role" in content or "Role Name" in content:
                            guidance_found = True
                            break
                if guidance_found:
                    break

        assert guidance_found, "Command panel should include role naming guidance"

    def test_error_suggestion_card_structure(self) -> None:
        """Error suggestion card should maintain its structure."""
        card = build_error_suggestion_card(
            "Missing role name",
            [{"label": "Try this", "command": "/role add \"Senior Coder\""}],
            channel_id="test_channel",
        )

        assert card.get("schema") == "2.0"
        body = card.get("body", {})
        elements = body.get("elements", [])
        assert len(elements) > 0

        # Should contain the error message
        card_str = json.dumps(card, ensure_ascii=False)
        assert "Missing role name" in card_str

    def test_role_info_no_args_returns_usage(self) -> None:
        """/role info without target should return ROLE_INFO_USAGE, not ROLE_INFO."""
        cmd = parse_slock_command("/role info")
        assert cmd.action == SlockCommandAction.ROLE_INFO_USAGE

    def test_role_info_with_target_returns_info(self) -> None:
        """/role info with target should return ROLE_INFO with target."""
        cmd = parse_slock_command("/role info coder")
        assert cmd.action == SlockCommandAction.ROLE_INFO
        assert cmd.target == "coder"

    def test_role_info_with_quoted_target_returns_info(self) -> None:
        """/role info with quoted multi-word target should return ROLE_INFO."""
        cmd = parse_slock_command('/role info "Senior Coder"')
        assert cmd.action == SlockCommandAction.ROLE_INFO
        assert "Senior Coder" in cmd.target
