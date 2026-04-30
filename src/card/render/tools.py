"""Tool call panel rendering."""

from __future__ import annotations

import json
from src.card.state.models import ContentBlock

_MAX_OUTPUT_CHARS = 2000
_MAX_SUMMARY_CHARS = 80

_STATUS_ICONS = {
    "active": "⏳",
    "completed": "✓",
    "failed": "✗",
}

_BASH_TOOLS = {"bash", "shell", "run_command", "execute_command"}

_PATH_TOOLS = {"read", "write", "edit", "read_file", "write_file", "edit_file"}

_SEARCH_TOOLS = {"grep", "search", "find", "glob", "search_codebase"}


def render_tool_panel(block: ContentBlock) -> dict:
    """Render a single tool call as a collapsible_panel."""
    icon = _STATUS_ICONS.get(block.status, "⏳")
    summary = generate_tool_summary(block)
    title_text = f"{icon} **{block.tool_name or 'tool'}** — {summary}"

    expanded = block.status == "active"
    border_color = "red" if block.status == "failed" else "grey"

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
        "border": {"color": border_color, "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": detail_content}],
    }


def render_tool_history_panel(blocks: list[ContentBlock]) -> dict:
    """Render multiple completed tools as a folded panel."""
    n = len(blocks)
    nested = [render_tool_panel(b) for b in blocks]

    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": f"📋 **{n} 个工具调用已完成**"},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "blue", "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
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
            return line[:_MAX_SUMMARY_CHARS] + "..."
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
                        return val[:_MAX_SUMMARY_CHARS] + "..."
                    return val
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def _truncate_output(output: str) -> str:
    """Truncate output to last _MAX_OUTPUT_CHARS with '...' prefix."""
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output
    return "..." + output[-_MAX_OUTPUT_CHARS:]


def _render_detail(block: ContentBlock) -> str:
    """Render detail content based on tool type."""
    tool_name = (block.tool_name or "").lower()
    tool_input = block.tool_input or ""
    tool_output = block.tool_output or ""

    if tool_name in _BASH_TOOLS:
        return _render_bash_detail(tool_input, tool_output)
    return _render_generic_detail(tool_input, tool_output)


def _render_bash_detail(tool_input: str, tool_output: str) -> str:
    """Render bash-specific detail."""
    parts = [f"**Command**\n```bash\n{tool_input}\n```"]
    output = _truncate_output(tool_output)
    parts.append(f"**Result**\n```\n{output}\n```")
    return "\n".join(parts)


def _render_generic_detail(tool_input: str, tool_output: str) -> str:
    """Render generic tool detail."""
    parts = [f"**Input**\n```json\n{tool_input}\n```"]
    output = _truncate_output(tool_output)
    parts.append(f"**Output**\n```\n{output}\n```")
    return "\n".join(parts)
