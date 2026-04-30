"""Header rendering: title + subtitle + template color."""

from __future__ import annotations

from src.card.state.models import CardState


def render_header(state: CardState) -> dict:
    """Generate Feishu Schema 2.0 header JSON.

    Title uses header.title directly (pre-formatted by reducer).
    Subtitle uses header.subtitle if present.
    Template uses header.template value.
    """
    result: dict = {
        "title": {"tag": "plain_text", "content": state.header.title},
        "template": state.header.template,
    }

    if state.header.subtitle is not None:
        result["subtitle"] = {"tag": "plain_text", "content": state.header.subtitle}

    return result
