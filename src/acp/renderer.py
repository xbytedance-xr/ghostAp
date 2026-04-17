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
        self._text_content: str = ""
        self._text_dirty: bool = False
        self._active_tools: dict[str, ToolCallInfo] = {}
        self._completed_tool_count: int = 0
        self._plan: Optional[PlanInfo] = None
        self._modified_files: set[str] = set()
        self._todo_content: str = ""  # Latest TodoWrite rendered content
        # Consecutive same-kind completed tool aggregation state.
        # Tracks the last tool run so repeated read/edit/etc. collapse into a
        # single line like "📖 Read 3 个文件: a.py, b.py, c.py ✅".
        self._last_tool_run: Optional[dict] = None

    def _format_tool_run_line(self, kind: str, items: list[tuple[str, str]]) -> str:
        icon = _KIND_ICONS.get(kind, "🔧")
        status_icon = _STATUS_ICONS.get("completed", "✅")
        if len(items) == 1:
            title, loc = items[0]
            loc_str = f" `{loc}`" if loc else ""
            return f"\n{icon} {title}{loc_str} {status_icon}\n"
        # Aggregate multiple same-kind calls
        locs = [loc for _, loc in items if loc]
        titles = [t for t, _ in items]
        shown = locs if locs else titles
        sample = ", ".join(f"`{s}`" if locs else s for s in shown[:3])
        more = f" (+{len(shown) - 3})" if len(shown) > 3 else ""
        return f"\n{icon} {len(items)} 个调用: {sample}{more} {status_icon}\n"

    def process_event(self, event: ACPEvent) -> str:
        """Process an event and return the current complete rendered content."""
        match event.event_type:
            case ACPEventType.TEXT_CHUNK:
                if event.text:
                    self._text_chunks.append(event.text)
                    self._text_content += event.text
                    # Any real text breaks the aggregation run.
                    self._last_tool_run = None

            case ACPEventType.THOUGHT_CHUNK:
                # Optionally show thoughts — for now, skip to avoid noise
                pass

            case ACPEventType.TOOL_CALL_START:
                if event.tool_call:
                    self._active_tools[event.tool_call.id] = event.tool_call
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)
                    # Track TodoWrite content
                    if event.tool_call.content:
                        self._todo_content = event.tool_call.content

            case ACPEventType.TOOL_CALL_UPDATE:
                if event.tool_call:
                    self._active_tools[event.tool_call.id] = event.tool_call
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)
                    if event.tool_call.content:
                        self._todo_content = event.tool_call.content

            case ACPEventType.TOOL_CALL_DONE:
                if event.tool_call:
                    tool_id = event.tool_call.id
                    self._completed_tool_count += 1
                    self._active_tools.pop(tool_id, None)
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)

                    if event.tool_call.content:
                        # TodoWrite: update dedicated section, don't pollute text buffer
                        self._todo_content = event.tool_call.content
                        self._last_tool_run = None
                    else:
                        # Add inline summary to text (skip empty titles)
                        title = (event.tool_call.title or "").strip()
                        if title:
                            kind = event.tool_call.kind or "other"
                            loc = event.tool_call.locations[0] if event.tool_call.locations else ""
                            item = (title, loc)
                            run = self._last_tool_run
                            if (
                                run
                                and run["kind"] == kind
                                and run["line_idx"] == len(self._text_chunks) - 1
                            ):
                                # Same-kind consecutive run — update the aggregated line in place.
                                run["items"].append(item)
                                new_line = self._format_tool_run_line(kind, run["items"])
                                self._text_chunks[run["line_idx"]] = new_line
                                self._text_dirty = True
                            else:
                                # Start a new run.
                                line = self._format_tool_run_line(kind, [item])
                                self._text_chunks.append(line)
                                self._text_content += line
                                self._last_tool_run = {
                                    "kind": kind,
                                    "items": [item],
                                    "line_idx": len(self._text_chunks) - 1,
                                }

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
        if self._text_dirty:
            self._text_content = "".join(self._text_chunks)
            self._text_dirty = False
        return self._text_content

    @property
    def modified_files(self) -> set[str]:
        """Set of file paths modified during this session (read-only view)."""
        return self._modified_files

    @property
    def todo_content(self) -> str:
        """Latest TodoWrite content."""
        return self._todo_content

    @property
    def completed_tool_count(self) -> int:
        return self._completed_tool_count

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render(self) -> str:
        """Render complete content: plan + todo + active tools + text."""
        parts: list[str] = []

        if self._plan:
            rendered_plan = self._render_plan()
            if rendered_plan:
                parts.append(rendered_plan)

        if self._todo_content:
            parts.append(f"**📝 任务进度**\n{self._todo_content}")

        if self._active_tools:
            rendered_tools = self._render_active_tools()
            if rendered_tools:
                parts.append(rendered_tools)

        text_content = self.text_content
        if text_content:
            parts.append(text_content)

        return "\n".join(parts) if parts else ""

    def _render_plan(self) -> str:
        """Render plan as a checklist."""
        if not self._plan or not self._plan.entries:
            return ""

        lines: list[str] = []
        for entry in self._plan.entries:
            content = (entry.content or "").strip()
            if not content:
                continue
            icon = _STATUS_ICONS.get(entry.status, "⬜")
            lines.append(f"{icon} {content}")

        if not lines:
            return ""
        return "\n".join(["**📋 执行计划**", *lines])

    def _render_active_tools(self) -> str:
        """Render currently active tool calls, grouped by kind to reduce noise."""
        groups: dict[str, list[tuple[str, str]]] = {}
        order: list[str] = []
        for tool in self._active_tools.values():
            if tool.content:
                continue
            title = (tool.title or "").strip()
            if tool.status not in ("in_progress", "pending") or not title:
                continue
            loc = tool.locations[0] if tool.locations else ""
            kind = tool.kind or "other"
            if kind not in groups:
                groups[kind] = []
                order.append(kind)
            groups[kind].append((title, loc))

        lines: list[str] = []
        for kind in order:
            items = groups[kind]
            icon = _KIND_ICONS.get(kind, "🔧")
            if len(items) == 1:
                title, loc = items[0]
                loc_str = f" `{loc}`" if loc else ""
                lines.append(f"{icon} {title}{loc_str}...")
            else:
                locs = [l for _, l in items if l]
                shown = locs if locs else [t for t, _ in items]
                sample = ", ".join(f"`{s}`" if locs else s for s in shown[:3])
                more = f" (+{len(shown) - 3})" if len(shown) > 3 else ""
                lines.append(f"{icon} {len(items)} 个进行中: {sample}{more}...")
        return "\n".join(lines) if lines else ""

    def render_summary(self) -> str:
        """Render a compact summary of completed work."""
        parts = []
        if self._completed_tool_count:
            parts.append(f"🛠️ {self._completed_tool_count} 次工具调用")
        if self._modified_files:
            parts.append(f"🗂️ {len(self._modified_files)} 个文件")
        return "  ·  ".join(parts)

    def render_plan_view(self) -> str:
        """Render plan + todo + active tools only (no text history). For plan update cards."""
        parts: list[str] = []

        if self._plan:
            rendered_plan = self._render_plan()
            if rendered_plan:
                parts.append(rendered_plan)

        if self._todo_content:
            parts.append(f"**📝 任务进度**\n{self._todo_content}")

        if self._active_tools:
            rendered_tools = self._render_active_tools()
            if rendered_tools:
                parts.append(rendered_tools)

        return "\n".join(parts) if parts else ""
