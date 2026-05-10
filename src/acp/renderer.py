"""ACP Event Renderer — converts ACP events to Feishu Markdown content.

Maintains state across events to build a complete view of:
- Agent text output (accumulated)
- Active tool calls (with kind-specific icons)
- Execution plan progress (checklist)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from src.utils.text import get_acp_result_header_text

from .models import ACPEvent, ACPEventType, PlanInfo, PromptResult, ToolCallInfo

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

# Regex to detect tool-run summary lines produced by _format_tool_run_line().
# Matches lines like "\n📖 Read `foo.py` ✅\n" or "\n🔧 3 个调用: ... ✅\n".
_TOOL_LINE_RE = re.compile(
    r"^\n?("
    + "|".join(re.escape(ic) for ic in _KIND_ICONS.values())
    + r")\s.+("
    + "|".join(re.escape(ic) for ic in [_STATUS_ICONS["completed"]])
    + r")\s*\n?$"
)


# ------------------------------------------------------------------
# Structured content model (for collapsible-panel rendering)
# ------------------------------------------------------------------
@dataclass
class ContentSection:
    """A logical section of rendered content."""

    section_type: str  # "text" | "tool_group" | "thought" | "plan" | "todo" | "active_tools"
    markdown: str
    tool_kind: str = ""
    tool_count: int = 0
    is_complete: bool = True
    collapsed_by_default: bool = False
    has_failure: bool = False


@dataclass
class RenderedContent:
    """Structured rendering result — can be serialised to flat markdown or to Feishu card elements."""

    sections: list[ContentSection] = field(default_factory=list)

    # ---- backward-compat helpers ----

    def to_markdown(self) -> str:
        """Join all sections into a single markdown string (same output as legacy _render)."""
        parts = [s.markdown for s in self.sections if s.markdown]
        return "\n".join(parts) if parts else ""

    def to_elements(self, *, collapsible: bool = True) -> list[dict]:
        """Convert sections to Feishu card Schema 2.0 elements.

        When *collapsible* is True, ``tool_group`` and ``thought`` sections are
        wrapped in ``collapsible_panel``; otherwise everything becomes plain
        ``markdown`` elements.

        Tool grouping strategy (collapsible mode only):
        - ≤2 consecutive completed tool calls → individual panels
        - >2 consecutive completed tool calls → merged into one "☕ N个工具调用" panel
        """
        if not collapsible:
            return [{"tag": "markdown", "content": sec.markdown}
                    for sec in self.sections if sec.markdown]

        elements: list[dict] = []
        i = 0
        while i < len(self.sections):
            sec = self.sections[i]
            if not sec.markdown:
                i += 1
                continue

            if sec.collapsed_by_default and sec.section_type == "tool_group" and sec.is_complete:
                # Collect consecutive completed tool_group sections
                group_start = i
                merged_md_parts: list[str] = []
                total_tools = 0
                any_failure = False
                while i < len(self.sections):
                    s = self.sections[i]
                    if s.section_type == "tool_group" and s.is_complete and s.collapsed_by_default and s.markdown:
                        merged_md_parts.append(s.markdown)
                        total_tools += max(s.tool_count, 1)
                        if s.has_failure:
                            any_failure = True
                        i += 1
                    else:
                        break

                group_count = i - group_start
                if group_count == 1 or total_tools <= 2:
                    # Individual panels
                    for j in range(group_start, i):
                        s = self.sections[j]
                        if s.markdown:
                            elements.append(self._wrap_collapsible(s))
                else:
                    # Merge into grouped panel
                    merged_sec = ContentSection(
                        section_type="tool_group",
                        markdown="\n".join(merged_md_parts),
                        tool_kind="",
                        tool_count=total_tools,
                        is_complete=True,
                        collapsed_by_default=True,
                        has_failure=any_failure,
                    )
                    elements.append(self._wrap_grouped_tools(merged_sec))
            elif sec.collapsed_by_default and sec.section_type in ("tool_group", "thought"):
                elements.append(self._wrap_collapsible(sec))
                i += 1
            else:
                elements.append({"tag": "markdown", "content": sec.markdown})
                i += 1
        return elements

    @staticmethod
    def _wrap_grouped_tools(sec: ContentSection) -> dict:
        """Wrap merged tool sections into a single grouped collapsible panel."""
        from src.card.themes import PANEL_STYLES

        header_text = f"☕ **{sec.tool_count}个工具调用**（已结束）"
        border_color = PANEL_STYLES["border_failed"] if sec.has_failure else PANEL_STYLES["border_history"]

        return {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {"tag": "markdown", "content": header_text},
            "border": {"color": border_color},
            "background_color": "bg-fill-tag-neutral",
            "vertical_spacing": PANEL_STYLES["vertical_spacing"],
            "padding": PANEL_STYLES["padding"],
            "corner_radius": PANEL_STYLES["corner_radius"],
            "elements": [{"tag": "markdown", "content": sec.markdown}],
        }

    @staticmethod
    def _wrap_collapsible(sec: ContentSection) -> dict:
        """Wrap a section in a ``collapsible_panel`` element."""
        from src.card.themes import PANEL_STYLES

        if sec.section_type == "tool_group":
            icon = _KIND_ICONS.get(sec.tool_kind, "🔧")
            header_text = f"{icon} {sec.tool_count} 次工具调用" if sec.tool_count > 1 else f"{icon} 工具调用"
        elif sec.section_type == "thought":
            if not sec.is_complete:
                header_text = "🧠 思考中"
            else:
                header_text = "🧠 思考完成，点击查看"
        else:
            header_text = "详情"

        # Dynamic border color based on section state
        if sec.section_type == "tool_group" and sec.has_failure:
            border_color = PANEL_STYLES["border_failed"]
        elif sec.section_type == "tool_group" and sec.is_complete:
            border_color = PANEL_STYLES["border_history"]
        else:
            border_color = PANEL_STYLES["border_normal"]

        md_element: dict = {"tag": "markdown", "content": sec.markdown}
        if sec.section_type == "thought":
            md_element["text_size"] = "notation"

        return {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {"tag": "markdown", "content": f"**{header_text}**"},
            "border": {"color": border_color},
            "background_color": "bg-fill-tag-neutral",
            "vertical_spacing": PANEL_STYLES["vertical_spacing"],
            "padding": PANEL_STYLES["padding"],
            "corner_radius": PANEL_STYLES["corner_radius"],
            "elements": [md_element],
        }


@dataclass
class _MutableTurn:
    """Internal mutable turn representation."""

    reasoning_chunks: list[str] = field(default_factory=list)
    tools: dict[str, ToolCallInfo] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnSnapshot:
    """Read-only ACP turn snapshot for card v2 rendering."""

    reasoning: str = ""
    tools: tuple[ToolCallInfo, ...] = ()


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
        self._thought_chunks: list[str] = []  # Accumulated thought text for structured rendering
        self._turns: list[_MutableTurn] = []
        # Consecutive same-kind completed tool aggregation state.
        # Tracks the last tool run so repeated read/edit/etc. collapse into a
        # single line like "📖 Read 3 个文件: a.py, b.py, c.py ✅".
        self._last_tool_run: Optional[dict] = None

    def _record_text_turn(self, text: str) -> None:
        if not text:
            return
        if not self._turns or self._turns[-1].tools:
            self._turns.append(_MutableTurn())
        self._turns[-1].reasoning_chunks.append(text)

    def _record_tool_turn(self, tool_call: ToolCallInfo) -> None:
        if not tool_call:
            return
        for turn in reversed(self._turns):
            if tool_call.id in turn.tools:
                turn.tools[tool_call.id] = tool_call
                return
        if not self._turns:
            self._turns.append(_MutableTurn())
        self._turns[-1].tools[tool_call.id] = tool_call

    def snapshot_turns(self) -> tuple[TurnSnapshot, ...]:
        """Return immutable reasoning/tool turns without changing legacy markdown."""
        snapshots: list[TurnSnapshot] = []
        for turn in self._turns:
            reasoning = "".join(turn.reasoning_chunks)
            tools = tuple(replace(tool) for tool in turn.tools.values())
            if reasoning or tools:
                snapshots.append(TurnSnapshot(reasoning=reasoning, tools=tools))
        return tuple(snapshots)

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

    def _ingest_event(self, event: ACPEvent) -> None:
        """Pure state mutation — update internal state from an ACP event."""
        match event.event_type:
            case ACPEventType.TEXT_CHUNK:
                if event.text:
                    self._record_text_turn(event.text)
                    self._text_chunks.append(event.text)
                    self._text_content += event.text
                    # Any real text breaks the aggregation run.
                    self._last_tool_run = None

            case ACPEventType.THOUGHT_CHUNK:
                if event.text:
                    self._thought_chunks.append(event.text)

            case ACPEventType.TOOL_CALL_START:
                if event.tool_call:
                    self._record_tool_turn(event.tool_call)
                    self._active_tools[event.tool_call.id] = event.tool_call
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)
                    # Track TodoWrite content
                    if event.tool_call.content:
                        self._todo_content = event.tool_call.content

            case ACPEventType.TOOL_CALL_UPDATE:
                if event.tool_call:
                    self._record_tool_turn(event.tool_call)
                    self._active_tools[event.tool_call.id] = event.tool_call
                    for loc in event.tool_call.locations:
                        self._modified_files.add(loc)
                    if event.tool_call.content:
                        self._todo_content = event.tool_call.content

            case ACPEventType.TOOL_CALL_DONE:
                if event.tool_call:
                    self._record_tool_turn(event.tool_call)
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

    def process_event(self, event: ACPEvent) -> str:
        """Process an event and return the current complete rendered content."""
        self._ingest_event(event)
        return self._render()

    def process_event_structured(self, event: ACPEvent) -> RenderedContent:
        """Process an event and return structured content for collapsible rendering."""
        self._ingest_event(event)
        return self._render_structured()

    def get_final_content(self) -> str:
        """Return the final rendered content (no active tools shown).

        If no text output was produced but thought chunks exist, includes
        thought content as fallback so the user sees *something* meaningful
        rather than a blank "执行完成" message.
        """
        self._active_tools = {}
        rendered = self._render()
        if not rendered and self._thought_chunks:
            thought_text = "".join(self._thought_chunks).strip()
            if thought_text:
                rendered = f"🧠 **思考过程**\n{thought_text}"
        return rendered

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

    def reset(self) -> None:
        """Reset all accumulated state for a fresh rendering cycle.

        Call this method when reusing the same ``ACPEventRenderer`` instance for
        a new round of ACP prompt processing — for example, before issuing a
        retry in ``DeepEngine`` or starting a new user prompt within the same
        session.  After calling ``reset()``, the renderer behaves identically to
        a newly constructed instance.

        **What gets reset**:

        - Accumulated agent text output
        - Tool call records (active calls and completed count)
        - Execution plan snapshot
        - File change tracking and todo content

        .. note::
           For the full list of internal fields cleared by this method,
           see ``__init__``.

        **Important notes**:

        - This method **only** resets the renderer's internal accumulation
          state.  It does **not** trigger any cleanup callbacks, cancel running
          ACP sessions, or notify external subscribers.
        - Any external references previously obtained via properties (e.g.
          ``text_content``, ``modified_files``) remain **independent** after
          ``reset()`` — they still hold the data captured before the reset.
          Internally, ``reset()`` rebinds each mutable container to a fresh
          instance (matching ``__init__`` semantics), so prior references are
          never mutated.
        - If ``reset()`` is called while the renderer is in the middle of
          processing events (i.e. between ``process_event`` calls), subsequent
          ``process_event`` calls will start from a clean state — prior text,
          tool calls, and plan information will be lost.

        Example::

            renderer = ACPEventRenderer()
            # … process events for prompt 1 …
            content = renderer.get_final_content()

            # Reuse for prompt 2
            renderer.reset()
            # … process events for prompt 2 …
        """
        self._text_chunks = []
        self._text_content = ""
        self._text_dirty = False
        self._active_tools = {}
        self._completed_tool_count = 0
        self._plan = None
        self._modified_files = set()
        self._todo_content = ""
        self._thought_chunks = []
        self._turns = []
        self._last_tool_run = None

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

    def _render_structured(self) -> RenderedContent:
        """Render content as structured sections for collapsible-panel output.

        Groups consecutive tool-run summary lines into ``tool_group`` sections
        (collapsed by default), interleaved with ``text`` sections for agent prose.
        Thought chunks become a single ``thought`` section (collapsed by default).
        """
        sections: list[ContentSection] = []

        # Plan
        if self._plan:
            plan_md = self._render_plan()
            if plan_md:
                sections.append(ContentSection(section_type="plan", markdown=plan_md))

        # Todo
        if self._todo_content:
            sections.append(ContentSection(section_type="todo", markdown=f"**📝 任务进度**\n{self._todo_content}"))

        # Active tools (not collapsed — they represent in-progress work)
        if self._active_tools:
            active_md = self._render_active_tools()
            if active_md:
                sections.append(ContentSection(section_type="active_tools", markdown=active_md, is_complete=False))

        # Text chunks — split into text vs tool_group stages
        text_sections = self._split_text_into_stages()
        sections.extend(text_sections)

        # Thought chunks — apply reasoning tail truncation
        if self._thought_chunks:
            thought_text = "".join(self._thought_chunks).strip()
            if thought_text:
                from src.card.truncation import cap_reasoning_tail
                truncated_thought = cap_reasoning_tail(thought_text)
                sections.append(
                    ContentSection(section_type="thought", markdown=truncated_thought, collapsed_by_default=True)
                )

        return RenderedContent(sections=sections)

    def _split_text_into_stages(self) -> list[ContentSection]:
        """Split _text_chunks into alternating text / tool_group sections.

        Tool-run summary lines (matching _TOOL_LINE_RE) are grouped into
        ``tool_group`` sections; everything else becomes ``text`` sections.
        Consecutive same-kind tool lines merge into one group.
        """
        if not self._text_chunks:
            return []

        sections: list[ContentSection] = []
        current_text: list[str] = []
        current_tools: list[str] = []
        current_tool_kind: str = ""
        current_tool_count: int = 0

        def flush_text():
            nonlocal current_text
            if current_text:
                md = "".join(current_text).strip()
                if md:
                    sections.append(ContentSection(section_type="text", markdown=md))
                current_text = []

        def flush_tools():
            nonlocal current_tools, current_tool_kind, current_tool_count
            if current_tools:
                md = "".join(current_tools).strip()
                if md:
                    sections.append(
                        ContentSection(
                            section_type="tool_group",
                            markdown=md,
                            tool_kind=current_tool_kind,
                            tool_count=current_tool_count,
                            collapsed_by_default=True,
                        )
                    )
                current_tools = []
                current_tool_kind = ""
                current_tool_count = 0

        for chunk in self._text_chunks:
            if _TOOL_LINE_RE.match(chunk):
                # Extract kind from the icon at the start
                kind = self._extract_tool_kind_from_line(chunk)
                if current_tools and kind != current_tool_kind:
                    # Different tool kind — flush current group, start new
                    flush_tools()
                if not current_tools:
                    flush_text()
                    current_tool_kind = kind
                current_tools.append(chunk)
                current_tool_count += 1
            else:
                if current_tools:
                    flush_tools()
                current_text.append(chunk)

        flush_tools()
        flush_text()
        return sections

    @staticmethod
    def _extract_tool_kind_from_line(line: str) -> str:
        """Extract the tool kind from a tool-run summary line by matching its icon."""
        stripped = line.strip()
        for kind, icon in _KIND_ICONS.items():
            if stripped.startswith(icon):
                return kind
        return "other"

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

    def render_continuation_summary(self) -> str:
        """Render a rich summary for continuation card header (前文摘要).

        Called just before ``reset_for_continuation()`` to capture the
        current state as a compact summary for the new card.
        """
        parts: list[str] = []

        if self._plan and self._plan.entries:
            completed = sum(1 for e in self._plan.entries if e.status == "completed")
            total = len(self._plan.entries)
            parts.append(f"📋 执行计划: {completed}/{total} 已完成")

        if self._completed_tool_count:
            parts.append(f"🛠️ 已完成 {self._completed_tool_count} 次工具调用")

        if self._modified_files:
            files = sorted(self._modified_files)
            if len(files) <= 5:
                file_list = ", ".join(f"`{f}`" for f in files)
            else:
                file_list = ", ".join(f"`{f}`" for f in files[:5]) + f" (+{len(files) - 5})"
            parts.append(f"🗂️ 涉及文件: {file_list}")

        if not parts:
            return ""

        return "**📄 前文摘要**\n" + "\n".join(parts) + "\n\n---\n"

    def reset_for_continuation(self, summary: str = "") -> None:
        """Reset state for a continuation card, optionally seeding a summary prefix.

        Like ``reset()``, clears all accumulated state.  If *summary* is
        provided, it is injected as the first text chunk so that subsequent
        renders include a brief context section at the top of the new card.
        """
        self.reset()
        if summary:
            self._record_text_turn(summary)
            self._text_chunks.append(summary)
            self._text_content = summary

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


def render_prompt_result_markdown(result: PromptResult, max_text_chars: int = 8000, max_items: int = 50) -> str:
    """Render a compact Markdown summary for a :class:`PromptResult`.

    该函数位于渲染层（而非模型层），负责将 PromptResult 中累积的文本、计划、
    工具调用与改动文件信息转换为适合 Feishu/终端展示的 Markdown 片段。

    文案配置（各小节标题等）统一通过 :func:`get_acp_result_header_text` 注入，
    从而避免在核心模型层引入对文案模块的直接依赖。
    """

    parts: list[str] = []

    headers = get_acp_result_header_text()

    # Header
    parts.append(f"**✅ PromptResult** · stop_reason=`{result.stop_reason}`")

    # Text
    if result.text:
        text = result.text
        if max_text_chars > 0 and len(text) > max_text_chars:
            text = text[:max_text_chars] + "\n... (truncated)"
        parts.append(f"\n**{headers.get('text', 'Text')}**")
        parts.append(text)

    # Plan
    if result.plan and result.plan.entries:
        parts.append(f"\n**{headers.get('plan', 'Plan')}**")
        for ent in result.plan.entries[:max_items]:
            icon = {"completed": "✅", "in_progress": "🔄", "pending": "⏳"}.get(ent.status, "⬜")
            parts.append(f"- {icon} {ent.content}")

    # Tool calls
    if result.tool_calls:
        parts.append(f"\n**{headers.get('tools', 'Tool calls')}**")
        for tc in result.tool_calls[:max_items]:
            loc = f" `{tc.locations[0]}`" if tc.locations else ""
            parts.append(f"- `{tc.kind}` {tc.title}{loc} · `{tc.status}`")

    # Tool results
    if result.tool_results:
        parts.append(f"\n**{headers.get('tool_results', 'Tool results')}**")
        for e in result.tool_results[:max_items]:
            kind = e.get("kind", "unknown")
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            if kind == "execute":
                cmd = data.get("command", "")
                code = data.get("exit_code")
                parts.append(f"- `execute` `{cmd}` · exit_code={code}")
            elif kind in ("read_file", "write_file"):
                p = data.get("path", "")
                parts.append(f"- `{kind}` `{p}`")
            elif kind == "permission":
                parts.append(f"- `permission` {data.get('outcome', '')} · {data.get('reason', '')}")
            else:
                parts.append(f"- `{kind}`")

    # Modified files
    if result.modified_files:
        parts.append(f"\n**{headers.get('files', 'Modified files')}**")
        for p in sorted(result.modified_files)[:max_items]:
            parts.append(f"- `{p}`")

    return "\n".join(parts).strip() + "\n"
