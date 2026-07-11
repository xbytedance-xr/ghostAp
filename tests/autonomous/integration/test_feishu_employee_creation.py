"""Integration tests for Feishu interaction: employee creation via cards."""

from __future__ import annotations

import pytest

from src.autonomous.manager.cards import (
    build_approval_card,
    build_employee_created_card,
    build_employee_creation_card,
    build_goal_progress_card,
)


def test_employee_creation_card_has_required_fields() -> None:
    card = build_employee_creation_card()

    # Has header
    assert card["header"]["title"]["content"] == "Create New Employee"

    # Has action elements with select fields
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert len(actions) >= 3  # role, tool, model selects + buttons

    # Has create button
    all_buttons = []
    for action_group in actions:
        for a in action_group.get("actions", []):
            if a.get("tag") == "button":
                all_buttons.append(a)
    create_buttons = [b for b in all_buttons if b.get("value", {}).get("action") == "create_employee"]
    assert len(create_buttons) == 1


def test_employee_creation_card_custom_options() -> None:
    card = build_employee_creation_card(
        available_roles=["analyst", "auditor"],
        available_tools=["custom_tool"],
        available_models=["custom_model"],
    )
    elements_json = str(card)
    assert "analyst" in elements_json
    assert "custom_tool" in elements_json
    assert "custom_model" in elements_json


def test_employee_created_card_shows_details() -> None:
    card = build_employee_created_card(
        employee_id="emp_abc123",
        name="coder_codex",
        role="coder",
        tool="codex",
        model="gpt-4o",
        worker_type="logical",
    )
    assert card["header"]["template"] == "green"
    content = str(card)
    assert "emp_abc123" in content
    assert "coder" in content
    assert "codex" in content
    assert "gpt-4o" in content


def test_goal_progress_card() -> None:
    card = build_goal_progress_card(
        goal_id="goal_x",
        description="Improve test coverage",
        state="executing",
        run_id="run_1",
        step_progress="2/5",
    )
    assert card["header"]["template"] == "wathet"
    content = str(card)
    assert "goal_x" in content
    assert "2/5" in content


def test_approval_card_high_risk() -> None:
    card = build_approval_card(
        approval_id="appr_1",
        description="Delete production database",
        risk_level="r4",
        effect_summary="DROP TABLE users",
    )
    assert card["header"]["template"] == "red"
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert len(actions) >= 1


def test_approval_card_low_risk() -> None:
    card = build_approval_card(
        approval_id="appr_2",
        description="Read config file",
        risk_level="r1",
        effect_summary="cat config.yaml",
    )
    assert card["header"]["template"] == "orange"


@pytest.mark.asyncio
async def test_employee_creation_via_container() -> None:
    """Test the full employee creation flow via AutonomousContainer."""
    from src.autonomous.bootstrap import AutonomousContainer

    container = AutonomousContainer(mode="manager_only")
    await container.start()

    result = await container.handle_employee_creation(
        chat_id="oc_test_chat",
        role="coder",
        tool="codex",
        model="gpt-4o",
        user_id="ou_test_user",
    )

    assert result["role"] == "coder"
    assert result["tool"] == "codex"
    assert result["model"] == "gpt-4o"
    assert result["employee_id"].startswith("emp_")

    await container.shutdown()
