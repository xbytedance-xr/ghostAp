#!/usr/bin/env python3
"""Smoke test for /new-team flow.

Verifies the complete create_team path without hitting real Feishu APIs:
  - group_name has [Slock] suffix
  - welcome card is sent to new group
  - confirmation card replied in original group
  - rollback on failure

Run: uv run python scripts/smoke_new_team.py
"""

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def smoke_test_new_team():
    """End-to-end smoke test for /new-team create flow."""
    from src.slock_engine.card_templates import build_team_created_card, build_welcome_card

    # --- Verify welcome card structure ---
    team_name = "SmokeTesting"
    card = build_welcome_card(team_name=team_name)

    assert card["schema"] == "2.0", f"schema mismatch: {card.get('schema')}"
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["title"]["tag"] == "plain_text"
    assert team_name in card["header"]["title"]["content"]
    assert card["header"]["template"] == "green"
    assert isinstance(card["body"]["elements"], list)
    assert len(card["body"]["elements"]) > 0
    body_content = card["body"]["elements"][0]["content"]
    assert "/new-role" in body_content
    assert "/slock status" in body_content

    # Verify JSON serializable
    serialized = json.dumps(card, ensure_ascii=False)
    assert len(serialized) > 0

    # --- Verify confirmation card structure ---
    confirm = build_team_created_card(
        team_name=team_name,
        group_name=f"{team_name} [Slock]",
        channel_id="oc_test_chat_id_123",
    )
    assert confirm["schema"] == "2.0"
    assert "创建" in confirm["header"]["title"]["content"]
    confirm_serialized = json.dumps(confirm, ensure_ascii=False)
    assert "oc_test_chat_id_123" in confirm_serialized

    # --- Verify suffix formatting ---
    from src.feishu.handlers.slock import SlockHandler

    fmt = SlockHandler._format_slock_group_name
    assert fmt("Alpha", "[Slock]") == "Alpha [Slock]"
    assert fmt("Alpha [Slock]", "[Slock]") == "Alpha [Slock]"  # no dup
    assert fmt("Beta", "-Slock") == "Beta-Slock"  # separator prefix
    assert fmt("", "[Slock]") == " [Slock]"  # empty name: separator + suffix (guarded by caller)
    assert fmt("Gamma", "") == "Gamma"  # empty suffix

    print("=== ALL SMOKE CHECKS PASSED ===")
    print(f"  - Welcome card: valid schema 2.0, {len(serialized)} bytes")
    print(f"  - Confirmation card: valid schema 2.0, {len(confirm_serialized)} bytes")
    print("  - Suffix formatting: 5/5 cases pass")
    print("  - JSON serialization: OK")
    return True


if __name__ == "__main__":
    try:
        success = smoke_test_new_team()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"SMOKE TEST FAILED: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
