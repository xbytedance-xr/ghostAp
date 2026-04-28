import collections
import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional, Tuple, Type

logger = logging.getLogger(__name__)

__all__ = ["CircuitState", "CircuitBreakerOpenException", "CircuitBreaker"]


class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenException(Exception):
    """Raised when the circuit breaker is open."""

    is_ghostap_error = True

    pass


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: Tuple[Type[Exception], ...] = (Exception,),
        on_state_change: Optional[Callable[[CircuitState, CircuitState], None]] = None,
        window_duration: float = 120.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions
        self.on_state_change = on_state_change
        self.window_duration = window_duration

        self._state = CircuitState.CLOSED
        self._failure_timestamps: collections.deque[float] = collections.deque()
        self._last_failure_time = 0.0
        self._lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock

    def _purge_old_failures(self) -> None:
        cutoff = time.time() - self.window_duration
        while self._failure_timestamps and self._failure_timestamps[0] < cutoff:
            self._failure_timestamps.popleft()

    @property
    def _failures(self) -> int:
        self._purge_old_failures()
        return len(self._failure_timestamps)

    def _set_state(self, new_state: CircuitState) -> None:
        old_state = self._state
        if old_state == new_state:
            return
        self._state = new_state
        if self.on_state_change is not None:
            try:
                self.on_state_change(old_state, new_state)
            except Exception:
                logger.debug("on_state_change callback failed", exc_info=True)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._set_state(CircuitState.HALF_OPEN)
            return self._state

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            current_state = self.state
            if current_state == CircuitState.OPEN:
                raise CircuitBreakerOpenException("Circuit breaker is OPEN")

        try:
            result = func(*args, **kwargs)
        except self.expected_exceptions as e:
            self._record_failure()
            raise e

        self._record_success()
        return result

    async def async_call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            current_state = self.state
            if current_state == CircuitState.OPEN:
                raise CircuitBreakerOpenException("Circuit breaker is OPEN")

        try:
            result = await func(*args, **kwargs)
        except self.expected_exceptions as e:
            self._record_failure()
            raise e

        self._record_success()
        return result

    def reset(self) -> None:
        with self._lock:
            self._failure_timestamps.clear()
            self._last_failure_time = 0.0
            self._set_state(CircuitState.CLOSED)

    def on_failure(self, is_timeout: bool = False) -> None:
        """Public API to record an external failure.

        Mirrors :meth:`reset` (success) for symmetry.  Callers that manage
        their own try/except can feed failure signals without going through
        :meth:`call`/:meth:`async_call`.

        Args:
            is_timeout: hint flag (currently unused, reserved for future
                timeout-specific policies).
        """
        self._record_failure()

    def _record_failure(self) -> None:
        with self._lock:
            now = time.time()
            self._failure_timestamps.append(now)
            self._last_failure_time = now
            self._purge_old_failures()
            failure_count = len(self._failure_timestamps)
            if self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if failure_count >= self.failure_threshold or self._state == CircuitState.HALF_OPEN:
                    self._set_state(CircuitState.OPEN)

    def _record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._set_state(CircuitState.CLOSED)
                self._failure_timestamps.clear()
            elif self._state == CircuitState.CLOSED:
                self._failure_timestamps.clear()
