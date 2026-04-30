"""CardSession: Handler's single interaction point for card management.

Orchestrates the full pipeline: dispatch → reduce → render → deliver.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import replace

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.render.budget import RenderBudget
from src.card.render.renderer import render_card
from src.card.state.models import CardMetadata, CardState
from src.card.state.reducer import reduce_card_state

logger = logging.getLogger(__name__)

# Terminal events that trigger immediate flush
_TERMINAL_EVENTS = frozenset({
    CardEventType.COMPLETED,
    CardEventType.FAILED,
    CardEventType.CANCELLED,
})

# Structural events that always trigger a full card update
_STRUCTURAL_EVENTS = frozenset({
    CardEventType.STARTED,
    CardEventType.TOOL_STARTED,
    CardEventType.TOOL_DONE,
    CardEventType.TOOL_FAILED,
    CardEventType.REASONING_STARTED,
    CardEventType.REASONING_DONE,
    CardEventType.PLAN_UPDATED,
    CardEventType.TOOL_MODEL_CHANGED,
    CardEventType.PAUSED,
    CardEventType.RESUMED,
    CardEventType.APPROVAL_REQUESTED,
    CardEventType.APPROVAL_RESOLVED,
})


class CardSession:
    """Card session: single interaction point for handlers.

    Handlers only call dispatch(event) — the session handles
    reduce → render → deliver internally.
    """

    def __init__(
        self,
        chat_id: str,
        metadata: CardMetadata,
        delivery: CardDelivery,
        budget: RenderBudget | None = None,
        *,
        session_id: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self._session_id = session_id or str(uuid.uuid4())
        self._chat_id = chat_id
        self._metadata = metadata
        self._delivery = delivery
        self._budget = budget or RenderBudget()
        self._reply_to = reply_to
        self._state: CardState | None = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> CardState | None:
        """Read-only access to current card state."""
        with self._lock:
            return self._state

    @property
    def closed(self) -> bool:
        return self._closed

    def dispatch(self, event: CardEvent) -> None:
        """Dispatch an event through the full pipeline.

        Pipeline: reduce → render → deliver.
        Thread-safe: acquires internal lock.
        """
        if self._closed:
            logger.debug("CardSession %s: dispatch after close, ignoring %s", self._session_id, event.type)
            return

        with self._lock:
            # 1. Reduce: old state + event → new state
            self._state = reduce_card_state(self._state, event, self._metadata)

            # 2. Render: state → list[RenderedCard]
            rendered = render_card(self._state, self._budget)

            # 3. Deliver: send to Feishu
            is_terminal = event.type in _TERMINAL_EVENTS
            self._delivery.deliver(
                session_id=self._session_id,
                chat_id=self._chat_id,
                rendered=rendered,
                reply_to=self._reply_to,
            )

            # 4. Auto-close on terminal events
            if is_terminal:
                self._closed = True

    def close(self) -> None:
        """Explicitly close the session. Idempotent."""
        if self._closed:
            return
        with self._lock:
            self._closed = True
            self._delivery.close(self._session_id)


class CardSessionFactory:
    """Factory for creating CardSession instances.

    Injects shared delivery instance so handlers don't manage it.
    """

    def __init__(self, delivery: CardDelivery, budget: RenderBudget | None = None) -> None:
        self._delivery = delivery
        self._budget = budget

    def create(
        self,
        chat_id: str,
        metadata: CardMetadata,
        *,
        session_id: str | None = None,
        reply_to: str | None = None,
        budget: RenderBudget | None = None,
    ) -> CardSession:
        """Create a new CardSession."""
        return CardSession(
            chat_id=chat_id,
            metadata=metadata,
            delivery=self._delivery,
            budget=budget or self._budget,
            session_id=session_id,
            reply_to=reply_to,
        )
