import time
from unittest.mock import MagicMock

import pytest

from src.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenException,
    CircuitState,
)


def _advance_time(cb: CircuitBreaker, seconds: float) -> None:
    """Simulate time passing by shifting internal timestamps backward."""
    cb._last_failure_time -= seconds
    shifted = [t - seconds for t in cb._failure_timestamps]
    cb._failure_timestamps.clear()
    cb._failure_timestamps.extend(shifted)


class TestCircuitBreakerBasic:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_successful_call(self):
        cb = CircuitBreaker()
        result = cb.call(lambda: 42)
        assert result == 42

    def test_failure_opens_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_raises(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(CircuitBreakerOpenException):
            cb.call(lambda: 1)

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.OPEN
        _advance_time(cb, 0.02)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.02)
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.02)
        assert cb.state == CircuitState.HALF_OPEN
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("again")))
        assert cb.state == CircuitState.OPEN


class TestAsyncCall:
    @pytest.mark.asyncio
    async def test_async_call_success(self):
        cb = CircuitBreaker()

        async def ok():
            return "async_result"

        result = await cb.async_call(ok)
        assert result == "async_result"

    @pytest.mark.asyncio
    async def test_async_call_with_args(self):
        cb = CircuitBreaker()

        async def add(a, b):
            return a + b

        result = await cb.async_call(add, 3, 7)
        assert result == 10

    @pytest.mark.asyncio
    async def test_async_call_with_kwargs(self):
        cb = CircuitBreaker()

        async def greet(name="world"):
            return f"hello {name}"

        result = await cb.async_call(greet, name="test")
        assert result == "hello test"

    @pytest.mark.asyncio
    async def test_async_call_records_failure(self):
        cb = CircuitBreaker(failure_threshold=2)

        async def fail():
            raise ValueError("async_error")

        with pytest.raises(ValueError, match="async_error"):
            await cb.async_call(fail)
        assert cb._failures == 1

    @pytest.mark.asyncio
    async def test_async_call_opens_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        async def fail():
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.async_call(fail)
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_async_call_raises_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        async def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.async_call(fail)

        async def ok():
            return 1

        with pytest.raises(CircuitBreakerOpenException):
            await cb.async_call(ok)

    @pytest.mark.asyncio
    async def test_async_call_half_open_recovery(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        async def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.async_call(fail)

        _advance_time(cb, 0.02)

        async def ok():
            return "recovered"

        result = await cb.async_call(ok)
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_async_call_expected_exceptions_filter(self):
        cb = CircuitBreaker(failure_threshold=1, expected_exceptions=(ValueError,))

        async def fail():
            raise TypeError("not expected")

        with pytest.raises(TypeError):
            await cb.async_call(fail)
        assert cb.state == CircuitState.CLOSED


class TestReset:
    def test_reset_from_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failures == 0

    def test_reset_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_reset_from_closed_is_noop(self):
        cb = CircuitBreaker()
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failures == 0

    def test_reset_clears_failure_timestamps(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb._failures == 3
        cb.reset()
        assert cb._failures == 0

    def test_reset_allows_calls_again(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(CircuitBreakerOpenException):
            cb.call(lambda: 1)
        cb.reset()
        result = cb.call(lambda: "works")
        assert result == "works"


class TestOnStateChange:
    def test_callback_on_open(self):
        transitions = []
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=60.0,
            on_state_change=lambda old, new: transitions.append((old, new)),
        )
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert transitions == [(CircuitState.CLOSED, CircuitState.OPEN)]

    def test_callback_on_half_open(self):
        transitions = []
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.01,
            on_state_change=lambda old, new: transitions.append((old, new)),
        )
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.02)
        _ = cb.state
        assert (CircuitState.OPEN, CircuitState.HALF_OPEN) in transitions

    def test_callback_on_close_from_half_open(self):
        transitions = []
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.01,
            on_state_change=lambda old, new: transitions.append((old, new)),
        )
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.02)
        cb.call(lambda: "ok")
        assert (CircuitState.HALF_OPEN, CircuitState.CLOSED) in transitions

    def test_callback_on_reset(self):
        transitions = []
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=60.0,
            on_state_change=lambda old, new: transitions.append((old, new)),
        )
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        transitions.clear()
        cb.reset()
        assert transitions == [(CircuitState.OPEN, CircuitState.CLOSED)]

    def test_callback_not_called_on_same_state(self):
        transitions = []
        cb = CircuitBreaker(
            failure_threshold=5,
            on_state_change=lambda old, new: transitions.append((old, new)),
        )
        cb.call(lambda: "ok")
        assert transitions == []

    def test_callback_exception_is_swallowed(self):
        def bad_callback(old, new):
            raise RuntimeError("callback error")

        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=60.0,
            on_state_change=bad_callback,
        )
        with pytest.raises(RuntimeError, match="boom"):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.OPEN

    def test_full_lifecycle_transitions(self):
        transitions = []
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.01,
            on_state_change=lambda old, new: transitions.append((old, new)),
        )
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.02)
        cb.call(lambda: "ok")
        assert transitions == [
            (CircuitState.CLOSED, CircuitState.OPEN),
            (CircuitState.OPEN, CircuitState.HALF_OPEN),
            (CircuitState.HALF_OPEN, CircuitState.CLOSED),
        ]

    def test_no_callback_when_none(self):
        cb = CircuitBreaker(failure_threshold=1, on_state_change=None)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.OPEN


class TestSlidingWindow:
    def test_default_window_duration(self):
        cb = CircuitBreaker()
        assert cb.window_duration == 120.0

    def test_custom_window_duration(self):
        cb = CircuitBreaker(window_duration=60.0)
        assert cb.window_duration == 60.0

    def test_old_failures_expire(self):
        cb = CircuitBreaker(failure_threshold=3, window_duration=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb._failures == 2
        _advance_time(cb, 0.06)
        assert cb._failures == 0

    def test_expired_failures_dont_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=3, window_duration=0.05, recovery_timeout=60.0)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        _advance_time(cb, 0.06)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.CLOSED
        assert cb._failures == 1

    def test_window_only_counts_recent_failures(self):
        cb = CircuitBreaker(failure_threshold=3, window_duration=0.05, recovery_timeout=60.0)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # Shift first failure so it's "old" by ~0.03s
        _advance_time(cb, 0.03)
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # Advance another 0.03s — first failure now > window_duration old
        _advance_time(cb, 0.03)
        assert cb._failures == 1

    def test_rapid_failures_within_window_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=3, window_duration=1.0, recovery_timeout=60.0)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cb.state == CircuitState.OPEN

    def test_sliding_window_with_reset(self):
        cb = CircuitBreaker(failure_threshold=3, window_duration=0.05)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        cb.reset()
        assert cb._failures == 0
        # Even after "time passes", no phantom failures
        _advance_time(cb, 0.06)
        assert cb._failures == 0
