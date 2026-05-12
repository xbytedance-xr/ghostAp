"""Small domain views over the aggregate card UI_TEXT mapping."""

from __future__ import annotations

import types

from .ui_text import UI_TEXT

_CORE_CARD_KEYS = (
    "log_truncated_warning",
    "claude_mode_title",
    "gemini_mode_title",
    "coco_mode_title",
    "smart_mode_title",
    "project_dir_label",
    "image_alt_text",
    "system_error_title",
    "system_error_prompt_title",
)

CORE_CARD_UI_TEXT = types.MappingProxyType({key: UI_TEXT[key] for key in _CORE_CARD_KEYS})

__all__ = ["CORE_CARD_UI_TEXT"]

