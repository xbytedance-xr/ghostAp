"""Worktree-specific rendering functions.

Converts worktree structured data (from ContentBlock.data dict)
into formatted Feishu Schema 2.0 markdown elements.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.card.state.models import ContentBlock
from src.card.themes import PANEL_STYLES
from src.card.ui_text import UI_TEXT

if TYPE_CHECKING:
    from src.worktree_engine.models import WorktreeInfo, WorktreeRuntimeState, WorktreeUnit


# ---------------------------------------------------------------------------
# WorktreeCallbacks protocol (migrated from src/card/protocols.py)
# ---------------------------------------------------------------------------


@runtime_checkable
class WorktreeCallbacks(Protocol):
    """Protocol defining the reporter interface for worktree engine card rendering.

    WorktreeReporter must satisfy this protocol.
    """

    def refresh_state(self, state: "WorktreeRuntimeState") -> "WorktreeRuntimeState": ...

    def build_unit_summary_lines(self, units: "list[WorktreeUnit]") -> list[str]: ...

    def build_merge_notes(self, units: "list[WorktreeUnit]", base_branch: str) -> list[dict]: ...

    def format_worktree_table(self, entries: "list[WorktreeInfo]") -> str: ...


# ---------------------------------------------------------------------------
# Worktree step mapping
# ---------------------------------------------------------------------------

# Worktree step mapping: block.kind → (step_index_0based, total_steps)
_STEP_MAP: dict[str, int] = {
    "worktree_tool_select": 0,
    "worktree_confirm": 1,
    "worktree_units": 2,
    "worktree_merge": 3,
    "worktree_cleanup": 3,
}
_TOTAL_STEPS = 4

# Map block.kind → UI_TEXT key for step title (used to generate dynamic "步骤 N/M · title")
_STEP_TITLE_KEY_MAP: dict[str, str] = {
    "worktree_tool_select": "worktree_step_tool_select",
    "worktree_confirm": "worktree_step_confirm",
    "worktree_units": "worktree_step_units",
    "worktree_merge": "worktree_step_merge",
    "worktree_cleanup": "worktree_step_cleanup",
}

# Map block.kind → UI_TEXT key for step hint (grey notation below stepper)
_STEP_HINT_KEY_MAP: dict[str, str] = {
    "worktree_tool_select": "worktree_step_tool_select_hint",
    "worktree_confirm": "worktree_step_confirm_hint",
    "worktree_units": "worktree_step_units_hint",
    "worktree_merge": "worktree_step_merge_hint",
    "worktree_cleanup": "worktree_step_cleanup_hint",
}

# Import-time assertion: every kind in _STEP_MAP must have a corresponding UI_TEXT title key
_missing_title_keys = [
    kind for kind in _STEP_MAP
    if _STEP_TITLE_KEY_MAP.get(kind) not in UI_TEXT
]
if _missing_title_keys:
    raise RuntimeError(
        f"_STEP_MAP contains kinds without corresponding UI_TEXT title keys: {_missing_title_keys}"
    )
del _missing_title_keys


def _render_stepper(current_step: int) -> list[dict]:
    """Render an inline stepper with two segments for visual hierarchy.

    Returns two markdown elements:
    - Active part (completed ✓ + current ◉): default/theme color
    - Pending part (○): grey color

    Example output: ['✓ ✓ ◉', '○ ○'] rendered as '✓ ✓ ◉ ○ ○ (3/5)'
    """
    active_parts = []
    pending_parts = []
    for i in range(_TOTAL_STEPS):
        if i < current_step:
            active_parts.append("✓")
        elif i == current_step:
            active_parts.append("◉")
        else:
            pending_parts.append("○")
    step_num = current_step + 1
    step_counter = f"({step_num}/{_TOTAL_STEPS})"

    elements: list[dict] = []
    if active_parts:
        active_text = " ".join(active_parts)
        if pending_parts:
            # Active part in default color (no text_color = inherits theme)
            elements.append({"tag": "markdown", "content": f"{active_text} ", "text_size": "notation"})
            # Pending part in grey
            pending_text = " ".join(pending_parts) + f" {step_counter}"
            elements.append({"tag": "markdown", "content": pending_text, "text_size": "notation"})
        else:
            # All steps complete
            elements.append({"tag": "markdown", "content": f"{active_text} {step_counter}", "text_size": "notation"})
    else:
        # First step is current (shouldn't happen but handle gracefully)
        pending_text = " ".join(pending_parts) + f" {step_counter}"
        elements.append({"tag": "markdown", "content": pending_text, "text_size": "notation"})

    return elements


def render_worktree_panel(block: ContentBlock) -> dict:
    """Render worktree structured data to formatted markdown.

    Args:
        block: The ContentBlock containing worktree structured data in .data field.

    Follows the same contract as render_plan_panel/render_tool_panel etc.:
    the caller (renderer.py) is responsible for block lookup from the atom.

    Includes an inline stepper (● ○ ○ ○) at the top to indicate current step.
    """
    data = block.data
    if data is None:
        return {"tag": "markdown", "content": UI_TEXT["worktree_render_load_failed"].format(reason=UI_TEXT["worktree_data_empty"])}

    kind = block.kind

    if kind == "worktree_tool_select":
        content_el = _render_worktree_tool_select(data)
    elif kind == "worktree_confirm":
        content_el = _render_worktree_confirm(data)
    elif kind == "worktree_units":
        content_el = _render_worktree_units(data)
    elif kind == "worktree_merge":
        content_el = _render_worktree_merge(data)
    elif kind == "worktree_cleanup":
        content_el = _render_worktree_cleanup(data)
    else:
        return {"tag": "markdown", "content": block.content}

    # Prepend inline stepper + dynamic step title if this kind has a known step
    step_idx = _STEP_MAP.get(kind)
    if step_idx is not None:
        stepper_elements = _render_stepper(step_idx)
        # Generate dynamic step title: "步骤 N/M · {title}"
        title_key = _STEP_TITLE_KEY_MAP.get(kind, "")
        step_title = UI_TEXT.get(title_key, "")
        step_num = step_idx + 1
        step_label = f"**{UI_TEXT['worktree_step_label_fmt'].format(num=step_num, total=_TOTAL_STEPS, title=step_title)}**"
        title_el = {"tag": "markdown", "content": step_label, "text_size": "normal"}
        # Grey hint notation below stepper
        hint_key = _STEP_HINT_KEY_MAP.get(kind, "")
        hint_text = UI_TEXT.get(hint_key, "")
        elements = stepper_elements + [title_el]
        if hint_text:
            elements.append({"tag": "markdown", "content": hint_text, "text_size": "notation"})
        elements.append(content_el)
        return {
            "tag": "div",
            "elements": elements,
        }
    return content_el


def _render_worktree_tool_select(data: dict) -> dict:
    """Render tool selection panel."""
    tools = data.get("tools", [])
    selected = data.get("selected", [])
    message = data.get("message", "")

    lines = []
    if message:
        lines.append(message)
    for tool in tools:
        tool_id = tool.get("id", tool.get("tool_name", ""))
        name = tool.get("name", tool.get("display_name", tool_id))
        desc = tool.get("description", "")
        marker = UI_TEXT["worktree_tool_selected"] if tool_id in selected else UI_TEXT["worktree_tool_unselected"]
        line = f"{marker} **{name}**"
        if desc:
            line += f" — {desc}"
        lines.append(line)

    return {"tag": "markdown", "content": "\n".join(lines)}


def _render_worktree_confirm(data: dict) -> dict:
    """Render confirmation panel."""
    selected_items = data.get("selected_items", [])
    goal = data.get("goal", "")
    message = data.get("message", "")

    lines = []
    if message:
        lines.append(message)
    lines.append(UI_TEXT["worktree_render_selected_items"])
    for item in selected_items:
        tool = item.get("tool", "")
        model = item.get("model", "")
        lines.append(f"- {tool} ({model})")
    if goal:
        lines.append(f"\n{UI_TEXT['worktree_render_goal'].format(goal=goal)}")

    return {"tag": "markdown", "content": "\n".join(lines)}


def _render_worktree_units(data: dict) -> dict:
    """Render execution progress units with visual grouping.

    Units are sorted by status priority: running → failed → completed → pending
    to surface actionable items first. Failed units use red background for emphasis.
    Completed units are auto-collapsed when running/pending units exist to reduce noise.
    """
    units = data.get("units", [])
    message = data.get("message", "")

    _STATUS_ICONS = {"completed": "✅", "running": "⏳", "failed": "❌", "pending": "○"}
    _STATUS_ORDER = {"running": 0, "failed": 1, "completed": 2, "pending": 3}
    sorted_units = sorted(units, key=lambda u: _STATUS_ORDER.get(u.get("status", "pending"), 99))

    # Determine if we should auto-collapse the panel (completed-heavy state)
    has_active = any(u.get("status") in ("running", "pending") for u in units)
    completed_count = sum(1 for u in units if u.get("status") == "completed")
    # Auto-collapse when there are active units AND completed units dominate
    panel_collapsed = has_active and completed_count >= 2

    # Separate failed units for distinct visual treatment
    failed_lines: list[str] = []
    normal_lines: list[str] = []
    if message:
        normal_lines.append(message)

    for unit in sorted_units:
        name = unit.get("name", unit.get("display_name", unit.get("unit_id", "")))
        status = unit.get("status", "pending")
        summary = unit.get("summary", "")
        error = unit.get("error", "")
        icon = _STATUS_ICONS.get(status, "○")
        line = f"{icon} {name}"
        # Show elapsed time for running units
        if status == "running":
            metadata = unit.get("metadata") or {}
            started_at = metadata.get("started_at")
            if started_at:
                elapsed_s = int(time.time() - started_at)
                if elapsed_s >= 60:
                    line += f" ({elapsed_s // 60}min)"
                else:
                    line += f" ({elapsed_s}s)"
        if summary:
            line += f" — {summary}"
        if status == "failed":
            failed_lines.append(line)
            if error:
                failed_lines.append(UI_TEXT["worktree_render_fail_reason"].format(error=error))
        else:
            normal_lines.append(line)

    # Build elements: failed units promoted OUTSIDE panel for emphasis
    # Panel contains only normal (non-failed) units
    panel_elements: list[dict] = []
    if normal_lines:
        panel_elements.append({"tag": "markdown", "content": "\n".join(normal_lines)})
    if not panel_elements:
        panel_elements.append({"tag": "markdown", "content": "—"})

    # Use red border when failures exist to unify emphasis
    border_color = PANEL_STYLES["border_failed"] if failed_lines else PANEL_STYLES["border_active"]
    panel = {
        "tag": "collapsible_panel",
        "expanded": not panel_collapsed,
        "background_style": "default",
        "border": {"color": border_color, "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "header": {
            "title": {"tag": "markdown", "content": f"**{UI_TEXT['worktree_execution_unit']}**"},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "elements": panel_elements,
    }

    # If no failures, return panel directly
    if not failed_lines:
        return panel

    # Failed units promoted ABOVE the panel.
    # Feishu Schema 2.0 does not allow `padding`/`background_style` on `div`,
    # so use `column_set` to provide background emphasis.
    failed_div = {
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "red",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": "\n".join(failed_lines)}],
            }
        ],
    }
    return {
        "tag": "div",
        "elements": [failed_div, panel],
    }


def _render_merge_notes(merge_notes: list, base_branch: str, header_key: str) -> list[str]:
    """Shared helper: render merge notes header + summary + per-branch lines.

    Returns a list of markdown lines (without trailing newline join).
    """
    _MERGE_ICONS = {"ready": "🟢", "conflict": "🔴", "merged": "✅"}
    lines = [UI_TEXT[header_key].format(base_branch=base_branch)]

    # Summary counts
    ready_count = sum(1 for n in merge_notes if n.get("status") == "ready")
    conflict_count = sum(1 for n in merge_notes if n.get("status") == "conflict")
    merged_count = sum(1 for n in merge_notes if n.get("status") == "merged")
    summary_parts = []
    if ready_count:
        summary_parts.append(f"🟢 {ready_count} {UI_TEXT['worktree_merge_status_ready']}")
    if conflict_count:
        summary_parts.append(f"🔴 {conflict_count} {UI_TEXT['worktree_merge_status_conflict']}")
    if merged_count:
        summary_parts.append(f"✅ {merged_count} {UI_TEXT['worktree_merge_status_merged']}")
    if summary_parts:
        lines.append(" · ".join(summary_parts))

    for note in merge_notes:
        branch = note.get("branch", "")
        status = note.get("status", "")
        summary = note.get("summary", "")
        icon = _MERGE_ICONS.get(status, "○")
        line = f"{icon} `{branch}`"
        if summary:
            line += f" — {summary}"
        lines.append(line)

    return lines


def _render_worktree_merge(data: dict) -> dict:
    """Render merge panel with summary counts."""
    merge_notes = data.get("merge_notes", [])
    base_branch = data.get("base_branch", "main")
    lines = _render_merge_notes(merge_notes, base_branch, "worktree_render_branches_to_merge")
    return {"tag": "markdown", "content": "\n".join(lines)}


def _render_worktree_cleanup(data: dict) -> dict:
    """Render cleanup panel with summary counts and merge results."""
    merge_notes = data.get("merge_notes", [])
    base_branch = data.get("base_branch", "main")
    merge_results = data.get("merge_results")

    lines = _render_merge_notes(merge_notes, base_branch, "worktree_render_merge_and_cleanup")

    if merge_results:
        lines.append(f"\n{UI_TEXT['worktree_render_merge_results']}")
        for result in merge_results:
            branch = result.get("branch", "")
            success = result.get("success", False)
            icon = "✅" if success else "❌"
            lines.append(f"{icon} `{branch}`")

    return {"tag": "markdown", "content": "\n".join(lines)}
