"""Tests for RuntimeBridge in-flight counter.

Semantics: ``in_flight_count`` is derived solely from the size of the
``_active_futures`` set — tracked across submit/done callbacks. The legacy
``_pending_submit_count`` was removed (it double-counted the same
futures); tests must not be present in bridge.py.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Generator

import pytest

from src.workflow_engine.bridge import RuntimeBridge

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "src" / "workflow_engine" / "bridge.py"

# Track all RuntimeBridge instances created during tests so we can guarantee
# cleanup via .stop() — idempotent and safe to call even if start() was never
# invoked.  This future-proofs the tests against __init__ acquiring resources
# (thread pools, sockets, etc.) in later changes.
_bridge_registry: list[RuntimeBridge] = []


@pytest.fixture(autouse=True)
def _cleanup_bridges() -> Generator[None, None, None]:
    """Stop every RuntimeBridge created during a test once it finishes."""
    before = len(_bridge_registry)
    yield
    for bridge in _bridge_registry[before:]:
        try:
            bridge.stop()
        except Exception:
            pass
    del _bridge_registry[before:]


def _make_bridge(**kwargs: object) -> RuntimeBridge:
    """Create a RuntimeBridge registered for teardown cleanup."""
    bridge = RuntimeBridge(**kwargs)  # type: ignore[arg-type]
    _bridge_registry.append(bridge)
    return bridge


# ---------------------------------------------------------------------------
# Legacy attribute contract: _pending_submit_count must not exist
# ---------------------------------------------------------------------------


def test_bridge_no_pending_submit_count_attribute() -> None:
    """bridge.py must NOT expose ``_pending_submit_count`` anywhere (double-counter was removed)."""
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    assert "_pending_submit_count" not in source, (
        "bridge.py still references the removed _pending_submit_count; "
        "please remove it and rely solely on _active_futures for in_flight_count"
    )
    # Structural: confirm no ``_pending_submit_count`` attribute is referenced.
    assert not hasattr(
        _make_bridge(script_path="/dev/null", cwd="/tmp"), "_pending_submit_count"
    ), (
        "RuntimeBridge exposes _pending_submit_count attribute; "
        "this was removed in favor of len(_active_futures) only."
    )


# ---------------------------------------------------------------------------
# in_flight_count is len(_active_futures) contract
# ---------------------------------------------------------------------------


def test_in_flight_count_initial_zero() -> None:
    bridge = _make_bridge(script_path="/dev/null", cwd="/tmp")
    assert bridge.in_flight_count == 0
    assert len(bridge._active_futures) == 0


def test_in_flight_count_reflects_active_futures_only() -> None:
    """in_flight_count == len(_active_futures)."""
    bridge = _make_bridge(script_path="/dev/null", cwd="/tmp")
    fake1 = object()
    with bridge._futures_lock:
        bridge._active_futures.add(fake1)  # type: ignore[arg-type]
    assert bridge.in_flight_count == 1

    fake2 = object()
    with bridge._futures_lock:
        bridge._active_futures.add(fake2)  # type: ignore[arg-type]
    assert bridge.in_flight_count == 2

    bridge._discard_future(fake1)  # type: ignore[arg-type]
    assert bridge.in_flight_count == 1
    bridge._discard_future(fake2)  # type: ignore[arg-type]
    assert bridge.in_flight_count == 0


def test_discard_unknown_future_is_safe() -> None:
    bridge = _make_bridge(script_path="/dev/null", cwd="/tmp")
    bridge._discard_future(object())  # type: ignore[arg-type]
    assert bridge.in_flight_count == 0


def test_n_submits_then_m_completions() -> None:
    """N submits produce in_flight == N (not 2*N — no double counting)."""
    bridge = _make_bridge(script_path="/dev/null", cwd="/tmp")
    futures: list[object] = []
    n = 10
    for _ in range(n):
        future = object()
        with bridge._futures_lock:
            bridge._active_futures.add(future)  # type: ignore[arg-type]
        futures.append(future)

    assert bridge.in_flight_count == n

    m = 4
    for future in futures[:m]:
        bridge._discard_future(future)  # type: ignore[arg-type]

    remaining = n - m
    assert bridge.in_flight_count == remaining

    for future in futures[m:]:
        bridge._discard_future(future)  # type: ignore[arg-type]

    assert bridge.in_flight_count == 0


def test_in_flight_count_thread_safety() -> None:
    """Add/discard from many threads must converge to zero."""
    bridge = _make_bridge(script_path="/dev/null", cwd="/tmp")
    threads = 20
    per_thread = 50
    barriers = threading.Barrier(threads)

    def worker() -> None:
        barriers.wait()
        for _ in range(per_thread):
            future = object()
            with bridge._futures_lock:
                bridge._active_futures.add(future)  # type: ignore[arg-type]
            bridge._discard_future(future)  # type: ignore[arg-type]

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert bridge.in_flight_count == 0


# ---------------------------------------------------------------------------
# End-to-end: real ThreadPoolExecutor submit path
# ---------------------------------------------------------------------------


def test_counter_through_real_submit_and_done_callback() -> None:
    bridge = _make_bridge(script_path="/dev/null", cwd="/tmp", max_concurrent=1)
    bridge._executor = ThreadPoolExecutor(max_workers=1)
    try:
        hold = threading.Event()

        def slow() -> int:
            hold.wait(timeout=10)
            return 42

        future = bridge._executor.submit(slow)
        with bridge._futures_lock:
            bridge._active_futures.add(future)
        future.add_done_callback(lambda f: bridge._discard_future(f))

        assert bridge.in_flight_count >= 1

        # Release the held task so it completes.
        hold.set()
        future.result(timeout=10)
        # Spin briefly to give the done-callback thread time to run.
        deadline = threading.Event()
        deadline.wait(timeout=0.5)

        assert future not in bridge._active_futures
        assert bridge.in_flight_count == 0
    finally:
        bridge._executor.shutdown(wait=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
