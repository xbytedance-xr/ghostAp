"""LoopRenderer round-change split tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.feishu.renderers.loop_renderer import LoopRenderer


def _build_renderer() -> LoopRenderer:
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
    return LoopRenderer(handler)


def test_loop_renderer_splits_on_round_change():
    renderer = _build_renderer()
    renderer._current_session = MagicMock(closed=False)

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_round_change(current_round=1)
    renderer.notify_round_change(current_round=2)

    assert any(reason == "round_changed" for reason, _ in captured), (
        f"expected round_changed split, got {captured}"
    )
    matching = [hint for reason, hint in captured if reason == "round_changed"]
    assert any("第 2 轮" in (hint or "") for hint in matching)


def test_loop_renderer_no_split_on_first_round():
    """Initial round set should NOT trigger split."""
    renderer = _build_renderer()
    renderer._current_session = MagicMock(closed=False)

    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

    renderer.notify_round_change(current_round=1)

    assert captured == []
