"""BaseRenderer card_split helper tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.card.events import CardEventType
from src.feishu.renderers.base import BaseRenderer, EngineRenderStrategy


class _FakeRenderer(BaseRenderer):
    def __init__(self):
        self.handler = MagicMock()
        self.ctx = MagicMock()
        self.settings = MagicMock()
        self.ui_states = {}
        self._session_factory = None
        self.render_strategy = EngineRenderStrategy(self)
        self.split_calls: list[tuple[str, str | None, str | None]] = []

    def _on_card_split_completed(self, reason: str, hint: str | None, bridge_phrase: str | None = None) -> None:
        self.split_calls.append((reason, hint, bridge_phrase))


def test_dispatch_card_split_emits_event_and_registers_callback():
    renderer = _FakeRenderer()
    session = MagicMock()

    renderer._dispatch_card_split(session, reason="task_done", hint="task 3", bridge_phrase="续接：")

    args = session.dispatch.call_args
    assert args is not None
    event = args.args[0]
    assert event.type == CardEventType.CARD_SPLIT
    assert event.payload["reason"] == "task_done"
    assert event.payload["hint"] == "task 3"
    assert event.payload["bridge_phrase"] == "续接："

    cb = getattr(session, "on_card_split_completed", None)
    assert callable(cb)
    cb("task_done", "task 3", "续接：")
    assert renderer.split_calls == [("task_done", "task 3", "续接：")]


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


def test_engine_render_strategy_dispatches_split_with_shared_bridge_phrase():
    renderer = _FakeRenderer()
    session = MagicMock()

    renderer.render_strategy.dispatch_card_split(session, reason="cycle_changed", hint="cycle 2")

    event = session.dispatch.call_args.args[0]
    assert event.type == CardEventType.CARD_SPLIT
    assert event.payload["reason"] == "cycle_changed"
    assert event.payload["hint"] == "cycle 2"
    assert event.payload["bridge_phrase"] == EngineRenderStrategy.DEFAULT_BRIDGE_PHRASE


def test_deep_and_spec_renderers_share_strategy_facade_type():
    from src.feishu.renderers.deep_renderer import DeepRenderer
    from src.feishu.renderers.spec_renderer import SpecRenderer

    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()

    assert isinstance(DeepRenderer(handler).render_strategy, EngineRenderStrategy)
    assert isinstance(SpecRenderer(handler).render_strategy, EngineRenderStrategy)
