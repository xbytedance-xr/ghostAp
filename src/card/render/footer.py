"""Footer rendering: status line + progress + duration (banners rendered in body top by renderer.py)."""

from __future__ import annotations

import math
import json
import time

from src.card.render.atoms import RenderAtom, estimate_atom_size
from src.card.state.models import CardMetadata, CardState, ContentBlock
from src.card.ui_text import UI_TEXT
from .progress import render_progress_bar, MOBILE_SEGMENTS
from .budget import RenderBudget


def _format_idle_timeout(seconds: int) -> str:
    """Format idle timeout seconds into human-friendly display (e.g. '30 分钟', '2 小时')."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 3600:
        hours = seconds / 3600
        return f"约 {hours:.1g} 小时"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} 分钟"


def _format_timestamp(raw: str) -> str:
    """Format a timestamp into relative time (e.g. '刚刚', '3 秒前', '5 分钟前').

    Input format: "MM-DD HH:MM" or "HH:MM:SS" (from session.py).
    Falls back to raw string if parsing fails.
    """
    if not raw:
        return raw
    import datetime

    now = time.time()
    today = datetime.date.today()

    try:
        # Try "MM-DD HH:MM" format first
        if len(raw) >= 11 and raw[5] == " ":
            month, day = int(raw[:2]), int(raw[3:5])
            hour, minute = int(raw[6:8]), int(raw[9:11])
            dt = datetime.datetime(today.year, month, day, hour, minute)
            ts = dt.timestamp()
        elif ":" in raw and len(raw) <= 8:
            # "HH:MM" or "HH:MM:SS"
            parts = raw.split(":")
            hour, minute = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
            dt = datetime.datetime(today.year, today.month, today.day, hour, minute, second)
            ts = dt.timestamp()
        else:
            return raw
    except (ValueError, IndexError):
        return raw

    diff = int(now - ts)
    if diff < 0:
        diff = 0

    if diff < 5:
        return UI_TEXT["time_just_now"]
    if diff < 60:
        return UI_TEXT["time_secs_ago"].format(seconds=diff)
    minutes = diff // 60
    if minutes < 60:
        return UI_TEXT["time_mins_ago"].format(minutes=minutes)
    hours = minutes // 60
    remaining_mins = minutes % 60
    if hours < 24:
        if remaining_mins:
            return UI_TEXT["time_hours_mins_ago"].format(hours=hours, minutes=remaining_mins)
        return UI_TEXT["time_hours_ago"].format(hours=hours)
    days = hours // 24
    return UI_TEXT["time_days_ago"].format(days=days)


# Engine type → progress bar theme color
_ENGINE_PROGRESS_COLOR: dict[str, str] = {
    "deep": "violet",
    "loop": "indigo",
    "spec": "green",
    "worktree": "wathet",
}

_TOOL_BRIEF = {
    "Read": lambda p: f"读取 {p.get('path', '...')}",
    "Edit": lambda p: f"写入 {p.get('path', '...')}",
    "Write": lambda p: f"创建 {p.get('path', '...')}",
    "Grep": lambda p: f"搜索 “{p.get('pattern') or p.get('query') or '...'}”",
    "Glob": lambda p: f"列出 {p.get('pattern', '...')}",
    "Bash": lambda p: f"执行 {_short_cmd(p.get('command') or p.get('cmd') or '')}",
}


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration string."""
    seconds = max(0, int(seconds))
    if seconds <= 1:
        return UI_TEXT["duration_sub_second"]
    if seconds < 60:
        return UI_TEXT["duration_secs"].format(seconds=seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return UI_TEXT["duration_mins_secs"].format(minutes=minutes, seconds=secs)
    hours, mins = divmod(minutes, 60)
    return UI_TEXT["duration_hours_mins_secs"].format(hours=hours, minutes=mins, seconds=secs)


def render_now_tool_hint(tool) -> str:
    """Render the v2 footer's one-line hint for the currently running tool."""
    if tool is None:
        return ""
    status = _tool_status(tool)
    if status not in {"active", "in_progress", "running"}:
        return ""
    name = _tool_name(tool)
    payload = _tool_payload(tool)
    brief_fn = _TOOL_BRIEF.get(name)
    brief = brief_fn(payload) if brief_fn else name
    return f"⚙ {name} · {brief}"


def render_subagent_badge(metadata: CardMetadata) -> str:
    """Render the v2 footer's compact subagent badge."""
    if not metadata.is_subagent:
        return ""
    sub_parts = ["🧬 sub"]
    if metadata.model_name:
        sub_parts.append(f"model: {metadata.model_name}")
    if metadata.tool_name:
        sub_parts.append(f"tool: {metadata.tool_name}")
    if metadata.parent_card_seq:
        sub_parts.append(f"from #{metadata.parent_card_seq}")
    return " · ".join(sub_parts)


def build_footer_atoms(state: CardState) -> list[RenderAtom]:
    """Build footer-specific atoms for tests and future appendices.

    The production renderer still emits Feishu elements directly from
    ``render_footer`` because footer elements are appended only on the final
    page. This helper centralizes the v2 footer text contract for reusable
    assertions and follow-up render paths.
    """
    atoms: list[RenderAtom] = []
    now_tool_hint = render_now_tool_hint(_find_running_tool(state))
    if now_tool_hint:
        atom = RenderAtom(kind="text", content=now_tool_hint, node_count=1)
        atom.byte_size = estimate_atom_size(atom)
        atoms.append(atom)

    subagent_badge = render_subagent_badge(state.metadata)
    if subagent_badge:
        atom = RenderAtom(kind="text", content=subagent_badge, node_count=1)
        atom.byte_size = estimate_atom_size(atom)
        atoms.append(atom)
    return atoms


def render_footer(state: CardState, budget: RenderBudget | None = None) -> list[dict]:
    """Generate footer elements.

    Layout order:
      1. hr separator
      2. Status text + progress merged (notation size)
      3. Tool/model info line
      4. Duration (terminal states show final, running states show elapsed)

    Note: All warning banners (error/warning/info/success) are now rendered
    at body top by renderer.py for unified positioning.
    """
    elements: list[dict] = []

    # Determine if we have any status/progress content to show
    has_status_content = state.footer.status is not None
    # Also render footer for terminal states (tool/model/duration)
    has_meta_content = bool(state.metadata.tool_name or state.metadata.model_name or state.footer.duration_seconds)

    if not has_status_content and not has_meta_content:
        return []

    elements.append({"tag": "hr"})
    status_text = state.footer.status_text or ""

    # Progress rendering: merge status + progress bar into one line (only when status is active)
    if has_status_content and state.footer.progress_pct is not None:
        bar_color = _ENGINE_PROGRESS_COLOR.get(state.metadata.engine_type or "", "blue")
        mobile_segs = MOBILE_SEGMENTS if (budget is None or budget.mobile) else None
        bar_text = render_progress_bar(state.footer.progress_pct, color=bar_color, mobile_segments=mobile_segs)
        # Add semantic label prefix based on context
        prefix = ""
        if state.engine_ext and state.engine_ext.criteria_total > 0:
            prefix = f"{UI_TEXT['card_progress_criteria_label']}: "
        elif state.metadata.engine_type == "deep":
            prefix = f"{UI_TEXT['card_progress_tool_label']}: "
        elif state.metadata.engine_type == "loop":
            prefix = f"{UI_TEXT['card_progress_loop_label']}: "
        elif state.metadata.engine_type == "worktree":
            prefix = f"{UI_TEXT['card_progress_worktree_label']}: "
        # Merge status text + bar + progress count into single line
        parts = []
        if status_text:
            parts.append(status_text)
        bar_part = f"{prefix}{bar_text}"
        if state.footer.progress:
            bar_part = f"{bar_part}\u2003{state.footer.progress}"
        parts.append(bar_part)
        content = " · ".join(parts) if len(parts) > 1 else parts[0]
        elements.append(
            {"tag": "markdown", "content": content, "text_size": "notation"}
        )
    elif has_status_content and state.footer.progress is not None:
        # Plain progress text merged with status
        if status_text:
            content = f"{status_text} · {state.footer.progress}"
        else:
            content = state.footer.progress
        elements.append(
            {"tag": "markdown", "content": content, "text_size": "notation"}
        )
    elif has_status_content and status_text:
        elements.append(
            {"tag": "markdown", "content": status_text, "text_size": "notation"}
        )
    elif not has_status_content and status_text:
        # Terminal states: show CTA text even without active status
        elements.append(
            {"tag": "markdown", "content": status_text, "text_size": "notation"}
        )

    now_tool_hint = render_now_tool_hint(_find_running_tool(state))
    if now_tool_hint:
        elements.append(
            {"tag": "markdown", "content": now_tool_hint, "text_size": "notation"}
        )

    # Tool/model info line + duration (combined into one line)
    meta_parts = []
    if state.metadata.tool_name:
        meta_parts.append(f"🔧 {state.metadata.tool_name}")
    if state.metadata.model_name:
        meta_parts.append(f"🧩 {state.metadata.model_name}")

    # Duration: terminal states use final duration_seconds; running states compute elapsed
    duration_str = None
    if state.footer.duration_seconds is not None and state.terminal in ("completed", "failed", "cancelled", "blocked", "archived", "ttl_expired"):
        duration_str = _format_duration(state.footer.duration_seconds)
    elif state.footer.progress_started_at is not None and not state.terminal:
        elapsed = time.monotonic() - state.footer.progress_started_at
        if elapsed >= 1:
            duration_str = _format_duration(elapsed)

    if duration_str:
        meta_parts.append(f"⏱ {duration_str}")

    if meta_parts:
        elements.append(
            {"tag": "markdown", "content": " · ".join(meta_parts), "text_size": "notation"}
        )

    subagent_badge = render_subagent_badge(state.metadata)
    if subagent_badge:
        elements.append(
            {"tag": "markdown", "content": subagent_badge, "text_size": "notation"}
        )

    # Blocked reason as visible text below footer status
    if state.terminal == "blocked" and state.engine_ext and state.engine_ext.blocked_reason:
        reason_text = UI_TEXT["card_lifecycle_blocked_reason_fmt"].format(reason=state.engine_ext.blocked_reason)
        elements.append(
            {"tag": "markdown", "content": reason_text, "text_size": "notation"}
        )

    # Idle timeout hint — only show when remaining time <= warn_before_seconds
    if state.terminal == "running" and state.metadata and state.metadata.idle_timeout_seconds:
        warn_before = state.metadata.warn_before_seconds if hasattr(state.metadata, "warn_before_seconds") and state.metadata.warn_before_seconds else state.metadata.idle_timeout_seconds
        idle_remaining = getattr(state.footer, "idle_remaining_seconds", None)
        show_hint = idle_remaining is not None and idle_remaining <= warn_before
        if show_hint or idle_remaining is None:
            # Fallback: show hint when we can't determine remaining (legacy behavior)
            if idle_remaining is None:
                timeout_display = _format_idle_timeout(state.metadata.idle_timeout_seconds)
                hint = UI_TEXT["card_footer_idle_timeout_hint"].format(timeout_display=timeout_display)
                elements.append(
                    {"tag": "markdown", "content": hint, "text_size": "notation"}
                )
            else:
                timeout_display = _format_idle_timeout(state.metadata.idle_timeout_seconds)
                hint = UI_TEXT["card_footer_idle_timeout_hint"].format(timeout_display=timeout_display)
                elements.append(
                    {"tag": "markdown", "content": hint, "text_size": "notation"}
                )

    # Last updated timestamp on non-terminal (active) cards
    if not state.terminal and state.footer.last_updated_at:
        _ts_display = _format_timestamp(state.footer.last_updated_at)
        elements.append(
            {"tag": "markdown", "content": UI_TEXT["card_footer_last_updated"].format(timestamp=_ts_display), "text_size": "notation"}
        )

    return elements


def _find_running_tool(state: CardState) -> ContentBlock | None:
    for block in reversed(state.blocks):
        if getattr(block, "kind", "") != "tool_call":
            continue
        if getattr(block, "status", "") in {"active", "in_progress", "running"}:
            return block
    return None


def _tool_name(tool) -> str:
    return str(getattr(tool, "tool_name", None) or getattr(tool, "name", None) or "tool")


def _tool_status(tool) -> str:
    return str(getattr(tool, "status", ""))


def _tool_payload(tool) -> dict:
    raw = getattr(tool, "tool_input", None)
    if raw is None:
        raw = getattr(tool, "input", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            if _tool_name(tool) == "Bash":
                return {"command": raw}
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _short_cmd(command: str) -> str:
    command = " ".join(command.split())
    if not command:
        return "..."
    return command[:80] + ("…" if len(command) > 80 else "")
