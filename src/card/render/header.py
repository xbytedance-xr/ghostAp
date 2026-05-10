"""Header rendering: legacy title/subtitle or programming-card v2 header."""

from __future__ import annotations

import time
from pathlib import Path

from src.card.state.models import CardState

_TOOL_DISPLAY = {
    "coco": "Coco",
    "claude": "Claude",
    "aiden": "Aiden",
    "codex": "Codex",
    "gemini": "Gemini",
    "ttadk": "TTADK",
}


def render_header(state: CardState) -> dict:
    """Generate Feishu Schema 2.0 header JSON.

    Programming cards with v2 metadata render project/tool/#seq on the first
    row and context/time on the second row. Plain/static cards keep the legacy
    reducer-provided title/subtitle.
    """
    if _should_render_v2_header(state):
        return _render_v2_header(state)

    result: dict = {
        "title": {"tag": "plain_text", "content": state.header.title},
        "template": state.header.template,
    }

    if state.header.subtitle is not None:
        result["subtitle"] = {"tag": "plain_text", "content": state.header.subtitle}

    return result


def _should_render_v2_header(state: CardState) -> bool:
    metadata = state.metadata
    return any((
        metadata.tool_name,
        metadata.model_name,
        metadata.working_dir,
        metadata.card_sequence != 1,
        metadata.session_started_at is not None,
        metadata.is_subagent,
        metadata.frozen,
    ))


def _render_v2_header(state: CardState) -> dict:
    metadata = state.metadata
    tool_id = metadata.tool_name or metadata.mode_name
    tool_label = _TOOL_DISPLAY.get((tool_id or "").lower(), tool_id or metadata.mode_name or "?")
    project_name = metadata.project_name or state.header.title or "当前项目"
    seq = metadata.card_sequence
    archived = " · 已封存" if metadata.frozen else ""
    model_suffix = f" · {metadata.model_name}" if metadata.model_name else ""

    title = f"📁 {project_name} · 🤖 {tool_label} · #{seq}{model_suffix}{archived}"

    if metadata.is_subagent and metadata.parent_card_seq:
        left = f"↳ from #{metadata.parent_card_seq}"
    else:
        left = _short_path(metadata.working_dir) if metadata.working_dir else "工作目录未设置"

    elapsed = _elapsed_seconds(state)
    marker = "⏸" if metadata.frozen else "🟢"
    elapsed_label = _format_elapsed(elapsed)
    if metadata.frozen:
        right = f"{marker} final {elapsed_label}"
    elif elapsed > 0:
        right = f"{marker} {elapsed_label}"
    else:
        right = marker

    result: dict = {
        "title": {"tag": "plain_text", "content": title},
        "subtitle": {"tag": "plain_text", "content": f"{left} · {right}"},
        "template": "grey" if metadata.frozen else state.header.template,
    }
    return result


def _elapsed_seconds(state: CardState) -> float:
    metadata = state.metadata
    if metadata.frozen:
        return float(metadata.frozen_total_elapsed or 0)
    runtime = getattr(state, "runtime_stats", None)
    if runtime is not None:
        elapsed = getattr(runtime, "elapsed_seconds", None)
        if elapsed is not None:
            return float(elapsed)
    if metadata.session_started_at is not None:
        return max(0.0, time.monotonic() - metadata.session_started_at)
    return 0.0


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def _short_path(path: str | None) -> str:
    if not path:
        return ""
    try:
        resolved = Path(path).expanduser().resolve()
        home = Path.home().resolve()
        rel = resolved.relative_to(home)
        return f"~/{rel}"
    except (OSError, ValueError):
        return str(path)
