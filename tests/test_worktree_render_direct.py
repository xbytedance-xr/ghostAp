"""Direct unit tests for render_worktree_panel function.

Covers boundary cases: data=None, empty lists, missing fields, unknown kinds.
"""
import pytest

from src.card.events import CardEvent
from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card
from src.card.state.models import CardMetadata, CardState
from src.card.state.reducer import reduce_card_state
from src.card.render.worktree import render_worktree_panel
from src.card.state.models import (
    WorktreeSelectBlock,
    WorktreeConfirmBlock,
    WorktreeUnitsBlock,
    WorktreeMergeBlock,
    WorktreeCleanupBlock,
)


def _collect_buttons(node):
    if isinstance(node, dict):
        found = [node] if node.get("tag") == "button" else []
        for value in node.values():
            found.extend(_collect_buttons(value))
        return found
    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(_collect_buttons(item))
        return found
    return []


def _assert_callback_button(button, expected_value):
    assert button.get("value") == expected_value
    assert button.get("behaviors") == [
        {"type": "callback", "value": expected_value}
    ]


class TestRenderWorktreePanelDataNone:
    """block.data=None should produce a safe fallback."""

    def test_tool_select_data_none(self):
        block = WorktreeSelectBlock(block_id="ts", data=None)
        result = render_worktree_panel(block)
        assert result is not None
        assert result.get("tag") in ("markdown", "collapsible_panel", "column_set")

    def test_confirm_data_none(self):
        block = WorktreeConfirmBlock(block_id="cf", data=None)
        result = render_worktree_panel(block)
        assert result is not None

    def test_units_data_none(self):
        block = WorktreeUnitsBlock(block_id="un", data=None)
        result = render_worktree_panel(block)
        assert result is not None
        assert result.get("tag") == "markdown"  # Fallback for None data

    def test_merge_data_none(self):
        block = WorktreeMergeBlock(block_id="mg", data=None)
        result = render_worktree_panel(block)
        assert result is not None

    def test_cleanup_data_none(self):
        block = WorktreeCleanupBlock(block_id="cl", data=None)
        result = render_worktree_panel(block)
        assert result is not None


class TestRenderWorktreePanelEmptyLists:
    """Empty lists in data should not crash."""

    def test_tool_select_empty_tools(self):
        block = WorktreeSelectBlock(block_id="ts", data={"tools": [], "selected": []})
        result = render_worktree_panel(block)
        assert result is not None

    def test_units_empty_units(self):
        block = WorktreeUnitsBlock(block_id="un", data={"units": [], "message": ""})
        result = render_worktree_panel(block)
        # render_worktree_panel wraps in column_set with stepper elements + content
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        # Stepper produces 2 markdown elements (active + pending) + 1 content element
        assert len(elements) >= 2
        # First element is stepper markdown (active part)
        assert elements[0]["tag"] == "markdown"
        # Last element is the collapsible_panel
        panel = elements[-1]
        assert panel["tag"] == "collapsible_panel"
        assert len(panel["elements"]) >= 1

    def test_merge_empty_notes(self):
        block = WorktreeMergeBlock(block_id="mg", data={"merge_notes": [], "base_branch": "main"})
        result = render_worktree_panel(block)
        assert result is not None

    def test_cleanup_empty_notes(self):
        block = WorktreeCleanupBlock(block_id="cl", data={
            "merge_notes": [], "base_branch": "main",
            "merge_results": None, "cleanup_phase": "summary",
        })
        result = render_worktree_panel(block)
        assert result is not None


class TestRenderWorktreePanelMissingFields:
    """Dicts with missing expected keys should use defaults."""

    def test_unit_missing_name_and_status(self):
        block = WorktreeUnitsBlock(block_id="un", data={
            "units": [{}],  # No name, no status
            "message": "",
        })
        result = render_worktree_panel(block)
        # Wrapped in column_set with stepper
        assert result["tag"] == "column_set"
        panel = result["columns"][0]["elements"][-1]
        assert panel["tag"] == "collapsible_panel"

    def test_tool_select_missing_selected(self):
        block = WorktreeSelectBlock(block_id="ts", data={
            "tools": [{"name": "tool1"}],
            # 'selected' key missing
        })
        result = render_worktree_panel(block)
        assert result is not None

    def test_merge_note_missing_status(self):
        block = WorktreeMergeBlock(block_id="mg", data={
            "merge_notes": [{"branch": "feat-1"}],  # No 'status'
            "base_branch": "main",
        })
        result = render_worktree_panel(block)
        assert result is not None


class TestRenderWorktreeToolSelectInteractions:
    """Tool selection rows must be real Feishu actions, not static markdown checkboxes."""

    def test_tool_select_panel_renders_tool_callback_buttons(self):
        block = WorktreeSelectBlock(
            block_id="ts",
            data={
                "project_id": "proj-1",
                "tools": [
                    {
                        "provider": "acp",
                        "tool_name": "coco",
                        "display_name": "Coco",
                        "description": "字节跳动 AI",
                        "supports_model": True,
                    },
                    {
                        "provider": "cli",
                        "tool_name": "claude",
                        "display_name": "Claude",
                        "description": "Anthropic Claude CLI",
                        "supports_model": False,
                    },
                ],
                "selected": [{"tool_name": "coco", "display_name": "Coco"}],
            },
        )

        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)
        expected = {
            "action": "worktree_select_tool",
            "tool_name": "coco",
            "display_name": "Coco",
            "agent_name": "",
            "provider": "acp",
            "supports_model": True,
            "skip_model_selection": False,
            "project_id": "proj-1",
        }

        coco_button = next(
            btn for btn in buttons
            if btn.get("value", {}).get("tool_name") == "coco"
        )
        _assert_callback_button(coco_button, expected)
        assert any(
            btn.get("value", {}).get("tool_name") == "claude"
            and btn.get("behaviors", [{}])[0].get("type") == "callback"
            for btn in buttons
        )

    def test_reducer_render_path_keeps_tool_buttons_interactive(self):
        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            CardEvent.worktree_tool_select(
                tools=[
                    {
                        "provider": "acp",
                        "tool_name": "coco",
                        "display_name": "Coco",
                        "description": "字节跳动 AI",
                        "supports_model": True,
                    }
                ],
                selected=[],
                project_id="proj-render",
            ),
        )

        rendered = render_card(state, RenderBudget())[0].to_feishu_json()
        buttons = _collect_buttons(rendered)
        button = next(
            btn for btn in buttons
            if (
                btn.get("value", {}).get("action") == "worktree_select_tool"
                and btn.get("value", {}).get("tool_name") == "coco"
                and btn.get("value", {}).get("project_id") == "proj-render"
            )
        )
        assert button.get("behaviors") == [
            {"type": "callback", "value": button.get("value")}
        ]

    def test_selected_actions_render_callback_behaviors(self):
        block = WorktreeSelectBlock(
            block_id="ts",
            data={
                "project_id": "proj-1",
                "tools": [{"provider": "cli", "tool_name": "claude", "display_name": "Claude"}],
                "selected": [{"selection_key": "cli:claude:default", "display_label": "CLI · Claude"}],
            },
        )

        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)

        for action in ("worktree_remove_item", "worktree_clear_items", "worktree_finish_selection"):
            button = next(
                btn for btn in buttons
                if btn.get("value", {}).get("action") == action
            )
            assert button.get("behaviors") == [
                {"type": "callback", "value": button.get("value")}
            ]

    def test_reducer_render_path_keeps_tool_buttons_interactive_legacy_value(self):
        state = CardState(metadata=CardMetadata(engine_type="worktree"))
        state = reduce_card_state(
            state,
            CardEvent.worktree_tool_select(
                tools=[
                    {
                        "provider": "acp",
                        "tool_name": "coco",
                        "display_name": "Coco",
                        "description": "字节跳动 AI",
                        "supports_model": True,
                    }
                ],
                selected=[],
                project_id="proj-render",
            ),
        )

        rendered = render_card(state, RenderBudget())[0].to_feishu_json()
        buttons = _collect_buttons(rendered)
        assert any(
            btn.get("value", {}).get("action") == "worktree_select_tool"
            and btn.get("value", {}).get("tool_name") == "coco"
            and btn.get("value", {}).get("project_id") == "proj-render"
            for btn in buttons
        )

    def test_model_select_panel_routes_model_callbacks(self):
        block = WorktreeSelectBlock(
            block_id="models",
            data={
                "project_id": "proj-2",
                "select_action": "worktree_select_model",
                "tools": [
                    {
                        "id": "gpt-5.2",
                        "name": "GPT-5.2",
                        "description": "模型: GPT-5.2",
                    }
                ],
                "selected": [],
            },
        )

        result = render_worktree_panel(block)
        buttons = _collect_buttons(result)
        expected = {
            "action": "worktree_select_model",
            "model_name": "gpt-5.2",
            "model_display_name": "GPT-5.2",
            "project_id": "proj-2",
        }
        button = next(
            btn for btn in buttons
            if btn.get("value", {}).get("action") == "worktree_select_model"
        )
        _assert_callback_button(button, expected)


class TestRenderWorktreePanelUnknownKind:
    """Unknown block kind should produce a safe fallback."""

    def test_unknown_kind_returns_fallback(self):
        # Use a worktree block but give it data so it passes the None check,
        # then test the else branch by using a block whose kind isn't in the dispatch
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class FakeBlock:
            kind: str = "worktree_unknown"
            block_id: str = "fake"
            content: str = "fallback content"
            data: dict | None = None

        block = FakeBlock(data={"something": True})
        result = render_worktree_panel(block)
        # Falls through to the else branch: returns markdown with block.content
        assert result == {"tag": "markdown", "content": "fallback content"}


class TestRenderWorktreeUnitsElapsedTime:
    """Running units should display elapsed time from metadata.started_at."""

    def test_running_unit_shows_elapsed_seconds(self):
        import time
        block = WorktreeUnitsBlock(
            block_id="progress",
            data={
                "units": [
                    {
                        "unit_id": "wt-01",
                        "display_name": "工作空间 A",
                        "status": "running",
                        "summary": "",
                        "error": "",
                        "metadata": {"started_at": time.time() - 30},
                    }
                ],
                "message": "",
            },
        )
        result = render_worktree_panel(block)
        # Should contain time indication (Ns or Nmin)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        assert "工作空间 A" in rendered
        # 30 seconds → shows "30s" or similar
        assert "s)" in rendered or "min)" in rendered

    def test_running_unit_shows_elapsed_minutes(self):
        import time
        block = WorktreeUnitsBlock(
            block_id="progress",
            data={
                "units": [
                    {
                        "unit_id": "wt-02",
                        "display_name": "工作空间 B",
                        "status": "running",
                        "summary": "",
                        "error": "",
                        "metadata": {"started_at": time.time() - 180},
                    }
                ],
                "message": "",
            },
        )
        result = render_worktree_panel(block)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        assert "3min)" in rendered

    def test_completed_unit_no_elapsed_time(self):
        import time
        block = WorktreeUnitsBlock(
            block_id="progress",
            data={
                "units": [
                    {
                        "unit_id": "wt-03",
                        "display_name": "工作空间 C",
                        "status": "completed",
                        "summary": "Done",
                        "error": "",
                        "metadata": {"started_at": time.time() - 60},
                    }
                ],
                "message": "",
            },
        )
        result = render_worktree_panel(block)
        import json
        rendered = json.dumps(result, ensure_ascii=False)
        # Completed units should NOT show elapsed time
        assert "min)" not in rendered
        assert "s)" not in rendered


class TestWorktreeStepperHintRendering:
    """AC-14: Each worktree step panel includes a hint notation element."""

    def test_tool_select_has_hint(self):
        block = WorktreeSelectBlock(block_id="ts", data={"tools": [{"name": "Coco"}], "selected": []})
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        # Find hint element
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "工具" in el.get("content", "")]
        assert len(hints) >= 1, "Tool select panel should have a hint element"

    def test_confirm_has_hint(self):
        block = WorktreeConfirmBlock(block_id="cf", data={
            "selected": [{"name": "Coco"}], "goal": "test goal", "project_id": "p1"
        })
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "确认" in el.get("content", "")]
        assert len(hints) >= 1, "Confirm panel should have a hint element"

    def test_units_has_hint(self):
        block = WorktreeUnitsBlock(block_id="un", data={"units": [], "message": ""})
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "执行中" in el.get("content", "")]
        assert len(hints) >= 1, "Units panel should have a hint element"

    def test_merge_has_hint(self):
        block = WorktreeMergeBlock(block_id="mg", data={"merge_notes": [], "base_branch": "main"})
        result = render_worktree_panel(block)
        assert result["tag"] == "column_set"
        elements = result["columns"][0]["elements"]
        hints = [el for el in elements
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "合并" in el.get("content", "")]
        assert len(hints) >= 1, "Merge panel should have a hint element"
