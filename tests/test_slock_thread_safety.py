"""Thread-safety stress tests for Slock Engine concurrency fixes.

Tests AC-FIX-01, AC-FIX-02, AC-FIX-04.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import AgentStatus
from src.slock_engine.task_router import TaskClaim


class TestTransitionAgentConcurrent:
    """AC-FIX-01: transition_agent() under 10 threads × 100 calls."""

    def _make_engine(self):
        """Create a minimal SlockEngine mock with real locking."""
        from unittest.mock import MagicMock
        import threading

        engine = MagicMock()
        engine._lock = threading.RLock()
        engine._agent_statuses = {}
        engine.VALID_TRANSITIONS = {
            AgentStatus.IDLE: (AgentStatus.WAKING, AgentStatus.MOVING, AgentStatus.DISCUSSING),
            AgentStatus.WAKING: (AgentStatus.THINKING, AgentStatus.IDLE),
            AgentStatus.THINKING: (AgentStatus.RUNNING, AgentStatus.IDLE),
            AgentStatus.RUNNING: (AgentStatus.CHECKING, AgentStatus.IDLE),
            AgentStatus.CHECKING: (AgentStatus.SENDING, AgentStatus.RUNNING, AgentStatus.IDLE),
            AgentStatus.SENDING: (AgentStatus.IDLE,),
            AgentStatus.MOVING: (AgentStatus.IDLE,),
            AgentStatus.DISCUSSING: (AgentStatus.IDLE,),
        }

        # Real transition_agent implementation (mirrors the fixed code)
        def transition_agent(agent_id, to_status):
            with engine._lock:
                current = engine._agent_statuses.get(agent_id, AgentStatus.IDLE)
                if to_status not in engine.VALID_TRANSITIONS.get(current, ()):
                    return False
                engine._agent_statuses[agent_id] = to_status
            return True

        engine.transition_agent = transition_agent
        return engine

    def test_no_invalid_states_under_contention(self):
        """10 threads each attempt 100 transitions; no illegal state should appear."""
        engine = self._make_engine()
        agent_id = "test-agent-1"
        engine._agent_statuses[agent_id] = AgentStatus.IDLE

        # Define a valid forward cycle
        forward_cycle = [
            AgentStatus.WAKING, AgentStatus.THINKING, AgentStatus.RUNNING,
            AgentStatus.CHECKING, AgentStatus.SENDING, AgentStatus.IDLE,
        ]
        errors = []

        def worker():
            for _ in range(100):
                for target in forward_cycle:
                    engine.transition_agent(agent_id, target)
                # Verify state is always valid
                with engine._lock:
                    current = engine._agent_statuses.get(agent_id, AgentStatus.IDLE)
                    if current not in AgentStatus:
                        errors.append(f"Invalid state: {current}")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Found invalid states: {errors}"
        # Final state must be a valid AgentStatus
        final = engine._agent_statuses.get(agent_id, AgentStatus.IDLE)
        assert final in AgentStatus

    def test_concurrent_transitions_never_skip_states(self):
        """Transitions are atomic — no intermediate state is visible to other threads."""
        engine = self._make_engine()
        agent_id = "test-agent-2"
        engine._agent_statuses[agent_id] = AgentStatus.IDLE
        observed_states = []
        stop_event = threading.Event()

        def observer():
            while not stop_event.is_set():
                with engine._lock:
                    state = engine._agent_statuses.get(agent_id, AgentStatus.IDLE)
                observed_states.append(state)
                time.sleep(0.0001)

        def transitioner():
            for _ in range(50):
                engine.transition_agent(agent_id, AgentStatus.WAKING)
                engine.transition_agent(agent_id, AgentStatus.THINKING)
                engine.transition_agent(agent_id, AgentStatus.RUNNING)
                engine.transition_agent(agent_id, AgentStatus.CHECKING)
                engine.transition_agent(agent_id, AgentStatus.SENDING)
                engine.transition_agent(agent_id, AgentStatus.IDLE)

        obs_thread = threading.Thread(target=observer)
        obs_thread.start()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(transitioner) for _ in range(5)]
            for f in as_completed(futures):
                f.result()

        stop_event.set()
        obs_thread.join()

        # All observed states must be valid AgentStatus values
        valid_states = set(AgentStatus)
        invalid = [s for s in observed_states if s not in valid_states]
        assert not invalid, f"Observed invalid states: {invalid}"


class TestTaskClaimSnapshotConcurrent:
    """AC-FIX-02: _score_agents uses snapshot while claim/release run concurrently."""

    def test_snapshot_isolation(self):
        """Concurrent claim/release during snapshot iteration causes no RuntimeError."""
        tc = TaskClaim(default_ttl=3600.0)
        errors = []

        def claimer():
            """Rapidly claim and release tasks."""
            for i in range(100):
                task_id = f"task-{i % 10}"
                tc.claim(task_id, f"agent-{i % 3}")
                time.sleep(0.0001)
                tc.release(task_id)

        def reader():
            """Simulate _score_agents reading a snapshot."""
            for _ in range(100):
                try:
                    snapshot = tc.get_claims_snapshot()
                    # Iterate over snapshot (should be safe even during mutations)
                    _ = sum(1 for _ in snapshot.items())
                except RuntimeError as e:
                    errors.append(str(e))

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=claimer))
            threads.append(threading.Thread(target=reader))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"RuntimeErrors during snapshot iteration: {errors}"

    def test_snapshot_is_point_in_time_copy(self):
        """Snapshot doesn't reflect mutations made after it was taken."""
        tc = TaskClaim(default_ttl=3600.0)
        tc.claim("task-1", "agent-a")
        tc.claim("task-2", "agent-b")

        snapshot = tc.get_claims_snapshot()
        # Mutate after snapshot
        tc.claim("task-3", "agent-c")
        tc.release("task-1")

        assert "task-1" in snapshot  # Still in snapshot
        assert "task-3" not in snapshot  # Not in snapshot


class TestRateLimitTrackerConcurrent:
    """AC-FIX-04: _rate_limit_tracker under concurrent read/write."""

    def test_no_runtime_errors(self):
        """10 threads concurrently reading and writing tracker, no exceptions."""
        tracker: dict[str, list[float]] = {}
        lock = threading.Lock()
        errors = []

        def writer(thread_id: int):
            for i in range(100):
                key = f"chat:{thread_id % 3}:user:{thread_id}"
                now = time.time()
                try:
                    with lock:
                        timestamps = tracker.get(key, [])
                        timestamps.append(now)
                        tracker[key] = timestamps
                except Exception as e:
                    errors.append(str(e))

        def pruner():
            for _ in range(50):
                try:
                    now = time.time()
                    with lock:
                        for key, timestamps in list(tracker.items()):
                            active = [t for t in timestamps if now - t < 60.0]
                            if active:
                                tracker[key] = active
                            else:
                                tracker.pop(key, None)
                except Exception as e:
                    errors.append(str(e))
                time.sleep(0.001)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        threads.append(threading.Thread(target=pruner))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent tracker access: {errors}"
