"""Direct unit tests for render_worktree_panel function.

Covers boundary cases: data=None, empty lists, missing fields, unknown kinds.
"""
import pytest

from src.card.render.worktree import render_worktree_panel
from src.card.state.models import (
    WorktreeSelectBlock,
    WorktreeConfirmBlock,
    WorktreeUnitsBlock,
    WorktreeMergeBlock,
    WorktreeCleanupBlock,
)


class TestRenderWorktreePanelDataNone:
    """block.data=None should produce a safe fallback."""

    def test_tool_select_data_none(self):
        block = WorktreeSelectBlock(block_id="ts", data=None)
        result = render_worktree_panel(block)
        assert result is not None
        assert result.get("tag") in ("markdown", "collapsible_panel", "div")

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
        # render_worktree_panel wraps in div with stepper elements + content
        assert result["tag"] == "div"
        # Stepper produces 2 markdown elements (active + pending) + 1 content element
        assert len(result["elements"]) >= 2
        # First element is stepper markdown (active part)
        assert result["elements"][0]["tag"] == "markdown"
        # Last element is the collapsible_panel
        panel = result["elements"][-1]
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
        # Wrapped in div with stepper
        assert result["tag"] == "div"
        panel = result["elements"][-1]
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
        assert result["tag"] == "div"
        # Find hint element
        hints = [el for el in result["elements"]
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "工具" in el.get("content", "")]
        assert len(hints) >= 1, "Tool select panel should have a hint element"

    def test_confirm_has_hint(self):
        block = WorktreeConfirmBlock(block_id="cf", data={
            "selected": [{"name": "Coco"}], "goal": "test goal", "project_id": "p1"
        })
        result = render_worktree_panel(block)
        assert result["tag"] == "div"
        hints = [el for el in result["elements"]
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "确认" in el.get("content", "")]
        assert len(hints) >= 1, "Confirm panel should have a hint element"

    def test_units_has_hint(self):
        block = WorktreeUnitsBlock(block_id="un", data={"units": [], "message": ""})
        result = render_worktree_panel(block)
        assert result["tag"] == "div"
        hints = [el for el in result["elements"]
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "执行中" in el.get("content", "")]
        assert len(hints) >= 1, "Units panel should have a hint element"

    def test_merge_has_hint(self):
        block = WorktreeMergeBlock(block_id="mg", data={"merge_notes": [], "base_branch": "main"})
        result = render_worktree_panel(block)
        assert result["tag"] == "div"
        hints = [el for el in result["elements"]
                 if el.get("tag") == "markdown"
                 and el.get("text_size") == "notation"
                 and "合并" in el.get("content", "")]
        assert len(hints) >= 1, "Merge panel should have a hint element"
