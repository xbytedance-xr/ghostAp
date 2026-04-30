"""Adapter: bridges existing engine callbacks to CardSession dispatching.

This module provides factory functions that return standard engine callback
objects (DeepEngineCallbacks, LoopEngineCallbacks) but internally dispatch
all events through a CardSession instead of directly building/sending cards.

Usage in handlers:
    session = card_session_factory.create(chat_id, metadata)
    callbacks = create_deep_card_callbacks(session)
    engine.plan_and_execute(requirement, callbacks)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession

if TYPE_CHECKING:
    from src.acp.models import ACPEvent
    from src.deep_engine.engine import DeepEngineCallbacks, DeepProject
    from src.loop_engine.engine import LoopEngineCallbacks, LoopProject, IterationRecord, ReviewResult

logger = logging.getLogger(__name__)


def create_deep_card_callbacks(session: CardSession) -> "DeepEngineCallbacks":
    """Create DeepEngineCallbacks that dispatch to a CardSession.

    Replaces the old pattern of CardBuilder + ACPEventRenderer.
    """
    from src.deep_engine.engine import DeepEngineCallbacks

    def on_analyzing_start(requirement: str) -> None:
        session.dispatch(CardEvent.started())

    def on_analyzing_done(project: "DeepProject") -> None:
        # Plan phase complete, execution starting
        plan_text = ""
        if hasattr(project, "plan") and project.plan:
            plan_text = project.plan if isinstance(project.plan, str) else str(project.plan)
        if plan_text:
            session.dispatch(CardEvent.plan_updated(plan_text))

    def on_event(event: "ACPEvent") -> None:
        card_event = CardEvent.from_acp(event)
        session.dispatch(card_event)

    def on_text(text: str) -> None:
        session.dispatch(CardEvent.text_delta("_active_text", text))

    def on_project_done(project: "DeepProject") -> None:
        session.dispatch(CardEvent.completed())

    def on_error(error: str) -> None:
        session.dispatch(CardEvent.failed())

    return DeepEngineCallbacks(
        on_analyzing_start=on_analyzing_start,
        on_analyzing_done=on_analyzing_done,
        on_event=on_event,
        on_text=on_text,
        on_project_done=on_project_done,
        on_error=on_error,
    )


def create_loop_card_callbacks(session: CardSession) -> "LoopEngineCallbacks":
    """Create LoopEngineCallbacks that dispatch to a CardSession.

    Loop-specific: handles iteration tracking and progress updates.
    """
    from src.loop_engine.engine import LoopEngineCallbacks

    def on_analyzing_start(requirement: str) -> None:
        session.dispatch(CardEvent.started())

    def on_analyzing_done(project: "LoopProject") -> None:
        if hasattr(project, "acceptance_criteria") and project.acceptance_criteria:
            session.dispatch(CardEvent.plan_updated(project.acceptance_criteria))

    def on_iteration_start(current: int, total: int) -> None:
        session.dispatch(CardEvent.progress_updated(current, total, f"迭代 {current}"))
        # Start new text block for this iteration
        session.dispatch(CardEvent.text_started(f"iter_{current}_text"))

    def on_iteration_event(iteration: int, event: "ACPEvent") -> None:
        card_event = CardEvent.from_acp(event)
        session.dispatch(card_event)

    def on_iteration_done(iteration: int, record: "IterationRecord") -> None:
        session.dispatch(CardEvent.text_done(f"iter_{iteration}_text"))

    def on_review_done(iteration: int, review: "ReviewResult") -> None:
        status = "✅ 通过" if review.passed else "❌ 未通过"
        feedback = getattr(review, "feedback", "") or ""
        review_text = f"**Review {status}**\n{feedback}" if feedback else f"**Review {status}**"
        session.dispatch(CardEvent.text_started(f"review_{iteration}"))
        session.dispatch(CardEvent.text_delta(f"review_{iteration}", review_text))
        session.dispatch(CardEvent.text_done(f"review_{iteration}"))

    def on_project_done(project: "LoopProject") -> None:
        session.dispatch(CardEvent.completed())

    def on_error(error: str) -> None:
        session.dispatch(CardEvent.failed())

    return LoopEngineCallbacks(
        on_analyzing_start=on_analyzing_start,
        on_analyzing_done=on_analyzing_done,
        on_iteration_start=on_iteration_start,
        on_iteration_event=on_iteration_event,
        on_iteration_done=on_iteration_done,
        on_review_done=on_review_done,
        on_project_done=on_project_done,
        on_error=on_error,
    )
