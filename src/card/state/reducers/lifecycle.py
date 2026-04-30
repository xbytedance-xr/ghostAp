"""Lifecycle sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, CardMetadata, HeaderState, FooterState, ButtonSpec
from ...events import CardEvent, CardEventType


# Header template colors by terminal state
_TERMINAL_TEMPLATES: dict[str, str] = {
    "completed": "green",
    "failed": "red",
    "cancelled": "grey",
    "paused": "orange",
    "awaiting_approval": "indigo",
}

# Header template colors by mode (when running)
_MODE_TEMPLATES: dict[str, str] = {
    "Coco": "blue",
    "Claude": "purple",
    "Gemini": "turquoise",
    "TTADK": "orange",
    "Deep Agent": "turquoise",
    "Loop Engine": "indigo",
    "Spec Engine": "green",
    "Smart": "turquoise",
}


def _build_header(metadata: CardMetadata, terminal: str) -> HeaderState:
    """Build header state from metadata and terminal status."""
    # Title
    if metadata.project_name:
        title = f"{metadata.mode_emoji} {metadata.project_name} · {metadata.mode_name}"
    else:
        title = f"{metadata.mode_emoji} {metadata.mode_name} 编程模式"

    # Subtitle
    parts = []
    if metadata.tool_name:
        parts.append(metadata.tool_name)
    if metadata.model_name:
        parts.append(metadata.model_name)
    subtitle = "🔧 " + " · ".join(parts) if parts else None

    # Template color: terminal overrides mode
    template = _TERMINAL_TEMPLATES.get(terminal) or _MODE_TEMPLATES.get(metadata.mode_name, "blue")

    return HeaderState(title=title, subtitle=subtitle, template=template)


def reduce_lifecycle(state: CardState, event: CardEvent) -> CardState:
    """Handle lifecycle events: STARTED, COMPLETED, FAILED, CANCELLED, PAUSED, RESUMED."""
    match event.type:
        case CardEventType.STARTED:
            header = _build_header(state.metadata, "running")
            return replace(state, terminal="running", header=header,
                           footer=FooterState(status="thinking", status_text="💭 正在思考..."))

        case CardEventType.COMPLETED:
            header = _build_header(state.metadata, "completed")
            return replace(state, terminal="completed", header=header,
                           footer=FooterState(), buttons=())

        case CardEventType.FAILED:
            header = _build_header(state.metadata, "failed")
            return replace(state, terminal="failed", header=header,
                           footer=FooterState())

        case CardEventType.CANCELLED:
            header = _build_header(state.metadata, "cancelled")
            return replace(state, terminal="cancelled", header=header,
                           footer=FooterState(), buttons=())

        case CardEventType.PAUSED:
            header = _build_header(state.metadata, "paused")
            return replace(state, terminal="paused", header=header,
                           footer=FooterState(status="idle"))

        case CardEventType.RESUMED:
            header = _build_header(state.metadata, "running")
            return replace(state, terminal="running", header=header,
                           footer=FooterState(status="thinking", status_text="💭 正在思考..."))

    return state
