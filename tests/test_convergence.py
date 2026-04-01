"""Tests for spec_engine.convergence — new parametrized features."""

import pytest

from src.engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from src.spec_engine.convergence import (
    ContinuationPolicy,
    compute_cycle_metrics,
    detect_backlog_stuck,
    detect_convergence,
)
from src.spec_engine.models import (
    SpecCycle,
    SpecCycleMetrics,
    SpecProject,
    SpecWorkItem,
    SpecWorkItemStatus,
)


def _make_metrics(cycle_number: int, backlog_pending: int = 0, **kwargs) -> SpecCycleMetrics:
    defaults = dict(
        cycle_number=cycle_number,
        satisfied_count=0,
        total_criteria=2,
        new_satisfied=0,
        review_suggestions=0,
        backlog_pending=backlog_pending,
        goal_attainment=0.0,
        improvement_space=0.2,
    )
    defaults.update(kwargs)
    return SpecCycleMetrics(**defaults)


def _make_review(iteration: int, passed: bool = True):
    return ReviewResult(
        reviews=[
            PerspectiveReview(
                perspective=p,
                passed=passed,
                suggestions=[] if passed else ["fix something"],
                summary="ok" if passed else "needs work",
            )
            for p in ReviewPerspective
        ],
        iteration=iteration,
    )


class TestComputeCycleMetricsWeights:

    def _make_project_and_cycle(self, satisfied: int, total: int, review_passed: bool):
        project = SpecProject.create(root_path="/tmp")
        criteria = [f"C{i}" for i in range(total)]
        project.criteria_tracker.init_criteria(criteria)
        for i in range(satisfied):
            project.criteria_tracker.update(i, True, 1)
        cycle = SpecCycle(cycle_number=1)
        if review_passed:
            cycle.review_result = _make_review(1, passed=True)
        return project, cycle

    def test_default_weights_backward_compat(self):
        project, cycle = self._make_project_and_cycle(1, 2, review_passed=True)
        m = compute_cycle_metrics(cycle, project)
        expected = min(1.0, max(0.0, (1 / 2) * 0.8 + 1.0 * 0.2))
        assert abs(m.goal_attainment - expected) < 1e-9

    def test_custom_weights_criteria_only(self):
        project, cycle = self._make_project_and_cycle(1, 2, review_passed=True)
        m = compute_cycle_metrics(cycle, project, criteria_weight=1.0, review_weight=0.0)
        expected = 1 / 2
        assert abs(m.goal_attainment - expected) < 1e-9

    def test_custom_weights_review_only(self):
        project, cycle = self._make_project_and_cycle(1, 2, review_passed=True)
        m = compute_cycle_metrics(cycle, project, criteria_weight=0.0, review_weight=1.0)
        assert abs(m.goal_attainment - 1.0) < 1e-9

    def test_custom_weights_equal(self):
        project, cycle = self._make_project_and_cycle(2, 4, review_passed=False)
        m = compute_cycle_metrics(cycle, project, criteria_weight=0.5, review_weight=0.5)
        expected = min(1.0, max(0.0, (2 / 4) * 0.5 + 0.0 * 0.5))
        assert abs(m.goal_attainment - expected) < 1e-9

    def test_no_project_returns_zero(self):
        cycle = SpecCycle(cycle_number=1)
        m = compute_cycle_metrics(cycle, None, criteria_weight=0.5, review_weight=0.5)
        assert m.goal_attainment == 0.0


class TestDetectConvergenceTolerance:

    def _make_project_with_criteria(self, n_criteria: int) -> SpecProject:
        project = SpecProject.create(root_path="/tmp")
        project.criteria_tracker.init_criteria([f"C{i}" for i in range(n_criteria)])
        return project

    def test_tolerance_zero_exact_match_required(self):
        project = self._make_project_with_criteria(3)
        project.criteria_tracker.update(0, True, 1)

        review = _make_review(1, passed=False)
        project.cycles = [
            SpecCycle(cycle_number=1, review_result=review),
            SpecCycle(cycle_number=2, review_result=review),
        ]

        assert detect_convergence(
            project, convergence_window=2, review_enabled=True, tolerance=0
        )

    def test_tolerance_zero_rejects_difference(self):
        project = self._make_project_with_criteria(3)
        project.criteria_tracker.update(0, True, 1)
        project.criteria_tracker.update(1, True, 2)

        review = _make_review(1, passed=False)
        project.cycles = [
            SpecCycle(cycle_number=1, review_result=review),
            SpecCycle(cycle_number=2, review_result=review),
        ]

        assert not detect_convergence(
            project, convergence_window=2, review_enabled=True, tolerance=0
        )

    def test_tolerance_allows_small_difference(self):
        project = self._make_project_with_criteria(5)
        project.criteria_tracker.update(0, True, 1)
        project.criteria_tracker.update(1, True, 2)

        review = _make_review(1, passed=False)
        project.cycles = [
            SpecCycle(cycle_number=1, review_result=review),
            SpecCycle(cycle_number=2, review_result=review),
        ]

        assert detect_convergence(
            project, convergence_window=2, review_enabled=True, tolerance=1
        )

    def test_tolerance_rejects_large_difference(self):
        project = self._make_project_with_criteria(5)
        project.criteria_tracker.update(0, True, 1)
        project.criteria_tracker.update(1, True, 2)
        project.criteria_tracker.update(2, True, 2)

        review = _make_review(1, passed=False)
        project.cycles = [
            SpecCycle(cycle_number=1, review_result=review),
            SpecCycle(cycle_number=2, review_result=review),
        ]

        assert not detect_convergence(
            project, convergence_window=2, review_enabled=True, tolerance=1
        )

    def test_default_tolerance_is_zero(self):
        project = self._make_project_with_criteria(3)

        review = _make_review(1, passed=False)
        project.cycles = [
            SpecCycle(cycle_number=1, review_result=review),
            SpecCycle(cycle_number=2, review_result=review),
        ]

        result_default = detect_convergence(
            project, convergence_window=2, review_enabled=True
        )
        result_explicit = detect_convergence(
            project, convergence_window=2, review_enabled=True, tolerance=0
        )
        assert result_default == result_explicit


class TestDetectBacklogStuck:

    def test_empty_project_returns_false(self):
        assert not detect_backlog_stuck(None)

    def test_not_enough_history_returns_false(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [_make_metrics(1, backlog_pending=5)]
        assert not detect_backlog_stuck(project, window=3)

    def test_stuck_when_backlog_not_decreasing(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [
            _make_metrics(1, backlog_pending=5),
            _make_metrics(2, backlog_pending=5),
            _make_metrics(3, backlog_pending=5),
        ]
        assert detect_backlog_stuck(project, window=3)

    def test_stuck_when_backlog_increasing(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [
            _make_metrics(1, backlog_pending=3),
            _make_metrics(2, backlog_pending=4),
            _make_metrics(3, backlog_pending=5),
        ]
        assert detect_backlog_stuck(project, window=3)

    def test_not_stuck_when_backlog_decreasing(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [
            _make_metrics(1, backlog_pending=5),
            _make_metrics(2, backlog_pending=4),
            _make_metrics(3, backlog_pending=3),
        ]
        assert not detect_backlog_stuck(project, window=3)

    def test_not_stuck_when_middle_dips(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [
            _make_metrics(1, backlog_pending=5),
            _make_metrics(2, backlog_pending=3),
            _make_metrics(3, backlog_pending=5),
        ]
        assert not detect_backlog_stuck(project, window=3)

    def test_custom_window(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [
            _make_metrics(1, backlog_pending=5),
            _make_metrics(2, backlog_pending=5),
        ]
        assert detect_backlog_stuck(project, window=2)
        assert not detect_backlog_stuck(project, window=3)

    def test_window_less_than_one(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [_make_metrics(1, backlog_pending=5)]
        assert not detect_backlog_stuck(project, window=0)
        assert not detect_backlog_stuck(project, window=-1)

    def test_zero_backlog_stuck(self):
        project = SpecProject.create(root_path="/tmp")
        project.metrics_history = [
            _make_metrics(1, backlog_pending=0),
            _make_metrics(2, backlog_pending=0),
            _make_metrics(3, backlog_pending=0),
        ]
        assert not detect_backlog_stuck(project, window=3)


class TestContinuationPolicyBacklogStuck:

    def _make_metrics(self, **kwargs) -> SpecCycleMetrics:
        return _make_metrics(cycle_number=kwargs.pop("cycle_number", 3), **kwargs)

    def test_backlog_stuck_stops_at_cycle_3(self):
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(backlog_pending=5)
        result = policy.should_stop(
            cycle_num=3,
            all_satisfied=False,
            review_passed=False,
            converged=False,
            metrics=metrics,
            backlog_stuck=True,
        )
        assert result == "backlog_stuck"

    def test_backlog_stuck_not_triggered_before_cycle_3(self):
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(cycle_number=2)
        result = policy.should_stop(
            cycle_num=2,
            all_satisfied=False,
            review_passed=False,
            converged=False,
            metrics=metrics,
            backlog_stuck=True,
        )
        assert result is None

    def test_backlog_stuck_default_false(self):
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(backlog_pending=5)
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=False,
            review_passed=False,
            converged=False,
            metrics=metrics,
        )
        assert result != "backlog_stuck"

    def test_converged_takes_priority_over_backlog_stuck(self):
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics()
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=False,
            review_passed=False,
            converged=True,
            metrics=metrics,
            backlog_stuck=True,
        )
        assert result == "converged"

    def test_success_takes_priority_over_backlog_stuck(self):
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(backlog_pending=0)
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=True,
            review_passed=True,
            converged=False,
            metrics=metrics,
            backlog_stuck=True,
        )
        assert result == "success"

    def test_infinite_mode_ignores_backlog_stuck(self):
        policy = ContinuationPolicy(max_cycles=10, infinite_mode=True)
        metrics = self._make_metrics(backlog_pending=5)
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=False,
            review_passed=False,
            converged=False,
            metrics=metrics,
            backlog_stuck=True,
        )
        assert result is None

    def test_backlog_stuck_at_high_cycle(self):
        policy = ContinuationPolicy(max_cycles=100)
        metrics = self._make_metrics(cycle_number=50)
        result = policy.should_stop(
            cycle_num=50,
            all_satisfied=False,
            review_passed=False,
            converged=False,
            metrics=metrics,
            backlog_stuck=True,
        )
        assert result == "backlog_stuck"
