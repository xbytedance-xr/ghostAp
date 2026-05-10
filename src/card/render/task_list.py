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
    "cancelled": "⊘",
}

_ACTIVE_STATUSES = frozenset({"in_progress", "active", "running"})
_COMPLETED_STATUSES = frozenset({"completed", "done"})
_FAILED_STATUSES = frozenset({"failed", "cancelled"})

_DONE_FOLD_THRESHOLD = 8
_PENDING_VISIBLE = 5
_ACTIVE_VISIBLE = 3


def group_tasks(plan_or_tasks) -> tuple[list, list, list]:
    """Group tasks into v2 buckets: in-progress, ended, pending."""
    tasks = getattr(plan_or_tasks, "entries", plan_or_tasks)
    in_progress: list = []
    completed: list = []
    pending: list = []

    for task in tasks or ():
        status = _task_status(task)
        if status in _ACTIVE_STATUSES:
            in_progress.append(task)
        elif status in _COMPLETED_STATUSES or status in _FAILED_STATUSES:
            completed.append(task)
        else:
            pending.append(task)

    return in_progress, completed, pending


def render_task_list_panel(block: TaskListBlock, *, compact: bool = False) -> dict | None:
    """Render the task list panel with progress summary.

    Returns None if tasks is empty (no panel rendered).
    Compact mode keeps the same three always-open groups while applying the
    same height-saving downgrade rules as the full panel.
    Current task is highlighted with bold + arrow prefix.
    """
    tasks = block.tasks
    if not tasks:
        return None

    current_id = block.current_task_id
    total = len(tasks)
    completed_count = sum(1 for t in tasks if str(t.get("status")) in _COMPLETED_STATUSES)

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

    completed_only = sum(1 for task in completed if _task_status(task) in _COMPLETED_STATUSES)
    failed_count = len(completed) - completed_only
    if failed_count:
        ended_label = f"✅ **已结束 ({len(completed)})** · 完成 {completed_only} / 失败 {failed_count}"
    else:
        ended_label = f"✅ **已完成 ({len(completed)})**"
    lines.append(ended_label)
    done_visible = completed
    if len(completed) > _DONE_FOLD_THRESHOLD or total > 12:
        done_visible = completed[:3]
    for task in done_visible:
        lines.append(_format_done_line(task))
    if len(done_visible) < len(completed):
        folded_label = "已结束" if failed_count else "已完成"
        lines.append(f"　…还有 {len(completed) - len(done_visible)} 个{folded_label}")

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
    return f"　○ {_task_name(task)}"


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
