"""Tests for the worktree selection-loop card flow (state transitions).

These tests exercise WorktreeManager's selection state machine directly,
without involving Feishu API or card rendering.
"""

from unittest.mock import MagicMock

from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.selection import WorktreeToolOption, format_selection_lines


def _make_project():
    p = MagicMock()
    p.project_id = "proj-test"
    p.project_name = "TestProject"
    p.root_path = "/tmp/test"
    return p


def _make_manager():
    pm = MagicMock()
    return WorktreeManager(pm)


TOOL_ACP = WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco", supports_model=False)
TOOL_TTADK = WorktreeToolOption(provider="ttadk", tool_name="codex", display_name="Codex", supports_model=True)
TOOL_CLI = WorktreeToolOption(provider="cli", tool_name="claude", display_name="Claude", supports_model=False)


def test_full_selection_cycle():
    """tool_select → model_select → review → continue → tool_select → finish → ready."""
    mgr = _make_manager()
    project = _make_project()

    # Start
    state = mgr.start_selection(project)
    assert state.selection.stage == "tool_select"
    assert state.selection.active is True

    # Select TTADK tool (supports model)
    state = mgr.select_tool(project, TOOL_TTADK)
    assert state.selection.stage == "model_select"
    assert state.selection.pending_item is not None

    # Add model
    state, added, msg = mgr.add_pending_item(project, model_name="gpt-4.1")
    assert added is True
    assert state.selection.stage == "review"
    assert len(state.selection.selected_items) == 1

    # Continue → back to tool_select
    state = mgr.back_to_tool_selection(project)
    assert state.selection.stage == "tool_select"

    # Select ACP tool (no model)
    state = mgr.select_tool(project, TOOL_ACP)
    assert state.selection.stage == "review"  # skipped model_select

    # Add without model
    state, added, _ = mgr.add_pending_item(project)
    assert added is True
    assert len(state.selection.selected_items) == 2

    # Finalize
    state = mgr.finalize_selection(project)
    assert state.selection.stage == "ready"
    assert state.enabled is True
    assert len(state.summary_lines) == 2


def test_skip_model_for_non_ttadk():
    """supports_model=False skips model_select → goes directly to review."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    state = mgr.select_tool(project, TOOL_ACP)
    assert state.selection.stage == "review"
    assert state.selection.pending_item is not None


def test_model_select_for_ttadk():
    """supports_model=True goes to model_select."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    state = mgr.select_tool(project, TOOL_TTADK)
    assert state.selection.stage == "model_select"


def test_selection_lines_update():
    """Each add_pending_item updates the numbered list correctly."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    mgr.select_tool(project, TOOL_ACP)
    mgr.add_pending_item(project)
    state = mgr.get_state(project)
    lines = format_selection_lines(state.selection.selected_items)
    assert len(lines) == 1
    assert "1." in lines[0]

    mgr.back_to_tool_selection(project)
    mgr.select_tool(project, TOOL_CLI)
    mgr.add_pending_item(project)
    state = mgr.get_state(project)
    lines = format_selection_lines(state.selection.selected_items)
    assert len(lines) == 2
    assert "2." in lines[1]


def test_duplicate_selection_ignored():
    """Duplicate tool-model pair should be ignored."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    mgr.select_tool(project, TOOL_ACP)
    mgr.add_pending_item(project)

    mgr.back_to_tool_selection(project)
    mgr.select_tool(project, TOOL_ACP)
    _, added, _ = mgr.add_pending_item(project)
    assert added is False

    state = mgr.get_state(project)
    assert len(state.selection.selected_items) == 1


def test_finalize_empty_selection_fails():
    """Finalize without any selection does not enable worktree mode."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    state = mgr.finalize_selection(project)
    assert state.enabled is False
    assert state.selection.stage == "tool_select"  # stays at tool_select


def test_reset_selection_clears_all():
    """reset_selection should clear all previous selections."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)
    mgr.select_tool(project, TOOL_ACP)
    mgr.add_pending_item(project)
    assert len(mgr.get_state(project).selection.selected_items) == 1

    state = mgr.reset_selection(project)
    assert len(state.selection.selected_items) == 0
    assert state.selection.stage == "tool_select"


def test_worktree_tool_select_card_renders_buttons():
    """Card JSON has correct header, action buttons with worktree_select_tool action."""
    import json

    from src.card.builders.worktree import WorktreeBuilder

    def _find_all_buttons(elements):
        """递归从 elements 树中提取所有 button。"""
        buttons = []
        for el in elements:
            if el.get("tag") == "button":
                buttons.append(el)
            elif el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    buttons.extend(_find_all_buttons(col.get("elements", [])))
            elif el.get("tag") == "column":
                buttons.extend(_find_all_buttons(el.get("elements", [])))
            elif el.get("tag") == "action":
                buttons.extend(_find_all_buttons(el.get("actions", [])))
        return buttons

    tools = [
        {
            "tool_name": "coco",
            "display_name": "Coco",
            "provider": "acp",
            "supports_model": False,
            "description": "字节跳动 AI 编程",
        },
        {
            "tool_name": "claude",
            "display_name": "Claude",
            "provider": "cli",
            "supports_model": False,
            "description": "Anthropic Claude CLI",
        },
    ]
    msg_type, card_json = WorktreeBuilder.build_worktree_tool_select_card(
        tools, selected_items=[], project_id="proj-test",
    )

    assert msg_type == "interactive"
    card = json.loads(card_json)

    # Header check
    assert card["header"]["title"]["content"] == "🌳 Worktree — 选择工具"
    assert card["header"]["template"] == "turquoise"

    # Find buttons in card elements (schema 2.0: buttons inside column_set grid)
    elements = card["body"]["elements"]
    buttons = _find_all_buttons(elements)
    tool_buttons = [b for b in buttons if b.get("value", {}).get("action") == "worktree_select_tool"]
    assert len(tool_buttons) == len(tools)

    for btn in tool_buttons:
        assert btn["tag"] == "button"
        assert btn["value"]["action"] == "worktree_select_tool"
        assert btn["value"]["project_id"] == "proj-test"

    # Button labels match tool display names
    btn_labels = [btn["text"]["content"] for btn in tool_buttons]
    assert btn_labels == ["Coco", "Claude"]
