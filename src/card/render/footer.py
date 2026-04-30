"""Footer rendering: status line."""

from __future__ import annotations

from src.card.state.models import CardState


def render_footer(state: CardState) -> list[dict]:
    """Generate footer elements.

    If footer.status is None, returns empty list.
    Otherwise returns hr separator + status markdown, optionally with progress line.
    """
    if state.footer.status is None:
        return []

    elements: list[dict] = [
        {"tag": "hr"},
        {"tag": "markdown", "content": state.footer.status_text, "text_size": "notation"},
    ]

    if state.footer.progress is not None:
        elements.append(
            {"tag": "markdown", "content": state.footer.progress, "text_size": "notation"}
        )

    return elements
