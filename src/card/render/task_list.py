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
    "active": "🔄",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
}

_DONE_FOLD_THRESHOLD = 8
_PENDING_VISIBLE = 5
_ACTIVE_VISIBLE = 3


def group_tasks(plan_or_tasks) -> tuple[list, list, list]:
    """Group tasks into v2 buckets: in-progress, completed/terminal, pending."""
    tasks = getattr(plan_or_tasks, "entries", plan_or_tasks)
    in_progress: list = []
    completed: list = []
    pending: list = []

    for task in tasks or ():
        status = _task_status(task)
        if status in {"in_progress", "active", "running"}:
            in_progress.append(task)
        elif status in {"completed", "done", "failed", "cancelled"}:
            completed.append(task)
        else:
            pending.append(task)

    return in_progress, completed, pending


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

    lines = _build_v2_task_lines(tasks, current_id)
    content = "\n".join(lines)

    # Header with progress summary
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


def _build_v2_task_lines(tasks: tuple[TaskSnapshotPayload, ...], current_id: str) -> list[str]:
    """Build v2 three-bucket task markdown for sticky and full panels."""
    if not tasks:
        return []

    total = len(tasks)
    in_progress, completed, pending = group_tasks(tasks)
    lines: list[str] = []

    lines.append(f"▶ **进行中 ({len(in_progress)})**")
    for task in in_progress[:_ACTIVE_VISIBLE]:
        lines.append(_format_task_line(task, current_id, tasks, total))

    lines.append(f"✅ **已完成 ({len(completed)})**")
    done_visible = completed
    if len(completed) > _DONE_FOLD_THRESHOLD or total > 12:
        done_visible = completed[:3]
    for task in done_visible:
        lines.append(_format_done_line(task))
    if len(done_visible) < len(completed):
        lines.append(f"　…还有 {len(completed) - len(done_visible)} 个已完成")

    lines.append(f"⏳ **未处理 ({len(pending)})**")
    pending_visible = pending[:_PENDING_VISIBLE]
    for task in pending_visible:
        lines.append(_format_pending_line(task))
    if len(pending_visible) < len(pending):
        lines.append(f"　…还有 {len(pending) - len(pending_visible)} 个")
    return lines


def _format_task_line(task: TaskSnapshotPayload, current_id: str, tasks: tuple[TaskSnapshotPayload, ...], total: int) -> str:
    """Format a single task line with step number (step/total).

    Current in_progress task is highlighted with bold + arrow prefix.
    """
    task_id = _task_id(task)
    name = _task_name(task)
    status = _task_status(task)
    icon = _STATUS_ICONS.get(status, "⏳")
    step = next((idx + 1 for idx, item in enumerate(tasks) if _task_id(item) == task_id), 1)
    step_num = f"{step}/{total}"

    if task_id == current_id:
        return f"▶ {icon} {step_num} **{name}**"
    return f"　{icon} {step_num} {name}"


def _format_done_line(task) -> str:
    status = _task_status(task)
    icon = _STATUS_ICONS.get(status, "✅")
    return f"　{icon} ~~{_task_name(task)}~~"


def _format_pending_line(task) -> str:
    return f"　○ ⏳ {_task_name(task)}"


def _task_id(task) -> str:
    if isinstance(task, dict):
        return str(task.get("task_id", ""))
    return str(getattr(task, "task_id", ""))


def _task_name(task) -> str:
    if isinstance(task, dict):
        return str(task.get("name") or task.get("title") or "未命名任务")
    return str(getattr(task, "name", None) or getattr(task, "title", None) or "未命名任务")


def _task_status(task) -> str:
    if isinstance(task, dict):
        return str(task.get("status", "pending"))
    return str(getattr(task, "status", "pending"))
