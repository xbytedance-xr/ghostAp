"""Smoke test: delivery pool async submission works end-to-end."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_pool_state():
    """Reset pool module state before/after each test."""
    import src.card.delivery.pool as pool_mod

    orig_pool = pool_mod._pool
    orig_shutting = pool_mod._shutting_down

    pool_mod._pool = None
    pool_mod._shutting_down = False
    yield
    if pool_mod._pool is not None:
        pool_mod._pool.shutdown(wait=False)
    pool_mod._pool = orig_pool
    pool_mod._shutting_down = orig_shutting


def test_pool_submit_executes_callable():
    """Verify pool.submit() can execute a callable and return a result."""
    from src.card.delivery.pool import get_delivery_pool

    pool = get_delivery_pool()
    future = pool.submit(lambda: 42)
    assert future.result(timeout=5) == 42


def test_pool_submit_propagates_exception():
    """Verify pool.submit() propagates exceptions from submitted work."""
    from src.card.delivery.pool import get_delivery_pool

    pool = get_delivery_pool()

    def _raise():
        raise ValueError("test error")

    future = pool.submit(_raise)
    with pytest.raises(ValueError, match="test error"):
        future.result(timeout=5)
