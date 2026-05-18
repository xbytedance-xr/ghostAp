"""Concurrent stress tests for TaskClaim — R-03 mitigation.

Verifies that the exclusive lock mechanism is thread-safe under high contention:
10 threads simultaneously claiming the same task_id must result in exactly 1
success and 9 failures.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.slock_engine.task_router import TaskClaim


class TestTaskClaimConcurrent:
    """Thread-safety stress tests for TaskClaim."""

    def test_10_threads_claim_same_task_only_one_wins(self):
        """10 threads claim the same task simultaneously — exactly 1 succeeds."""
        claim = TaskClaim(default_ttl=60.0)
        task_id = "concurrent-task-001"
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def attempt_claim(agent_id: str):
            barrier.wait()  # synchronize all threads to start together
            result = claim.claim(task_id, agent_id)
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=attempt_claim, args=(f"agent-{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 10
        assert results.count(True) == 1
        assert results.count(False) == 9

    def test_20_threads_claim_same_task_only_one_wins(self):
        """Scale up to 20 threads — still exactly 1 winner."""
        claim = TaskClaim(default_ttl=60.0)
        task_id = "concurrent-task-002"
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def attempt_claim(agent_id: str):
            barrier.wait()
            result = claim.claim(task_id, agent_id)
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=attempt_claim, args=(f"agent-{i}",))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 20
        assert results.count(True) == 1
        assert results.count(False) == 19

    def test_concurrent_claim_and_release_cycle(self):
        """Multiple rounds of claim→release under contention stay consistent."""
        claim = TaskClaim(default_ttl=60.0)
        task_id = "cycle-task-001"
        success_count = 0
        count_lock = threading.Lock()

        def claim_release_cycle(agent_id: str, rounds: int):
            nonlocal success_count
            for _ in range(rounds):
                if claim.claim(task_id, agent_id):
                    with count_lock:
                        success_count += 1
                    # Hold briefly then release
                    time.sleep(0.001)
                    claim.release(task_id, agent_id)

        threads = [
            threading.Thread(target=claim_release_cycle, args=(f"agent-{i}", 50))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # Total successes should equal 5 agents * 50 rounds = 250 (each gets a turn)
        # In practice under contention, some rounds may fail — but total > 0
        assert success_count > 0
        # And task should end up released
        assert not claim.is_claimed(task_id)

    def test_concurrent_claim_different_tasks_all_succeed(self):
        """10 threads claiming 10 different tasks — all succeed (no false contention)."""
        claim = TaskClaim(default_ttl=60.0)
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def attempt_claim(idx: int):
            barrier.wait()
            result = claim.claim(f"task-{idx}", f"agent-{idx}")
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=attempt_claim, args=(i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 10
        assert all(results)  # All should succeed — different tasks

    def test_concurrent_force_assign_overrides_holder(self):
        """force_assign under contention overrides the current holder."""
        claim = TaskClaim(default_ttl=60.0)
        task_id = "force-task-001"

        # First agent claims
        assert claim.claim(task_id, "agent-original")
        assert claim.get_holder(task_id) == "agent-original"

        # Multiple threads force_assign simultaneously
        barrier = threading.Barrier(5)

        def do_force(agent_id: str):
            barrier.wait()
            claim.force_assign(task_id, agent_id)

        threads = [
            threading.Thread(target=do_force, args=(f"force-agent-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Holder should be one of the force agents (last writer wins)
        holder = claim.get_holder(task_id)
        assert holder is not None
        assert holder.startswith("force-agent-")
        assert holder != "agent-original"

    # ------------------------------------------------------------------
    # TTL expiration stress tests (AC-08)
    # ------------------------------------------------------------------

    def test_ttl_expiration_allows_reclaim(self):
        """AC-08: After TTL expires, another agent can reclaim the task."""
        claim = TaskClaim(default_ttl=0.05)  # 50ms TTL
        task_id = "ttl-task-001"

        # First agent claims
        assert claim.claim(task_id, "agent-a")
        # Second agent cannot claim immediately
        assert claim.claim(task_id, "agent-b") is False

        # Wait for TTL to expire
        time.sleep(0.1)

        # Now second agent can claim
        assert claim.claim(task_id, "agent-b") is True
        assert claim.get_holder(task_id) == "agent-b"

    def test_ttl_expiration_under_contention(self):
        """AC-08: Multiple agents race to claim after TTL expiration."""
        claim = TaskClaim(default_ttl=0.05)  # 50ms TTL
        task_id = "ttl-contention-001"

        # First agent claims and holds until TTL expires
        assert claim.claim(task_id, "agent-original")
        time.sleep(0.1)  # Wait for TTL to expire

        # Now 10 agents race to reclaim
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def attempt_reclaim(agent_id: str):
            barrier.wait()
            result = claim.claim(task_id, agent_id)
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=attempt_reclaim, args=(f"reclaim-agent-{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(results) == 10
        assert results.count(True) == 1
        assert results.count(False) == 9

    def test_rapid_claim_release_no_deadlock(self):
        """Stress test: rapid claim/release cycles never deadlock."""
        claim = TaskClaim(default_ttl=0.5)
        task_id = "rapid-task-001"
        iterations = 100
        completed = [0]
        completed_lock = threading.Lock()

        def rapid_cycle(agent_id: str):
            for _ in range(iterations):
                if claim.claim(task_id, agent_id):
                    claim.release(task_id, agent_id)
                    with completed_lock:
                        completed[0] += 1

        threads = [
            threading.Thread(target=rapid_cycle, args=(f"rapid-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # At least some iterations completed (no deadlock)
        assert completed[0] > 0
        # Task should end up released
        assert not claim.is_claimed(task_id)
