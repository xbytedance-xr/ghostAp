"""BaseRenderer card_split helper tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.card.events import CardEventType
from src.feishu.renderers.base import BaseRenderer


class _FakeRenderer(BaseRenderer):
    def __init__(self):
        self.handler = MagicMock()
        self.ctx = MagicMock()
        self.settings = MagicMock()
        self.ui_states = {}
        self._session_factory = None
        self.split_calls: list[tuple[str, str | None]] = []

    def _on_card_split_completed(self, reason: str, hint: str | None) -> None:
        self.split_calls.append((reason, hint))


def test_dispatch_card_split_emits_event_and_registers_callback():
    renderer = _FakeRenderer()
    session = MagicMock()

    renderer._dispatch_card_split(session, reason="task_done", hint="task 3")

    args = session.dispatch.call_args
    assert args is not None
    event = args.args[0]
    assert event.type == CardEventType.CARD_SPLIT
    assert event.payload["reason"] == "task_done"
    assert event.payload["hint"] == "task 3"

    cb = getattr(session, "on_card_split_completed", None)
    assert callable(cb)
    cb("task_done", "task 3")
    assert renderer.split_calls == [("task_done", "task 3")]


def test_default_on_card_split_completed_is_noop():
    """BaseRenderer default _on_card_split_completed must be safe no-op."""

    class _Plain(BaseRenderer):
        def __init__(self):
            self.handler = MagicMock()
            self.ctx = MagicMock()
            self.settings = MagicMock()
            self.ui_states = {}
            self._session_factory = None

    renderer = _Plain()
    renderer._on_card_split_completed("any_reason", None)
