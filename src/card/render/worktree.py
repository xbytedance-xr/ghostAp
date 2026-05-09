"""Worktree-specific rendering functions.

Converts worktree structured data (from ContentBlock.data dict)
into formatted Feishu Schema 2.0 markdown elements.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.card.actions.dispatch import (
    SHOW_WORKTREE_MENU,
    WORKTREE_CLEAR_ITEMS,
    WORKTREE_FINISH_SELECTION,
    WORKTREE_REMOVE_ITEM,
    WORKTREE_SELECT_MODEL,
    WORKTREE_SELECT_TOOL,
)
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
        # Tool select kind doubles as model select stage; pick title/hint by select_action.
        is_model_select = (
            kind == "worktree_tool_select"
            and str((data or {}).get("select_action") or "") == WORKTREE_SELECT_MODEL
        )
        if is_model_select:
            title_key = "worktree_step_model_select"
            hint_key = "worktree_step_model_select_hint"
        else:
            title_key = _STEP_TITLE_KEY_MAP.get(kind, "")
            hint_key = _STEP_HINT_KEY_MAP.get(kind, "")
        step_title = UI_TEXT.get(title_key, "")
        step_num = step_idx + 1
        step_label = f"**{UI_TEXT['worktree_step_label_fmt'].format(num=step_num, total=_TOTAL_STEPS, title=step_title)}**"
        title_el = {"tag": "markdown", "content": step_label, "text_size": "normal"}
        # Grey hint notation below stepper
        hint_text = UI_TEXT.get(hint_key, "")
        elements = stepper_elements + [title_el]
        if hint_text:
            elements.append({"tag": "markdown", "content": hint_text, "text_size": "notation"})
        elements.append(content_el)
        return {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": elements,
            }],
        }
    return content_el


def _render_worktree_tool_select(data: dict) -> dict:
    """Render the worktree tool/model selection panel.

    Layout (TOOL_SELECT stage):
      [可选] 顶部 message banner
      [tool rows]            每行：工具描述 + "+ 添加 X" 按钮
      ── hr ──
      [已选组合 (N)]          仅当 N>0 时展示，每条带 ✕ 移除 + 清空按钮
      [✅ 确认选择 / 置灰提示]  N>0 highlighted；N==0 灰底提示

    Layout (MODEL_SELECT stage):
      [醒目 banner: "为 X 选择模型"]
      [model rows]           每行：模型描述 + "选择 X" 按钮
      ── hr ──
      [已选组合 (N)]          保留上下文展示，但禁用 ✕/清空 操作
      [← 返回工具选择]        允许撤销本次添加，重新挑工具
    """
    tools = data.get("tools", [])
    selected = data.get("selected", [])
    message = data.get("message", "")
    project_id = str(data.get("project_id") or "")
    default_action = str(data.get("select_action") or WORKTREE_SELECT_TOOL)
    pending_tool = str(data.get("pending_tool") or "").strip()
    selected_keys = _selected_tool_keys(selected)
    is_model_select = default_action == WORKTREE_SELECT_MODEL

    elements: list[dict] = []
    if is_model_select:
        # 醒目 banner，避免与工具选择卡视觉上混淆
        banner_text = UI_TEXT["worktree_model_select_banner"].format(
            tool=pending_tool or "当前工具",
            back=UI_TEXT["worktree_back_to_tools_btn"],
        )
        elements.append({
            "tag": "column_set",
            "flex_mode": "stretch",
            "background_style": "blue",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": banner_text}],
            }],
        })
    elif message:
        elements.append({"tag": "markdown", "content": message})

    for tool in tools:
        elements.append(
            _render_worktree_select_option(
                tool,
                project_id=project_id,
                default_action=default_action,
                selected_keys=selected_keys,
            )
        )

    if not tools:
        elements.append({"tag": "markdown", "content": UI_TEXT.get("worktree_data_empty", "暂无数据")})

    selected_dicts = [item for item in (selected or []) if isinstance(item, dict)]

    if is_model_select:
        if selected_dicts:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": UI_TEXT["worktree_selected_block_title"].format(count=len(selected_dicts)),
                "text_size": "notation",
            })
            for item in selected_dicts:
                label = str(
                    item.get("display_label")
                    or item.get("display_name")
                    or item.get("tool_name")
                    or ""
                ).strip() or "(unknown)"
                elements.append({
                    "tag": "markdown",
                    "content": f"• {label}",
                    "text_size": "notation",
                })
        elements.append({"tag": "hr"})
        elements.append(_callback_button(
            text=UI_TEXT["worktree_back_to_tools_btn"],
            value={"action": SHOW_WORKTREE_MENU, "project_id": project_id},
        ))
    else:
        if selected_dicts:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": UI_TEXT["worktree_selected_block_title"].format(count=len(selected_dicts)),
            })
            for item in selected_dicts:
                elements.append(_render_selected_item_row(item, project_id=project_id))
            elements.append(_callback_button(
                text=UI_TEXT["worktree_clear_items_btn"],
                value={"action": WORKTREE_CLEAR_ITEMS, "project_id": project_id},
            ))

        elements.append({"tag": "hr"})
        if selected_dicts:
            elements.append(_callback_button(
                text=UI_TEXT["worktree_confirm_selection_btn"],
                value={"action": WORKTREE_FINISH_SELECTION, "project_id": project_id},
                button_type="primary",
            ))
        else:
            elements.append({
                "tag": "markdown",
                "content": UI_TEXT["worktree_confirm_selection_disabled_btn"],
                "text_size": "notation",
            })

    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [{
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "top",
            "elements": elements,
        }],
    }


def _callback_button(
    *,
    text: str,
    value: dict,
    button_type: str = "default",
    size: str = "medium",
) -> dict:
    """Build a Schema 2.0 callback button while preserving legacy value access."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "value": value,
        "behaviors": [{"type": "callback", "value": value}],
        "size": size,
    }


def _render_selected_item_row(item: dict, *, project_id: str) -> dict:
    """One already-selected item row: label + ✕ remove button."""
    label = str(
        item.get("display_label")
        or item.get("display_name")
        or item.get("tool_name")
        or ""
    ).strip() or "(unknown)"
    selection_key = str(item.get("selection_key") or "").strip()
    return {
        "tag": "column_set",
        "flex_mode": "bisect",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 3,
                "vertical_align": "center",
                "elements": [{"tag": "markdown", "content": f"• {label}"}],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [_callback_button(
                    text=UI_TEXT["worktree_remove_item_btn"],
                    value={
                        "action": WORKTREE_REMOVE_ITEM,
                        "selection_key": selection_key,
                        "project_id": project_id,
                    },
                    size="small",
                )],
            },
        ],
    }


def _selected_tool_keys(selected: list) -> set[str]:
    """Normalize selected worktree items for display-only highlighting."""
    keys: set[str] = set()
    for item in selected or []:
        if isinstance(item, str):
            if item:
                keys.add(item)
            continue
        if not isinstance(item, dict):
            continue
        for field in ("tool_name", "id", "name", "display_name"):
            value = str(item.get(field) or "").strip()
            if value:
                keys.add(value)
    return keys


def _tool_identity(tool: dict) -> tuple[str, str, str]:
    tool_id = str(tool.get("tool_name") or tool.get("id") or tool.get("name") or "").strip()
    name = str(tool.get("display_name") or tool.get("name") or tool_id).strip()
    desc = str(tool.get("description") or "").strip()
    return tool_id, name, desc


def _render_worktree_select_option(
    tool: dict,
    *,
    project_id: str,
    default_action: str,
    selected_keys: set[str],
) -> dict:
    """Render one tool/model choice as a real callback button row."""
    # `selected_keys` 保留在签名以便未来其他高亮策略复用；当前已选状态由独立 "已选组合" 板块呈现
    _ = selected_keys
    tool_id, name, desc = _tool_identity(tool)
    action = str(tool.get("action") or default_action or WORKTREE_SELECT_TOOL)

    if action == WORKTREE_SELECT_MODEL:
        value = {
            "action": WORKTREE_SELECT_MODEL,
            "model_name": tool_id,
            "model_display_name": name,
            "project_id": project_id,
        }
        button_text = UI_TEXT["worktree_pick_model_btn"].format(name=name)
        button_type = "primary"
    else:
        value = {
            "action": WORKTREE_SELECT_TOOL,
            "tool_name": tool_id,
            "display_name": name,
            "agent_name": tool.get("agent_name", ""),
            "provider": tool.get("provider", ""),
            "supports_model": bool(tool.get("supports_model", False)),
            "skip_model_selection": bool(tool.get("skip_model_selection", False)),
            "project_id": project_id,
        }
        button_text = UI_TEXT["worktree_add_tool_btn"].format(name=name)
        # 工具按钮始终保持可点击的中性样式，让 "已选组合" 板块承担状态反馈
        button_type = "default"

    # Title shows the clean name; description (ACP metadata blurb / tool tagline)
    # renders below as small notation so it doesn't crowd the button column.
    label_elements: list[dict] = [{"tag": "markdown", "content": f"**{name}**"}]
    if desc:
        label_elements.append({
            "tag": "markdown",
            "content": desc,
            "text_size": "notation",
        })

    return {
        "tag": "column_set",
        "flex_mode": "bisect",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 2,
                "vertical_align": "center",
                "elements": label_elements,
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [_callback_button(
                    text=button_text,
                    value=value,
                    button_type=button_type,
                )],
            },
        ],
    }


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
        lines.append(f"- {_format_confirm_selection_item(item)}")
    if goal:
        lines.append(f"\n{UI_TEXT['worktree_render_goal'].format(goal=goal)}")

    return {"tag": "markdown", "content": "\n".join(lines)}


def _format_confirm_selection_item(item: dict) -> str:
    """Format one selected programming tuple as agent/tool/model."""
    if not isinstance(item, dict):
        return str(item or "").strip() or "(unknown)"

    label = str(item.get("display_label") or "").strip()
    if label:
        return label

    agent = str(item.get("agent_display_name") or item.get("agent_name") or "").strip()
    tool = str(
        item.get("tool")
        or item.get("display_name")
        or item.get("tool_name")
        or ""
    ).strip()
    model = str(
        item.get("model")
        or item.get("effective_model_display_name")
        or item.get("model_display_name")
        or item.get("model_name")
        or "默认模型"
    ).strip()

    subject = tool or "(unknown)"
    if agent:
        subject = f"{agent} · {subject}"
    return f"{subject} / {model or '默认模型'}"


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
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [{
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "top",
            "elements": [failed_div, panel],
        }],
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
