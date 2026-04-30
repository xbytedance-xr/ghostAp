"""Delivery throttle: schedules flush with adaptive timing."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from src.card.flow_control import FlowControlConfig, FlowControlState, FlowControlStrategy
from src.card.render.renderer import RenderedCard

DELIVERY_INTERVAL_MS = 200  # Minimum flush interval for structural changes


@dataclass
class _PendingFlush:
    """A pending flush scheduled for a session."""

    rendered: list[RenderedCard]
    timer: threading.Timer | None = None
    scheduled_at: float = 0.0


class DeliveryThrottle:
    """Throttle delivery to avoid excessive API calls.

    Strategy:
    - Terminal events: immediate flush
    - Structural changes: 200ms throttle (coalesce rapid updates)
    - Text-only stream: uses FlowControlStrategy (EMA-adaptive)
    """

    def __init__(
        self,
        flush_callback: Callable[[str, list[RenderedCard]], None],
        flow_config: FlowControlConfig | None = None,
    ) -> None:
        self._flush_callback = flush_callback
        self._pending: dict[str, _PendingFlush] = {}
        self._flow_states: dict[str, FlowControlState] = {}
        self._flow_strategy = FlowControlStrategy(flow_config or FlowControlConfig())
        self._lock = threading.Lock()

    def schedule(
        self,
        session_id: str,
        rendered: list[RenderedCard],
        *,
        immediate: bool = False,
        text_delta_len: int = 0,
    ) -> None:
        """Schedule a delivery for the session.

        Args:
            session_id: Session identifier.
            rendered: Rendered cards to deliver.
            immediate: If True, flush immediately (terminal events).
            text_delta_len: Length of text delta (for adaptive flow control).
        """
        with self._lock:
            # Cancel any existing pending flush
            existing = self._pending.get(session_id)
            if existing is not None and existing.timer is not None:
                existing.timer.cancel()
                existing.timer = None

            if immediate:
                # Flush immediately
                self._pending.pop(session_id, None)
                self._do_flush(session_id, rendered)
                return

            # Determine delay
            if text_delta_len > 0:
                # Text streaming: use adaptive flow control
                delay = self._get_adaptive_delay(session_id, text_delta_len)
            else:
                # Structural change: fixed 200ms throttle
                delay = DELIVERY_INTERVAL_MS / 1000.0

            # Schedule delayed flush
            pending = _PendingFlush(rendered=rendered, scheduled_at=time.time())
            timer = threading.Timer(delay, self._on_timer, args=[session_id])
            timer.daemon = True
            pending.timer = timer
            self._pending[session_id] = pending
            timer.start()

    def flush_now(self, session_id: str) -> None:
        """Immediately flush any pending delivery for a session."""
        with self._lock:
            pending = self._pending.pop(session_id, None)
            if pending is not None:
                if pending.timer is not None:
                    pending.timer.cancel()
                self._do_flush(session_id, pending.rendered)

    def cancel(self, session_id: str) -> None:
        """Cancel any pending delivery for a session."""
        with self._lock:
            pending = self._pending.pop(session_id, None)
            if pending is not None and pending.timer is not None:
                pending.timer.cancel()

    def has_pending(self, session_id: str) -> bool:
        """Check if there's a pending delivery for a session."""
        with self._lock:
            return session_id in self._pending

    def _on_timer(self, session_id: str) -> None:
        """Timer callback: flush the pending delivery."""
        with self._lock:
            pending = self._pending.pop(session_id, None)
            if pending is not None:
                self._do_flush(session_id, pending.rendered)

    def _do_flush(self, session_id: str, rendered: list[RenderedCard]) -> None:
        """Execute the flush callback (called within lock)."""
        # Release lock before callback to avoid deadlock
        # Note: we copy rendered ref before releasing
        try:
            self._flush_callback(session_id, rendered)
        except Exception:
            pass  # Errors handled by the callback layer

    def _get_adaptive_delay(self, session_id: str, delta_len: int) -> float:
        """Get adaptive delay based on content arrival rate."""
        if session_id not in self._flow_states:
            self._flow_states[session_id] = FlowControlState()

        state = self._flow_states[session_id]
        self._flow_strategy.update_rate(state, time.time(), delta_len)
        return state.min_update_interval_s
