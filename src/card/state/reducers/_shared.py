"""Shared utilities for card state reducers.

Public functions here are shared infrastructure used across multiple sub-reducers.
"""
from __future__ import annotations

from ..models import CardMetadata, HeaderState
from src.card.themes import TERMINAL_TEMPLATES, MODE_TEMPLATES
from ...ui_text import UI_TEXT


def build_header(metadata: CardMetadata, terminal: str) -> HeaderState:
    """Build header state from metadata and terminal status.

    Shared across lifecycle and approval reducers.
    """
    # Title
    if metadata.project_name:
        title = f"{metadata.mode_emoji} {metadata.project_name} · {metadata.mode_name}"
    else:
        title = UI_TEXT["card_lifecycle_mode_title"].format(
            emoji=metadata.mode_emoji, mode_name=metadata.mode_name
        )

    # Append continuation marker for rotated cards
    if metadata.continuation_seq > 0:
        title = f"{title} (续 #{metadata.continuation_seq})"

    # Subtitle
    parts = []
    if metadata.tool_name:
        parts.append(metadata.tool_name)
    if metadata.model_name:
        parts.append(metadata.model_name)
    subtitle = "🔧 " + " · ".join(parts) if parts else None

    # Template color: terminal overrides mode
    template = TERMINAL_TEMPLATES.get(terminal) or MODE_TEMPLATES.get(metadata.mode_name, "blue")

    return HeaderState(title=title, subtitle=subtitle, template=template)
