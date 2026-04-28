"""Shared type definitions for the review subsystem.

Houses ReviewCircuitState so that review.py and review_retry.py can both
import it at module top-level without circular dependencies.
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, NotRequired, Optional, TypedDict

from .retry_status import RetryEvent

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


class RetryTexts(TypedDict, total=False):
    """Formal contract for retry UI text overrides.

    All keys are optional (total=False) — callers may provide a subset,
    and consumers fall back to hardcoded defaults for missing keys.
    """

    retry_no_retry: str
    retry_exhausted: str
    retry_waiting: str
    retry_succeeded: str


@dataclass
class ReviewCircuitState:
    """Circuit-breaker state for the review pipeline.

    Thread safety: accessed only from the single SpecEngine thread that owns
    the review cycle.  No lock required.
    """

    last_review_failure_diag: Optional[dict] = None
    review_failure_consecutive: int = 0
    review_circuit_open_until_cycle: int = 0
    backoff_level: int = 0
    consecutive_timeouts: int = 0
    consecutive_skips: int = 0
    last_review_elapsed_ms: int = 0
    last_failure_timestamp: float = 0.0
    recent_outcomes: list = field(default_factory=list)

    def reset_on_success(self) -> None:
        """Reset all failure/backoff counters after a successful review.

        Centralises the 5-field reset + recent_outcomes bookkeeping that was
        previously duplicated in three call-sites.
        """
        self.review_failure_consecutive = 0
        self.review_circuit_open_until_cycle = 0
        self.backoff_level = 0
        self.consecutive_timeouts = 0
        self.consecutive_skips = 0
        try:
            self.recent_outcomes.append("success")
            if len(self.recent_outcomes) > 20:
                self.recent_outcomes[:] = self.recent_outcomes[-20:]
        except Exception:
            logger.debug("ReviewCircuitState.reset_on_success: recent_outcomes bookkeeping failed", exc_info=True)

    def on_failure(self, all_timeout: bool) -> None:
        """Record a failure — symmetric counterpart to reset_on_success.

        Args:
            all_timeout: If True, also increments consecutive_timeouts.
        """
        self.last_failure_timestamp = time.monotonic()
        if all_timeout:
            self.consecutive_timeouts = int(self.consecutive_timeouts or 0) + 1
            self.review_failure_consecutive = int(self.review_failure_consecutive or 0) + 1
        else:
            self.review_failure_consecutive = int(self.review_failure_consecutive or 0) + 1
        try:
            self.recent_outcomes.append("partial_failure")
            if len(self.recent_outcomes) > 20:
                self.recent_outcomes[:] = self.recent_outcomes[-20:]
        except Exception:
            logger.debug("ReviewCircuitState.on_failure: recent_outcomes bookkeeping failed", exc_info=True)

    def to_dict(self) -> dict:
        return {
            "review_failure_consecutive": self.review_failure_consecutive,
            "review_circuit_open_until_cycle": self.review_circuit_open_until_cycle,
            "backoff_level": self.backoff_level,
            "consecutive_timeouts": self.consecutive_timeouts,
            "consecutive_skips": self.consecutive_skips,
            "last_review_elapsed_ms": self.last_review_elapsed_ms,
            "last_failure_timestamp": self.last_failure_timestamp,
            "recent_outcomes": list(self.recent_outcomes)[-20:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewCircuitState":
        return cls(
            review_failure_consecutive=int(data.get("review_failure_consecutive") or 0),
            review_circuit_open_until_cycle=int(data.get("review_circuit_open_until_cycle") or 0),
            backoff_level=int(data.get("backoff_level") or 0),
            consecutive_timeouts=int(data.get("consecutive_timeouts") or 0),
            consecutive_skips=int(data.get("consecutive_skips") or 0),
            last_review_elapsed_ms=int(data.get("last_review_elapsed_ms") or 0),
            last_failure_timestamp=float(data.get("last_failure_timestamp") or 0.0),
            recent_outcomes=list(data.get("recent_outcomes") or []),
        )


@dataclass
class ReviewPipelineConfig:
    """Bundles the keyword-heavy parameters of ``conduct_review``.

    Callers construct *one* config object instead of threading 10+ kwargs
    through every wrapper layer.  ``conduct_review`` accepts **either**
    the flat kwargs (backward-compat) or a single ``pipeline_cfg`` instance.
    """

    settings: "Settings"
    circuit: ReviewCircuitState
    cycle: int
    session: object = None
    project: object = None
    send_prompt_with_retry_fn: Optional[Callable] = None
    build_review_exception_diagnostics_fn: Optional[Callable[..., dict]] = None
    on_review_done: Optional[Callable] = None
    # Pipeline params
    artifacts: object = None
    agent_type: str = "coco"
    model_name: Optional[str] = None
    # Retry control
    cancel_event: Optional[threading.Event] = None
    on_retry_status: Optional[Callable[[RetryEvent], None]] = None
    skip_retry_event: Optional[threading.Event] = None
