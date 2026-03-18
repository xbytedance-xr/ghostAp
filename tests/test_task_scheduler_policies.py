import time

import pytest

from src.tasking.scheduler import TaskScheduler, TaskSpec, TaskStatus
from src.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException
from src.utils.rate_limit import RateLimiter, RateLimitExceededException


def test_task_scheduler_rate_limit():
    scheduler = TaskScheduler(max_concurrent=5)
    rl = RateLimiter(capacity=2, fill_rate=1.0)
    scheduler.register_policy("limited_task", rate_limiter=rl)

    def dummy_task(ctx):
        return "ok"

    spec = TaskSpec(chat_id="chat1", name="t", task_type="limited_task")

    # First two should succeed
    scheduler.submit(spec, dummy_task)
    scheduler.submit(spec, dummy_task)

    # Third should fail immediately
    with pytest.raises(RateLimitExceededException):
        scheduler.submit(spec, dummy_task)

    scheduler.stop()


def test_task_scheduler_circuit_breaker():
    scheduler = TaskScheduler(max_concurrent=5)
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.2, expected_exceptions=(ValueError,))
    scheduler.register_policy("breakable_task", circuit_breaker=cb)

    def failing_task(ctx):
        raise ValueError("failing")

    spec = TaskSpec(chat_id="chat1", name="t", task_type="breakable_task")

    h1 = scheduler.submit(spec, failing_task)
    h2 = scheduler.submit(spec, failing_task)

    h1.wait()
    h2.wait()

    # Now circuit breaker should be OPEN
    with pytest.raises(CircuitBreakerOpenException):
        scheduler.submit(spec, failing_task)

    time.sleep(0.3)  # Wait for HALF_OPEN

    # Should accept again
    def success_task(ctx):
        return "ok"

    h3 = scheduler.submit(spec, success_task)
    res = h3.wait()
    assert res is not None  # Actually wait returns TaskResult, we can just check state
    assert h3.get_state().status == TaskStatus.SUCCEEDED

    scheduler.stop()
