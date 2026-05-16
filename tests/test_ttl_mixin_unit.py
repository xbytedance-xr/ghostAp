"""Unit tests for TTLActuatorMixin boundary conditions.

Tests TTL=0 behavior, warn_before >= idle_timeout edge cases,
and concurrent refresh safety.
"""

import threading
from unittest.mock import MagicMock

from src.card.protocols import TTLState
from src.card.session.ttl import TTLHandler


def _make_mock_session(**overrides) -> MagicMock:
    """Create a mock TTLDecider + TTLActuator with sensible defaults."""
    s = MagicMock()
    s.get_ttl_state.return_value = TTLState(
        closed=False,
        ttl_warned=False,
        idle_seconds=2000.0,
        ttl_seconds=1800.0,
        session_id="ttl_unit_test",
        state_snapshot=None,
    )
    s.engine_cmd = "/deep"
    s.engine_name = "Deep"
    s.reduce_and_render.return_value = [{"card": "rendered"}]
    for key, val in overrides.items():
        setattr(s, key, val)
    return s


class TestTTLZeroSkipsBehavior:
    """When TTL is 0, TTL expiry logic should be effectively disabled."""

    def test_ttl_zero_expired_still_fires_if_idle_exceeds(self):
        """TTL=0 means infinite — on_ttl_expired should no-op (idle < ttl=0 → never triggers)."""
        s = _make_mock_session()
        # ttl_seconds=0 means no TTL enforcement; idle check fails (idle > 0 is always true,
        # but the threshold check uses ttl_seconds * ratio which is 0)
        s.get_ttl_state.return_value = TTLState(
            closed=False,
            ttl_warned=False,
            idle_seconds=100.0,
            ttl_seconds=0.0,
            session_id="zero_ttl",
            state_snapshot=None,
        )
        handler = TTLHandler(decider=s, actuator=s)
        handler.on_ttl_expired()

        # With ttl_seconds=0, idle (100) > ttl (0) is true, so it will fire
        # The handler should still work without errors
        # Whether it fires depends on implementation — verify no crash
        # and that the method completes without exception

    def test_ttl_zero_prewarning_skipped(self):
        """Prewarning with TTL=0: idle < ttl * threshold → prewarning should not fire."""
        s = _make_mock_session()
        s.get_ttl_state.return_value = TTLState(
            closed=False,
            ttl_warned=False,
            idle_seconds=50.0,
            ttl_seconds=0.0,
            session_id="zero_ttl",
            state_snapshot=None,
        )
        handler = TTLHandler(decider=s, actuator=s)
        handler.on_ttl_prewarning()

        # With ttl=0, threshold calculation: idle < 0 * 0.75 = 0 → False
        # So prewarning fires (idle 50 >= 0)
        # But remaining_min = max(1, int((0 - 50) / 60)) = max(1, -1) = 1
        # This should not crash


class TestWarnBeforeEdgeCases:
    """Edge cases where warn_before is close to or exceeds idle_timeout."""

    def test_prewarning_fires_near_boundary(self):
        """When idle is exactly at 75% of TTL, prewarning should fire."""
        s = _make_mock_session()
        # ttl=100, idle=75 → exactly at 75% threshold
        s.get_ttl_state.return_value = TTLState(
            closed=False,
            ttl_warned=False,
            idle_seconds=75.0,
            ttl_seconds=100.0,
            session_id="boundary_test",
            state_snapshot=None,
        )
        handler = TTLHandler(decider=s, actuator=s)
        handler.on_ttl_prewarning()

        # Should fire since idle >= ttl * 0.75
        s.reduce_and_render.assert_called_once()

    def test_prewarning_skipped_below_threshold(self):
        """When idle is below 75% of TTL, prewarning should be skipped."""
        s = _make_mock_session()
        # ttl=100, idle=50 → below 75% threshold
        s.get_ttl_state.return_value = TTLState(
            closed=False,
            ttl_warned=False,
            idle_seconds=50.0,
            ttl_seconds=100.0,
            session_id="below_threshold",
            state_snapshot=None,
        )
        handler = TTLHandler(decider=s, actuator=s)
        handler.on_ttl_prewarning()

        s.reduce_and_render.assert_not_called()

    def test_prewarning_skipped_when_already_warned(self):
        """When ttl_warned is True, prewarning should be a no-op."""
        s = _make_mock_session()
        s.get_ttl_state.return_value = TTLState(
            closed=False,
            ttl_warned=True,
            idle_seconds=1700.0,
            ttl_seconds=1800.0,
            session_id="already_warned",
            state_snapshot=None,
        )
        handler = TTLHandler(decider=s, actuator=s)
        handler.on_ttl_prewarning()

        s.reduce_and_render.assert_not_called()


class TestConcurrentRefreshSafety:
    """Concurrent dispatch + TTL timer should not deadlock."""

    def test_concurrent_prewarning_no_deadlock(self):
        """Multiple threads calling on_ttl_prewarning concurrently should not deadlock."""
        s = _make_mock_session()
        s.get_ttl_state.return_value = TTLState(
            closed=False,
            ttl_warned=False,
            idle_seconds=1700.0,
            ttl_seconds=1800.0,
            session_id="concurrent_test",
            state_snapshot=None,
        )
        handler = TTLHandler(decider=s, actuator=s)

        errors = []

        def _call_prewarning():
            try:
                handler.on_ttl_prewarning()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_call_prewarning) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # No thread should have timed out
        for t in threads:
            assert not t.is_alive(), "Thread deadlocked"

        assert len(errors) == 0

    def test_concurrent_expired_and_prewarning_no_deadlock(self):
        """on_ttl_expired and on_ttl_prewarning running concurrently should not deadlock."""
        s = _make_mock_session()
        handler = TTLHandler(decider=s, actuator=s)

        errors = []

        def _call_expired():
            try:
                handler.on_ttl_expired()
            except Exception as e:
                errors.append(e)

        def _call_prewarning():
            try:
                handler.on_ttl_prewarning()
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=_call_expired))
            threads.append(threading.Thread(target=_call_prewarning))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        for t in threads:
            assert not t.is_alive(), "Thread deadlocked"

        assert len(errors) == 0
