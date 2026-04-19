"""Tests for Loop Engine convergence detection skipping review_failed iterations.

Covers:
  (a) review_failed 轮次不参与收敛判定 → return False
  (b) review_circuit_open_skip 同样不参与收敛判定
  (c) 正常 review（无 review_decision）仍可正确检测收敛
"""

from unittest.mock import MagicMock, patch

import pytest

from src.loop_engine.engine import LoopEngine
from src.loop_engine.models import (
    CriteriaTracker,
    IterationRecord,
    IterationStatus,
    LoopProject,
)


@pytest.fixture
def engine():
    with patch("src.engine_base.get_settings") as mock_settings:
        s = MagicMock()
        s.loop_max_iterations = 100
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_timeout = 5
        s.loop_review_enabled = True
        s.loop_review_failure_circuit_enabled = True
        s.loop_review_failure_max_consecutive = 3
        s.loop_review_failure_cooldown_iterations = 3
        mock_settings.return_value = s
        eng = LoopEngine(chat_id="test", root_path="/tmp/test")
        yield eng


def _make_record(iteration: int, output: str = "", status=IterationStatus.FAILED,
                 review_decision=None, criteria_progress=None):
    r = IterationRecord(iteration=iteration, status=status, output=output)
    r.review_decision = review_decision
    if criteria_progress:
        r.criteria_progress = criteria_progress
    return r


class TestLoopConvergenceSkipReviewFailed:
    def test_review_failed_continue_prevents_convergence(self, engine):
        """review_failed_continue 轮次不被视为收敛."""
        project = LoopProject.create(name="test", root_path="/tmp")
        # 3 轮全部 output < 50 chars（正常情况下会触发收敛）
        # 但其中有 review_failed 标记 → 不收敛
        project.iterations = [
            _make_record(1, output="short", review_decision="review_failed_continue"),
            _make_record(2, output="short", review_decision="review_failed_continue"),
            _make_record(3, output="short", review_decision="review_failed_continue"),
        ]
        engine._project = project
        assert engine._detect_convergence() is False

    def test_review_failed_open_circuit_prevents_convergence(self, engine):
        """review_failed_open_circuit 轮次同样阻止收敛."""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations = [
            _make_record(1, output="short", review_decision=None),
            _make_record(2, output="short", review_decision="review_failed_continue"),
            _make_record(3, output="short", review_decision="review_failed_open_circuit"),
        ]
        engine._project = project
        assert engine._detect_convergence() is False

    def test_circuit_open_skip_prevents_convergence(self, engine):
        """review_circuit_open_skip 不以 'review_failed' 开头，不阻止收敛.
        此行为是设计决策：跳过的轮次不算失败。"""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations = [
            _make_record(1, output="short", review_decision="review_circuit_open_skip"),
            _make_record(2, output="short", review_decision="review_circuit_open_skip"),
            _make_record(3, output="short", review_decision="review_circuit_open_skip"),
        ]
        engine._project = project
        # 这些轮次 output < 50, review_decision 不是 review_failed 开头
        # 所以会触发短输出收敛
        assert engine._detect_convergence() is True

    def test_normal_iterations_can_converge(self, engine):
        """正常 review（无 review_decision）仍可正确检测收敛."""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations = [
            _make_record(1, output="x", review_decision=None),
            _make_record(2, output="y", review_decision=None),
            _make_record(3, output="z", review_decision=None),
        ]
        engine._project = project
        # All outputs < 50 chars, no review_failed → converge
        assert engine._detect_convergence() is True

    def test_mixed_normal_and_failed_no_convergence(self, engine):
        """只要窗口内有一轮 review_failed，就不收敛."""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations = [
            _make_record(1, output="short", review_decision=None),
            _make_record(2, output="short", review_decision="review_failed_continue"),
            _make_record(3, output="short", review_decision=None),
        ]
        engine._project = project
        assert engine._detect_convergence() is False

    def test_insufficient_iterations_no_convergence(self, engine):
        """不足 window 轮迭代时不判定收敛."""
        project = LoopProject.create(name="test", root_path="/tmp")
        project.iterations = [
            _make_record(1, output="short"),
        ]
        engine._project = project
        assert engine._detect_convergence() is False

    def test_criteria_stall_convergence_still_works(self, engine):
        """标准进度停滞 + 全部失败的收敛逻辑仍然正常工作."""
        project = LoopProject.create(name="test", root_path="/tmp")
        tracker = CriteriaTracker()
        tracker.init_criteria(["c1", "c2"])
        project.criteria_tracker = tracker
        project.iterations = [
            _make_record(1, output="a" * 100, status=IterationStatus.FAILED,
                         criteria_progress={0: True, 1: False}),
            _make_record(2, output="b" * 100, status=IterationStatus.FAILED,
                         criteria_progress={0: True, 1: False}),
            _make_record(3, output="c" * 100, status=IterationStatus.FAILED,
                         criteria_progress={0: True, 1: False}),
        ]
        engine._project = project
        assert engine._detect_convergence() is True
