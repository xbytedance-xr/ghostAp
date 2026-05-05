"""DeliveryTracker: encapsulates delivery failure tracking and recovery banner logic.

REQUIRES external synchronization (CardSession._lock).

All methods must be called under the owning CardSession's lock.
This class does NOT maintain its own lock — it relies on the caller
to guarantee serial access. Do NOT use from multiple threads without
holding the owning session's lock.

Thread-interleaving semantic guarantee:
    When two threads interleave (thread A completes delivery → sets pending flags;
    thread B enters Phase 1 → consumes pending actions), the banner state is
    **eventually consistent**: any pending flag set by thread A will be consumed
    by the *next* dispatch that enters Phase 1. Between the flag-set and the
    consumption, an intermediate render may reflect stale banner state, but
    this stale render will be immediately overwritten by the consuming dispatch's
    render. No banner state is ever permanently lost or duplicated.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum, auto


class PendingAction(Enum):
    """Actions the session should perform before the next reduce."""
    SHOW_RECOVERY = auto()
    CLEAR_BANNER = auto()
    SHOW_MAX_FAILURES_WARNING = auto()
    SHOW_RETRY_PENDING = auto()  # Terminal retry in progress — show "updating" banner


class DeliveryTracker:
    """Tracks delivery failures and manages recovery/warning banner state.

    Design:
    - All public methods must be called under the owning session's lock.
    - Exposes `consume_pending_actions() → list[PendingAction]` for Phase 1 (banner actions).
    - Exposes `on_success(is_terminal) → None` and `on_failure() → None` for Phase 2.
    - `notify_callback` is invoked (outside lock by caller) when max_failures is reached.

    Args:
        max_failures: Number of consecutive failures before triggering degraded warning.
        clear_threshold: Number of consecutive successes required to clear recovery banner.
        min_banner_display_secs: Minimum seconds the recovery banner must be shown.
        clock: Injectable monotonic clock for testability.
    """

    def __init__(
        self,
        *,
        max_failures: int = 3,
        clear_threshold: int = 5,
        min_banner_display_secs: float = 3.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_failures < 1:
            raise ValueError(f"max_failures must be >= 1, got {max_failures}")
        if clear_threshold < 1:
            raise ValueError(f"clear_threshold must be >= 1, got {clear_threshold}")
        self._max_failures = max_failures
        self._clear_threshold = clear_threshold
        self._min_banner_display_secs = min_banner_display_secs
        self._clock = clock or time.monotonic

        # Mutable state (protected by caller's lock)
        self._delivery_failures: int = 0
        self._pending_recovery: bool = False
        self._pending_clear_banner: bool = False
        self._pending_max_failures_warning: bool = False
        self._pending_retry: bool = False
        self._consecutive_successes_after_recovery: int = 0
        self._recovery_banner_active: bool = False
        self._recovery_banner_shown_at: float = 0.0
        self._should_notify_max_failures: bool = False
        self._last_failure_timestamp: str | None = None

    @property
    def delivery_failures(self) -> int:
        return self._delivery_failures

    @property
    def last_failure_timestamp(self) -> str | None:
        """Timestamp string (HH:MM) of the most recent delivery failure."""
        return self._last_failure_timestamp

    @property
    def should_notify_max_failures(self) -> bool:
        """Check and consume the max-failures notification flag."""
        if self._should_notify_max_failures:
            self._should_notify_max_failures = False
            return True
        return False

    def consume_pending_actions(self) -> list[PendingAction]:
        """Consume pending banner actions and return PendingAction enums.

        Must be called at the start of dispatch() Phase 1 (under lock).
        Implements mutual exclusion: recovery takes priority over max_failures.

        The caller (CardSession.dispatch) is responsible for converting
        PendingAction values into CardEvent.warning_updated() calls.
        """
        actions: list[PendingAction] = []

        # Mutual exclusion: if recovery is pending, it trumps max_failures
        if self._pending_recovery and self._pending_max_failures_warning:
            self._pending_max_failures_warning = False

        if self._pending_recovery:
            self._pending_recovery = False
            self._consecutive_successes_after_recovery = 0
            self._recovery_banner_active = True
            self._recovery_banner_shown_at = self._clock()
            actions.append(PendingAction.SHOW_RECOVERY)

        if self._pending_clear_banner:
            self._pending_clear_banner = False
            self._recovery_banner_active = False
            actions.append(PendingAction.CLEAR_BANNER)

        if self._pending_max_failures_warning:
            self._pending_max_failures_warning = False
            actions.append(PendingAction.SHOW_MAX_FAILURES_WARNING)

        if self._pending_retry:
            self._pending_retry = False
            actions.append(PendingAction.SHOW_RETRY_PENDING)

        return actions

    def on_success(self, is_terminal: bool) -> None:
        """Record a successful delivery. Called in Phase 2 under lock.

        Args:
            is_terminal: Whether this was a terminal event delivery.
        """
        had_failures = self._delivery_failures > 0
        self._delivery_failures = 0

        if is_terminal:
            # Terminal success — no recovery banner needed
            return

        if had_failures:
            # First success after failures — flag recovery banner
            self._pending_recovery = True
            self._consecutive_successes_after_recovery = 0
            return

        # Track consecutive successes while recovery banner is active
        if self._recovery_banner_active:
            self._consecutive_successes_after_recovery += 1
            elapsed = self._clock() - self._recovery_banner_shown_at
            if (
                self._consecutive_successes_after_recovery >= self._clear_threshold
                and elapsed >= self._min_banner_display_secs
            ):
                self._pending_clear_banner = True
                self._consecutive_successes_after_recovery = 0

    def on_failure(self) -> None:
        """Record a delivery failure. Called in Phase 2 under lock."""
        self._delivery_failures += 1
        self._last_failure_timestamp = datetime.now().strftime("%H:%M")
        if self._delivery_failures >= self._max_failures:
            # Mutual exclusion: if recovery was pending, clear it
            if self._pending_recovery:
                pass  # recovery still takes priority in consume_pending_actions
            self._pending_max_failures_warning = True
            # Only flip notification flag once (False→True), avoid repeated notifications
            if not self._should_notify_max_failures:
                self._should_notify_max_failures = True

    def flag_retry_pending(self) -> None:
        """Flag that a terminal retry is scheduled — show 'updating' banner on next render."""
        self._pending_retry = True
