"""Tests for src.card.render.task_list module."""
from __future__ import annotations

import pytest
from src.card.state.models import TaskListBlock
from src.card.render.task_list import render_task_list_panel


def _make_block(tasks, current_task_id="t1"):
    return TaskListBlock(
        tasks=tuple(tasks),
        current_task_id=current_task_id,
    )


class TestRenderTaskListPanel:
    def test_basic_rendering(self):
        """Renders tasks with correct status icons."""
        tasks = [
            {"task_id": "t1", "name": "分析需求", "status": "in_progress"},
            {"task_id": "t2", "name": "编写代码", "status": "pending"},
        ]
        block = _make_block(tasks, "t1")
        result = render_task_list_panel(block)

        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is True
        content = result["elements"][0]["content"]
        assert "🔄" in content
        assert "⏳" in content
        assert "分析需求" in content
        assert "编写代码" in content

    def test_current_task_bold_with_arrow(self):
        """Current task is bolded with ▶ prefix."""
        tasks = [
            {"task_id": "t1", "name": "任务一", "status": "in_progress"},
            {"task_id": "t2", "name": "任务二", "status": "pending"},
        ]
        block = _make_block(tasks, "t1")
        result = render_task_list_panel(block)
        content = result["elements"][0]["content"]

        assert "▶" in content
        assert "**任务一**" in content
        # Non-current task should NOT be bold
        assert "**任务二**" not in content

    def test_all_status_icons(self):
        """All four status types render correct icons."""
        tasks = [
            {"task_id": "t1", "name": "A", "status": "pending"},
            {"task_id": "t2", "name": "B", "status": "in_progress"},
            {"task_id": "t3", "name": "C", "status": "completed"},
            {"task_id": "t4", "name": "D", "status": "failed"},
        ]
        block = _make_block(tasks, "t2")
        result = render_task_list_panel(block)
        content = result["elements"][0]["content"]

        assert "⏳" in content  # pending
        assert "🔄" in content  # in_progress
        assert "✅" in content  # completed
        assert "❌" in content  # failed

    def test_empty_tasks(self):
        """Empty tasks list returns None (no panel rendered)."""
        block = _make_block([], "")
        result = render_task_list_panel(block)
        assert result is None

    def test_compact_mode_shows_three_groups(self):
        """Compact mode keeps v2 sticky task context: active/done/pending groups."""
        tasks = [
            {"task_id": "t1", "name": "探索代码", "status": "completed"},
            {"task_id": "t2", "name": "修复路由", "status": "in_progress"},
            {"task_id": "t3", "name": "单元测试", "status": "pending"},
            {"task_id": "t4", "name": "集成测试", "status": "pending"},
            {"task_id": "t5", "name": "文档更新", "status": "pending"},
        ]
        block = _make_block(tasks, "t2")

        result = render_task_list_panel(block, compact=True)

        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is True
        content = result["elements"][0]["content"]
        assert "修复路由" in content
        assert "探索代码" in content
        assert "文档更新" in content
        assert "进行中 (1)" in content
        assert "已完成 (1)" in content
        assert "未处理 (3)" in content
        header_content = result["header"]["title"]["content"]
        assert "1/5" in header_content

    def test_compact_mode_keeps_in_progress_when_current_id_missing(self):
        """Compact mode still shows in-progress task if current_task_id is stale."""
        tasks = [
            {"task_id": "t1", "name": "已完成", "status": "completed"},
            {"task_id": "t2", "name": "正在执行", "status": "in_progress"},
            {"task_id": "t3", "name": "后续任务", "status": "pending"},
        ]
        block = _make_block(tasks, "missing")

        result = render_task_list_panel(block, compact=True)

        content = result["elements"][0]["content"]
        assert "正在执行" in content
        assert "后续任务" in content
        assert "未处理 (1)" in content

    def test_compact_empty_tasks(self):
        """Compact mode also returns None for empty task lists."""
        block = _make_block([], "")

        assert render_task_list_panel(block, compact=True) is None

    def test_fold_completed_when_over_threshold(self):
        """When > 5 tasks, completed tasks shown in gray compact format."""
        tasks = [
            {"task_id": f"t{i}", "name": f"Task {i}", "status": "completed"}
            for i in range(4)
        ] + [
            {"task_id": "t4", "name": "Running", "status": "in_progress"},
            {"task_id": "t5", "name": "Pending1", "status": "pending"},
        ]
        block = _make_block(tasks, "t4")
        result = render_task_list_panel(block)
        content = result["elements"][0]["content"]

        # Completed tasks shown with strikethrough in compact format
        assert "已完成 (4)" in content
        # Non-completed still shown individually
        assert "Running" in content
        assert "Pending1" in content

    def test_no_fold_under_threshold(self):
        """When <= 5 tasks, all shown individually."""
        tasks = [
            {"task_id": f"t{i}", "name": f"Task {i}", "status": "completed"}
            for i in range(5)
        ]
        block = _make_block(tasks, "t0")
        result = render_task_list_panel(block)
        content = result["elements"][0]["content"]

        # All shown individually, no folded tail summary
        assert "已完成 (5)" in content
        assert "还有" not in content
        for i in range(5):
            assert f"Task {i}" in content

    def test_panel_header(self):
        """Panel header shows task list title with progress summary."""
        block = _make_block([{"task_id": "t1", "name": "X", "status": "pending"}])
        result = render_task_list_panel(block)
        header_content = result["header"]["title"]["content"]
        assert "任务列表" in header_content
        assert "0/1 ✅" in header_content


class TestTaskListAtomOrdering:
    """Verify task_list atoms appear first in rendered card."""

    def test_task_list_first_in_status_section(self):
        from src.card.render.atoms import flatten_to_atoms, RenderAtom
        from src.card.render.renderer import _order_atoms_by_section
        from src.card.render.budget import RenderBudget
        from src.card.state.models import ContentBlock, TaskListBlock

        blocks = (
            ContentBlock(kind="text", block_id="txt1", content="Hello"),
            TaskListBlock(tasks=({"task_id": "t1", "name": "A", "status": "pending"},), current_task_id="t1"),
            ContentBlock(kind="phase", block_id="ph1", content="Phase 1"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        ordered = _order_atoms_by_section(atoms)

        assert ordered[0].kind == "task_list"
