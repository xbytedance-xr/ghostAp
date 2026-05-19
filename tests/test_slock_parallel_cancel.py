"""Tests for execute_parallel timeout: futures_wait replaces time.sleep, agents cancelled.

Covers:
- TestParallelTimeoutUsesWait: after timeout, `futures_wait` is called (not `time.sleep`)
- TestParallelTimeoutCancelsAgents: after timeout, all incomplete agents' cancel_event is set
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.engine import SlockEngine
from src.slock_engine.models import AgentIdentity, SlockChannel, SlockTask, TaskStatus


# ============================================================
# Helpers
# ============================================================


def _make_engine(tmp_path):
    """Create a minimal SlockEngine with an activated channel."""
    engine = SlockEngine(
        chat_id="test_chat",
        root_path=str(tmp_path),
        engine_name="ParallelCancelTest",
        memory_base_path=str(tmp_path),
    )
    channel = SlockChannel(channel_id="test_chat", name="TestChannel")
    engine.activate_channel(channel)
    return engine


def _register_agent(engine, name="Coder", agent_id=None):
    """Register an agent and return the identity."""
    agent = AgentIdentity(
        name=name,
        agent_type="coco",
        owner_group="test_chat",
    )
    if agent_id:
        agent.agent_id = agent_id
    engine.registry.register(agent)
    return agent


# ============================================================
# TestParallelTimeoutUsesWait
# ============================================================


class TestParallelTimeoutUsesWait:
    """After timeout, `futures_wait` is called with timeout=5.0, NOT `time.sleep`."""

    def test_futures_wait_called_not_sleep(self, tmp_path):
        """When as_completed raises TimeoutError, futures_wait is invoked
        and time.sleep is NOT called for the grace period."""
        engine = _make_engine(tmp_path)
        agent = _register_agent(engine, name="Coder", agent_id="agent_1")

        # Add a task to the engine
        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            task = engine.add_task("Do something")
            assert task is not None

        # Create a future that simulates an incomplete task
        incomplete_future = Future()

        # Mock the executor to return our controlled future
        mock_executor = MagicMock()
        mock_executor.submit.return_value = incomplete_future

        with patch("src.slock_engine.engine.get_settings") as mock_settings, \
             patch("src.slock_engine.engine.futures_wait") as mock_futures_wait, \
             patch("src.slock_engine.engine.as_completed", side_effect=TimeoutError), \
             patch("src.slock_engine.engine.time.sleep") as mock_sleep:

            settings = MagicMock()
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            # Patch _get_executor to return our mock executor
            with patch.object(engine, "_get_executor", return_value=mock_executor):
                results = engine.execute_parallel(
                    [(task.task_id, agent.agent_id)],
                    timeout=10.0,
                )

            # futures_wait MUST be called with the incomplete futures and timeout=5.0
            mock_futures_wait.assert_called_once()
            call_args = mock_futures_wait.call_args
            # First positional arg is the list of futures
            assert incomplete_future in call_args[0][0]
            # timeout=5.0 as keyword
            assert call_args[1]["timeout"] == 5.0

            # time.sleep must NOT be called for the grace period
            mock_sleep.assert_not_called()

    def test_futures_wait_not_called_when_no_incomplete(self, tmp_path):
        """When all tasks complete before timeout, futures_wait is not called."""
        engine = _make_engine(tmp_path)
        agent = _register_agent(engine, name="Coder", agent_id="agent_2")

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            task = engine.add_task("Complete quickly")
            assert task is not None

        # Create a future that completes immediately
        completed_future = Future()
        completed_future.set_result("done")

        mock_executor = MagicMock()
        mock_executor.submit.return_value = completed_future

        def fake_as_completed(futures_dict, timeout=None):
            """Yield all futures immediately (no timeout)."""
            yield from futures_dict

        with patch("src.slock_engine.engine.get_settings") as mock_settings, \
             patch("src.slock_engine.engine.futures_wait") as mock_futures_wait, \
             patch("src.slock_engine.engine.as_completed", side_effect=fake_as_completed):

            settings = MagicMock()
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            with patch.object(engine, "_get_executor", return_value=mock_executor):
                results = engine.execute_parallel(
                    [(task.task_id, agent.agent_id)],
                    timeout=10.0,
                )

            # No timeout occurred, so futures_wait should not be called
            mock_futures_wait.assert_not_called()
            assert results[task.task_id] == "done"


# ============================================================
# TestParallelTimeoutCancelsAgents
# ============================================================


class TestParallelTimeoutCancelsAgents:
    """After timeout, all incomplete agents have their cancel_event set."""

    def test_incomplete_agents_cancel_event_is_set(self, tmp_path):
        """When timeout fires, each incomplete agent's cancel_event is set."""
        engine = _make_engine(tmp_path)
        agent1 = _register_agent(engine, name="Coder", agent_id="agent_cancel_1")
        agent2 = _register_agent(engine, name="Writer", agent_id="agent_cancel_2")

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            task1 = engine.add_task("Task A")
            task2 = engine.add_task("Task B")
            assert task1 is not None
            assert task2 is not None

        # Pre-create cancel events so we can inspect them after timeout
        cancel_event_1 = engine._get_cancel_event("agent_cancel_1")
        cancel_event_2 = engine._get_cancel_event("agent_cancel_2")

        # Both agents have incomplete futures
        future1 = Future()
        future2 = Future()

        mock_executor = MagicMock()
        mock_executor.submit.side_effect = [future1, future2]

        with patch("src.slock_engine.engine.get_settings") as mock_settings, \
             patch("src.slock_engine.engine.futures_wait") as mock_futures_wait, \
             patch("src.slock_engine.engine.as_completed", side_effect=TimeoutError):

            settings = MagicMock()
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            with patch.object(engine, "_get_executor", return_value=mock_executor):
                results = engine.execute_parallel(
                    [(task1.task_id, "agent_cancel_1"), (task2.task_id, "agent_cancel_2")],
                    timeout=5.0,
                )

        # Both agents' cancel events should be set
        assert cancel_event_1.is_set(), "Agent 1 cancel_event should be set after timeout"
        assert cancel_event_2.is_set(), "Agent 2 cancel_event should be set after timeout"

        # Results should be None for timed-out tasks
        assert results[task1.task_id] is None
        assert results[task2.task_id] is None

    def test_partial_completion_only_incomplete_cancelled(self, tmp_path):
        """When some tasks complete and others timeout, only incomplete agents are cancelled."""
        engine = _make_engine(tmp_path)
        agent1 = _register_agent(engine, name="Fast", agent_id="agent_fast")
        agent2 = _register_agent(engine, name="Slow", agent_id="agent_slow")

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            task1 = engine.add_task("Fast task")
            task2 = engine.add_task("Slow task")
            assert task1 is not None
            assert task2 is not None

        # Pre-create cancel events
        cancel_event_fast = engine._get_cancel_event("agent_fast")
        cancel_event_slow = engine._get_cancel_event("agent_slow")

        # future1 completes, future2 does not
        future1 = Future()
        future1.set_result("fast result")
        future2 = Future()

        mock_executor = MagicMock()
        mock_executor.submit.side_effect = [future1, future2]

        def fake_as_completed_partial(futures_dict, timeout=None):
            """Yield only the first (completed) future, then raise TimeoutError."""
            # Find the completed future
            for f in futures_dict:
                if f.done():
                    yield f
            raise TimeoutError

        with patch("src.slock_engine.engine.get_settings") as mock_settings, \
             patch("src.slock_engine.engine.futures_wait") as mock_futures_wait, \
             patch("src.slock_engine.engine.as_completed", side_effect=fake_as_completed_partial):

            settings = MagicMock()
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            with patch.object(engine, "_get_executor", return_value=mock_executor):
                results = engine.execute_parallel(
                    [(task1.task_id, "agent_fast"), (task2.task_id, "agent_slow")],
                    timeout=5.0,
                )

        # Only the slow agent should be cancelled
        assert not cancel_event_fast.is_set(), "Fast agent should NOT be cancelled"
        assert cancel_event_slow.is_set(), "Slow agent should be cancelled after timeout"

        # Fast task completed, slow task timed out
        assert results[task1.task_id] == "fast result"
        assert results[task2.task_id] is None

    def test_on_error_callback_fired_on_timeout(self, tmp_path):
        """The on_error callback fires with timeout message after parallel timeout."""
        engine = _make_engine(tmp_path)
        agent = _register_agent(engine, name="Worker", agent_id="agent_cb")

        with patch("src.slock_engine.engine.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_max_open_tasks = 50
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            task = engine.add_task("Will timeout")
            assert task is not None

        future = Future()
        mock_executor = MagicMock()
        mock_executor.submit.return_value = future

        from src.slock_engine.engine import SlockEngineCallbacks

        error_messages = []
        callbacks = SlockEngineCallbacks(on_error=lambda msg: error_messages.append(msg))

        with patch("src.slock_engine.engine.get_settings") as mock_settings, \
             patch("src.slock_engine.engine.futures_wait"), \
             patch("src.slock_engine.engine.as_completed", side_effect=TimeoutError):

            settings = MagicMock()
            settings.slock_max_parallel_agents = 4
            settings.slock_max_queue_size = 10
            settings.coco_execution_timeout = 60
            settings.slock_agent_execution_timeout = 300
            mock_settings.return_value = settings

            with patch.object(engine, "_get_executor", return_value=mock_executor):
                engine.execute_parallel(
                    [(task.task_id, agent.agent_id)],
                    callbacks=callbacks,
                    timeout=30.0,
                )

        assert len(error_messages) == 1
        assert "timed out after 30.0s" in error_messages[0]
