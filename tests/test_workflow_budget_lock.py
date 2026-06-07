"""Tests for thread-safe budget tracking in WorkflowEngine (Task 3/12).

Validates:
- _on_token_usage uses self._lock for thread safety
- Concurrent token updates don't race
"""

import threading
import unittest
from unittest.mock import MagicMock, patch


class TestBudgetLockThreadSafety(unittest.TestCase):
    """Test that _on_token_usage is thread-safe."""

    def _make_engine(self):
        """Create a WorkflowEngine with minimal mocked dependencies."""
        from src.workflow_engine.engine import WorkflowEngine
        from src.workflow_engine.models import BudgetState, WorkflowProject
        from src.workflow_engine.state_manager import WorkflowStateManager

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = WorkflowProject(
            budget=BudgetState(total=10_000_000, used=0)
        )
        engine._state_manager = WorkflowStateManager(engine._project)
        engine._cancel_event = threading.Event()
        engine._callbacks = None
        return engine

    def test_on_token_usage_increments_budget(self):
        engine = self._make_engine()
        engine._on_token_usage(1000)
        self.assertEqual(engine._project.budget.used, 1000)

    def test_on_token_usage_no_state_manager(self):
        engine = self._make_engine()
        engine._state_manager = None
        # Should not raise
        engine._on_token_usage(1000)

    def test_concurrent_token_updates_are_consistent(self):
        """Simulate concurrent token updates from multiple ThreadPoolExecutor workers."""
        engine = self._make_engine()
        n_threads = 10
        increments_per_thread = 100
        token_per_call = 50

        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(increments_per_thread):
                engine._on_token_usage(token_per_call)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = n_threads * increments_per_thread * token_per_call
        self.assertEqual(engine._project.budget.used, expected)

    def test_handle_agent_call_counts_budget_once(self):
        """Agent completion should not add token usage a second time via the renderer."""
        from src.workflow_engine.engine import WorkflowEngine
        from src.workflow_engine.models import (
            AgentCallParams,
            AgentCallResult,
            BudgetState,
            WorkflowMetrics,
            WorkflowProject,
            WorkflowStatus,
        )
        from src.workflow_engine.renderer import WorkflowProgressRenderer
        from src.workflow_engine.state_manager import WorkflowStateManager

        class _FakeExecutor:
            def __init__(self, on_token_usage):
                self._on_token_usage = on_token_usage

            def execute(self, params):
                self._on_token_usage(120)
                return AgentCallResult(
                    output="done",
                    token_usage=120,
                    duration_s=1.5,
                    tool=params.tool,
                    model=params.model,
                )

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = WorkflowProject(
            workflow_id="wf-1",
            status=WorkflowStatus.RUNNING,
            budget=BudgetState(total=1_000_000, used=0),
            metrics=WorkflowMetrics(),
        )
        engine._cancel_event = threading.Event()
        engine._callbacks = None
        engine._agent_call_count = 0
        engine._journal = None
        engine._progress_coalescer = None
        engine._state_manager = WorkflowStateManager(engine._project)
        engine._renderer_wf = WorkflowProgressRenderer(engine._project)
        engine._executor = _FakeExecutor(engine._on_token_usage)

        result = engine._handle_agent_call(AgentCallParams(prompt="do work", tool="coco"))

        self.assertEqual(result.token_usage, 120)
        self.assertEqual(engine._project.budget.used, 120)
        self.assertEqual(engine._project.metrics.total_tokens, 120)


if __name__ == "__main__":
    unittest.main()


class TestSubflowBudgetReservation(unittest.TestCase):
    """验证子 WF spawn 的预算预留逻辑（父 WF remaining 上限 + 并发安全）。"""

    def _make_bridge(self, budget_total: int = 1_000_000):
        """构造一个未启动的 RuntimeBridge 仅用于预算方法测试。"""
        from src.workflow_engine.bridge import RuntimeBridge

        bridge = RuntimeBridge.__new__(RuntimeBridge)
        bridge._budget_total = budget_total
        bridge._budget_reserved = 0
        bridge._budget_lock = threading.Lock()
        bridge._shutdown_done = False
        return bridge

    def test_reserve_basic_amount_capped_by_ratio(self):
        """子 WF 预算不超过 ratio * total。"""
        bridge = self._make_bridge(budget_total=1_000_000)
        # 申请 500k (50%)，但 ratio=0.2 (20%) 封顶
        reserved = bridge._reserve_subflow_budget(500_000, ratio=0.2)
        self.assertEqual(reserved, 200_000)
        self.assertEqual(bridge._budget_reserved, 200_000)

    def test_reserve_fair_share_cap(self):
        """并发多个子 WF 时，每个最多为 remaining / max_concurrent。"""
        bridge = self._make_bridge(budget_total=1_000_000)
        # 第一次预留，fair share = 1_000_000 // 2 = 500_000
        r1 = bridge._reserve_subflow_budget(800_000, ratio=0.8)
        # 第二次预留，剩余 500_000，fair share = 500_000 // 2 = 250_000
        r2 = bridge._reserve_subflow_budget(800_000, ratio=0.8)
        self.assertEqual(r1, 500_000)
        self.assertEqual(r2, 250_000)
        self.assertEqual(bridge._budget_reserved, 750_000)

    def test_release_budget_releases_reserved(self):
        """子 WF 完成后释放预留。"""
        bridge = self._make_bridge(budget_total=1_000_000)
        reserved = bridge._reserve_subflow_budget(200_000, ratio=0.2)
        self.assertEqual(reserved, 200_000)
        bridge._release_subflow_budget(reserved)
        self.assertEqual(bridge._budget_reserved, 0)
        # 第二次可重新预留
        r2 = bridge._reserve_subflow_budget(200_000, ratio=0.2)
        self.assertEqual(r2, 200_000)

    def test_release_zero_noop(self):
        """释放 0 是 no-op，不抛出异常。"""
        bridge = self._make_bridge(budget_total=1_000_000)
        bridge._release_subflow_budget(0)  # no-op, should not raise
        self.assertEqual(bridge._budget_reserved, 0)

    def test_reserve_zero_or_negative_returns_zero(self):
        bridge = self._make_bridge(budget_total=1_000_000)
        self.assertEqual(bridge._reserve_subflow_budget(0), 0)
        self.assertEqual(bridge._reserve_subflow_budget(-100), 0)

    def test_concurrent_reservation_no_overdraw(self):
        """并发多线程同时预留，总量不超过父级预算。"""
        bridge = self._make_bridge(budget_total=1_000_000)
        n_threads = 20
        results = [0] * n_threads
        barrier = threading.Barrier(n_threads)

        def worker(i):
            barrier.wait()
            # 每个线程都尝试申请 200k（20%）
            results[i] = bridge._reserve_subflow_budget(
                200_000, ratio=0.2, max_concurrent_subflows=20
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_reserved = sum(results)
        # 不允许透支
        self.assertLessEqual(total_reserved, bridge._budget_total)
        self.assertEqual(total_reserved, bridge._budget_reserved)

    def test_release_clamped_non_negative(self):
        """release 量超过已预留量时也不会让 _budget_reserved 变负。"""
        bridge = self._make_bridge(budget_total=1_000_000)
        bridge._reserve_subflow_budget(100_000, ratio=0.2)
        bridge._release_subflow_budget(999_999)
        self.assertEqual(bridge._budget_reserved, 0)


class TestExecutorShutdownIdempotency(unittest.TestCase):
    """验证 AgentExecutor / RuntimeBridge stop 的幂等与资源释放。"""

    def test_agent_executor_shutdown_twice_is_safe(self):
        """shutdown 多次调用不抛异常，且不会重复释放。"""
        from src.workflow_engine.executor import AgentExecutor

        executor = AgentExecutor.__new__(AgentExecutor)
        executor.cancel_event = threading.Event()
        executor._session_pool = None
        executor._shutdown_done = False
        # 先构造真实 pool 再验证两次 shutdown
        from concurrent.futures import ThreadPoolExecutor

        executor._session_pool = ThreadPoolExecutor(max_workers=2)
        executor.shutdown(wait=True)
        # 第二次调用应为空操作（幂等）
        executor.shutdown(wait=True)
        executor.shutdown(wait=True)
        self.assertIsNone(executor._session_pool)

    def test_bridge_stop_idempotent(self):
        """RuntimeBridge.stop() 多次调用不抛异常。"""
        from src.workflow_engine.bridge import RuntimeBridge

        bridge = RuntimeBridge.__new__(RuntimeBridge)
        bridge._budget_total = 100_000
        bridge._budget_reserved = 0
        bridge._budget_lock = threading.Lock()
        bridge._shutdown_done = False
        bridge._children = []
        bridge._children_lock = threading.Lock()
        bridge._cancel_event = threading.Event()
        bridge._process = None
        bridge._executor = None
        bridge._workflow_executor = None

        # 没有真实进程/pool，应直接空转且不抛异常；两次调用均安全。
        bridge.stop()
        bridge.stop()
        bridge.cleanup()
        self.assertTrue(bridge._shutdown_done)

    def test_engine_cleanup_without_executor_is_safe(self):
        """engine 在 executor 未初始化时调用 cleanup() 不抛异常。"""
        from src.workflow_engine.engine import WorkflowEngine

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = None
        engine._bridge = None
        engine._executor = None
        engine._session = None
        engine._run_state = 0  # EngineRunState.IDLE 的实际取值
        engine._cancel_event = threading.Event()
        # 什么都没有 —— 验证不抛
        try:
            engine.cleanup()
        except Exception as exc:
            self.fail(f"cleanup() raised unexpectedly: {exc}")


class TestBudgetReservationTwoConcurrentSubflows(unittest.TestCase):
    """两个并发子 WF 的预留总合不能超过父级 total，且单 WF 不超过 remaining/2。"""

    def test_two_concurrent_subflows_shared_pool(self):
        from src.workflow_engine.bridge import RuntimeBridge

        bridge = RuntimeBridge.__new__(RuntimeBridge)
        bridge._budget_total = 2_000_000
        bridge._budget_reserved = 0
        bridge._budget_lock = threading.Lock()
        bridge._shutdown_done = False

        # 模拟两个并发子 WF 同时 spawn
        r1 = bridge._reserve_subflow_budget(1_000_000, ratio=0.5)
        r2 = bridge._reserve_subflow_budget(1_000_000, ratio=0.5)
        # 两个之和不得超过 total
        self.assertLessEqual(r1 + r2, 2_000_000)
        # 每个都受 fair share 约束
        self.assertLessEqual(r1, 1_000_000)
        self.assertLessEqual(r2, 1_000_000)

        # 释放后可恢复
        bridge._release_subflow_budget(r1)
        bridge._release_subflow_budget(r2)
        self.assertEqual(bridge._budget_reserved, 0)
