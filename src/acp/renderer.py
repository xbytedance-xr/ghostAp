"""ACP Event Renderer — converts ACP events to Feishu Markdown content.

Maintains state across events to build a complete view of:
- Agent text output (accumulated)
- Active tool calls (with kind-specific icons)
- Execution plan progress (checklist)
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import ACPEvent, ACPEventType, PlanInfo, ToolCallInfo

logger = logging.getLogger(__name__)

# Tool kind → display icon
_KIND_ICONS = {
    "read": "📖",
    "edit": "✏️",
    "delete": "🗑️",
    "move": "📁",
    "search": "🔍",
    "execute": "⚡",
    "think": "🧠",
    "fetch": "🌐",
    "switch_mode": "🔄",
    "other": "🔧",
}

# Tool status → display icon
_STATUS_ICONS = {
    "pending": "⏳",
    "in_progress": "🔄",
    "completed": "✅",
    "failed": "❌",
}


class ACPEventRenderer:
    """Converts ACP events into Feishu-displayable Markdown content."""

    def __init__(self):
        self._text_chunks: list[str] = []
        self._active_tools: dict[str, ToolCallInfo] = {}
        self._completed_tools: list[ToolCallInfo] = []
        self._plan: Optional[PlanInfo] = None
        self._modified_files: set[str] = set()

    def process_event(self, event: ACPEvent) -> str:
        """Process an event and return the current complete rendered content."""
        match event.event_type:
            case ACPEventType.TEXT_CHUNK:
                if event.text:
                    self._text_chunks.append(event.text)

            case ACPEventType.THOUGHT_CHUNK:
                # Optionally show thoughts — for now, skip to avoid noise
                pass

            case ACPEventType.TOOL_CALL_START:
                if event.tool_call:
                    self._active_tools[event.tool_call.id] = event.tool_call
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)

            case ACPEventType.TOOL_CALL_UPDATE:
                if event.tool_call:
                    self._active_tools[event.tool_call.id] = event.tool_call
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)

            case ACPEventType.TOOL_CALL_DONE:
                if event.tool_call:
                    tool_id = event.tool_call.id
                    self._completed_tools.append(event.tool_call)
                    self._active_tools.pop(tool_id, None)
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)
                    # Add inline summary to text
                    icon = _KIND_ICONS.get(event.tool_call.kind, "🔧")
                    status_icon = _STATUS_ICONS.get(event.tool_call.status, "✅")
                    loc_str = ""
                    if event.tool_call.locations:
                        loc_str = f" `{event.tool_call.locations[0]}`"
                    self._text_chunks.append(f"\n{icon} {event.tool_call.title}{loc_str} {status_icon}\n")

            case ACPEventType.PLAN_UPDATE:
                if event.plan:
                    self._plan = event.plan

        return self._render()

    def get_final_content(self) -> str:
        """Return the final rendered content (no active tools shown)."""
        self._active_tools.clear()
        return self._render()

    @property
    def text_content(self) -> str:
        """Raw accumulated text."""
        return "".join(self._text_chunks)

    @property
    def modified_files(self) -> set[str]:
        """Set of file paths modified during this session (read-only view)."""
        return self._modified_files

    @property
    def completed_tool_count(self) -> int:
        return len(self._completed_tools)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render(self) -> str:
        """Render complete content: plan + active tools + text."""
        parts: list[str] = []

        if self._plan:
            rendered_plan = self._render_plan()
            if rendered_plan:
                parts.append(rendered_plan)

        if self._active_tools:
            rendered_tools = self._render_active_tools()
            if rendered_tools:
                parts.append(rendered_tools)

        if self._text_chunks:
            parts.append("".join(self._text_chunks))

        return "\n".join(parts) if parts else ""

    def _render_plan(self) -> str:
        """Render plan as a checklist."""
        if not self._plan or not self._plan.entries:
            return ""

        lines = ["**📋 执行计划**"]
        for entry in self._plan.entries:
            icon = _STATUS_ICONS.get(entry.status, "⬜")
            lines.append(f"{icon} {entry.content}")
        return "\n".join(lines)

    def _render_active_tools(self) -> str:
        """Render currently active tool calls."""
        lines: list[str] = []
        for tool in self._active_tools.values():
            if tool.status in ("in_progress", "pending"):
                kind_icon = _KIND_ICONS.get(tool.kind, "🔧")
                loc_str = ""
                if tool.locations:
                    loc_str = f" `{tool.locations[0]}`"
                lines.append(f"{kind_icon} {tool.title}{loc_str}...")
        return "\n".join(lines) if lines else ""
