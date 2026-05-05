"""SessionConfig: frozen dataclass grouping CardSession configuration parameters.

Consolidates the 10+ configuration/callback parameters that CardSession.__init__
previously accepted individually, reducing the constructor signature to a manageable
number of logical groups.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.card.render.budget import RenderBudget
from src.card.state.models import CardMetadata

if TYPE_CHECKING:
    from src.card.events import CardEvent
    from src.card.hooks import SessionHook


@dataclass(frozen=True)
class SessionCallbacks:
    """Grouped callback functions and hook configuration for CardSession.

    All callbacks are optional; None means the feature is degraded/disabled.
    ``action_registry`` and ``hooks`` are also grouped here to keep the
    factory's ``create()`` signature lean (≤7 keyword parameters).
    """

    notify_callback: Callable[[str, str], None] | None = None
    """Callable(chat_id, text) for out-of-band user notifications."""

    cancel_callback: Callable[[], None] | None = None
    """Callable() invoked on terminal cancellation (resource cleanup)."""

    reply_text_fn: Callable[[str, str], None] | None = None
    """Callable(message_id, text) fallback for text replies."""

    action_registry: dict[str, Callable[[dict], "CardEvent"]] | None = None
    """Maps action_id strings to CardEvent constructor callables."""

    hooks: tuple["SessionHook", ...] = ()
    """Lifecycle hooks (on_dispatched, on_terminal) injected at session creation."""


@dataclass(frozen=True)
class SessionConfig:
    """Immutable configuration for a CardSession instance.

    Thread-safe by virtue of being frozen — no synchronization needed.
    Budget clamping is performed by CardSessionFactory.create() before
    constructing this object.
    """

    metadata: CardMetadata
    """Card metadata (engine_type, title, theme, etc.)."""

    budget: RenderBudget = field(default_factory=RenderBudget)
    """Render budget controlling max chars / truncation."""

    reply_to: str | None = None
    """Optional message_id to reply to (creates card as reply)."""

    ttl_seconds: float | None = None
    """Idle timeout in seconds. None → falls back to config.card_session_idle_timeout (1800)."""

    warn_before_seconds: float | None = None
    """Seconds before TTL expiry to show prewarning (= session_idle_warn_at_remaining).
    None → falls back to config (420)."""

    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    """Monotonic clock callable for time tracking (injectable for testing)."""

    retry_delay: float = 3.0
    """Seconds to wait before terminal delivery retry."""

    sync_delivery: bool | None = None
    """If True, delivery runs synchronously on the calling thread (for tests).
    None → uses async thread pool (production default)."""

    def __post_init__(self) -> None:
        """Validate constraint invariants at construction time."""
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0 when set, got {self.ttl_seconds}")
        if self.warn_before_seconds is not None and self.warn_before_seconds < 0:
            raise ValueError(f"warn_before_seconds must be >= 0, got {self.warn_before_seconds}")
        if (self.ttl_seconds is not None and self.warn_before_seconds is not None
                and self.warn_before_seconds >= self.ttl_seconds):
            raise ValueError(
                f"warn_before_seconds ({self.warn_before_seconds}) must be < ttl_seconds ({self.ttl_seconds})"
            )
