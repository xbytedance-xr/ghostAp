"""WorkflowProgressRenderer — renders workflow progress tree for Feishu cards."""

from __future__ import annotations

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
# Constants
# ---------------------------------------------------------------------------

STATUS_ICONS: dict[AgentStatus, str] = {
    AgentStatus.PENDING: "\u23f3",
    AgentStatus.RUNNING: "\ud83d\udd04",
    AgentStatus.DONE: "\u2705",
    AgentStatus.FAILED: "\u274c",
    AgentStatus.CACHED: "\ud83d\udce6",
}

WORKFLOW_STATUS_ICONS: dict[WorkflowStatus, str] = {
    WorkflowStatus.IDLE: "\u23f3",
    WorkflowStatus.GENERATING_SCRIPT: "\ud83d\udd04",
    WorkflowStatus.AWAITING_CONFIRM: "\u23f3",
    WorkflowStatus.RUNNING: "\ud83d\udd04",
    WorkflowStatus.COMPLETED: "\u2705",
    WorkflowStatus.FAILED: "\u274c",
    WorkflowStatus.CANCELLED: "\u274c",
}

_PHASE_AGENT_DISPLAY_LIMIT = 20
_PHASE_COMPLETED_TAIL = 5


# ---------------------------------------------------------------------------
# Helper builders for Feishu card elements
# ---------------------------------------------------------------------------


def _md_element(content: str) -> dict[str, Any]:
    """Create a markdown text element."""
    return {"tag": "markdown", "content": content}


def _hr_element() -> dict[str, Any]:
    """Create a horizontal rule divider."""
    return {"tag": "hr"}


def _column_set(columns: list[dict[str, Any]], *, flex_mode: str = "none") -> dict[str, Any]:
    """Create a column_set layout element."""
    return {
        "tag": "column_set",
        "flex_mode": flex_mode,
        "columns": columns,
    }


def _column(elements: list[dict[str, Any]], *, weight: int = 1, width: str = "weighted") -> dict[str, Any]:
    """Create a single column inside a column_set."""
    return {
        "tag": "column",
        "width": width,
        "weight": weight,
        "elements": elements,
    }


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

    def render_progress_card(self) -> dict[str, Any]:
        """Generate the full Feishu card JSON structure.

        Returns a dict with 'header' and 'elements' suitable for
        Feishu Interactive Card v2.
        """
        elements: list[dict[str, Any]] = []

        # -- Progress bar section --
        elements.append(self._render_progress_bar_section())
        elements.append(_hr_element())

        # -- Phase tree --
        for idx, phase in enumerate(self._project.phases):
            elements.extend(self._render_phase_section(idx, phase))

        # -- Budget section --
        elements.append(_hr_element())
        elements.append(self._render_budget_section())

        # -- Metrics footer --
        elements.append(_hr_element())
        elements.append(self._render_metrics_footer())

        return {
            "header": self._render_header(),
            "elements": elements,
        }

    def render_compact_status(self) -> str:
        """One-line text summary of workflow status.

        Example: "WF: code-audit | Phase 2/3 | 7/12 agents Done | 450K tokens"
        """
        name = self._project.name or "workflow"
        total_phases = len(self._project.phases)
        current_phase = self._current_phase_index() + 1

        metrics = self._project.metrics
        completed = metrics.completed_agents
        total = metrics.total_agents

        tokens = _format_tokens(self._project.budget.used)

        status_icon = WORKFLOW_STATUS_ICONS.get(self._project.status, "\u23f3")

        return (
            f"WF: {name} | Phase {current_phase}/{total_phases} | "
            f"{completed}/{total} agents {status_icon} | {tokens} tokens"
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
        """Render overall progress with Unicode block progress bar."""
        metrics = self._project.metrics
        completed = metrics.completed_agents
        total = max(metrics.total_agents, 1)
        ratio = completed / total
        pct = _pct(completed, total)
        bar = _unicode_progress_bar(ratio)
        return _md_element(f"✅ {completed}/{total} ({pct}) {bar}")

    def _render_phase_section(self, idx: int, phase: PhaseProgress) -> list[dict[str, Any]]:
        """Render a single phase as compact single-column markdown (mobile-friendly)."""
        elements: list[dict[str, Any]] = []

        # Phase header
        phase_status = self._get_phase_status_icon(phase)
        duration = ""
        if phase.started_at:
            elapsed = (phase.finished_at or time.time()) - phase.started_at
            duration = f" ({_format_duration(elapsed)})"

        elements.append(
            _md_element(f"**{phase_status} Phase {idx + 1}: {phase.title}**{duration}")
        )

        # Agent list as compact single-column lines
        agents = phase.agents
        display_agents = self._paginate_agents(agents)

        if display_agents:
            lines: list[str] = []
            for agent in display_agents:
                icon = STATUS_ICONS.get(agent.status, "\u23f3")
                tool_badge = f"`{agent.tool}`" if agent.tool else ""
                label = agent.label or "agent"
                dur = _format_duration(agent.duration_s) if agent.duration_s > 0 else ""
                dur_suffix = f" {dur}" if dur else ""

                if agent.status == AgentStatus.RUNNING:
                    lines.append(f"{icon} {label} {tool_badge} 执行中…")
                elif agent.error:
                    safe_err = _strip_internal_details(agent.error[:60])
                    lines.append(f"{icon} {label} {tool_badge}{dur_suffix} — {safe_err}")
                else:
                    lines.append(f"{icon} {label} {tool_badge}{dur_suffix}")

            elements.append(_md_element("\n".join(lines)))

            # Pagination notice
            if len(agents) > _PHASE_AGENT_DISPLAY_LIMIT:
                hidden = len(agents) - len(display_agents)
                elements.append(
                    _md_element(f"*... +{hidden} agents*")
                )

        return elements

    def _render_budget_section(self) -> dict[str, Any]:
        """Render token budget with Unicode progress bar and usage warning markers."""
        budget = self._project.budget
        used_str = _format_tokens(budget.used)
        total_str = _format_tokens(budget.total)
        total = max(budget.total, 1)
        ratio = budget.used / total
        pct = _pct(budget.used, total)
        bar = _unicode_progress_bar(ratio)

        # Add warning markers based on usage percentage
        pct_value = min(ratio * 100, 100)
        if pct_value > 80:
            warning = " 🔴 "
        elif pct_value >= 50:
            warning = " ⚠️ "
        else:
            warning = " "

        return _md_element(f"💰 {used_str}/{total_str} ({pct}){warning}{bar}")

    def _render_metrics_footer(self) -> dict[str, Any]:
        """Render metrics footer: total agents, time elapsed, cached count."""
        metrics = self._project.metrics
        elapsed = time.time() - self._start_time
        elapsed_str = _format_duration(elapsed)

        parts = [
            f"Agents: {metrics.completed_agents}/{metrics.total_agents}",
            f"Time: {elapsed_str}",
        ]
        if metrics.cached_agents > 0:
            parts.append(f"Cached: {metrics.cached_agents}")
        if metrics.failed_agents > 0:
            parts.append(f"Failed: {metrics.failed_agents}")

        return _md_element(" | ".join(parts))

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
            return "\u23f3"

        has_running = any(a.status == AgentStatus.RUNNING for a in phase.agents)
        has_failed = any(a.status == AgentStatus.FAILED for a in phase.agents)
        all_done = all(
            a.status in (AgentStatus.DONE, AgentStatus.CACHED)
            for a in phase.agents
        )

        if has_running:
            return "\ud83d\udd04"
        if all_done:
            return "\u2705"
        if has_failed:
            return "\u274c"
        return "\u23f3"

    def _paginate_agents(self, agents: list[AgentProgress]) -> list[AgentProgress]:
        """Apply pagination: if >20 agents, show active + last 5 completed."""
        if len(agents) <= _PHASE_AGENT_DISPLAY_LIMIT:
            return agents

        # Split into active (PENDING/RUNNING) and completed (DONE/FAILED/CACHED)
        active: list[AgentProgress] = []
        completed: list[AgentProgress] = []

        for agent in agents:
            if agent.status in (AgentStatus.PENDING, AgentStatus.RUNNING):
                active.append(agent)
            else:
                completed.append(agent)

        # Show all active + last N completed
        tail = completed[-_PHASE_COMPLETED_TAIL:] if completed else []
        return active + tail


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


# ---------------------------------------------------------------------------
# Completion card helper (module-level, used by WorkflowHandler on_done)
# ---------------------------------------------------------------------------


def render_completion_card(project: WorkflowProject) -> dict[str, Any]:
    """Render a final completion card summarizing the workflow run.

    Returns a dict with 'header' and 'elements' ready for Feishu card.
    """
    status = project.status
    metrics = project.metrics
    budget = project.budget

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
    elements.append(_md_element(
        f"**任务**: {project.requirement[:200] if project.requirement else name}"
    ))

    # Stats grid — 2x2 column_set layout
    total_phases = len(project.phases)
    completed_phases = sum(
        1 for phase in project.phases
        if all(a.status in (AgentStatus.DONE, AgentStatus.CACHED) for a in phase.agents)
    )
    total_agents = max(metrics.total_agents, 1)
    success_rate = int((metrics.completed_agents / total_agents) * 100)

    def _stat_column(value: str, label: str) -> dict[str, Any]:
        """Create a single stat column with large number + description."""
        return _column([
            _md_element(f"**{value}**"),
            _md_element(f"<font color='grey'>{label}</font>"),
        ], weight=1)

    # Row 1: 总耗时 + 总 Token 消耗
    elements.append(_column_set([
        _stat_column(_format_duration(elapsed), "总耗时"),
        _stat_column(_format_tokens(budget.used), "总 Token 消耗"),
    ], flex_mode="none"))

    # Row 2: 完成阶段数 + 成功率
    elements.append(_column_set([
        _stat_column(f"{completed_phases}/{total_phases}", "完成阶段数"),
        _stat_column(f"{success_rate}%", "成功率"),
    ], flex_mode="none"))

    # Phase summary
    if project.phases:
        elements.append(_hr_element())
        phase_lines = []
        for idx, phase in enumerate(project.phases, 1):
            phase_icon = "\u2705"
            if any(a.status == AgentStatus.FAILED for a in phase.agents):
                phase_icon = "\u274c"
            done_count = sum(
                1 for a in phase.agents
                if a.status in (AgentStatus.DONE, AgentStatus.CACHED)
            )
            phase_lines.append(
                f"{phase_icon} Phase {idx}: **{phase.title}** — {done_count}/{len(phase.agents)} agents"
            )
        elements.append(_md_element("\n".join(phase_lines)))

    # Result preview (truncated)
    if project.result:
        elements.append(_hr_element())
        preview = project.result[:500]
        if len(project.result) > 500:
            preview += "\n\n_(结果已截断)_"
        elements.append(_md_element(f"**结果摘要**:\n{preview}"))

    # Error message for failed workflows
    if status == WorkflowStatus.FAILED and project.error:
        elements.append(_hr_element())
        safe_project_err = _strip_internal_details(project.error[:200])
        elements.append(_md_element(f"\u274c **错误**: {safe_project_err}"))

    return {
        "header": {
            "title": {"tag": "plain_text", "content": f"{icon} {name} — {title_suffix}"},
            "template": template,
        },
        "elements": elements,
    }
