"""WorkflowProgressRenderer — renders workflow progress tree for Feishu cards."""

from __future__ import annotations

import json
import time
from typing import Any

from .errors import _strip_internal_details
from .models import (
    AgentProgress,
    AgentStatus,
    PhaseProgress,
    WorkflowProject,
    WorkflowStatus,
)

# ---------------------------------------------------------------------------
# Internal: string helpers
# ---------------------------------------------------------------------------

# Keep phase/agent labels readable on narrow mobile screens without
# truncating important context (e.g. a task identifier at the end of a
# long title). A middle ellipsis keeps both the leading description and
# the trailing identifier visible.
_LABEL_TRUNCATION_LIMIT = 40
_ELLIPSIS = "…"


def _middle_ellipsis(text: str, limit: int = _LABEL_TRUNCATION_LIMIT) -> str:
    """Return ``text`` with middle characters replaced by an ellipsis when
    it exceeds ``limit`` characters. Preserves the head and tail so both
    human-readable descriptions and trailing identifiers survive.

    Examples::

        _middle_ellipsis("code-review: verify payment-gateway auth flow")
        # → "code-review: ver…t flow"  (when limit == 24)
        _middle_ellipsis("short")  # → "short"
    """
    if not text:
        return text
    if len(text) <= limit:
        return text
    # Reserve room for the ellipsis itself.
    available = max(limit - len(_ELLIPSIS), 4)
    head = available // 2 + available % 2
    tail = available // 2
    return f"{text[:head]}{_ELLIPSIS}{text[-tail:]}"


def _escape_md(text: str) -> str:
    """Escape markdown special characters in user-supplied text."""
    for ch in ("*", "_", "`", "|", "[", "]", "~"):
        text = text.replace(ch, "\\" + ch)
    return text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel marker list used by lint-level defensive checks.
# An empty tuple means "no markers configured → no checks applied". Tests can
# monkey-patch this to inject sentinel values and verify the defensive gate.
_AGENT_OUTPUT_FORBIDDEN_MARKERS: tuple[str, ...] = ()

STATUS_ICONS: dict[AgentStatus, str] = {
    AgentStatus.PENDING: "\u23f3",
    AgentStatus.RUNNING: "\U0001f504",
    AgentStatus.DONE: "\u2705",
    AgentStatus.FAILED: "\u274c",
    AgentStatus.CACHED: "\U0001f4e6",
    AgentStatus.CANCELLED: "⏹️",
}

WORKFLOW_STATUS_ICONS: dict[WorkflowStatus, str] = {
    WorkflowStatus.IDLE: "\u23f3",
    WorkflowStatus.GENERATING_SCRIPT: "\U0001f504",
    WorkflowStatus.AWAITING_AGENT_SELECT: "\U0001f916",
    WorkflowStatus.AWAITING_TOOL_SELECT: "\U0001f527",
    WorkflowStatus.AWAITING_CONFIRM: "\u23f3",
    WorkflowStatus.RUNNING: "\U0001f504",
    WorkflowStatus.COMPLETED: "\u2705",
    WorkflowStatus.FAILED: "\u274c",
    WorkflowStatus.CANCELLED: "\u274c",
}

_PHASE_AGENT_DISPLAY_LIMIT = 20
_PHASE_COMPLETED_TAIL = 5
_CARD_MAX_BYTES = 28_000  # Feishu card payload limit with safety margin
_COMPLETION_REPORT_MAX_CHARS = 4_000
_COMPLETION_SECTION_MAX_CHARS = 2_000


# ---------------------------------------------------------------------------
# Defensive (lint-level) helpers — no-op under normal operation
# ---------------------------------------------------------------------------


def _card_text_for_agent_output(
    elements: list[dict],
    forbidden_markers: tuple[str, ...],
) -> None:
    """Scan ``elements`` recursively for forbidden marker strings.

    Iterates through the element list and any nested ``dict``/``list`` values,
    looking for ``text`` / ``content`` string fields that contain any of the
    ``forbidden_markers``. If a match is found, raises
    :class:`RuntimeError` with the message ``"card leaked agent output"``.

    When ``forbidden_markers`` is empty, the function is a no-op — this is the
    normal production configuration. Tests monkey-patch
    ``_AGENT_OUTPUT_FORBIDDEN_MARKERS`` to inject sentinel strings and verify
    the gate trips when agent output accidentally leaks into card text.
    """
    if not forbidden_markers:
        return

    stack: list[Any] = list(elements)
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in ("text", "content") and isinstance(value, str):
                    for marker in forbidden_markers:
                        if marker and marker in value:
                            raise RuntimeError("card leaked agent output")
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


# ---------------------------------------------------------------------------
# Helper builders for Feishu card elements
# ---------------------------------------------------------------------------


def _md_element(content: str, *, text_align: str | None = None) -> dict[str, Any]:
    """Create a markdown text element."""
    element: dict[str, Any] = {"tag": "markdown", "content": content}
    if text_align is not None:
        element["text_align"] = text_align
    return element


def _hr_element() -> dict[str, Any]:
    """Create a horizontal rule divider."""
    return {"tag": "hr"}


def _collapsible_panel(
    header: str | dict[str, Any],
    elements: list[dict[str, Any]],
    *,
    expanded: bool = False,
    template: str | None = None,
) -> dict[str, Any]:
    """Wrap elements in a Feishu collapsible_panel.

    The ``expanded`` flag matches the Feishu schema and the convention used
    by the rest of the codebase (see ``card/render/tools.py`` and
    ``card/render/worktree.py``). When ``expanded=True`` the panel is shown
    open on first render; when ``expanded=False`` it is collapsed by default.
    """
    if isinstance(header, str):
        header_obj: dict[str, Any] = {
            "title": {"tag": "plain_text", "content": header},
        }
    else:
        header_obj = header
        header_obj.pop("template", None)
    panel = {
        "tag": "collapsible_panel",
        "header": header_obj,
        "elements": elements,
        "expanded": expanded,
    }
    if template is not None:
        panel["border"] = {"color": template, "corner_radius": "8px"}
    return panel


def _column_set(columns: list[dict[str, Any]], *, flex_mode: str = "none") -> dict[str, Any]:
    """Create a column_set layout element."""
    return {
        "tag": "column_set",
        "flex_mode": flex_mode,
        "columns": columns,
    }


def _column(
    elements: list[dict[str, Any]],
    *,
    weight: int = 1,
    width: str = "weighted",
    vertical_align: str | None = None,
) -> dict[str, Any]:
    """Create a single column inside a column_set."""
    column: dict[str, Any] = {
        "tag": "column",
        "width": width,
        "weight": weight,
        "elements": elements,
    }
    if vertical_align is not None:
        column["vertical_align"] = vertical_align
    return column


def _pct(used: int, total: int) -> str:
    """Calculate percentage string: "63%"."""
    if total <= 0:
        return "0%"
    return f"{int(min(used / total, 1.0) * 100)}%"


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h{mins}m"


def _format_tokens(tokens: int) -> str:
    """Format token count with K/M suffix."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}K"
    return str(tokens)


def _unicode_progress_bar(ratio: float, *, length: int = 20) -> str:
    """Render a Unicode block progress bar.

    Args:
        ratio: Progress ratio (0.0 to 1.0), clamped automatically.
        length: Total number of block characters (default: 20).

    Returns:
        Progress bar string like "┃████████████░░░░░░┃"
    """
    ratio = max(0.0, min(1.0, ratio))
    filled = int(ratio * length)
    empty = length - filled
    return f"┃{'█' * filled}{'░' * empty}┃"


# ---------------------------------------------------------------------------
# WorkflowProgressRenderer
# ---------------------------------------------------------------------------


class WorkflowProgressRenderer:
    """Renders workflow execution state into Feishu card-compatible JSON.

    Read-only: all state mutations happen through WorkflowStateManager.
    This class only reads the WorkflowProject to produce card elements.
    """

    def __init__(self, project: WorkflowProject) -> None:
        self._project = project
        self._start_time: float = project.started_at or time.time()

    # ------------------------------------------------------------------
    # Rendering — produce Feishu card elements
    # ------------------------------------------------------------------

    def render_progress_card(self, project: WorkflowProject | None = None) -> dict[str, Any]:
        """Generate the full Feishu card JSON structure.

        Args:
            project: Optional snapshot to render; falls back to self._project.
                Callers under concurrent mutation MUST pass a snapshot() for safety.
        """
        if project is not None:
            saved = self._project
            self._project = project
            try:
                return self._render_progress_card_impl()
            finally:
                self._project = saved
        return self._render_progress_card_impl()

    def _render_progress_card_impl(self) -> dict[str, Any]:
        elements: list[dict[str, Any]] = []

        # -- Current execution summary section (top) --
        summary = self._render_summary_section()
        if summary is not None:
            elements.append(summary)
            elements.append(_hr_element())

        # -- Progress bar section --
        elements.append(self._render_progress_bar_section())
        elements.append(_hr_element())

        # -- Phase tree --
        for idx, phase in enumerate(self._project.phases):
            elements.extend(self._render_phase_section(idx, phase))

        # -- Token usage section (informational, no budget limit) --
        elements.append(_hr_element())
        elements.append(self._render_token_usage_section())

        # -- Metrics footer --
        elements.append(_hr_element())
        elements.append(self._render_metrics_footer())

        # Defensive check: ensure no accidental agent-output sentinel leaks
        _card_text_for_agent_output(elements, _AGENT_OUTPUT_FORBIDDEN_MARKERS)

        # Enforce Feishu card payload size limit
        elements = _enforce_card_size(elements)

        return {
            "header": self._render_header(),
            "elements": elements,
        }

    def _render_summary_section(self) -> dict[str, Any] | None:
        """Render a compact current/latest activity summary block."""
        # Find a running agent first; fall back to the most-recently changed
        # agent across all phases.
        running_agent: Any = None
        latest_agent: Any = None
        latest_phase: PhaseProgress | None = None
        latest_changed_at: float | None = None
        latest_phase_only: PhaseProgress | None = None
        latest_phase_changed_at: float | None = None

        for phase in self._project.phases:
            phase_changed_at = getattr(phase, "finished_at", None) or getattr(phase, "started_at", None) or 0.0
            if latest_phase_changed_at is None or phase_changed_at > latest_phase_changed_at:
                latest_phase_only = phase
                latest_phase_changed_at = phase_changed_at
            for agent in phase.agents:
                if agent.status == AgentStatus.RUNNING and running_agent is None:
                    running_agent = agent
                    latest_phase = phase
                # Track most recently changed agent
                changed_at = getattr(agent, "finished_at", None) or getattr(agent, "started_at", None) or 0.0
                if latest_changed_at is None or changed_at > latest_changed_at:
                    latest_agent = agent
                    latest_changed_at = changed_at
                    if running_agent is None:
                        latest_phase = phase

        active_agent = running_agent or latest_agent
        if active_agent is None and latest_phase is None:
            latest_phase = latest_phase_only
            latest_changed_at = latest_phase_changed_at

        if active_agent is None and latest_phase is None:
            return None

        # Compose the summary lines
        lines: list[str] = []
        terminal = self._project.status in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        )
        if self._project.status == WorkflowStatus.COMPLETED:
            summary_title = "执行已完成"
        elif self._project.status == WorkflowStatus.FAILED:
            summary_title = "执行已失败"
        elif self._project.status == WorkflowStatus.CANCELLED:
            summary_title = "执行已取消"
        else:
            summary_title = "当前执行中"

        # Phase
        phase_title = latest_phase.title if latest_phase is not None else "(暂无阶段)"
        phase_title = _middle_ellipsis(phase_title)
        phase_idx = self._project.phases.index(latest_phase) + 1 if latest_phase in self._project.phases else "—"
        lines.append(f"📌 **当前阶段:** 阶段 {phase_idx} · {phase_title}")

        # Active agent
        agent_label_key = "最近代理" if terminal else "当前代理"
        tool_label_key = "使用工具" if terminal else "正在使用"
        if active_agent is not None:
            agent_label = _middle_ellipsis(active_agent.label or "agent")
            agent_status_icon = STATUS_ICONS.get(active_agent.status, "⏳")
            lines.append(f"🤖 **{agent_label_key}:** {agent_status_icon} {agent_label}")
            if active_agent.tool:
                lines.append(f"🛠 **{tool_label_key}:** `{active_agent.tool}`")
            else:
                lines.append(f"🛠 **{tool_label_key}:** (未指定工具)")
            if active_agent.task_summary:
                lines.append(f"📋 **当前任务:** {_middle_ellipsis(active_agent.task_summary, 60)}")
            activity = getattr(active_agent, "current_activity", "") or ""
            if activity:
                lines.append(f"⚡ **正在:** {_middle_ellipsis(activity, 60)}")
        else:
            lines.append(f"🤖 **{agent_label_key}:** (无代理调用)")
            lines.append(f"🛠 **{tool_label_key}:** —")

        # Last-change time
        if latest_changed_at and latest_changed_at > 0:
            from datetime import datetime  # local import — keeps module import lean

            try:
                ts = datetime.fromtimestamp(latest_changed_at).strftime("%H:%M:%S")
            except (OSError, OverflowError, ValueError):
                ts = "—"
            lines.append(f"🕒 **最近变更:** {ts}")
        else:
            lines.append("🕒 **最近变更:** —")

        # Live elapsed for a genuinely running agent — mirrors the per-agent
        # elapsed suffix so the top summary keeps advancing during a long
        # blocking agent() call (only meaningful while non-terminal).
        if (
            not terminal
            and active_agent is not None
            and active_agent.status == AgentStatus.RUNNING
            and active_agent.started_at
        ):
            elapsed = time.time() - active_agent.started_at
            if elapsed > 0:
                lines.append(f"⏱ **已运行:** {_format_duration(elapsed)}")

        return _md_element(f"**{summary_title}**\n" + "\n".join(lines))

    def render_compact_status(self) -> str:
        """One-line text summary of workflow status.

        Example: "任务: code-audit | 阶段 2/3 | 7/12 代理 完成 | 450K tokens 消耗"
        """
        name = self._project.name or "workflow"
        total_phases = len(self._project.phases)
        current_phase = self._current_phase_index() + 1

        metrics = self._project.metrics
        completed = metrics.completed_agents
        total = metrics.total_agents

        tokens = _format_tokens(metrics.total_tokens if hasattr(metrics, "total_tokens") else 0)

        status_icon = WORKFLOW_STATUS_ICONS.get(self._project.status, "\u23f3")

        return (
            f"任务: {name} | 阶段 {current_phase}/{total_phases} | "
            f"{completed}/{total} 代理 {status_icon} | {tokens} tokens 消耗"
        )

    # ------------------------------------------------------------------
    # Private rendering helpers
    # ------------------------------------------------------------------

    def _render_header(self) -> dict[str, Any]:
        """Render card header with workflow name + status."""
        status = self._project.status
        icon = WORKFLOW_STATUS_ICONS.get(status, "\u23f3")

        # Map status to header template color
        if status == WorkflowStatus.COMPLETED:
            template = "green"
        elif status == WorkflowStatus.FAILED:
            template = "red"
        elif status == WorkflowStatus.RUNNING:
            template = "blue"
        else:
            template = "grey"

        title = f"{icon} {self._project.name or 'Workflow'}"

        return {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        }

    def _render_progress_bar_section(self) -> dict[str, Any]:
        """Render overall progress as compact "进度 M/N · Z%" line + bar."""
        metrics = self._project.metrics
        completed = metrics.completed_agents
        total = max(metrics.total_agents, 1)
        ratio = completed / total
        pct = _pct(completed, total)
        bar = _unicode_progress_bar(ratio)
        return _md_element(f"进度 {completed}/{total} · {pct}\n{bar}")

    def _render_phase_section(self, idx: int, phase: PhaseProgress) -> list[dict[str, Any]]:
        """Render a phase with agents grouped by status into collapsible panels."""
        elements: list[dict[str, Any]] = []

        agents = phase.agents
        total_agents = len(agents)
        completed_count = sum(1 for a in agents if a.status in (AgentStatus.DONE, AgentStatus.CACHED))

        # Phase header — row 1: title (middle-ellipsis); row 2: completion count + duration
        phase_status = self._get_phase_status_icon(phase)
        elements.append(_md_element(f"**{phase_status} 阶段 {idx + 1}: {_middle_ellipsis(phase.title)}**"))

        if phase.started_at and total_agents > 0:
            elapsed = (phase.finished_at or time.time()) - phase.started_at
            duration_text = _format_duration(elapsed)
            elements.append(_md_element(f"已完成 {completed_count}/{total_agents} · 耗时 {duration_text}"))
        elif total_agents > 0:
            elements.append(_md_element(f"已完成 {completed_count}/{total_agents}"))
        elif phase.finished_at:
            if phase.started_at:
                elapsed = phase.finished_at - phase.started_at
                elements.append(_md_element(f"已完成 0/0 · 耗时 {_format_duration(elapsed)}"))
            else:
                elements.append(_md_element("已完成 0/0"))
        elif phase.started_at:
            elements.append(_md_element("进行中 0/0"))
        else:
            elements.append(_md_element("等待中"))

        if not agents:
            return elements

        # Paginate: for phases with more agents than a small-phase threshold,
        # show all running/failed + the last N done/cached agents.
        small_phase_threshold = 8
        apply_pagination = total_agents > small_phase_threshold

        # Group agents by status buckets
        buckets: dict[str, list[AgentProgress]] = {
            "RUNNING": [],
            "FAILED": [],
            "DONE": [],
            "CACHED": [],
            "CANCELLED": [],
            "PENDING": [],
        }
        for agent in agents:
            raw = agent.status.value if hasattr(agent.status, "value") else str(agent.status)
            key = raw.upper()
            if key in buckets:
                buckets[key].append(agent)
            else:
                buckets["PENDING"].append(agent)

        # Track hidden done/cached for the counter line (only when pagination applies)
        hidden_done = 0
        hidden_cached = 0
        if apply_pagination:
            if len(buckets["DONE"]) > _PHASE_COMPLETED_TAIL:
                hidden_done = len(buckets["DONE"]) - _PHASE_COMPLETED_TAIL
                buckets["DONE"] = buckets["DONE"][-_PHASE_COMPLETED_TAIL:]
            if len(buckets["CACHED"]) > _PHASE_COMPLETED_TAIL:
                hidden_cached = len(buckets["CACHED"]) - _PHASE_COMPLETED_TAIL
                buckets["CACHED"] = buckets["CACHED"][-_PHASE_COMPLETED_TAIL:]

        # Status → label + color mapping for collapsible_panel headers
        status_meta: dict[str, tuple[str, str]] = {
            "RUNNING": ("执行中", "blue"),
            "FAILED": ("失败", "red"),
            "DONE": ("已完成", "green"),
            "CACHED": ("缓存", "turquoise"),
            "CANCELLED": ("已取消", "grey"),
            "PENDING": ("待执行", "grey"),
        }

        # Render status groups as collapsible panels (RUNNING/FAILED expanded, rest collapsed)
        display_order = [
            ("RUNNING", True),
            ("FAILED", True),
            ("DONE", False),
            ("CACHED", False),
            ("CANCELLED", False),
            ("PENDING", False),
        ]
        for key, expanded in display_order:
            group = buckets[key]
            if not group:
                continue
            label, color = status_meta[key]
            lines: list[str] = []
            for agent in group:
                tool_badge = f"`{agent.tool}`" if agent.tool else ""
                display_label = _middle_ellipsis(agent.label or "agent")
                if agent.status == AgentStatus.RUNNING:
                    summary_text = ""
                    activity_text = getattr(agent, "current_activity", "") or ""
                    if activity_text:
                        summary_text = f"\n    > {_middle_ellipsis(activity_text, 60)}"
                    elif agent.task_summary:
                        summary_text = f"\n    > {_middle_ellipsis(agent.task_summary, 60)}"
                    # Live elapsed suffix so a long-running agent's line keeps
                    # advancing (paired with the engine heartbeat re-render).
                    running_text = "执行中…"
                    if agent.started_at:
                        elapsed = time.time() - agent.started_at
                        if elapsed > 0:
                            running_text = f"执行中 {_format_duration(elapsed)}…"
                    lines.append(
                        f"{STATUS_ICONS.get(agent.status, '·')} {display_label} {tool_badge} {running_text}{summary_text}"
                    )
                elif agent.error:
                    safe_err = _strip_internal_details(agent.error[:60])
                    dur = _format_duration(agent.duration_s) if agent.duration_s > 0 else ""
                    lines.append(
                        f"{STATUS_ICONS.get(agent.status, '·')} {display_label} {tool_badge} {dur} — {safe_err}"
                    )
                else:
                    dur = _format_duration(agent.duration_s) if agent.duration_s > 0 else ""
                    summary_hint = ""
                    if agent.task_summary:
                        summary_hint = f" — {_middle_ellipsis(agent.task_summary, 40)}"
                    lines.append(
                        f"{STATUS_ICONS.get(agent.status, '·')} {display_label} {tool_badge} {dur}{summary_hint}"
                    )
            header_obj: dict[str, Any] = {
                "title": {"tag": "plain_text", "content": f"{label} ({len(group)})"},
            }
            panel = _collapsible_panel(
                header_obj,
                [_md_element("\n".join(lines))],
                expanded=expanded,
                template=color,
            )
            elements.append(panel)

        # Hidden done/cached counter line
        if hidden_done or hidden_cached:
            hidden_total = hidden_done + hidden_cached
            elements.append(_md_element(f"共 {hidden_total} 条已完成/缓存（已折叠）"))

        if not apply_pagination and len(agents) > _PHASE_AGENT_DISPLAY_LIMIT:
            hidden = len(agents) - _PHASE_AGENT_DISPLAY_LIMIT
            elements.append(_md_element(f"... 另有 {hidden} 个代理"))

        return elements

    def _render_token_usage_section(self) -> dict[str, Any]:
        """Render token consumption as compact informational line (no budget limit)."""
        metrics = self._project.metrics
        total_tokens = metrics.total_tokens if hasattr(metrics, "total_tokens") else 0
        used_str = _format_tokens(total_tokens)
        return _md_element(f"Token 消耗: {used_str}")

    def _render_metrics_footer(self) -> dict[str, Any]:
        """Render metrics footer as a 2-column stretch layout: Agents/耗时 · 缓存/失败。"""
        metrics = self._project.metrics
        start = self._project.started_at or self._start_time
        end = self._project.finished_at or time.time()
        elapsed = end - start if end >= start else time.time() - self._start_time
        elapsed_str = _format_duration(elapsed)

        # Left column: Agents + 耗时
        left_content = [
            f"**代理:** {metrics.completed_agents}/{metrics.total_agents}",
            f"**耗时:** {elapsed_str}",
        ]
        # Right column: 缓存 + 失败
        right_content = []
        if metrics.cached_agents > 0:
            right_content.append(f"**缓存:** {metrics.cached_agents}")
        if metrics.failed_agents > 0:
            right_content.append(f"**失败:** {metrics.failed_agents}")
        if not right_content:
            right_content.append("**缓存:** 0")

        return _column_set(
            [
                _column(
                    [_md_element("\n".join(left_content), text_align="center")],
                    weight=1,
                    vertical_align="center",
                ),
                _column(
                    [_md_element("\n".join(right_content), text_align="center")],
                    weight=1,
                    vertical_align="center",
                ),
            ],
            flex_mode="stretch",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _current_phase_index(self) -> int:
        """Return index of the current (last) phase, or 0 if none."""
        if not self._project.phases:
            return 0
        return len(self._project.phases) - 1

    def _get_phase_status_icon(self, phase: PhaseProgress) -> str:
        """Determine the overall status icon for a phase."""
        if not phase.agents:
            if phase.finished_at:
                return "\u2705"
            return "\u23f3"

        has_running = any(a.status == AgentStatus.RUNNING for a in phase.agents)
        has_failed = any(a.status == AgentStatus.FAILED for a in phase.agents)
        all_done = all(a.status in (AgentStatus.DONE, AgentStatus.CACHED, AgentStatus.CANCELLED) for a in phase.agents)

        if has_running:
            return "\U0001f504"
        if all_done:
            return "\u2705"
        if has_failed:
            return "\u274c"
        return "\u23f3"


# ---------------------------------------------------------------------------
# Script preview helper (module-level, used by WorkflowHandler confirm card)
# ---------------------------------------------------------------------------

_SCRIPT_PREVIEW_MAX_LINES = 80
_SCRIPT_PREVIEW_MAX_CHARS = 2000


def render_script_preview(
    script: str,
    *,
    max_lines: int = _SCRIPT_PREVIEW_MAX_LINES,
    max_chars: int = _SCRIPT_PREVIEW_MAX_CHARS,
) -> str:
    """Format a workflow script for user preview in confirmation cards.

    Returns the script wrapped in a JS code fence. If the script exceeds
    *max_lines* or *max_chars*, it is truncated with an ellipsis note.
    """
    if not script or not script.strip():
        return ""

    lines = script.splitlines()
    truncated = False

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    body = "\n".join(lines)

    if len(body) > max_chars:
        body = body[:max_chars]
        # Trim to last complete line to avoid mid-line cut in code fence
        last_nl = body.rfind("\n")
        if last_nl > 0:
            body = body[:last_nl]
        truncated = True

    result = f"```javascript\n{body}\n```"
    if truncated:
        result += "\n\n_(脚本已截断，完整内容将在执行时使用)_"

    return result


def _truncate_completion_text(text: str, limit: int) -> str:
    """Truncate completion report text while preserving valid markdown."""
    text = _strip_internal_details(str(text or "").strip())
    if len(text) <= limit:
        return text
    trimmed = text[:limit].rstrip()
    last_newline = trimmed.rfind("\n")
    if last_newline > limit // 2:
        trimmed = trimmed[:last_newline].rstrip()
    return f"{trimmed}\n\n_(内容已截断)_"


def _parse_result_payload(raw_result: str) -> Any | None:
    """Best-effort parse of Workflow JS return values encoded as JSON."""
    raw_result = str(raw_result or "").strip()
    if not raw_result or raw_result[0] not in "[{":
        return None
    try:
        return json.loads(raw_result)
    except (TypeError, json.JSONDecodeError, ValueError):
        return None


def _humanize_result_key(key: str) -> str:
    labels = {
        "summary": "摘要",
        "final_report": "最终报告",
        "report": "报告",
        "result": "结果",
        "output": "输出",
        "status": "状态",
        "conclusion": "结论",
        "verification": "验证",
        "reviews": "评审",
        "risks": "风险",
        "risk": "风险",
        "findings": "发现",
        "recommendations": "建议",
        "next_steps": "后续",
        "worker_findings": "执行发现",
        "parallel_results": "并行结果",
        "results": "结果",
    }
    return labels.get(key, key.replace("_", " "))


def _format_result_value(value: Any, *, max_chars: int = _COMPLETION_SECTION_MAX_CHARS) -> str:
    """Format a scalar/list/dict result payload into compact markdown text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate_completion_text(value, max_chars)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        lines: list[str] = []
        for idx, item in enumerate(value[:8], 1):
            item_text = _format_result_value(item, max_chars=280).replace("\n", "；")
            if item_text:
                lines.append(f"{idx}. {item_text}")
        if len(value) > 8:
            lines.append(f"... 还有 {len(value) - 8} 项")
        return _truncate_completion_text("\n".join(lines), max_chars)
    if isinstance(value, dict):
        priority = [
            "summary",
            "conclusion",
            "status",
            "result",
            "output",
            "findings",
            "risks",
            "recommendations",
            "next_steps",
            "reviews",
        ]
        ordered_keys = [key for key in priority if key in value]
        ordered_keys.extend(key for key in value.keys() if key not in ordered_keys)

        lines = []
        for key in ordered_keys[:10]:
            item_text = _format_result_value(value.get(key), max_chars=320).replace("\n", "；")
            if item_text:
                lines.append(f"- {_humanize_result_key(key)}: {item_text}")
        if len(ordered_keys) > 10:
            lines.append(f"... 还有 {len(ordered_keys) - 10} 个字段")
        return _truncate_completion_text("\n".join(lines), max_chars)

    return _truncate_completion_text(str(value), max_chars)


def _completion_report_markdown(raw_result: str) -> str:
    """Build a user-facing report from the workflow's final return value."""
    payload = _parse_result_payload(raw_result)
    if payload is None:
        body = _format_result_value(raw_result, max_chars=_COMPLETION_REPORT_MAX_CHARS)
        return f"**执行报告**\n{body}"

    if not isinstance(payload, dict):
        body = _format_result_value(payload, max_chars=_COMPLETION_REPORT_MAX_CHARS)
        return f"**执行报告**\n{body}"

    section_plan = [
        ("final_report", "执行报告", _COMPLETION_REPORT_MAX_CHARS),
        ("report", "执行报告", _COMPLETION_REPORT_MAX_CHARS),
        ("summary", "执行摘要", _COMPLETION_SECTION_MAX_CHARS),
        ("result", "任务结果", _COMPLETION_SECTION_MAX_CHARS),
        ("output", "任务结果", _COMPLETION_SECTION_MAX_CHARS),
        ("verification", "验证摘要", _COMPLETION_SECTION_MAX_CHARS),
        ("reviews", "评审摘要", _COMPLETION_SECTION_MAX_CHARS),
        ("risks", "风险提示", _COMPLETION_SECTION_MAX_CHARS),
        ("next_steps", "后续建议", _COMPLETION_SECTION_MAX_CHARS),
    ]
    sections: list[tuple[str, str]] = []
    used_keys: set[str] = set()
    used_titles: set[str] = set()
    for key, title, limit in section_plan:
        if key not in payload:
            continue
        text = _format_result_value(payload.get(key), max_chars=limit)
        if not text:
            continue
        if title in used_titles:
            title = _humanize_result_key(key)
        sections.append((title, text))
        used_keys.add(key)
        used_titles.add(title)

    if not sections:
        body = _format_result_value(payload, max_chars=_COMPLETION_REPORT_MAX_CHARS)
        return f"**执行报告**\n{body}"

    important_unknowns = [
        key
        for key in ("worker_findings", "parallel_results", "results", "findings", "recommendations")
        if key in payload and key not in used_keys
    ]
    for key in important_unknowns[:3]:
        text = _format_result_value(payload.get(key), max_chars=600)
        if text:
            sections.append((_humanize_result_key(key), text))

    return "\n\n".join(f"**{title}**\n{text}" for title, text in sections)


def _completion_process_markdown(
    project: WorkflowProject,
    *,
    completed_phases: int,
    total_phases: int,
    completed_agents: int,
    total_agents: int,
    failed_agents: int,
    cached_agents: int,
) -> str:
    """Build a concise run process summary for the completion card."""
    lines = [
        "**执行过程**",
        f"- 阶段: {completed_phases}/{total_phases}",
        f"- 代理: {completed_agents}/{total_agents} 完成，{failed_agents} 失败，{cached_agents} 缓存",
    ]

    for idx, phase in enumerate(project.phases, 1):
        agents = phase.agents
        total = len(agents)
        done = sum(1 for agent in agents if agent.status in (AgentStatus.DONE, AgentStatus.CACHED))
        cancelled = sum(1 for agent in agents if agent.status == AgentStatus.CANCELLED)
        failed = sum(1 for agent in agents if agent.status == AgentStatus.FAILED)
        if failed:
            icon = "\u274c"
            state = f"{done}/{total} 完成，{failed} 失败"
        elif cancelled:
            icon = "⏹️"
            state = f"{done}/{total} 完成，{cancelled} 已取消"
        else:
            icon = "\u2705"
            state = f"已完成 {done}/{total}"

        duration = ""
        if phase.started_at and phase.finished_at:
            duration = f" · {_format_duration(phase.finished_at - phase.started_at)}"
        lines.append(f"{icon} 阶段 {idx}: **{_middle_ellipsis(phase.title)}** — {state}{duration}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Completion card helper (module-level, used by WorkflowHandler on_done)
# ---------------------------------------------------------------------------


def _completion_report_status_markdown(report_status: dict[str, Any]) -> str:
    """Render the full-report attachment status for the compact completion card."""
    if report_status.get("attachment_sent"):
        return "📎 **完整 HTML 报告已发送**\n- 附件已回复到当前 Workflow 话题，包含完整结论、原始结果和执行详情。"

    if report_status.get("generated"):
        html_path = report_status.get("html_path") or ""
        markdown_path = report_status.get("markdown_path") or ""
        error = report_status.get("error") or "附件发送失败"
        lines = [
            "📎 **完整 HTML 报告已生成，附件发送失败**",
            f"- 原因: {_escape_md(str(error))}",
        ]
        if html_path:
            lines.append(f"- HTML: `{_escape_md(str(html_path))}`")
        if markdown_path:
            lines.append(f"- Markdown: `{_escape_md(str(markdown_path))}`")
        return "\n".join(lines)

    error = report_status.get("error") or "未知错误"
    return f"📎 **HTML 报告生成失败**\n- 原因: {_escape_md(str(error))}"


def render_completion_card(
    project: WorkflowProject,
    *,
    report_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a final completion card summarizing the workflow run.

    Returns a dict with 'header' and 'elements' ready for Feishu card.
    """
    status = project.status
    metrics = project.metrics

    # Header color
    if status == WorkflowStatus.COMPLETED:
        template = "green"
        icon = "\u2705"
        title_suffix = "完成"
    elif status == WorkflowStatus.FAILED:
        template = "red"
        icon = "\u274c"
        title_suffix = "失败"
    elif status == WorkflowStatus.CANCELLED:
        template = "grey"
        icon = "\u274c"
        title_suffix = "已取消"
    else:
        template = "blue"
        icon = "\u2705"
        title_suffix = "完成"

    name = project.name or "Workflow"

    elements: list[dict[str, Any]] = []

    # Summary section
    elapsed = 0.0
    if project.started_at:
        end_time = project.finished_at or time.time()
        elapsed = end_time - project.started_at

    # Task description
    elements.append(_md_element(f"**任务**: {_escape_md(project.requirement[:200]) if project.requirement else name}"))

    total_phases = len(project.phases)
    completed_phases = sum(
        1
        for phase in project.phases
        if all(a.status in (AgentStatus.DONE, AgentStatus.CACHED, AgentStatus.CANCELLED) for a in phase.agents)
    )
    phase_agents = [agent for phase in project.phases for agent in phase.agents]
    total_agents_count = metrics.total_agents or len(phase_agents)
    completed_agents_count = metrics.completed_agents or sum(
        1 for agent in phase_agents if agent.status in (AgentStatus.DONE, AgentStatus.CACHED)
    )
    failed_agents_count = metrics.failed_agents or sum(1 for agent in phase_agents if agent.status == AgentStatus.FAILED)
    cached_agents_count = metrics.cached_agents or sum(1 for agent in phase_agents if agent.status == AgentStatus.CACHED)
    total_tokens = metrics.total_tokens or sum(agent.token_usage for agent in phase_agents)
    total_agents_for_rate = max(total_agents_count, 1)
    success_rate = int((completed_agents_count / total_agents_for_rate) * 100)

    def _stat_column(value: str, label: str) -> dict[str, Any]:
        """Create a single stat column with large number + description."""
        return _column(
            [
                _md_element(f"**{value}**", text_align="center"),
                _md_element(f"<font color='grey'>{label}</font>", text_align="center"),
            ],
            weight=1,
            vertical_align="center",
        )

    # Row 1: 总耗时 + 总 Token 消耗
    elements.append(
        _column_set(
            [
                _stat_column(_format_duration(elapsed), "总耗时"),
                _stat_column(_format_tokens(total_tokens), "总 Token 消耗"),
            ],
            flex_mode="stretch",
        )
    )

    # Row 2: 完成阶段数 + 成功率
    elements.append(
        _column_set(
            [
                _stat_column(f"{completed_phases}/{total_phases}", "完成阶段数"),
                _stat_column(f"{success_rate}%", "成功率"),
            ],
            flex_mode="stretch",
        )
    )

    if report_status:
        elements.append(_hr_element())
        elements.append(_md_element(_completion_report_status_markdown(report_status)))

    # Final report, with structured JSON payloads rendered as user-facing sections.
    has_full_report_file = bool(report_status and report_status.get("generated"))
    if project.result and has_full_report_file:
        elements.append(_hr_element())
        elements.append(
            _md_element(
                "**执行报告摘要**\n"
                "完整结论、验证结果、原始输出和 Workflow 状态已写入 HTML 报告；卡片仅保留执行过程简表。"
            )
        )
    elif project.result:
        elements.append(_hr_element())
        elements.append(_md_element(_completion_report_markdown(project.result)))
    elif status == WorkflowStatus.COMPLETED:
        elements.append(_hr_element())
        elements.append(
            _md_element(
                "**执行报告**\n"
                "本次 Workflow 没有返回最终结果；请参考下方执行过程确认阶段和代理执行情况。"
            )
        )

    # Process summary
    if project.phases:
        elements.append(_hr_element())
        elements.append(
            _md_element(
                _completion_process_markdown(
                    project,
                    completed_phases=completed_phases,
                    total_phases=total_phases,
                    completed_agents=completed_agents_count,
                    total_agents=total_agents_count,
                    failed_agents=failed_agents_count,
                    cached_agents=cached_agents_count,
                )
            )
        )

    # Error message for failed workflows
    if status == WorkflowStatus.FAILED and project.error:
        elements.append(_hr_element())
        safe_project_err = _strip_internal_details(project.error[:200])
        elements.append(_md_element(f"\u274c **错误**: {safe_project_err}"))

    # Defensive check: ensure no accidental agent-output sentinel leaks
    # into rendered card text. Default markers tuple is empty → no-op.
    _card_text_for_agent_output(elements, _AGENT_OUTPUT_FORBIDDEN_MARKERS)
    elements = _enforce_card_size(elements)

    return {
        "header": {
            "title": {"tag": "plain_text", "content": f"{icon} {name} — {title_suffix}"},
            "template": template,
        },
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# Card size enforcement
# ---------------------------------------------------------------------------


def _enforce_card_size(elements: list[dict]) -> list[dict]:
    """Truncate card elements if they would exceed Feishu's 30KB payload limit.

    Progressive truncation strategy:
    1. Truncate any long text content (> 500 chars) in markdown elements
    2. Remove elements from the end (footer/metrics first, keeping phases)
    3. If still over, aggressively truncate remaining markdown to 200 chars

    Returns the (possibly trimmed) element list.
    """
    import json as _json

    serialized = _json.dumps(elements, ensure_ascii=False)
    byte_len = len(serialized.encode("utf-8", errors="surrogatepass"))
    if byte_len <= _CARD_MAX_BYTES:
        return elements

    # Strategy 1: Moderate truncation of long markdown text
    for elem in elements:
        if isinstance(elem, dict) and elem.get("tag") == "markdown":
            content = elem.get("content", "")
            if isinstance(content, str) and len(content) > 500:
                elem["content"] = content[:497] + "..."

    serialized = _json.dumps(elements, ensure_ascii=False)
    byte_len = len(serialized.encode("utf-8", errors="surrogatepass"))
    if byte_len <= _CARD_MAX_BYTES:
        return elements

    # Strategy 2: Remove from the end (footer/metrics go first) — keeping the
    # front which contains summary, progress, and phase details (most useful)
    while len(elements) > 3:
        serialized = _json.dumps(elements, ensure_ascii=False)
        byte_len = len(serialized.encode("utf-8", errors="surrogatepass"))
        if byte_len <= _CARD_MAX_BYTES:
            break
        elements.pop()

    # Strategy 3: Aggressive truncation if still over
    serialized = _json.dumps(elements, ensure_ascii=False)
    byte_len = len(serialized.encode("utf-8", errors="surrogatepass"))
    if byte_len > _CARD_MAX_BYTES:
        for elem in elements:
            if isinstance(elem, dict) and elem.get("tag") == "markdown":
                content = elem.get("content", "")
                if isinstance(content, str) and len(content) > 200:
                    elem["content"] = content[:197] + "..."

    return elements
