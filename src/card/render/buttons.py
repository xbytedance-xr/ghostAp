"""Button group rendering."""

from __future__ import annotations

from src.card.state.models import ButtonSpec, CardState


def _render_button(spec: ButtonSpec) -> dict:
    """Render a single button element."""
    btn: dict = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": spec.text},
        "type": spec.type,
        "value": {"action": spec.action_id},
    }

    if spec.confirm is not None:
        btn["confirm"] = {
            "title": {"tag": "plain_text", "content": "确认"},
            "text": {"tag": "plain_text", "content": spec.confirm},
        }

    return btn


def render_buttons(state: CardState) -> list[dict]:
    """Generate button group elements.

    Layout rules:
    - No buttons → empty list
    - ≤2 buttons → column_set with one column per button
    - >2 buttons → action block with flow layout
    """
    if not state.buttons:
        return []

    buttons = [_render_button(spec) for spec in state.buttons]

    if len(buttons) <= 2:
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [btn],
            }
            for btn in buttons
        ]
        return [
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": columns,
            }
        ]

    # >2 buttons: action flow layout
    return [
        {
            "tag": "action",
            "layout": "flow",
            "actions": buttons,
        }
    ]
