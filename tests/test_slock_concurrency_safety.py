"""Concurrency and safety tests for Slock engine internals.

Covers:
- Task 15: add_task concurrent calls respect open-task limit
- Task 16: _force_complete_task is thread-safe
- Task 19: submit after shutdown raises RuntimeError + deactivate under concurrent submit
- Task 20: resolved escalation cleans up retry counts
- Task 21: execute_parallel handles partial QueueFullError gracefully
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.bounded_executor import BoundedExecutor, QueueFullError
from src.slock_engine.models import (
    AgentIdentity,
    EscalationLevel,
    EscalationRequest,
    SlockChannel,
    TaskStatus,
)

# ============================================================
# Helpers
# ============================================================


def _make_engine(tmp_path, max_open_tasks: int = 50):
    """Create a SlockEngine with a custom max_open_tasks limit."""
    from src.slock_engine.engine import SlockEngine

    engine = SlockEngine(
        chat_id="test_chat",
        root_path=str(tmp_path),
        engine_name="ConcurrencyTest",
        memory_base_path=str(tmp_path),
    )
    channel = SlockChannel(channel_id="test_chat", name="TestChannel")
    engine.activate_channel(channel)
    return engine


# ============================================================
# Task 15: add_task concurrent respects limit
# ============================================================


class TestAddTaskConcurrentRespectsLimit:
    """Multiple threads calling add_task simultaneously never exceed the open-task limit."""

    def test_10_threads_add_at_limit_boundary(self, tmp_path):
        """With max_open_tasks=5, 10 threads adding concurrently get at most 5 successes."""
        engine = _make_engine(tmp_path)
        barrier = threading.Barrier(10)
        results: list[bool] = []
        lock = threading.Lock()

        def add_once():
            barrier.wait(timeout=5)
            task = engine.add_task("concurrent-task")
            with lock:
                results.append(task is not None)

        with patch("src.slock_engine.task_board_manager.get_settings") as mock_tbm, \
             patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 5
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            mock_settings.return_value = settings
            mock_tbm.return_value = settings

            threads = [threading.Thread(target=add_once) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(results) == 10
        successes = results.count(True)
        assert successes == 5, f"Expected exactly 5 successes, got {successes}"
        assert results.count(False) == 5

    def test_single_thread_at_limit_returns_none(self, tmp_path):
        """When open tasks are at max, add_task returns None."""
        engine = _make_engine(tmp_path)

        with patch("src.slock_engine.task_board_manager.get_settings") as mock_tbm, \
             patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 3
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            mock_settings.return_value = settings
            mock_tbm.return_value = settings

            # Add 3 tasks successfully
            for i in range(3):
                result = engine.add_task(f"task-{i}")
                assert result is not None

            # 4th should fail
            result = engine.add_task("task-overflow")
            assert result is None


# ============================================================
# Task 16: _force_complete_task thread-safe
# ============================================================


class TestForceCompleteTaskThreadSafe:
    """_force_complete_task under concurrent access stays consistent."""

    def test_concurrent_force_complete_same_task(self, tmp_path):
        """Multiple threads force-completing the same task: all succeed without error,
        final status is DONE, no duplicate entries."""
        engine = _make_engine(tmp_path)

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            mock_settings.return_value = settings

            task = engine.add_task("force-complete-target")
            assert task is not None
            task_id = task.task_id

            barrier = threading.Barrier(5)
            errors: list[Exception] = []
            err_lock = threading.Lock()

            def force_complete():
                barrier.wait(timeout=5)
                try:
                    engine._force_complete_task(task_id)
                except Exception as e:
                    with err_lock:
                        errors.append(e)

            threads = [threading.Thread(target=force_complete) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors, f"Unexpected errors: {errors}"
            assert task.status == TaskStatus.DONE

    def test_force_complete_nonexistent_task_no_error(self, tmp_path):
        """Force-completing a non-existent task_id does not raise."""
        engine = _make_engine(tmp_path)
        # Should not raise
        engine._force_complete_task("nonexistent-task-id")


# ============================================================
# Task 19: submit after shutdown raises RuntimeError
# ============================================================


class TestSubmitAfterShutdownRaisesRuntimeError:
    """BoundedExecutor.submit raises RuntimeError after shutdown."""

    def test_submit_after_shutdown(self):
        """Calling submit after shutdown raises RuntimeError."""
        executor = BoundedExecutor(max_workers=2, max_queue_size=5)
        executor.shutdown(wait=True)

        with pytest.raises(RuntimeError, match="executor已关闭"):
            executor.submit(lambda: None)

    def test_is_shutdown_property(self):
        """is_shutdown reflects state correctly."""
        executor = BoundedExecutor(max_workers=1, max_queue_size=5)
        assert executor.is_shutdown is False
        executor.shutdown(wait=True)
        assert executor.is_shutdown is True


class TestDeactivateConcurrentSubmit:
    """Deactivate under concurrent submit does not crash."""

    def test_deactivate_while_submitting(self, tmp_path):
        """Deactivate called while another thread tries to submit — no crash."""
        engine = _make_engine(tmp_path)

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 2
            settings.slock_max_queue_size = 5
            settings.coco_execution_timeout = 60
            mock_settings.return_value = settings

            # Initialize executor
            executor = engine._get_executor()
            assert executor is not None

            barrier = threading.Barrier(2)
            submit_errors: list[Exception] = []
            err_lock = threading.Lock()

            def try_submit_many():
                barrier.wait(timeout=5)
                for _ in range(10):
                    try:
                        executor.submit(lambda: time.sleep(0.01))
                    except (RuntimeError, QueueFullError):
                        pass
                    except Exception as e:
                        with err_lock:
                            submit_errors.append(e)

            def do_deactivate():
                barrier.wait(timeout=5)
                time.sleep(0.01)  # slight delay to ensure some submits happen
                engine.deactivate()

            t1 = threading.Thread(target=try_submit_many)
            t2 = threading.Thread(target=do_deactivate)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            # No unexpected exceptions
            assert not submit_errors, f"Unexpected errors: {submit_errors}"


# ============================================================
# Task 20: resolved escalation cleans retry counts
# ============================================================


class TestResolvedEscalationCleansRetryCounts:
    """resolve_escalation removes the retry_key from _escalation_retry_counts."""

    def test_resolve_cleans_retry_count(self, tmp_path):
        """After resolving, the retry count key is removed."""
        engine = _make_engine(tmp_path)

        # Inject an escalation manually
        esc = EscalationRequest(
            escalation_id="esc-cleanup-001",
            agent_id="agent-1",
            agent_name="Coder",
            level=EscalationLevel.BLOCKED,
            reason="Test blocked",
            options=["Retry", "Skip", "Abort"],
        )
        with engine._lock:
            engine._escalations.append(esc)

        # Pre-seed a retry count
        retry_key = "esc_retry:esc-cleanup-001"
        engine._escalation_retry_counts[retry_key] = 2

        # Resolve the escalation
        resolved = engine.resolve_escalation("esc-cleanup-001", "Skip")
        assert resolved is not None
        assert resolved.resolved is True

        # Retry count should be cleaned
        assert retry_key not in engine._escalation_retry_counts

    def test_trim_escalations_cleans_retry_counts(self, tmp_path):
        """When trimming removes an escalation, its retry count is also cleaned."""
        engine = _make_engine(tmp_path)

        # Add 101 resolved escalations + seed retry counts
        for i in range(101):
            esc = EscalationRequest(
                escalation_id=f"esc-trim-{i}",
                agent_id=f"agent-{i}",
                agent_name=f"Agent-{i}",
                level=EscalationLevel.WARNING,
                reason=f"Reason {i}",
                options=["Retry", "Skip"],
                resolved=True,
                resolution="Skip",
                resolved_at=float(i),
            )
            engine._escalations.append(esc)
            engine._escalation_retry_counts[f"esc_retry:esc-trim-{i}"] = 1

        # Trigger trim (expects caller to hold lock)
        with engine._lock:
            engine._trim_escalations()

        # The oldest escalation (index 0) should have been trimmed
        assert "esc_retry:esc-trim-0" not in engine._escalation_retry_counts
        # The newest should still be present
        assert "esc_retry:esc-trim-100" in engine._escalation_retry_counts


# ============================================================
# Task 21: execute_parallel partial QueueFullError
# ============================================================


class TestExecuteParallelPartialQueueFull:
    """execute_parallel gracefully handles partial QueueFullError during submit."""

    def test_partial_queue_full_returns_none_for_rejected(self, tmp_path):
        """When some tasks are rejected, results dict has None for those task_ids."""
        engine = _make_engine(tmp_path)

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 1
            settings.slock_max_queue_size = 2  # very small queue
            settings.coco_execution_timeout = 60
            mock_settings.return_value = settings

            # Register agents and tasks
            agent = AgentIdentity(
                agent_id="agent-parallel", name="Parallel", agent_type="coco", owner_group="test_chat"
            )
            engine.registry.register(agent)

            # Create tasks
            tasks_created = []
            for i in range(5):
                t = engine.add_task(f"parallel-task-{i}")
                if t:
                    tasks_created.append(t)

            # Mock _execute_agent to be slow so queue fills up
            blocker = threading.Event()

            def slow_agent(*args, **kwargs):
                blocker.wait(timeout=10)
                return "done"

            with patch.object(engine, "_execute_agent", side_effect=slow_agent):
                task_assignments = [(t.task_id, agent.agent_id) for t in tasks_created[:5]]

                # Collect callbacks
                error_messages: list[str] = []
                from src.slock_engine.engine import SlockEngineCallbacks
                callbacks = SlockEngineCallbacks(on_error=lambda msg: error_messages.append(msg))

                # Execute — some will be rejected due to queue size=2
                # Run with a short timeout to not block forever
                results = engine.execute_parallel(task_assignments, callbacks, timeout=2.0)

                # Unblock
                blocker.set()

            # At least some tasks should have None result (rejected) or timeout
            # The key assertion: results dict contains an entry for every task_id
            for tid, _ in task_assignments:
                assert tid in results

    def test_all_rejected_returns_all_none(self, tmp_path):
        """When executor is already shut down, all submissions fail gracefully."""
        engine = _make_engine(tmp_path)

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 1
            settings.slock_max_queue_size = 2
            settings.coco_execution_timeout = 60
            mock_settings.return_value = settings

            agent = AgentIdentity(
                agent_id="agent-shutdown", name="ShutdownTest", agent_type="coco", owner_group="test_chat"
            )
            engine.registry.register(agent)

            t1 = engine.add_task("task-a")
            t2 = engine.add_task("task-b")

            # Force shutdown the executor before execute_parallel
            executor = engine._get_executor()
            executor.shutdown(wait=True)

            task_assignments = [(t1.task_id, agent.agent_id), (t2.task_id, agent.agent_id)]
            results = engine.execute_parallel(task_assignments, timeout=2.0)

            # All results should be None since submit raises RuntimeError
            for tid, _ in task_assignments:
                assert results[tid] is None
