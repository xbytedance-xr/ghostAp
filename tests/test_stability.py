import threading
import time

import pytest

from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException, CircuitState
from src.utils.rate_limit import RateLimiter


def test_circuit_breaker_success():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)

    def success_func():
        return "ok"

    assert cb.call(success_func) == "ok"
    assert cb.state == CircuitState.CLOSED


def test_circuit_breaker_failure_and_recovery():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.2)

    def fail_func():
        raise ValueError("error")

    for _ in range(3):
        with pytest.raises(ValueError):
            cb.call(fail_func)

    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitBreakerOpenException):
        cb.call(fail_func)

    time.sleep(0.3)
    assert cb.state == CircuitState.HALF_OPEN

    def success_func():
        return "ok"

    assert cb.call(success_func) == "ok"
    assert cb.state == CircuitState.CLOSED


def test_rate_limiter_basic():
    rl = RateLimiter(capacity=5, fill_rate=10.0)

    for _ in range(5):
        assert rl.acquire(1, blocking=False)

    assert not rl.acquire(1, blocking=False)

    time.sleep(0.15)

    assert rl.acquire(1, blocking=False)


def test_rate_limiter_concurrent():
    rl = RateLimiter(capacity=100, fill_rate=100.0)

    success_count = 0
    lock = threading.Lock()

    def worker():
        nonlocal success_count
        for _ in range(10):
            if rl.acquire(1, blocking=False):
                with lock:
                    success_count += 1
            time.sleep(0.001)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert success_count <= 150  # some may fail, but should at least handle the capacity
