"""DeepRenderer task-done card split tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.acp import ACPEvent, ACPEventType
from src.acp.models import PlanEntryInfo, PlanInfo
from src.feishu.renderers.deep_renderer import DeepRenderer


def _build_renderer() -> DeepRenderer:
    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    handler.settings.engine_timeout_warning_seconds = 0
    handler.add_reaction = MagicMock()
    handler.send_text_to_chat = MagicMock()
    handler.reply_text = MagicMock()
    handler.context_manager = MagicMock()
    handler.ensure_request_id = MagicMock(return_value="req1")
    handler.get_card_delivery = MagicMock()
    handler.project_manager = MagicMock()
    handler.get_engine_name = MagicMock(return_value="Coco")
    renderer = DeepRenderer(handler)
    renderer.create_session = MagicMock(return_value=MagicMock(closed=False))
    return renderer


def test_deep_renderer_does_not_split_on_task_done_in_single_card_mode():
    """Single-card mode keeps task transitions inside the same Feishu card."""
    renderer = _build_renderer()
    captured: list[tuple[str, str | None, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None, bridge_phrase=None: captured.append((reason, hint, bridge_phrase))

    mock_settings = MagicMock()
    mock_settings.card.task_level_cards_enabled = False

    with patch("src.config.get_settings", return_value=mock_settings):
        callbacks = renderer.create_deep_callbacks(
            message_id="m1",
            chat_id="c1",
            project=None,
            engine_name="Coco",
        )

    initial_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="in_progress"),
        PlanEntryInfo(content="task 2", status="pending"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=initial_plan))

    updated_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="completed"),
        PlanEntryInfo(content="task 2", status="in_progress"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=updated_plan))

    assert captured == []


def test_deep_renderer_no_split_in_multi_card_mode():
    """Multi-card mode must not use task_done card_split either."""
    renderer = _build_renderer()
    captured: list[tuple[str, str | None, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None, bridge_phrase=None: captured.append((reason, hint, bridge_phrase))

    mock_settings = MagicMock()
    mock_settings.card.task_level_cards_enabled = True

    with patch("src.config.get_settings", return_value=mock_settings):
        callbacks = renderer.create_deep_callbacks(
            message_id="m1",
            chat_id="c1",
            project=None,
            engine_name="Coco",
        )

    initial_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="in_progress"),
        PlanEntryInfo(content="task 2", status="pending"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=initial_plan))

    updated_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="completed"),
        PlanEntryInfo(content="task 2", status="in_progress"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=updated_plan))

    # No card_split should have been dispatched
    assert not any(reason == "task_done" for reason, _, _ in captured)
