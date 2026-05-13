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
    CriteriaTracker,
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


class TestDetectConvergenceReviewFailed:
    """review 异常轮次不应参与收敛判定（避免 timeout 等 fallback suggestions 导致误判）。"""

    def _make_project_with_criteria(self, n_criteria: int) -> SpecProject:
        project = SpecProject.create(root_path="/tmp")
        project.criteria_tracker.init_criteria([f"C{i}" for i in range(n_criteria)])
        return project

    def _make_failed_review(self, iteration: int) -> ReviewResult:
        """模拟 review 异常时产生的 fallback ReviewResult。"""
        return ReviewResult(
            reviews=[
                PerspectiveReview(
                    perspective=p,
                    passed=False,
                    suggestions=["审查执行异常，将在下一轮重试"],
                    summary="异常",
                )
                for p in ReviewPerspective
            ],
            iteration=iteration,
        )

    def test_consecutive_review_failures_not_converged(self):
        """连续2轮 review timeout，不应被判定为收敛。"""
        project = self._make_project_with_criteria(3)
        project.criteria_tracker.update(0, True, 1)

        project.cycles = [
            SpecCycle(
                cycle_number=1,
                review_result=self._make_failed_review(1),
                review_decision="review_failed_continue",
            ),
            SpecCycle(
                cycle_number=2,
                review_result=self._make_failed_review(2),
                review_decision="review_failed_continue",
            ),
        ]

        assert not detect_convergence(
            project, convergence_window=2, review_enabled=True
        )

    def test_one_failed_one_normal_not_converged(self):
        """一轮异常 + 一轮正常，不应被判定为收敛。"""
        project = self._make_project_with_criteria(3)
        project.criteria_tracker.update(0, True, 1)

        project.cycles = [
            SpecCycle(
                cycle_number=1,
                review_result=self._make_failed_review(1),
                review_decision="review_failed_continue",
            ),
            SpecCycle(
                cycle_number=2,
                review_result=_make_review(2, passed=False),
                review_decision="",
            ),
        ]

        assert not detect_convergence(
            project, convergence_window=2, review_enabled=True
        )

    def test_circuit_breaker_decision_not_converged(self):
        """熔断决策也以 review_failed 开头，同样不应参与收敛。"""
        project = self._make_project_with_criteria(3)
        project.criteria_tracker.update(0, True, 1)

        project.cycles = [
            SpecCycle(
                cycle_number=1,
                review_result=self._make_failed_review(1),
                review_decision="review_failed_open_circuit",
            ),
            SpecCycle(
                cycle_number=2,
                review_result=self._make_failed_review(2),
                review_decision="review_failed_open_circuit",
            ),
        ]

        assert not detect_convergence(
            project, convergence_window=2, review_enabled=True
        )

    def test_normal_reviews_still_converge(self):
        """正常 review（无异常）相同建议仍然正确判定为收敛。"""
        project = self._make_project_with_criteria(3)
        project.criteria_tracker.update(0, True, 1)

        review = _make_review(1, passed=False)
        project.cycles = [
            SpecCycle(cycle_number=1, review_result=review, review_decision=""),
            SpecCycle(cycle_number=2, review_result=review, review_decision=""),
        ]

        assert detect_convergence(
            project, convergence_window=2, review_enabled=True
        )


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


# ── Bug 2 fix: ignore_backlog 参数 ──────────────────────────────────


class TestShouldStopIgnoreBacklog:
    """should_stop() 的 ignore_backlog 参数控制 backlog 是否阻塞 success。"""

    def _make_metrics(self, backlog_pending=0, **kw):
        return _make_metrics(1, backlog_pending=backlog_pending, **kw)

    def test_ignore_backlog_true_success_despite_backlog(self):
        """ignore_backlog=True（默认）时，backlog > 0 也返回 success。"""
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(backlog_pending=5)
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=True,
            review_passed=True,
            converged=False,
            metrics=metrics,
            ignore_backlog=True,
        )
        assert result == "success"

    def test_ignore_backlog_false_blocked_by_backlog(self):
        """ignore_backlog=False 时，backlog > 0 阻塞 success（向后兼容）。"""
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(backlog_pending=5)
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=True,
            review_passed=True,
            converged=False,
            metrics=metrics,
            ignore_backlog=False,
        )
        assert result is None

    def test_ignore_backlog_default_is_true(self):
        """默认参数下 backlog 不阻塞 success。"""
        policy = ContinuationPolicy(max_cycles=10)
        metrics = self._make_metrics(backlog_pending=3)
        result = policy.should_stop(
            cycle_num=5,
            all_satisfied=True,
            review_passed=True,
            converged=False,
            metrics=metrics,
        )
        assert result == "success"


# ── Bug 4 fix: improvement_space 不再被 backlog 撑高 ────────────────


class TestImprovementSpacePriority:
    """compute_cycle_metrics() 中 improvement_space 的优先级和数值。"""

    def _make_project(self, backlog_count=0):
        project = SpecProject.create(name="t", root_path="/tmp")
        project.requirement = "r"
        project.acceptance_criteria = ["C1", "C2"]
        project.criteria_tracker = CriteriaTracker(criteria=["C1", "C2"])
        for i in range(backlog_count):
            project.work_items.append(
                SpecWorkItem(
                    item_id=f"Q-{i}",
                    question=f"q{i}",
                    created_in_cycle=1,
                    spec_path=f"/tmp/s{i}.json",
                    status=SpecWorkItemStatus.PENDING,
                )
            )
        return project

    def test_backlog_only_gives_low_improvement_space(self):
        """backlog > 0 但无新满足、无审查建议时，improvement_space < 0.2。"""
        project = self._make_project(backlog_count=5)
        project.metrics_history.append(_make_metrics(0, satisfied_count=0))
        cycle = SpecCycle(cycle_number=1)
        m = compute_cycle_metrics(cycle, project)
        assert m.improvement_space <= 0.2
        assert m.improvement_space == pytest.approx(0.15)

    def test_review_suggestions_higher_than_backlog(self):
        """有 review 建议时 improvement_space = 0.5，高于纯 backlog。"""
        project = self._make_project(backlog_count=5)
        project.metrics_history.append(_make_metrics(0, satisfied_count=0))
        cycle = SpecCycle(cycle_number=1)
        cycle.review_result = _make_review(1, passed=False)
        m = compute_cycle_metrics(cycle, project)
        assert m.improvement_space == pytest.approx(0.5)

    def test_no_signals_gives_lowest_improvement_space(self):
        """无 backlog、无建议、无新满足时，improvement_space 最低。"""
        project = self._make_project(backlog_count=0)
        project.metrics_history.append(_make_metrics(0, satisfied_count=0))
        cycle = SpecCycle(cycle_number=1)
        m = compute_cycle_metrics(cycle, project)
        assert m.improvement_space == pytest.approx(0.1)


# ── Bug 1 fix: Discovery 门控 ───────────────────────────────────────


class TestDiscoveryGating:
    """discover_optimization_questions() 的门控逻辑。"""

    def _make_settings(self):
        class _S:
            spec_discovery_max_questions = 5
            spec_discovery_force_nonempty = True
            spec_discovery_gate_on_satisfied = True
            spec_discovery_max_pending = 3
            spec_discovery_cooldown_cycles = 3
        return _S()

    def _make_project(self):
        project = SpecProject.create(name="t", root_path="/tmp")
        project.requirement = "r"
        project.acceptance_criteria = ["C1"]
        project.criteria_tracker = CriteriaTracker(criteria=["C1"])
        return project

    def test_gate_on_all_satisfied(self):
        """所有标准满足后 discovery 返回空列表（early cycles bypass 后生效）。"""
        from src.spec_engine.discovery import discover_optimization_questions

        project = self._make_project()
        result = discover_optimization_questions(
            project=project, session=object(), send_prompt_fn=lambda *a, **k: None,
            last_review=None, cycle_num=5, settings=self._make_settings(),
            all_satisfied=True, backlog_pending=0,
        )
        assert result == []

    def test_gate_on_backlog_full(self):
        """backlog 达到上限时 discovery 返回空列表。"""
        from src.spec_engine.discovery import discover_optimization_questions

        project = self._make_project()
        result = discover_optimization_questions(
            project=project, session=object(), send_prompt_fn=lambda *a, **k: None,
            last_review=None, cycle_num=1, settings=self._make_settings(),
            all_satisfied=False, backlog_pending=5,
        )
        assert result == []

    def test_gate_cooldown_skips(self):
        """连续无进展时非冷却轮跳过 discovery。"""
        from src.spec_engine.discovery import discover_optimization_questions

        project = self._make_project()
        # 2 轮无进展，cooldown=3 → 2 % 3 != 0 → 跳过
        project.metrics_history.append(_make_metrics(1, new_satisfied=0))
        project.metrics_history.append(_make_metrics(2, new_satisfied=0))
        result = discover_optimization_questions(
            project=project, session=object(), send_prompt_fn=lambda *a, **k: None,
            last_review=None, cycle_num=3, settings=self._make_settings(),
            all_satisfied=False, backlog_pending=0,
        )
        assert result == []

    def test_gate_cooldown_triggers(self):
        """连续无进展达到冷却周期时允许 discovery（不被门控拦截）。"""
        from src.spec_engine.discovery import discover_optimization_questions

        project = self._make_project()
        # 3 轮无进展，cooldown=3 → 3 % 3 == 0 → 不被冷却拦截
        project.metrics_history.append(_make_metrics(1, new_satisfied=0))
        project.metrics_history.append(_make_metrics(2, new_satisfied=0))
        project.metrics_history.append(_make_metrics(3, new_satisfied=0))
        settings = self._make_settings()
        result = discover_optimization_questions(
            project=project, session=object(), send_prompt_fn=lambda *a, **k: None,
            last_review=None, cycle_num=4, settings=settings,
            all_satisfied=False, backlog_pending=0,
        )
        # 不被门控拦截，但 send_prompt_fn 是空函数会走 fallback
        # spec_discovery_force_nonempty=True → 返回兜底问题
        assert len(result) >= 1
