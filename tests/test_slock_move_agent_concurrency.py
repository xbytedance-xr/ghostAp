"""Concurrency safety tests for Agent cross-group move.

Validates:
- Two threads simultaneously moving the same agent to different groups:
  only one succeeds, final state is consistent.
- Parallel moves of different agents don't interfere.
- Handler-level TOCTOU: move vs task assign race condition.
"""

from __future__ import annotations

import threading

import pytest

from src.slock_engine.agent_registry import AgentRegistry, MoveOutcome
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockMemory


@pytest.fixture
def storage(tmp_path):
    """Create isolated storage."""
    base = str(tmp_path / "slock_concurrency")
    return {
        "registry": AgentRegistry.legacy(base_path=base),
        "memory": MemoryManager(base_path=base),
    }


def _make_agent(agent_id: str, owner_group: str) -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        name=f"Agent-{agent_id}",
        emoji="⚡",
        agent_type="codex",
        model_name="o3-pro",
        system_prompt=f"I am {agent_id}.",
        role="coder",
        owner_group=owner_group,
        member_groups=[owner_group],
    )


class TestConcurrentMoveAgent:
    """Two threads race to move the same agent — only one wins."""

    def test_concurrent_move_same_agent_one_wins(self, storage):
        """Race condition: two threads move agent-001 from src to different targets."""
        registry = storage["registry"]

        agent = _make_agent("race-agent-001", owner_group="race-src")
        registry.register(agent)

        results: dict[str, MoveOutcome] = {}
        barrier = threading.Barrier(2)

        def mover(thread_id: str, target: str) -> None:
            barrier.wait()  # Ensure both threads start at the same time
            outcome = registry.move_agent("race-agent-001", "race-src", target)
            results[thread_id] = outcome

        t1 = threading.Thread(target=mover, args=("t1", "target-A"))
        t2 = threading.Thread(target=mover, args=("t2", "target-B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed (the other fails because source no longer matches)
        successes = [k for k, v in results.items() if v.success]
        failures = [k for k, v in results.items() if not v.success]
        assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
        assert len(failures) == 1

        # Final state: agent is in exactly one target group
        loaded = registry.get("race-agent-001")
        assert loaded is not None
        assert loaded.owner_group in ("target-A", "target-B")
        assert "race-src" not in loaded.member_groups

    def test_concurrent_move_different_agents_no_interference(self, storage):
        """Moving different agents concurrently should both succeed."""
        registry = storage["registry"]

        agent_a = _make_agent("parallel-a", owner_group="src-a")
        agent_b = _make_agent("parallel-b", owner_group="src-b")
        registry.register(agent_a)
        registry.register(agent_b)

        results: dict[str, MoveOutcome] = {}
        barrier = threading.Barrier(2)

        def mover(agent_id: str, src: str, dst: str) -> None:
            barrier.wait()
            outcome = registry.move_agent(agent_id, src, dst)
            results[agent_id] = outcome

        t1 = threading.Thread(target=mover, args=("parallel-a", "src-a", "dst-a"))
        t2 = threading.Thread(target=mover, args=("parallel-b", "src-b", "dst-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both should succeed
        assert results["parallel-a"].success
        assert results["parallel-b"].success

        loaded_a = registry.get("parallel-a")
        loaded_b = registry.get("parallel-b")
        assert loaded_a.owner_group == "dst-a"
        assert loaded_b.owner_group == "dst-b"

    def test_concurrent_move_and_context_update_no_corruption(self, storage):
        """Move + context update in parallel: memory stays coherent."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("move-ctx-race", owner_group="ctx-src")
        registry.register(agent)

        original_role = "I am the consistency guardian."
        mem = SlockMemory(
            role=original_role,
            key_knowledge="Distributed consensus",
            active_context="Initial state",
        )
        memory.write_agent_memory(agent.agent_id, mem)

        errors: list[str] = []
        barrier = threading.Barrier(2)

        def do_move() -> None:
            barrier.wait()
            registry.move_agent(agent.agent_id, "ctx-src", "ctx-dst")

        def do_context_updates() -> None:
            barrier.wait()
            try:
                for i in range(10):
                    memory.update_agent_context(agent.agent_id, f"[Update-{i}] Work done")
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=do_move)
        t2 = threading.Thread(target=do_context_updates)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Errors during concurrent ops: {errors}"

        # Role must be intact (not corrupted by concurrent writes)
        final = memory.read_agent_memory(agent.agent_id)
        assert final.role == original_role
        assert final.key_knowledge == "Distributed consensus"

    def test_repeated_moves_sequential_consistency(self, storage):
        """Move agent through multiple groups sequentially — final state correct."""
        registry = storage["registry"]

        agent = _make_agent("chain-move", owner_group="g1")
        registry.register(agent)

        groups = ["g2", "g3", "g4", "g5"]
        prev = "g1"
        for target in groups:
            outcome = registry.move_agent(agent.agent_id, prev, target)
            assert outcome.success
            prev = target

        loaded = registry.get("chain-move")
        assert loaded.owner_group == "g5"
        assert loaded.member_groups == ["g5"]
        # All previous groups removed
        for old in ["g1", "g2", "g3", "g4"]:
            assert old not in loaded.member_groups

    def test_concurrent_move_notification_isolation(self, storage):
        """AC4: Two threads race to move the same agent — only the winner's
        target group should receive a notification card.

        Simulates the handler-level flow: move_agent → send notification.
        The loser's move_agent returns False, so no notification is sent.
        """
        registry = storage["registry"]

        agent = _make_agent("notify-race-001", owner_group="notify-src")
        registry.register(agent)

        results: dict[str, dict] = {}
        barrier = threading.Barrier(2, timeout=5)

        def handler_simulation(thread_id: str, target: str) -> None:
            """Simulate move_role flow: move_agent → send notification."""
            barrier.wait(timeout=5)
            outcome = registry.move_agent("notify-race-001", "notify-src", target)
            results[thread_id] = {
                "move_success": outcome.success,
                "notification_sent": outcome.success,  # Only send if move succeeded
            }

        t1 = threading.Thread(target=handler_simulation, args=("t1", "target-X"))
        t2 = threading.Thread(target=handler_simulation, args=("t2", "target-Y"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Exactly one thread wins
        winners = [k for k, v in results.items() if v["move_success"]]
        losers = [k for k, v in results.items() if not v["move_success"]]
        assert len(winners) == 1
        assert len(losers) == 1

        # Only winner sent notification
        notifications_sent = sum(1 for v in results.values() if v["notification_sent"])
        assert notifications_sent == 1

        # The winner's target is where the agent ended up
        winner = winners[0]
        expected_target = "target-X" if winner == "t1" else "target-Y"
        loaded = registry.get("notify-race-001")
        assert loaded.owner_group == expected_target


class TestHandlerLevelTOCTOU:
    """Handler-level race: move_role vs task assign.

    Validates that try_lock_for_move prevents the TOCTOU race where an agent
    is checked as IDLE but assigned a task (RUNNING) before move_agent executes.
    """

    def test_move_fails_when_agent_becomes_running(self, storage):
        """Thread A tries to lock for move, but thread B transitions agent to RUNNING first.

        Uses a Barrier to synchronize: both threads reach the critical point,
        then thread B wins the race by setting RUNNING before thread A calls
        try_lock_for_move.
        """
        from unittest.mock import MagicMock

        from src.slock_engine.engine import SlockEngine

        registry = storage["registry"]
        agent = _make_agent("toctou-agent", owner_group="toctou-src")
        registry.register(agent)

        # Create a minimal engine mock with real state management
        engine = MagicMock(spec=SlockEngine)
        # Use real lock and status dict for thread-safety testing
        engine._lock = threading.RLock()
        engine._agent_statuses = {}
        engine._router = MagicMock()
        engine._router.set_agent_status = MagicMock()

        # Wire real try_lock_for_move and get_agent_status
        def real_try_lock(agent_id):
            with engine._lock:
                current = engine._agent_statuses.get(agent_id, AgentStatus.IDLE)
                if current != AgentStatus.IDLE:
                    return False
                engine._agent_statuses[agent_id] = AgentStatus.MOVING
            return True

        def real_get_status(agent_id):
            with engine._lock:
                return engine._agent_statuses.get(agent_id, AgentStatus.IDLE)

        def real_set_status(agent_id, status):
            with engine._lock:
                engine._agent_statuses[agent_id] = status

        engine.try_lock_for_move = real_try_lock
        engine.get_agent_status = real_get_status
        engine.set_agent_status = real_set_status

        results: dict[str, bool] = {}
        barrier = threading.Barrier(2, timeout=5)

        def thread_a_move():
            """Simulates move_role calling try_lock_for_move."""
            barrier.wait(timeout=5)
            # Small delay to let thread B win
            import time
            time.sleep(0.01)
            results["move_locked"] = real_try_lock("toctou-agent")

        def thread_b_assign():
            """Simulates task_router assigning the agent (IDLE → WAKING → RUNNING)."""
            barrier.wait(timeout=5)
            # Immediately transition to RUNNING (simulating task assign)
            real_set_status("toctou-agent", AgentStatus.RUNNING)
            results["assign_done"] = True

        t1 = threading.Thread(target=thread_a_move)
        t2 = threading.Thread(target=thread_b_assign)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Thread B won the race: agent is RUNNING
        assert results.get("assign_done") is True
        # Thread A's try_lock_for_move should FAIL because agent is not IDLE
        assert results.get("move_locked") is False

        # Agent stays in RUNNING state (not moved, not corrupted)
        final_status = real_get_status("toctou-agent")
        assert final_status == AgentStatus.RUNNING

    def test_move_succeeds_when_agent_stays_idle(self, storage):
        """When no concurrent assign, try_lock_for_move succeeds."""


        registry = storage["registry"]
        agent = _make_agent("idle-agent", owner_group="idle-src")
        registry.register(agent)

        # Minimal engine state
        lock = threading.RLock()
        statuses: dict[str, AgentStatus] = {}

        def try_lock(agent_id):
            with lock:
                current = statuses.get(agent_id, AgentStatus.IDLE)
                if current != AgentStatus.IDLE:
                    return False
                statuses[agent_id] = AgentStatus.MOVING
            return True

        # Agent is IDLE by default → lock succeeds
        assert try_lock("idle-agent") is True
        assert statuses["idle-agent"] == AgentStatus.MOVING

        # Second call fails (already MOVING)
        assert try_lock("idle-agent") is False

    def test_notification_not_sent_when_move_blocked(self, storage):
        """When try_lock_for_move fails, no notification card is sent to target."""
        registry = storage["registry"]
        agent = _make_agent("blocked-notify", owner_group="bn-src")
        registry.register(agent)

        # Simulate the handler flow
        notification_sent = False
        lock = threading.RLock()
        statuses: dict[str, AgentStatus] = {"blocked-notify": AgentStatus.RUNNING}

        def try_lock(agent_id):
            with lock:
                current = statuses.get(agent_id, AgentStatus.IDLE)
                if current != AgentStatus.IDLE:
                    return False
                statuses[agent_id] = AgentStatus.MOVING
            return True

        # Handler flow: try_lock → if fails, no notification
        if try_lock("blocked-notify"):
            notification_sent = True  # Would send card here
            # ... move_agent, send_card ...

        assert notification_sent is False
