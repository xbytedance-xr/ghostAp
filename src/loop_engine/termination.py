"""TerminationChecker — 多维终止判定器。

每轮迭代后评估是否应继续、完成或中止。
支持 6 种终止信号，按优先级从高到低评估。
"""

import logging

from .models import (
    LoopProject,
    TerminationSignal,
    TerminationResult,
)

logger = logging.getLogger(__name__)


class TerminationChecker:
    """按优先级评估终止条件。

    评估顺序:
    1. USER_STOP  — 用户主动停止
    2. FATAL      — 连续 3 次失败
    3. MAX_ITER   — 达到最大迭代次数
    4. COMPLETE   — 所有验收标准满足
    5. CONVERGED  — 连续 N 轮无新进展
    6. CONTINUE   — 默认继续
    """

    def __init__(self, max_iterations: int = 10, convergence_window: int = 3):
        self._max_iterations = max_iterations
        self._convergence_window = convergence_window

    def evaluate(
        self, project: LoopProject, should_stop: bool = False
    ) -> TerminationResult:
        """评估是否应该终止。"""
        # 1. 用户终止
        if should_stop:
            return TerminationResult(
                signal=TerminationSignal.USER_STOP,
                reason="用户主动停止",
            )

        # 2. 致命错误 (连续 3 次失败)
        if project.consecutive_failures >= 3:
            return TerminationResult(
                signal=TerminationSignal.FATAL,
                reason=f"连续{project.consecutive_failures}次执行失败",
            )

        # 3. 超过最大迭代
        if project.current_iteration >= self._max_iterations:
            return TerminationResult(
                signal=TerminationSignal.MAX_ITER,
                reason=f"达到最大迭代次数 {self._max_iterations}",
            )

        # 4. 所有标准满足
        if project.is_all_satisfied:
            return TerminationResult(
                signal=TerminationSignal.COMPLETE,
                reason="所有验收标准已满足",
            )

        # 5. 收敛检测
        if self._detect_convergence(project):
            return TerminationResult(
                signal=TerminationSignal.CONVERGED,
                reason="连续多轮无新进展，判定为收敛",
            )

        # 6. 继续
        return TerminationResult(
            signal=TerminationSignal.CONTINUE,
            reason="继续迭代",
        )

    def _detect_convergence(self, project: LoopProject) -> bool:
        """增强的收敛检测: 条件A AND (条件B OR 条件C)。

        条件 A: 最近 N 轮没有新标准被满足
        条件 B: 最近 N 轮输出长度高度相似 (差异 < 20%)
        条件 C: 最近 N 轮都是同一角色
        """
        if len(project.iterations) < self._convergence_window:
            return False

        recent = project.iterations[-self._convergence_window :]

        # 条件 A: 最近 N 轮没有新标准被满足
        condition_a = all(
            not any(v for v in r.criteria_progress.values()) for r in recent
        )
        if not condition_a:
            return False

        # 条件 B: 输出长度高度相似
        outputs = [r.output for r in recent if r.output]
        condition_b = False
        if len(outputs) >= 2:
            lengths = [len(o) for o in outputs]
            avg_len = sum(lengths) / len(lengths)
            if avg_len > 0:
                condition_b = all(
                    abs(length - avg_len) / avg_len < 0.2 for length in lengths
                )

        # 条件 C: 最近 N 轮都是同一角色
        roles = [r.role for r in recent if r.role is not None]
        condition_c = len(roles) == self._convergence_window and len(set(roles)) == 1

        return condition_b or condition_c
