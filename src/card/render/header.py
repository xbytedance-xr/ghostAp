"""Header rendering: legacy title/subtitle or programming-card v2 header."""

from __future__ import annotations

import time
from pathlib import Path

from src.card.render.live_ticker import FROZEN_FRAME
from src.card.state.models import CardState

_TOOL_DISPLAY = {
    "coco": "Coco",
    "claude": "Claude",
    "aiden": "Aiden",
    "codex": "Codex",
    "gemini": "Gemini",
    "ttadk": "TTADK",
}


def render_header(state: CardState, *, page_index: int = 0, total_pages: int = 1) -> dict:
    """Generate Feishu Schema 2.0 header JSON.

    Programming cards with v2 metadata render project/tool/#seq on the first
    row and context/time on the second row. Plain/static cards keep the legacy
    reducer-provided title/subtitle.
    """
    if _should_render_v2_header(state):
        return _render_v2_header(state, page_index=page_index, total_pages=total_pages)

    title = _append_page_label(state.header.title, page_index=page_index, total_pages=total_pages)
    result: dict = {
        "title": {"tag": "plain_text", "content": title},
        "template": state.header.template,
    }

    if state.header.subtitle is not None:
        result["subtitle"] = {"tag": "plain_text", "content": state.header.subtitle}

    return result


def _should_render_v2_header(state: CardState) -> bool:
    metadata = state.metadata
    strong_v2_signal = any((
        metadata.working_dir,
        metadata.card_sequence != 1,
        metadata.is_subagent,
        metadata.frozen,
        metadata.bridge_phrase,
        metadata.session_started_at is not None,
    ))
    if metadata.engine_type:
        return strong_v2_signal
    return strong_v2_signal or bool(metadata.tool_name or metadata.model_name or metadata.session_started_at is not None)


def _render_v2_header(state: CardState, *, page_index: int = 0, total_pages: int = 1) -> dict:
    metadata = state.metadata
    tool_id = metadata.tool_name or metadata.mode_name
    tool_label = _TOOL_DISPLAY.get((tool_id or "").lower(), tool_id or metadata.mode_name or "?")
    project_name = metadata.project_name or state.header.title or "当前项目"
    seq = metadata.card_sequence
    archived = " · 已封存" if metadata.frozen else ""
    # v2 design: frozen cards hide model_name (mutual exclusion with 已封存 tag)
    model_suffix = "" if metadata.frozen else (f" · {metadata.model_name}" if metadata.model_name else "")

    context_suffix = _title_context_suffix(state)
    page_suffix = _page_label(page_index=page_index, total_pages=total_pages)
    title = f"📁 {project_name} · 🤖 {tool_label}{context_suffix} · #{seq}{page_suffix}{model_suffix}{archived}"
    if state.terminal == "failed" and "错误" not in title:
        title = f"❌ 错误 · {title}"

    # Minimal subtitle: only status marker + cumulative elapsed time
    cumulative_elapsed = _cumulative_elapsed_seconds(state)
    marker = _status_marker(state)

    if metadata.is_subagent and metadata.parent_card_seq:
        subtitle = f"↳ from #{metadata.parent_card_seq}"
        if cumulative_elapsed > 0:
            subtitle = f"{subtitle} · {marker} {_format_elapsed(cumulative_elapsed)}"
    elif metadata.frozen:
        subtitle = f"{marker} final {_format_elapsed(cumulative_elapsed)}"
    elif cumulative_elapsed > 0:
        subtitle = f"{marker} {_format_elapsed(cumulative_elapsed)}"
    else:
        subtitle = marker

    result: dict = {
        "title": {"tag": "plain_text", "content": title},
        "subtitle": {"tag": "plain_text", "content": subtitle},
        "template": _v2_header_template(state),
    }
    return result


def _title_context_suffix(state: CardState) -> str:
    labels: list[str] = []
    iteration = _iteration_label(state)
    if iteration:
        labels.append(iteration)

    unit = _unit_label(state.metadata, iteration)
    if unit:
        labels.append(unit)
    return "".join(f" · {label}" for label in labels)


def _iteration_label(state: CardState) -> str:
    metadata = state.metadata
    index = metadata.iteration_index
    total = metadata.iteration_total
    if index is None and state.engine_ext is not None and state.engine_ext.cycle_num > 0:
        index = state.engine_ext.cycle_num
        total = state.engine_ext.max_cycles or total
    if not index:
        return ""
    if total and total > 1:
        return f"第 {index}/{total} 轮"
    return f"第 {index} 轮"


def _unit_label(metadata, iteration_label: str) -> str:
    label = (metadata.unit_label or "").strip()
    if not label or label == iteration_label:
        return ""
    unit_id = (metadata.unit_id or "").strip()
    if metadata.unit_kind == "task":
        prefix = f"任务 {unit_id}" if unit_id else "任务"
        if label.startswith(prefix):
            return _truncate_title_part(label)
        return _truncate_title_part(f"{prefix}: {label}")
    if metadata.unit_kind == "subagent":
        prefix = f"子任务 {unit_id}" if unit_id else "子任务"
        if label.startswith(prefix):
            return _truncate_title_part(label)
        return _truncate_title_part(f"{prefix}: {label}")
    return _truncate_title_part(label)


def _truncate_title_part(text: str, limit: int = 28) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _append_page_label(title: str, *, page_index: int, total_pages: int) -> str:
    page = _page_label(page_index=page_index, total_pages=total_pages)
    return f"{title}{page}" if page else title


def _page_label(*, page_index: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    return f" · 页 {page_index + 1}/{total_pages}"


def _v2_header_template(state: CardState) -> str:
    metadata = state.metadata
    if metadata.frozen:
        return "grey"
    if metadata.is_subagent:
        return "orange"
    return state.header.template


def _status_marker(state: CardState) -> str:
    """Return the v2 header status marker.

    Live ticker frames are meaningful only while the card is actively running;
    terminal cards must show a stable semantic marker instead of the last
    animation frame retained in metadata.
    """
    metadata = state.metadata
    if metadata.frozen:
        return FROZEN_FRAME
    terminal_markers = {
        "completed": "✅",
        "failed": "❌",
        "cancelled": "⚪",
        "paused": "⏸",
        "blocked": "⛔",
    }
    if state.terminal != "running":
        return terminal_markers.get(state.terminal, "⚪")
    return metadata.live_ticker_frame or "🟢"


def _elapsed_seconds(state: CardState) -> float:
    """Return elapsed seconds using CardSession's monotonic start instant.

    ``CardMetadata.session_started_at`` is written from ``SessionConfig.clock``
    (monotonic by default). Do not populate it with wall-clock timestamps.
    """
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


def _cumulative_elapsed_seconds(state: CardState) -> float:
    metadata = state.metadata
    if metadata.frozen:
        return float(metadata.frozen_total_elapsed or 0)
    if metadata.session_started_at is None:
        return _elapsed_seconds(state)
    return max(0.0, time.monotonic() - metadata.session_started_at)


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
