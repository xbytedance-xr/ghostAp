"""Fallback card rendering when the main render pipeline fails.

Provides a minimal but branded fallback card so that state/card divergence
never silently occurs — the user always sees *something* indicating the
session state, even when the full renderer crashes.

This module lives in the render layer and is called by the session layer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.card.engine_meta import ENGINE_LABELS, ENGINE_LABEL_DEFAULT, engine_type_to_cmd
from src.card.types import RenderedCard

if TYPE_CHECKING:
    from src.card.state.models import CardState

logger = logging.getLogger(__name__)


def render_fallback_card(state: "CardState | None", engine_type: str | None = None) -> list[RenderedCard] | None:
    """Build a minimal fallback card when the main render pipeline fails.

    Args:
        state: The current CardState snapshot (may be None on double-fault).
        engine_type: The engine type string (e.g. "deep", "loop") for command hint.

    Returns:
        A single-element list of RenderedCard, or None if even fallback construction fails.
    """
    try:
        # Extract header info from state with safe defaults
        title = "任务"
        template = "orange"
        if state and state.header:
            title = state.header.title or title

        # Determine engine command for recovery hint
        engine_cmd = engine_type_to_cmd(engine_type, fallback="命令")
        restart_label = ENGINE_LABELS.get(engine_type or "", ENGINE_LABEL_DEFAULT)

        # Build warning banner text
        warning_text = f"⚠️ **渲染异常，卡片内容可能不完整。发送 {engine_cmd} 重新执行。**"

        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": [
                {
                    "tag": "div",
                    "fields": [
                        {"tag": "markdown", "content": warning_text},
                    ],
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": restart_label},
                            "type": "primary",
                            "value": {
                                "action": "retry_command",
                                "_t": engine_cmd,
                            },
                        },
                    ],
                },
            ],
        }
        return [RenderedCard(_card_json=card_json, structure_signature="fallback", content_hash="fallback")]
    except Exception:
        logger.debug("render_fallback_card: fallback card build also failed")
        return None
