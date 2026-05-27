"""ProgressTracker — rate-limited card progress updates for Slock engine.

Provides a throttled mechanism to push card updates reflecting task/plan
progress without exceeding Feishu API rate limits (≤2 updates/sec).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)


class CardChannelProtocol(Protocol):
    """Protocol for card delivery channel."""
    def send_card(self, card: dict | str, *, reply_to: str | None = None) -> Optional[str]: ...
    def update_card(self, message_id: str, card: dict | str) -> bool: ...


@dataclass
class ProgressState:
    """Current progress state for a tracked entity."""
    entity_id: str  # task_id or plan_id or any unique identifier
    entity_type: str = "task"  # arbitrary type label: "task", "plan", "agent", etc.
    progress_pct: int = 0
    status: str = ""
    detail: str = ""
    message_id: str = ""  # Feishu message_id of the progress card
    last_pushed_at: float = 0.0
    dirty: bool = False  # Has changed since last push
    metadata: dict = field(default_factory=dict)  # Extensible metadata for card builders


class ProgressTracker:
    """Rate-limited progress tracker with batched card updates.
    
    Tracks progress states for tasks/plans and pushes card updates
    at most every `min_interval` seconds per entity.
    """

    MIN_INTERVAL: float = 0.5  # Minimum seconds between updates per entity (≤2/sec)

    def __init__(
        self,
        card_channel: CardChannelProtocol,
        card_builder: Callable[[ProgressState], dict],
        *,
        min_interval: float = 0.5,
        auto_flush: bool = True,
        flush_period: float = 2.0,
    ) -> None:
        """
        Args:
            card_channel: Channel to push card updates through.
            card_builder: Function that builds a card dict from ProgressState.
            min_interval: Minimum seconds between updates for the same entity.
            auto_flush: Whether to run a background flush loop.
            flush_period: How often the background loop flushes dirty states.
        """
        self._channel = card_channel
        self._card_builder = card_builder
        self._min_interval = max(min_interval, self.MIN_INTERVAL)
        self._states: dict[str, ProgressState] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._stop_event = threading.Event()

        # Debounce state for overview card updates (per plan_id)
        self._overview_timers: dict[str, threading.Timer] = {}
        self._overview_msg_ids: dict[str, str] = {}  # plan_id -> message_id
        self._overview_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        if auto_flush:
            self._flush_thread = threading.Thread(
                target=self._flush_loop,
                args=(flush_period,),
                name="slock-progress-flush",
                daemon=True,
            )
            self._flush_thread.start()
        else:
            self._flush_thread = None

    def update(
        self,
        entity_id: str,
        *,
        entity_type: str = "task",
        progress_pct: int | None = None,
        status: str | None = None,
        detail: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Update progress for an entity. Marks state as dirty for next flush.

        Args:
            entity_id: Unique identifier for the tracked entity.
            entity_type: Type label (only used on first creation of state).
            progress_pct: 0-100 progress percentage.
            status: Short status label (e.g. "thinking", "running").
            detail: Human-readable detail text.
            metadata: Extra key-value pairs merged into state.metadata.
        """
        with self._lock:
            state = self._states.get(entity_id)
            if state is None:
                state = ProgressState(entity_id=entity_id, entity_type=entity_type)
                self._states[entity_id] = state

            if progress_pct is not None:
                state.progress_pct = max(0, min(100, progress_pct))
            if status is not None:
                state.status = status
            if detail is not None:
                state.detail = detail
            if metadata is not None:
                state.metadata.update(metadata)
            state.dirty = True

    def force_push(self, entity_id: str) -> bool:
        """Immediately push a card update for the given entity, ignoring rate limit."""
        with self._lock:
            state = self._states.get(entity_id)
            if state is None:
                return False
        return self._push_state(state)

    def flush_dirty(self) -> int:
        """Push all dirty states that have exceeded the rate limit interval.
        
        Returns the number of entities actually pushed.
        """
        now = time.monotonic()
        to_push: list[ProgressState] = []

        with self._lock:
            for state in self._states.values():
                if state.dirty and (now - state.last_pushed_at) >= self._min_interval:
                    to_push.append(state)

        pushed = 0
        for state in to_push:
            if self._push_state(state):
                pushed += 1
        return pushed

    def remove(self, entity_id: str) -> None:
        """Stop tracking an entity."""
        with self._lock:
            self._states.pop(entity_id, None)

    def get_state(self, entity_id: str) -> Optional[ProgressState]:
        """Get current progress state for an entity."""
        with self._lock:
            return self._states.get(entity_id)

    def set_overview_message_id(self, plan_id: str, message_id: str) -> None:
        """Register the Feishu message_id for a plan's overview card.

        This must be called before schedule_overview_update so the debouncer
        knows which message to update.
        """
        with self._overview_lock:
            self._overview_msg_ids[plan_id] = message_id

    def schedule_overview_update(self, plan_id: str) -> None:
        """Schedule a debounced overview card update for a plan.

        Uses a 500ms debounce timer to coalesce rapid updates.  If an update
        is already pending (timer running), the old timer is cancelled and a
        new 500ms timer starts — ensuring at most 2 updates per second.
        """
        with self._overview_lock:
            existing_timer = self._overview_timers.get(plan_id)
            if existing_timer is not None:
                existing_timer.cancel()

            timer = threading.Timer(0.5, self._flush_overview_update, args=(plan_id,))
            timer.daemon = True
            timer.name = f"slock-overview-debounce-{plan_id}"
            self._overview_timers[plan_id] = timer
            timer.start()

    def _flush_overview_update(self, plan_id: str) -> None:
        """Internal: fires when the debounce timer expires for a plan overview.

        Builds the overview card from current state and pushes it via the card
        channel.
        """
        with self._overview_lock:
            self._overview_timers.pop(plan_id, None)
            message_id = self._overview_msg_ids.get(plan_id)

        if not message_id:
            logger.warning(
                "Cannot flush overview update for plan %s: no message_id registered",
                plan_id,
            )
            return

        with self._lock:
            state = self._states.get(plan_id)

        if state is None:
            logger.warning(
                "Cannot flush overview update for plan %s: no progress state",
                plan_id,
            )
            return

        try:
            card = self._card_builder(state)
            self._channel.update_card(message_id, card)
            logger.debug("Flushed overview card update for plan %s", plan_id)
        except Exception:
            logger.exception("Failed to flush overview card for plan %s", plan_id)

    def shutdown(self) -> None:
        """Stop background flush, cancel overview timers, push remaining dirty states."""
        self._stop_event.set()

        # Cancel all pending overview debounce timers
        with self._overview_lock:
            for timer in self._overview_timers.values():
                timer.cancel()
            self._overview_timers.clear()

        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)
        self.flush_dirty()

    def _push_state(self, state: ProgressState) -> bool:
        """Push a single state's card update."""
        try:
            card = self._card_builder(state)
            if state.message_id:
                success = self._channel.update_card(state.message_id, card)
            else:
                msg_id = self._channel.send_card(card)
                if msg_id:
                    state.message_id = msg_id
                    success = True
                else:
                    success = False

            if success:
                state.last_pushed_at = time.monotonic()
                state.dirty = False
            return success
        except Exception:
            logger.exception("Failed to push progress card for %s", state.entity_id)
            return False

    def _flush_loop(self, period: float) -> None:
        """Background loop that periodically flushes dirty states."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=period)
            if not self._stop_event.is_set():
                try:
                    self.flush_dirty()
                except Exception:
                    logger.exception("Progress flush loop error")
