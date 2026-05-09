"""Task list panel rendering for task-level card management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.card.state.models import TaskListBlock
from src.card.themes import PANEL_STYLES

if TYPE_CHECKING:
    from src.card.events.payloads import TaskSnapshotPayload

_STATUS_ICONS = {
    "pending": "⏳",
    "in_progress": "🔄",
    "completed": "✅",
    "failed": "❌",
}

_FOLD_THRESHOLD = 5


def render_task_list_panel(block: TaskListBlock, *, compact: bool = False) -> dict | None:
    """Render the task list panel with progress summary.

    Returns None if tasks is empty (no panel rendered).
    Compact mode shows only the current task and progress ratio for sticky_head.
    When total tasks >= _FOLD_THRESHOLD, panel is collapsed by default and
    completed tasks are shown in gray compact format.
    Current task is highlighted with bold + arrow prefix.
    """
    tasks = block.tasks
    if not tasks:
        return None

    current_id = block.current_task_id
    total = len(tasks)
    completed_count = sum(1 for t in tasks if t.get("status") == "completed")

    if compact:
        return _build_compact_panel(tasks, current_id, completed_count, total)

    lines = _build_task_lines(tasks, current_id)
    content = "\n".join(lines)

    # Dynamic expand: collapse when tasks >= threshold
    expanded = total < _FOLD_THRESHOLD

    # Header with progress summary
    header_title = f"📋 **任务列表** — 进度：{completed_count}/{total} ✅"

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": header_title},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_task_list"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content}],
    }


def _build_compact_panel(
    tasks: tuple[TaskSnapshotPayload, ...],
    current_id: str,
    completed_count: int,
    total: int,
) -> dict:
    """Build compact task list panel for sticky_head.

    Shows one task only (current task, then in_progress fallback, then pending,
    then first task) while keeping progress in the header.
    """
    current_task = next((t for t in tasks if t.get("task_id") == current_id), None)
    if current_task is None:
        current_task = (
            next((t for t in tasks if t.get("status") == "in_progress"), None)
            or next((t for t in tasks if t.get("status") == "pending"), None)
            or tasks[0]
        )

    current_task_id = current_task.get("task_id", "")
    step_idx = next((idx + 1 for idx, t in enumerate(tasks) if t.get("task_id") == current_task_id), 1)
    content = _format_task_line(current_task, current_task_id, step_idx, total)
    header_title = f"📋 **任务列表** — 进度：{completed_count}/{total} ✅"

    return {
        "tag": "collapsible_panel",
        "expanded": True,
        "header": {
            "title": {"tag": "markdown", "content": header_title},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_task_list"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content}],
    }


def _build_task_lines(tasks: tuple[TaskSnapshotPayload, ...], current_id: str) -> list[str]:
    """Build markdown lines for the task list.

    Uses enumerate (O(n)) instead of tuple.index() to avoid O(n²).
    When total > _FOLD_THRESHOLD, completed tasks are shown in gray compact format.
    """
    if not tasks:
        return []

    total = len(tasks)
    should_fold = total > _FOLD_THRESHOLD

    if not should_fold:
        return [_format_task_line(t, current_id, idx + 1, total) for idx, t in enumerate(tasks)]

    # Fold mode: show non-completed tasks first with full format,
    # then show completed tasks in gray compact format
    lines: list[str] = []
    completed_lines: list[str] = []

    for idx, t in enumerate(tasks):
        status = t.get("status", "pending")
        if status == "completed":
            # Gray compact: just icon + name (no bold, no step number)
            name = t.get("name", "未命名任务")
            completed_lines.append(f"　~~{name}~~")
        else:
            lines.append(_format_task_line(t, current_id, idx + 1, total))

    # Append completed summary + list
    if completed_lines:
        lines.append(f"✅ **已完成 ({len(completed_lines)})**")
        lines.extend(completed_lines)

    return lines


def _format_task_line(task: TaskSnapshotPayload, current_id: str, step: int, total: int) -> str:
    """Format a single task line with step number (step/total).

    Current in_progress task is highlighted with bold + arrow prefix.
    """
    task_id = task.get("task_id", "")
    name = task.get("name", "未命名任务")
    status = task.get("status", "pending")
    icon = _STATUS_ICONS.get(status, "⏳")
    step_num = f"{step}/{total}"

    if task_id == current_id:
        return f"▶ {icon} {step_num} **{name}**"
    return f"　{icon} {step_num} {name}"
