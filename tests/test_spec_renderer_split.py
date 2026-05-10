"""SpecRenderer cycle/perspective change split tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.feishu.renderers.spec_renderer import SpecRenderer


def _build_renderer() -> SpecRenderer:
    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    handler.settings.engine_timeout_warning_seconds = 0
    handler.add_reaction = MagicMock()
    handler.reply_text = MagicMock()
    handler.send_text_to_chat = MagicMock()
    handler.ensure_request_id = MagicMock(return_value="req1")
    handler.get_card_delivery = MagicMock()
    handler.project_manager = MagicMock()
    handler.context_manager = MagicMock()
    return SpecRenderer(handler)


def test_spec_renderer_splits_on_cycle_change():
    renderer = _build_renderer()
    renderer._current_session = MagicMock(closed=False)

    captured: list[tuple[str, str | None, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None, bridge_phrase=None: captured.append((reason, hint, bridge_phrase))

    renderer.notify_cycle_change(current_cycle=1, perspective="spec")
    renderer.notify_cycle_change(current_cycle=2, perspective="code")

    assert any(reason == "cycle_changed" for reason, _, _ in captured)
    matching = [hint for reason, hint, _ in captured if reason == "cycle_changed"]
    assert any("cycle 2" in (hint or "") and "code" in (hint or "") for hint in matching)
    assert any(bridge == "续接：" for reason, _, bridge in captured if reason == "cycle_changed")


def test_spec_renderer_splits_on_perspective_change_within_cycle():
    renderer = _build_renderer()
    renderer._current_session = MagicMock(closed=False)

    captured: list[tuple[str, str | None, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None, bridge_phrase=None: captured.append((reason, hint, bridge_phrase))

    renderer.notify_cycle_change(current_cycle=1, perspective="spec")
    renderer.notify_cycle_change(current_cycle=1, perspective="code")

    assert any(reason == "cycle_changed" for reason, _, _ in captured)


def test_spec_renderer_no_split_on_first_cycle():
    renderer = _build_renderer()
    renderer._current_session = MagicMock(closed=False)

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_cycle_change(current_cycle=1, perspective="spec")

    assert captured == []
