"""Tool call panel rendering."""

from __future__ import annotations

import json
from src.card.state.models import ContentBlock
from src.card.themes import PANEL_STYLES
from src.card.ui_text import UI_TEXT

_MAX_OUTPUT_CHARS = 2000
_MAX_SUMMARY_CHARS = 80

_STATUS_ICONS = {
    "active": "⏳",
    "completed": "✅",
    "failed": "❌",
}

_BASH_TOOLS = {"bash", "shell", "run_command", "execute_command"}

_PATH_TOOLS = {"read", "write", "edit", "read_file", "write_file", "edit_file"}

_SEARCH_TOOLS = {"grep", "search", "find", "glob", "search_codebase"}

# Tools rendered as compact single-line summary (no input/output detail)
_COMPACT_TOOLS = {"task", "todowrite", "todo_write"}


def _is_empty_data(value) -> bool:
    """Check if a tool input/output value is considered empty."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (dict, list)) and not value:
        return True
    return False


def render_tool_panel(block: ContentBlock) -> dict | None:
    """Render a single tool call as a collapsible_panel.

    Returns None if both tool_input and tool_output are empty.

    Design intent (AC7/AC8):
    - AC7: When both input AND output are empty → returns None (panel omitted entirely).
    - AC8: When only output is empty (input non-empty) → renders input section only,
            no empty output section appears in the card JSON.
    """
    # AC7: suppress entirely when both sides are empty
    if _is_empty_data(block.tool_input) and _is_empty_data(block.tool_output):
        return None

    icon = _STATUS_ICONS.get(block.status, "⏳")
    summary = generate_tool_summary(block)
    title_text = f"{icon} **{block.tool_name or 'tool'}** — {summary}"

    expanded = block.status == "active"
    border_color = PANEL_STYLES["border_failed"] if block.status == "failed" else PANEL_STYLES["border_normal"]

    detail_content = _render_detail(block)

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title_text},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": border_color, "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": detail_content}],
    }


def render_tool_history_panel(blocks: list[ContentBlock]) -> dict | None:
    """Render multiple completed tools as a folded panel.

    Returns None if all tool panels are empty.
    """
    nested = [p for b in blocks if (p := render_tool_panel(b)) is not None]
    if not nested:
        return None
    n = len(nested)

    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": UI_TEXT["tool_history_panel_header"].format(n=n)},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_history"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": nested,
    }


def generate_tool_summary(block: ContentBlock) -> str:
    """Generate a short summary for a tool call."""
    tool_name = (block.tool_name or "").lower()
    tool_input = block.tool_input or ""

    # bash → command text
    if tool_name in _BASH_TOOLS:
        line = tool_input.split("\n")[0].strip()
        if len(line) > _MAX_SUMMARY_CHARS:
            return line[:_MAX_SUMMARY_CHARS] + "…"
        return line if line else (block.tool_summary or block.tool_name or "")

    # read/write/edit → file path
    if tool_name in _PATH_TOOLS:
        path = _extract_json_field(tool_input, ("path", "file_path", "file"))
        if path:
            return path

    # grep/search → "query · path"
    if tool_name in _SEARCH_TOOLS:
        parts = []
        query = _extract_json_field(tool_input, ("query", "pattern", "keyword"))
        if query:
            parts.append(query)
        path = _extract_json_field(tool_input, ("path", "directory", "dir"))
        if path:
            parts.append(path)
        if parts:
            return " · ".join(parts)

    # generic → try to extract common fields
    for fields in [("path", "file_path", "file"), ("name", "id"), ("query",)]:
        val = _extract_json_field(tool_input, fields)
        if val:
            return val

    # Fallback
    if block.tool_summary:
        return block.tool_summary
    return block.tool_name or ""


def _extract_json_field(text: str, fields: tuple[str, ...]) -> str:
    """Try to extract a field from JSON text."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for f in fields:
                if f in data and data[f]:
                    val = str(data[f])
                    if len(val) > _MAX_SUMMARY_CHARS:
                        return val[:_MAX_SUMMARY_CHARS] + "…"
                    return val
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def _truncate_output(output: str) -> str:
    """Truncate output to last _MAX_OUTPUT_CHARS with '...' prefix."""
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output
    return "…" + output[-_MAX_OUTPUT_CHARS:]


def _render_detail(block: ContentBlock) -> str:
    """Render detail content based on tool type.

    AC8 enforcement: output_empty flag is passed to sub-renderers which skip the
    output section when True, ensuring no empty output block in the card JSON.
    """
    tool_name = (block.tool_name or "").lower()
    tool_input = block.tool_input or ""
    tool_output = block.tool_output or ""
    output_empty = _is_empty_data(block.tool_output)

    # Compact tools (task, todowrite): single-line description only
    if tool_name in _COMPACT_TOOLS:
        return _render_compact_detail(tool_input)

    if tool_name in _BASH_TOOLS:
        return _render_bash_detail(tool_input, tool_output, output_empty)
    return _render_generic_detail(tool_input, tool_output, output_empty)


def _render_bash_detail(tool_input: str, tool_output: str, output_empty: bool = False) -> str:
    """Render bash-specific detail."""
    parts = [f"{UI_TEXT['tool_label_command']}\n```bash\n{tool_input}\n```"]
    if not output_empty:
        output = _truncate_output(tool_output)
        parts.append(f"{UI_TEXT['tool_label_result']}\n```\n{output}\n```")
    return "\n".join(parts)


def _render_compact_detail(tool_input: str) -> str:
    """Render compact single-line detail for task/todowrite tools.

    Extracts 'description' or 'query' from JSON input, or shows first line.
    """
    desc = _extract_json_field(tool_input, ("description", "query", "content", "name"))
    if desc:
        return desc
    # Fallback: first non-empty line of raw input
    for line in tool_input.split("\n"):
        stripped = line.strip()
        if stripped and stripped not in ("{", "}", "[", "]"):
            return stripped[:_MAX_SUMMARY_CHARS] + ("…" if len(stripped) > _MAX_SUMMARY_CHARS else "")
    return ""


def _render_generic_detail(tool_input: str, tool_output: str, output_empty: bool = False) -> str:
    """Render generic tool detail."""
    parts = [f"{UI_TEXT['tool_label_input']}\n```json\n{tool_input}\n```"]
    if not output_empty:
        output = _truncate_output(tool_output)
        parts.append(f"{UI_TEXT['tool_label_output']}\n```\n{output}\n```")
    return "\n".join(parts)
