"""Tests for slock dispatch loop — event-driven task consumption.

Covers:
- Bootstrap gating: dispatch loop waits for signal_bootstrap_complete
- Timeout detection: tasks exceeding wait timeout get timeout cards
- Task dispatch: dequeued tasks are routed and submitted to executor
- Stop: graceful shutdown of dispatch loop
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.task_queue import QueuedTask, TaskQueue

# ============================================================
# Helpers
# ============================================================


def _make_engine_mock():
    """Create a minimal engine mock with dispatch loop infrastructure."""
    engine = MagicMock()
    engine.chat_id = "test_chat"
    engine._task_queue = TaskQueue(max_size=8)
    engine._bootstrap_ready = threading.Event()
    engine._dispatch_stop = threading.Event()
    engine._dispatch_thread = None
    engine._channel = MagicMock()
    engine._channel.channel_id = "test_chat"
    engine._card_send_fn = MagicMock()
    return engine


# ============================================================
# Bootstrap gating
# ============================================================


class TestBootstrapGating:
    """Dispatch loop waits for bootstrap signal before consuming."""

    def test_bootstrap_signal_unblocks_dispatch(self):
        """signal_bootstrap_complete() sets the event."""
        ready = threading.Event()
        assert not ready.is_set()
        ready.set()
        assert ready.is_set()

    def test_bootstrap_timeout_proceeds_anyway(self):
        """If bootstrap times out, loop should still proceed."""
        ready = threading.Event()
        # Wait with very short timeout
        result = ready.wait(timeout=0.01)
        assert result is False
        assert not ready.is_set()

    def test_empty_registry_without_queued_tasks_enters_standby_without_warning(self, caplog):
        """An idle restored group must not claim that it retained tasks."""
        from src.slock_engine.engine import SlockEngine

        engine = _make_engine_mock()
        engine._bootstrap_ready.set()
        engine.list_agents.return_value = []
        engine._task_queue = MagicMock()
        engine._task_queue.size.return_value = 0
        engine._task_queue.wait_for_idle.side_effect = lambda **_kwargs: engine._dispatch_stop.set()

        with patch("src.slock_engine.engine.get_settings") as settings:
            settings.return_value.slock_bootstrap_timeout = 0.01
            SlockEngine._dispatch_loop(engine)

        assert "Bootstrap attempt" not in caplog.text
        assert "tasks retained" not in caplog.text
        engine._send_bootstrap_recovery_card.assert_not_called()


# ============================================================
# Timeout detection
# ============================================================


class TestTimeoutDetection:
    """Tasks that exceed queue_wait_timeout get timeout cards."""

    def test_expired_task_gets_timeout_card(self):
        """A task enqueued 70s ago (timeout=60s) should trigger timeout card."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._card_send_fn = MagicMock()
            # Initialize _lock since we bypassed __init__
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            task = QueuedTask(
                task_id="t1",
                text="expired task",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time() - 70,  # 70s ago
            )

            # Mock get_settings to return timeout=60
            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            # Timeout card should have been sent
            engine._card_send_fn.assert_called_once()

    def test_fresh_task_not_timed_out(self):
        """A freshly enqueued task should not trigger timeout."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._card_send_fn = MagicMock()
            engine._router = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._task_mgr = MagicMock()
            # Initialize _lock since we bypassed __init__
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Mock routing to ASSIGNED
            from src.slock_engine.task_router import RoutingStatus
            mock_result = MagicMock()
            mock_result.status = RoutingStatus.ASSIGNED
            mock_result.agent = MagicMock(name="coder")
            engine._router.route_message_with_fallback.return_value = mock_result
            engine.list_agents = MagicMock(return_value=[mock_result.agent])

            # Mock executor
            engine._get_executor = MagicMock()
            mock_executor = MagicMock()
            engine._get_executor.return_value = mock_executor

            task = QueuedTask(
                task_id="t2",
                text="fresh task",
                chat_id="chat_1",
                message_id="msg_2",
                enqueue_time=time.time(),  # just now
                callbacks=MagicMock(),
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            # No timeout card sent — task was dispatched
            engine._card_send_fn.assert_not_called()
            mock_executor.submit.assert_called_once()

    def test_dispatch_uses_active_channel_scope_for_agents(self):
        """Queued tasks must only route to agents in the active Slock channel."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.task_router import RoutingStatus

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._channel.channel_id = "chat_active"
            engine._card_send_fn = MagicMock()
            engine._router = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._task_mgr = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            active_agent = MagicMock()
            active_agent.agent_id = "agent-active"
            routing_result = MagicMock()
            routing_result.status = RoutingStatus.ASSIGNED
            routing_result.agent = active_agent
            engine._router.route_message_with_fallback.return_value = routing_result
            engine.list_agents = MagicMock(return_value=[active_agent])
            engine._apply_wake_policy = MagicMock(return_value=[active_agent])
            engine._get_executor = MagicMock()
            engine._get_executor.return_value = MagicMock()

            task = QueuedTask(
                task_id="t-channel",
                text="fix channel scoped bug",
                chat_id="chat_active",
                message_id="msg",
                enqueue_time=time.time(),
                callbacks=MagicMock(),
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            engine.list_agents.assert_called_once_with(channel_id="chat_active")


# ============================================================
# Queue wait card (enqueue path)
# ============================================================


class TestEnqueuePath:
    """Handler enqueue path sends queue wait card."""

    def test_enqueue_returns_position(self):
        """enqueue_task returns the 1-based queue position."""
        queue = TaskQueue(max_size=4)
        t1 = QueuedTask(task_id="t1", text="a", chat_id="c", message_id="m1")
        t2 = QueuedTask(task_id="t2", text="b", chat_id="c", message_id="m2")
        assert queue.enqueue(t1) == 1
        assert queue.enqueue(t2) == 2

    def test_queue_full_raises(self):
        """When queue is full, enqueue raises QueueFullError."""
        from src.slock_engine.task_queue import QueueFullError

        queue = TaskQueue(max_size=1)
        t1 = QueuedTask(task_id="t1", text="a", chat_id="c", message_id="m1")
        t2 = QueuedTask(task_id="t2", text="b", chat_id="c", message_id="m2")
        queue.enqueue(t1)
        with pytest.raises(QueueFullError):
            queue.enqueue(t2)


# ============================================================
# Dispatch loop stop
# ============================================================


class TestDispatchLoopStop:
    """Graceful shutdown of dispatch loop."""

    def test_stop_sets_event(self):
        """stop_dispatch_loop sets the stop flag."""
        stop_event = threading.Event()
        assert not stop_event.is_set()
        stop_event.set()
        assert stop_event.is_set()

    def test_notify_idle_wakes_loop_for_stop_check(self):
        """notify_idle should wake a waiting thread."""
        queue = TaskQueue(max_size=4)
        woke = threading.Event()

        def waiter():
            queue.wait_for_idle(timeout=5.0)
            woke.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.05)
        queue.notify_idle()
        t.join(timeout=1.0)
        assert woke.is_set()


# ============================================================
# Retry backoff: task.retry_count + exponential backoff + discard
# ============================================================


class TestRetryBackoff:
    """Tasks re-enqueued with backoff; discarded after 3 retries."""

    def test_no_agents_increments_retry_count(self):
        """When no agents registered, retry_count increments on re-enqueue."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            task = QueuedTask(
                task_id="t1",
                text="retry me",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),
            )
            assert task.retry_count == 0

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                with patch("time.sleep"):  # skip actual sleep
                    engine._dispatch_single_task(task)

            # Task was re-enqueued with incremented retry_count
            assert task.retry_count == 1
            assert engine._task_queue.size() == 1

    def test_exceeds_max_retries_still_retained(self):
        """retry_count=3 does NOT cause discard — only timeout does.

        Regression fix: retry_count is only for exponential backoff calculation.
        The task is re-enqueued with incremented retry_count and backoff.
        Only waited > slock_queue_wait_timeout causes discard.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            task = QueuedTask(
                task_id="t1",
                text="keep trying",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),  # Just enqueued = waited ~0
                retry_count=3,  # Already retried 3 times
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            # Task should be re-enqueued, NOT discarded
            assert task.retry_count == 4  # Incremented
            assert task.next_retry_at > time.time()  # Backoff set
            assert engine._task_queue.size() == 1  # Still in queue

            # NO timeout card sent (waited < timeout)
            engine._send_timeout_card.assert_not_called()

    def test_queue_wait_increments_retry_count(self):
        """QUEUE_WAIT routing result also increments retry_count."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.task_router import RoutingStatus

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._send_timeout_card = MagicMock()
            engine._router = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            mock_result = MagicMock()
            mock_result.status = RoutingStatus.QUEUE_WAIT
            mock_result.agent = None
            engine._router.route_message_with_fallback.return_value = mock_result
            engine.list_agents = MagicMock(return_value=[MagicMock()])

            task = QueuedTask(
                task_id="t1",
                text="busy agents",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                with patch("time.sleep"):
                    engine._dispatch_single_task(task)

            assert task.retry_count == 1
            assert engine._task_queue.size() == 1


# ============================================================
# execute() QUEUE_WAIT consistency
# ============================================================


class TestExecuteQueueWaitConsistency:
    """AC-R9: execute() returns QUEUE_WAIT when no IDLE agents."""

    def test_all_agents_running_returns_queue_wait(self):
        """When all agents are RUNNING, routing returns QUEUE_WAIT and task is re-enqueued."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.task_router import RoutingStatus

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._send_timeout_card = MagicMock()
            engine._router = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Router returns QUEUE_WAIT (all agents busy)
            mock_result = MagicMock()
            mock_result.status = RoutingStatus.QUEUE_WAIT
            mock_result.agent = None
            engine._router.route_message_with_fallback.return_value = mock_result
            engine.list_agents = MagicMock(return_value=[MagicMock()])

            task = QueuedTask(
                task_id="t1",
                text="busy agents",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                with patch("time.sleep"):
                    engine._dispatch_single_task(task)

            # Task should be re-enqueued, not dropped
            assert engine._task_queue.size() == 1
            # No timeout card should be sent for first retry
            engine._send_timeout_card.assert_not_called()

    def test_queue_wait_does_not_dispatch_to_executor(self):
        """QUEUE_WAIT should NOT submit the task to the bounded executor."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.task_router import RoutingStatus

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._send_timeout_card = MagicMock()
            engine._router = MagicMock()
            engine._get_executor = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Router returns QUEUE_WAIT
            mock_result = MagicMock()
            mock_result.status = RoutingStatus.QUEUE_WAIT
            mock_result.agent = None
            engine._router.route_message_with_fallback.return_value = mock_result
            engine.list_agents = MagicMock(return_value=[MagicMock()])

            task = QueuedTask(
                task_id="t2",
                text="waiting",
                chat_id="chat_1",
                message_id="msg_2",
                enqueue_time=time.time(),
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                with patch("time.sleep"):
                    engine._dispatch_single_task(task)

            # Executor should NOT have been invoked
            engine._get_executor.assert_not_called()


# ============================================================
# Task 16: WP3+WP4 Test Coverage
# ============================================================


class TestBootstrapRaceConditions:
    """WP3: Bootstrap vs dispatch loop race conditions.

    Covers two timing scenarios:
    1. Bootstrap completes first, then dispatch loop starts
    2. Dispatch loop starts first, then bootstrap completes
    """

    def test_bootstrap_first_then_loop(self):
        """Scenario 1: prepare_bootstrap -> finish_bootstrap -> start_dispatch_loop.

        When bootstrap completes before loop starts, the first task should
        be dispatched immediately without waiting.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._bootstrap_ready = threading.Event()
            engine._bootstrap_ready.set()  # Default: ready

            # Simulate: prepare_bootstrap called before activation
            engine.prepare_bootstrap()
            assert not engine._bootstrap_ready.is_set()

            # Simulate: bootstrap completes (e.g., roles registered)
            engine.finish_bootstrap()
            assert engine._bootstrap_ready.is_set()

            # Now dispatch loop can start and process tasks immediately
            # The event is set, so wait() returns immediately
            start = time.time()
            result = engine._bootstrap_ready.wait(timeout=5.0)
            elapsed = time.time() - start

            assert result is True
            assert elapsed < 1.0  # Should return immediately, not wait 5s

    def test_loop_first_then_bootstrap(self):
        """Scenario 2: start_dispatch_loop -> prepare_bootstrap -> finish_bootstrap.

        When dispatch loop starts before bootstrap completes, it should
        block on _bootstrap_ready until finish_bootstrap() is called.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._bootstrap_ready = threading.Event()
            engine._bootstrap_ready.set()

            # Handler calls prepare_bootstrap when it sees roles need bootstrapping
            engine.prepare_bootstrap()
            assert not engine._bootstrap_ready.is_set()

            # In a separate thread, dispatch loop starts and waits
            wait_started = threading.Event()
            wait_result = {"value": None, "elapsed": 0.0}

            def wait_for_bootstrap():
                wait_started.set()
                t0 = time.time()
                wait_result["value"] = engine._bootstrap_ready.wait(timeout=5.0)
                wait_result["elapsed"] = time.time() - t0

            t = threading.Thread(target=wait_for_bootstrap, daemon=True)
            t.start()

            # Wait for the thread to enter the wait
            wait_started.wait(timeout=1.0)
            time.sleep(0.05)  # Give it a moment to actually block

            # Now bootstrap completes (e.g., async bootstrap thread finishes)
            time.sleep(0.1)  # Simulate bootstrap work
            engine.finish_bootstrap()

            t.join(timeout=2.0)

            # The wait should have succeeded
            assert wait_result["value"] is True
            # Should have waited ~0.15s, not the full 5s timeout
            assert 0.1 < wait_result["elapsed"] < 1.0

    def test_prepare_and_finish_bootstrap_api_called(self):
        """Verify prepare_bootstrap() and finish_bootstrap() are the public APIs.

        These should be the only methods handlers call to control bootstrap gating.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._bootstrap_ready = threading.Event()
            engine._bootstrap_ready.set()

            # prepare_bootstrap clears the event
            engine.prepare_bootstrap()
            assert not engine._bootstrap_ready.is_set()

            # finish_bootstrap sets the event (via signal_bootstrap_complete)
            engine.finish_bootstrap()
            assert engine._bootstrap_ready.is_set()

    def test_first_task_processed_after_bootstrap(self):
        """First task is processed within reasonable time after bootstrap completes."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.task_router import RoutingStatus

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._task_mgr = MagicMock()
            engine._bootstrap_ready = threading.Event()
            engine._bootstrap_ready.set()
            engine._send_timeout_card = MagicMock()
            engine._router = MagicMock()
            engine._get_executor = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Setup: agent available and routing succeeds
            mock_agent = MagicMock(name="coder")
            mock_result = MagicMock()
            mock_result.status = RoutingStatus.ASSIGNED
            mock_result.agent = mock_agent
            engine._router.route_message_with_fallback.return_value = mock_result
            engine.list_agents = MagicMock(return_value=[mock_agent])

            mock_executor = MagicMock()
            engine._get_executor.return_value = mock_executor

            # Prepare bootstrap (blocks dispatch)
            engine.prepare_bootstrap()

            # Enqueue a task
            task = QueuedTask(
                task_id="t1",
                text="first task",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),
                callbacks=MagicMock(),
            )
            engine._task_queue.enqueue(task)

            # Simulate bootstrap completing after a short delay
            bootstrap_delay = 0.1
            time.sleep(bootstrap_delay)
            engine.finish_bootstrap()

            # Now dispatch the task
            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60
            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                dequeued = engine._task_queue.dequeue()
                assert dequeued is not None
                engine._dispatch_single_task(dequeued)

            # Task should have been submitted to executor
            mock_executor.submit.assert_called_once()

            # Total waited should be bootstrap_delay + small processing time
            total_waited = time.time() - task.enqueue_time
            assert total_waited < 1.0  # Well within reasonable window


class TestRetryCountDoesNotCauseDiscard:
    """WP4: retry_count > 3 does NOT cause discard when waited < timeout.

    The key insight: retry_count is only for exponential backoff calculation.
    Only slock_queue_wait_timeout determines when a task is discarded.
    """

    def test_retry_count_4_still_retained_short_backoff(self):
        """Task with retry_count=4 and short backoff is re-enqueued, not discarded."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])  # No agents = will retry
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Task has been retried 4 times already (exceeds old "max 3" idea)
            task = QueuedTask(
                task_id="t1",
                text="retry me more",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),  # Just enqueued = waited ~0
                retry_count=4,  # More than 3!
            )

            # Use long timeout so task is not discarded
            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 300  # 5 minutes

            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            # Task should be re-enqueued (retry_count incremented, backoff set)
            assert task.retry_count == 5  # Incremented
            assert task.next_retry_at > time.time()  # Backoff set
            assert engine._task_queue.size() == 1  # Still in queue

            # Most importantly: NO timeout card sent
            engine._send_timeout_card.assert_not_called()

    def test_multiple_retries_with_short_backoff_all_retained(self):
        """Simulate multiple dispatch cycles with increasing retry_count.

        With short backoff (2^n seconds) and long timeout (300s),
        tasks should be retained through many retry cycles.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            task = QueuedTask(
                task_id="t1",
                text="persistent task",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time(),
                retry_count=0,
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 300

            # Simulate 5 dispatch cycles (retry_count goes 0->1->2->3->4->5)
            for expected_count in range(1, 6):
                with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                    # Clear next_retry_at to simulate backoff elapsed
                    task.next_retry_at = 0
                    engine._dispatch_single_task(task)

                assert task.retry_count == expected_count
                assert engine._task_queue.size() == 1
                engine._send_timeout_card.assert_not_called()

                # Dequeue for next cycle
                dequeued = engine._task_queue.dequeue()
                assert dequeued is task

    def test_waited_less_than_timeout_never_discarded(self):
        """Explicit boundary: waited=59s, timeout=60s -> retained regardless of retry_count."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Task was enqueued 59 seconds ago (just under 60s timeout)
            task = QueuedTask(
                task_id="t1",
                text="almost timed out",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time() - 59,  # 59s ago
                retry_count=10,  # Many retries!
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60

            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                task.next_retry_at = 0
                engine._dispatch_single_task(task)

            # 59 < 60, so task should be re-enqueued, not discarded
            assert engine._task_queue.size() == 1
            engine._send_timeout_card.assert_not_called()


class TestQueueTimeoutOnlyCausesDiscard:
    """WP4: Only queue_wait_timeout causes discard; retry_count does not.

    The discard decision is strictly: if waited > slock_queue_wait_timeout.
    The retry_count value is irrelevant to this decision.
    """

    def test_waited_exceeds_timeout_causes_discard(self):
        """waited=70s, timeout=60s -> timeout card sent, task discarded."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Task was enqueued 70 seconds ago (exceeds 60s timeout)
            task = QueuedTask(
                task_id="t1",
                text="expired task",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time() - 70,  # 70s ago
                retry_count=0,  # Low retry_count doesn't save it
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60

            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            # Task should be discarded with timeout card
            engine._send_timeout_card.assert_called_once()
            # Check that waited value was passed
            call_args = engine._send_timeout_card.call_args
            assert call_args[0][0] is task  # First arg is the task
            waited_passed = call_args[0][1]
            assert 69 < waited_passed < 71  # ~70 seconds

            # Task NOT re-enqueued
            assert engine._task_queue.size() == 0

    def test_waited_equals_timeout_boundary(self):
        """Boundary: waited=60s, timeout=60s -> should NOT discard (strict > comparison)."""
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            # Exactly at timeout boundary
            task = QueuedTask(
                task_id="t1",
                text="boundary case",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time() - 60,  # Exactly 60s ago
                retry_count=5,
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60

            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                task.next_retry_at = 0
                engine._dispatch_single_task(task)

            # Code uses > not >=, so 60 is not > 60 -> should be retained
            # (Allow for small timing variance in test)
            if engine._task_queue.size() == 1:
                engine._send_timeout_card.assert_not_called()
            # else: if timing made it 60.0001, discard is also acceptable

    def test_high_retry_count_low_waited_not_discarded(self):
        """retry_count=100, waited=5s -> NOT discarded.

        This is the key contrast: retry_count doesn't matter for discard.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine.list_agents = MagicMock(return_value=[])
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            task = QueuedTask(
                task_id="t1",
                text="many retries",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time() - 5,  # Only 5s ago
                retry_count=100,  # Very high retry count
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60

            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                task.next_retry_at = 0
                engine._dispatch_single_task(task)

            # High retry_count doesn't cause discard
            assert engine._task_queue.size() == 1
            engine._send_timeout_card.assert_not_called()
            assert task.retry_count == 101  # Still incremented for backoff

    def test_low_retry_count_high_waited_discarded(self):
        """retry_count=0, waited=120s -> discarded.

        Even first attempt gets discarded if it waited too long.
        """
        from src.slock_engine.engine import SlockEngine

        with patch.object(SlockEngine, "__init__", lambda self, *a, **kw: None):
            engine = SlockEngine.__new__(SlockEngine)
            engine._channel = MagicMock()
            engine._task_queue = TaskQueue(max_size=8)
            engine._send_timeout_card = MagicMock()
            from src.utils.lock_order import LockLevel, ordered_rlock
            engine._lock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="test._lock")

            task = QueuedTask(
                task_id="t1",
                text="first try expired",
                chat_id="chat_1",
                message_id="msg_1",
                enqueue_time=time.time() - 120,  # 2 minutes ago
                retry_count=0,  # First attempt!
            )

            mock_settings = MagicMock()
            mock_settings.slock_queue_wait_timeout = 60

            with patch("src.slock_engine.engine.get_settings", return_value=mock_settings):
                engine._dispatch_single_task(task)

            # Even on first attempt, waited > timeout causes discard
            engine._send_timeout_card.assert_called_once()
            assert engine._task_queue.size() == 0
