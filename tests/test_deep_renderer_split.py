"""DeepRenderer task-done card split tests."""
from __future__ import annotations

from unittest.mock import MagicMock

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


def test_deep_renderer_splits_on_task_done():
    renderer = _build_renderer()
    captured: list[tuple[str, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None: captured.append((reason, hint))

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

    assert any(reason == "task_done" for reason, _ in captured)
    matching_hints = [hint for reason, hint in captured if reason == "task_done"]
    assert any(hint is not None and "task 2" in hint for hint in matching_hints)
