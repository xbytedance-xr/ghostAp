"""Tests for SlidingWindowTracker + dynamic circuit breaker integration."""

from __future__ import annotations

import pytest

from src.utils.review_helpers import SlidingWindowTracker


# ---------------------------------------------------------------------------
# SlidingWindowTracker unit tests
# ---------------------------------------------------------------------------
class TestSlidingWindowTracker:
    def test_empty_tracker_success_rate_is_one(self):
        t = SlidingWindowTracker(window_size=5)
        assert t.success_rate() == 1.0

    def test_all_success(self):
        t = SlidingWindowTracker(window_size=5)
        for _ in range(5):
            t.record("success")
        assert t.success_rate() == 1.0

    def test_all_failure(self):
        t = SlidingWindowTracker(window_size=5)
        for _ in range(5):
            t.record("timeout")
        assert t.success_rate() == 0.0

    def test_mixed(self):
        t = SlidingWindowTracker(window_size=5)
        t.record("success")
        t.record("timeout")
        t.record("success")
        t.record("error")
        t.record("success")
        assert t.success_rate() == pytest.approx(0.6)

    def test_window_slides(self):
        t = SlidingWindowTracker(window_size=3)
        t.record("success")
        t.record("success")
        t.record("success")
        assert t.success_rate() == 1.0
        # Slide: add failures, oldest success evicted
        t.record("timeout")
        t.record("timeout")
        t.record("timeout")
        assert t.success_rate() == 0.0

    def test_outcomes_list(self):
        t = SlidingWindowTracker(window_size=3)
        t.record("a")
        t.record("b")
        t.record("c")
        t.record("d")
        assert t.outcomes == ["b", "c", "d"]

    def test_window_size_property(self):
        t = SlidingWindowTracker(window_size=7)
        assert t.window_size == 7

    def test_window_size_minimum_is_one(self):
        t = SlidingWindowTracker(window_size=0)
        assert t.window_size == 1


class TestSlidingWindowShouldOpenCircuit:
    def test_not_full_window_does_not_trigger(self):
        t = SlidingWindowTracker(window_size=5)
        for _ in range(4):
            t.record("timeout")
        assert not t.should_open_circuit(threshold=0.3)

    def test_full_window_low_rate_triggers(self):
        t = SlidingWindowTracker(window_size=5)
        for _ in range(5):
            t.record("timeout")
        assert t.should_open_circuit(threshold=0.3)

    def test_full_window_high_rate_does_not_trigger(self):
        t = SlidingWindowTracker(window_size=5)
        for _ in range(4):
            t.record("success")
        t.record("timeout")
        assert not t.should_open_circuit(threshold=0.3)

    def test_exact_threshold_does_not_trigger(self):
        """success_rate == threshold → does NOT open (strictly less-than)."""
        t = SlidingWindowTracker(window_size=10)
        for _ in range(3):
            t.record("success")
        for _ in range(7):
            t.record("timeout")
        assert t.success_rate() == pytest.approx(0.3)
        assert not t.should_open_circuit(threshold=0.3)


class TestSlidingWindowFromList:
    def test_from_list_basic(self):
        t = SlidingWindowTracker.from_list(["success", "timeout", "error"], window_size=5)
        assert t.outcomes == ["success", "timeout", "error"]
        assert t.window_size == 5

    def test_from_list_trims_to_window(self):
        t = SlidingWindowTracker.from_list(
            ["a", "b", "c", "d", "e"], window_size=3
        )
        assert t.outcomes == ["c", "d", "e"]

    def test_from_empty_list(self):
        t = SlidingWindowTracker.from_list([], window_size=10)
        assert t.success_rate() == 1.0


# ---------------------------------------------------------------------------
# Serialization round-trip (via CircuitState)
# ---------------------------------------------------------------------------
class TestCircuitStateSerialization:
    def test_spec_circuit_roundtrip(self):
        from src.spec_engine.review import ReviewCircuitState

        c = ReviewCircuitState()
        c.recent_outcomes = ["success", "timeout", "error"]
        d = c.to_dict()
        c2 = ReviewCircuitState.from_dict(d)
        assert c2.recent_outcomes == ["success", "timeout", "error"]

    def test_loop_circuit_roundtrip(self):
        from src.loop_engine.engine import LoopReviewCircuitState

        c = LoopReviewCircuitState()
        c.recent_outcomes = ["timeout", "timeout", "success"]
        d = c.to_dict()
        c2 = LoopReviewCircuitState.from_dict(d)
        assert c2.recent_outcomes == ["timeout", "timeout", "success"]

    def test_spec_from_dict_missing_outcomes(self):
        """Old snapshots without recent_outcomes → empty list."""
        from src.spec_engine.review import ReviewCircuitState

        c = ReviewCircuitState.from_dict({"review_failure_consecutive": 2})
        assert c.recent_outcomes == []

    def test_loop_from_dict_missing_outcomes(self):
        from src.loop_engine.engine import LoopReviewCircuitState

        c = LoopReviewCircuitState.from_dict({})
        assert c.recent_outcomes == []

    def test_to_dict_trims_to_20(self):
        from src.spec_engine.review import ReviewCircuitState

        c = ReviewCircuitState()
        c.recent_outcomes = ["x"] * 30
        d = c.to_dict()
        assert len(d["recent_outcomes"]) == 20


# ---------------------------------------------------------------------------
# Integration: handle_review_exception records outcomes
# ---------------------------------------------------------------------------
class TestHandleReviewExceptionOutcome:
    def test_timeout_records_timeout_outcome(self):
        from dataclasses import dataclass, field
        from src.utils.review_helpers import handle_review_exception

        @dataclass
        class FakeCircuit:
            last_review_failure_diag: dict = field(default_factory=dict)
            review_failure_consecutive: int = 0
            review_circuit_open_until_cycle: int = 0
            backoff_level: int = 0
            consecutive_timeouts: int = 0
            consecutive_skips: int = 0
            last_review_elapsed_ms: int = 0
            recent_outcomes: list = field(default_factory=list)

        class FakeSettings:
            spec_review_failure_circuit_enabled = False
            spec_review_failure_max_consecutive = 3
            spec_review_failure_cooldown_cycles = 3
            spec_review_failure_max_cooldown_cycles = 12

        circuit = FakeCircuit()
        handle_review_exception(
            TimeoutError("slow"),
            circuit=circuit,
            cycle=1,
            settings=FakeSettings(),
        )
        assert circuit.recent_outcomes == ["timeout"]

    def test_regular_error_records_error_outcome(self):
        from dataclasses import dataclass, field
        from src.utils.review_helpers import handle_review_exception

        @dataclass
        class FakeCircuit:
            last_review_failure_diag: dict = field(default_factory=dict)
            review_failure_consecutive: int = 0
            review_circuit_open_until_cycle: int = 0
            backoff_level: int = 0
            consecutive_timeouts: int = 0
            consecutive_skips: int = 0
            last_review_elapsed_ms: int = 0
            recent_outcomes: list = field(default_factory=list)

        class FakeSettings:
            spec_review_failure_circuit_enabled = False
            spec_review_failure_max_consecutive = 3
            spec_review_failure_cooldown_cycles = 3
            spec_review_failure_max_cooldown_cycles = 12

        circuit = FakeCircuit()
        handle_review_exception(
            RuntimeError("oops"),
            circuit=circuit,
            cycle=1,
            settings=FakeSettings(),
        )
        assert circuit.recent_outcomes == ["error"]


# ---------------------------------------------------------------------------
# Integration: sliding window triggers circuit open
# ---------------------------------------------------------------------------
class TestSlidingWindowCircuitTrigger:
    def test_sliding_window_triggers_before_max_consecutive(self):
        """If window is full and rate is low, circuit opens before max_consecutive."""
        from dataclasses import dataclass, field
        from src.utils.review_helpers import handle_review_exception

        @dataclass
        class FakeCircuit:
            last_review_failure_diag: dict = field(default_factory=dict)
            review_failure_consecutive: int = 0
            review_circuit_open_until_cycle: int = 0
            backoff_level: int = 0
            consecutive_timeouts: int = 0
            consecutive_skips: int = 0
            last_review_elapsed_ms: int = 0
            recent_outcomes: list = field(default_factory=list)

        class FakeSettings:
            spec_review_failure_circuit_enabled = True
            spec_review_failure_max_consecutive = 100  # Very high — never triggers by consecutive
            spec_review_failure_cooldown_cycles = 3
            spec_review_failure_max_cooldown_cycles = 12
            review_circuit_window_size = 5
            review_circuit_success_rate_threshold = 0.3

        circuit = FakeCircuit()
        # Pre-fill window with failures
        circuit.recent_outcomes = ["timeout"] * 4

        result = handle_review_exception(
            TimeoutError("test"),
            circuit=circuit,
            cycle=10,
            settings=FakeSettings(),
        )
        # Window is now full (5 timeouts), rate=0.0 < 0.3 → sliding trigger
        assert result.review_decision == "review_failed_open_circuit"
        assert circuit.review_circuit_open_until_cycle > 10

    def test_consecutive_fallback_still_works(self):
        """If window is not full, consecutive still triggers."""
        from dataclasses import dataclass, field
        from src.utils.review_helpers import handle_review_exception

        @dataclass
        class FakeCircuit:
            last_review_failure_diag: dict = field(default_factory=dict)
            review_failure_consecutive: int = 2  # One more → triggers at max_consecutive=3
            review_circuit_open_until_cycle: int = 0
            backoff_level: int = 0
            consecutive_timeouts: int = 0
            consecutive_skips: int = 0
            last_review_elapsed_ms: int = 0
            recent_outcomes: list = field(default_factory=list)

        class FakeSettings:
            spec_review_failure_circuit_enabled = True
            spec_review_failure_max_consecutive = 3
            spec_review_failure_cooldown_cycles = 3
            spec_review_failure_max_cooldown_cycles = 12
            review_circuit_window_size = 100  # Window too large — never triggers
            review_circuit_success_rate_threshold = 0.3

        circuit = FakeCircuit()

        result = handle_review_exception(
            RuntimeError("oops"),
            circuit=circuit,
            cycle=5,
            settings=FakeSettings(),
        )
        assert result.review_decision == "review_failed_open_circuit"
