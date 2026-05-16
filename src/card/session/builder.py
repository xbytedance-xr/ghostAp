"""SessionBuilder: Constructs collaborators for CardSession.

Extracted from CardSession.__init__ to keep the session class as a thin
orchestrator (≤15 lines of init logic). The builder is a one-shot helper —
call ``build()`` once, assign the returned namespace to the session instance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.card.actions.router import ActionRouter
from src.card.delivery.tracker import DeliveryTracker
from src.card.hooks import HookFirer
from src.card.session._ttl_mixin import TTLActuator, TTLContext
from src.card.session.ttl import TTLHandler
from src.card.timers.manager import SessionTimerManager

if TYPE_CHECKING:
    from src.card.delivery.engine import CardDelivery
    from src.card.dispatch_coordinator import DispatchDeliveryCoordinator
    from src.card.render.budget import RenderBudget
    from src.card.session.config import SessionCallbacks
    from src.card.state.models import CardMetadata


@dataclass(slots=True)
class SessionCollaborators:
    """Namespace holding all collaborators built by SessionBuilder."""

    action_router: ActionRouter
    tracker: DeliveryTracker
    hook_firer: HookFirer
    timers: SessionTimerManager
    ttl_ctx: TTLContext
    ttl_actuator: TTLActuator
    ttl_handler: TTLHandler
    coordinator: "DispatchDeliveryCoordinator"


class SessionBuilder:
    """One-shot builder that constructs all CardSession collaborators.

    Usage (inside CardSession.__init__)::

        collaborators = SessionBuilder(
            session_id=self._session_id,
            chat_id=self._chat_id,
            metadata=self._metadata,
            delivery=self._delivery,
            budget=self._budget,
            reply_to=self._reply_to,
            lock=self._lock,
            closed=self._closed,
            clock=self._clock,
            callbacks=cbs,
            ttl_seconds=ttl_seconds,
            warn_before_seconds=warn_before,
            retry_delay=config.retry_delay,
            sync_delivery=bool(config.sync_delivery),
            deliver_and_track_fn=self._deliver_and_track,
            engine_cmd_fn=...,
            engine_name_fn=...,
        ).build()
    """

    __slots__ = (
        "_session_id", "_chat_id", "_metadata", "_delivery", "_budget",
        "_reply_to", "_lock", "_closed", "_clock", "_callbacks",
        "_ttl_seconds", "_warn_before_seconds", "_retry_delay",
        "_deliver_and_track_fn", "_engine_cmd_fn", "_engine_name_fn",
    )

    def __init__(
        self,
        *,
        session_id: str,
        chat_id: str,
        metadata: "CardMetadata",
        delivery: "CardDelivery",
        budget: "RenderBudget",
        reply_to: str | None,
        lock: object,  # threading.Lock
        closed: object,  # threading.Event
        clock: Callable[[], float],
        callbacks: "SessionCallbacks",
        ttl_seconds: float,
        warn_before_seconds: float,
        retry_delay: float,
        deliver_and_track_fn: Callable,
        engine_cmd_fn: Callable[[], str],
        engine_name_fn: Callable[[], str],
    ) -> None:
        self._session_id = session_id
        self._chat_id = chat_id
        self._metadata = metadata
        self._delivery = delivery
        self._budget = budget
        self._reply_to = reply_to
        self._lock = lock
        self._closed = closed
        self._clock = clock
        self._callbacks = callbacks
        self._ttl_seconds = ttl_seconds
        self._warn_before_seconds = warn_before_seconds
        self._retry_delay = retry_delay
        self._deliver_and_track_fn = deliver_and_track_fn
        self._engine_cmd_fn = engine_cmd_fn
        self._engine_name_fn = engine_name_fn

    def build(self) -> SessionCollaborators:
        """Construct and wire all collaborators, return as a namespace."""
        cbs = self._callbacks

        # 1. ActionRouter
        action_router = ActionRouter(
            session_id=self._session_id,
            engine_type=self._metadata.engine_type or "",
            action_registry=cbs.action_registry or {},
        )

        # 2. DeliveryTracker
        tracker = DeliveryTracker()

        # 3. HookFirer
        hook_firer = HookFirer(cbs.hooks, self._session_id)

        # 4. SessionTimerManager
        timers = SessionTimerManager(
            session_id=self._session_id,
            ttl_seconds=self._ttl_seconds,
            clock=self._clock,
            retry_delay=self._retry_delay,
            warn_before_seconds=self._warn_before_seconds,
        )

        # 5. TTL stack (mutable state → context → actuator → handler)
        _mutable = TTLContext._MutableState(
            state=None,
            ttl_warned=False,
            terminal_reason=None,
            last_dispatch_time=self._clock(),
            ttl_seconds=self._ttl_seconds,
        )
        ttl_ctx = TTLContext(
            lock=self._lock,
            clock=self._clock,
            closed=self._closed,
            session_id=self._session_id,
            chat_id=self._chat_id,
            metadata=self._metadata,
            budget=self._budget,
            timers=timers,
            delivery=self._delivery,
            reply_to=self._reply_to,
            notify_callback=cbs.notify_callback,
            reply_text_fn=cbs.reply_text_fn,
            hook_firer=hook_firer,
            tracker=tracker,
            deliver_and_track=self._deliver_and_track_fn,
            engine_cmd_fn=self._engine_cmd_fn,
            engine_name_fn=self._engine_name_fn,
            mutable=_mutable,
        )
        ttl_actuator = TTLActuator(ttl_ctx)
        # TTLHandler needs a `decider` — that will be the CardSession itself.
        # We pass None here; the caller must set ttl_handler.decider = session after build().
        ttl_handler = TTLHandler(decider=None, actuator=ttl_actuator)  # type: ignore[arg-type]

        # 6. DispatchDeliveryCoordinator
        from src.card.dispatch_coordinator import DispatchDeliveryCoordinator

        coordinator = DispatchDeliveryCoordinator(
            session_id=self._session_id,
            chat_id=self._chat_id,
            delivery=self._delivery,
            tracker=tracker,
            hook_firer=hook_firer,
            ttl_handler=ttl_handler,
            notify_callback=cbs.notify_callback,
            cancel_callback=cbs.cancel_callback,
            reply_text_fn=cbs.reply_text_fn,
            reply_to=self._reply_to,
        )

        return SessionCollaborators(
            action_router=action_router,
            tracker=tracker,
            hook_firer=hook_firer,
            timers=timers,
            ttl_ctx=ttl_ctx,
            ttl_actuator=ttl_actuator,
            ttl_handler=ttl_handler,
            coordinator=coordinator,
        )
