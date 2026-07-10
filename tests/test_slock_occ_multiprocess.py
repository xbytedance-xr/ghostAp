"""Tests for multi-process OCC correctness in MemoryManager.

Verifies that:
1. _refresh_write_count correctly reads .version and MEMORY.md embedded versions
2. File locks provide cross-process mutual exclusion
3. Two MemoryManager instances sharing the same base_path can write concurrently
   without version drift or data loss
"""

from __future__ import annotations

import multiprocessing
import time

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory

# ---------------------------------------------------------------------------
# Helpers for multi-process tests (must be at module level for pickling)
# ---------------------------------------------------------------------------

def _mp_write_agent_memory(args) -> int:
    """Worker function: write count times to the same agent memory.

    Args:
        args: tuple (base_path, agent_id, prefix, count)

    Returns:
        The final version number seen by this process.
    """
    base_path, agent_id, prefix, count = args
    mgr = MemoryManager(base_path=base_path)
    try:
        for i in range(count):
            mem = mgr.read_agent_memory(agent_id)
            if mem.key_knowledge:
                mem.key_knowledge += f"\n{prefix}-{i}"
            else:
                mem.key_knowledge = f"{prefix}-{i}"
            mgr.write_agent_memory(agent_id, mem)
        final_version = mgr._write_counts.get(agent_id, 0)
        return final_version
    finally:
        mgr.shutdown()


def _mp_hold_lock_then_write(args) -> tuple[float, float]:
    """Worker function: acquire file lock, hold it, then write.

    Args:
        args: tuple (base_path, agent_id, hold_seconds[, acquired_event])

    Returns:
        (acquire_time, release_time) as timestamps.
    """
    base_path, agent_id, hold_seconds, *optional = args
    acquired_event = optional[0] if optional else None
    mgr = MemoryManager(base_path=base_path)
    acquire_time = 0.0
    release_time = 0.0
    try:
        with mgr._agent_file_lock(agent_id):
            acquire_time = time.time()
            if acquired_event is not None:
                acquired_event.set()
            time.sleep(hold_seconds)
            mem = SlockMemory(role="tester", key_knowledge=f"written-at-{acquire_time}")
            mgr.write_agent_memory(agent_id, mem)
            release_time = time.time()
    finally:
        mgr.shutdown()
    return acquire_time, release_time


def _mp_write_agent_memory_for_queue(
    base_path: str,
    agent_id: str,
    prefix: str,
    count: int,
    result_queue: multiprocessing.Queue,
) -> None:
    """Worker function for Process: write count times to the same agent memory.

    Puts the final version number to result_queue.

    Adds small random jitter between iterations to prevent one process from
    monopolizing the lock under extreme contention.
    """
    import random
    mgr = MemoryManager(base_path=base_path)
    try:
        for i in range(count):
            mem = mgr.read_agent_memory(agent_id)
            if mem.key_knowledge:
                mem.key_knowledge += f"\n{prefix}-{i}"
            else:
                mem.key_knowledge = f"{prefix}-{i}"
            mgr.write_agent_memory(agent_id, mem)
            # Small jitter to prevent lock starvation under extreme contention
            time.sleep(random.uniform(0, 0.001))
        final_version = mgr._write_counts.get(agent_id, 0)
        result_queue.put(final_version)
    finally:
        mgr.shutdown()


# ---------------------------------------------------------------------------
# Test: _refresh_write_count behavior
# ---------------------------------------------------------------------------


class TestRefreshWriteCount:
    """Verify _refresh_write_count correctly syncs from disk."""

    def test_refresh_reads_version_file(self, tmp_path):
        """_refresh_write_count should read .version file and update _write_counts."""
        mgr = MemoryManager(base_path=str(tmp_path))
        agent_id = "agent-refresh-1"

        # Write once to create files
        mgr.write_agent_memory(agent_id, SlockMemory(role="tester", key_knowledge="initial"))
        assert mgr._write_counts.get(agent_id, 0) == 1

        # Manually bump .version file (simulate another process)
        version_path = tmp_path / "agents" / agent_id / ".version"
        version_path.write_text("10")

        # Refresh should pick up the new version
        refreshed = mgr._refresh_write_count(agent_id)
        assert refreshed == 10
        assert mgr._write_counts.get(agent_id, 0) == 10

        # Next write should start from 10, not 1
        mgr.write_agent_memory(agent_id, SlockMemory(role="tester", key_knowledge="updated"))
        assert mgr._write_counts.get(agent_id, 0) == 11

        mgr.shutdown()

    def test_refresh_reads_embedded_version_from_memory_md(self, tmp_path):
        """_refresh_write_count should read embedded version from MEMORY.md."""
        mgr = MemoryManager(base_path=str(tmp_path))
        agent_id = "agent-refresh-2"

        # Write once
        mgr.write_agent_memory(agent_id, SlockMemory(role="tester", key_knowledge="v1"))
        assert mgr._write_counts.get(agent_id, 0) == 1

        # Delete .version but keep MEMORY.md with embedded version
        version_path = tmp_path / "agents" / agent_id / ".version"
        version_path.unlink()

        # Manually edit MEMORY.md to have a higher embedded version
        memory_path = tmp_path / "agents" / agent_id / "MEMORY.md"
        content = memory_path.read_text()
        content = content.replace("<!-- version: 1 -->", "<!-- version: 15 -->")
        memory_path.write_text(content)

        # Refresh should pick up embedded version
        refreshed = mgr._refresh_write_count(agent_id)
        assert refreshed == 15
        assert mgr._write_counts.get(agent_id, 0) == 15

        mgr.shutdown()

    def test_refresh_takes_max_of_version_file_and_embedded(self, tmp_path):
        """_refresh_write_count should take max(.version, embedded)."""
        mgr = MemoryManager(base_path=str(tmp_path))
        agent_id = "agent-refresh-3"

        # Write once
        mgr.write_agent_memory(agent_id, SlockMemory(role="tester", key_knowledge="v1"))

        # Set .version to 5, embedded to 10
        version_path = tmp_path / "agents" / agent_id / ".version"
        version_path.write_text("5")

        memory_path = tmp_path / "agents" / agent_id / "MEMORY.md"
        content = memory_path.read_text()
        content = content.replace("<!-- version: 1 -->", "<!-- version: 10 -->")
        memory_path.write_text(content)

        # Should take max(5, 10) = 10
        refreshed = mgr._refresh_write_count(agent_id)
        assert refreshed == 10

        # Reverse: .version=20, embedded=10
        version_path.write_text("20")
        refreshed = mgr._refresh_write_count(agent_id)
        assert refreshed == 20

        mgr.shutdown()


# ---------------------------------------------------------------------------
# Test: File lock mutual exclusion across processes
# ---------------------------------------------------------------------------


class TestFileLockCrossProcess:
    """Verify file locks provide cross-process mutual exclusion."""

    def test_file_lock_blocks_other_process(self, tmp_path):
        """When one process holds the lock, another must wait."""
        agent_id = "agent-lock-test"
        hold_seconds = 0.3

        # Start process A that holds the lock for hold_seconds
        ctx = multiprocessing.get_context("spawn")
        with ctx.Manager() as manager:
            acquired_event = manager.Event()
            with ctx.Pool(processes=2) as pool:
                # Process A: acquire lock, signal readiness, hold, release.
                future_a = pool.apply_async(
                    _mp_hold_lock_then_write,
                    [(str(tmp_path), agent_id, hold_seconds, acquired_event)],
                )
                # Do not rely on Pool worker startup order: wait until A owns
                # the file lock before allowing B to contend for it.
                assert acquired_event.wait(timeout=10), "Process A did not acquire the lock in time"
                future_b = pool.apply_async(
                    _mp_hold_lock_then_write,
                    [(str(tmp_path), agent_id, 0.01)],
                )

                a_acquire, a_release = future_a.get(timeout=10)
                b_acquire, b_release = future_b.get(timeout=10)

        # B should acquire AFTER A releases
        assert b_acquire >= a_release, (
            f"Process B acquired lock at {b_acquire} but A released at {a_release}. "
            "Lock does not provide mutual exclusion."
        )

        # Total time should be at least hold_seconds (A's hold time)
        total_elapsed = b_release - a_acquire
        assert total_elapsed >= hold_seconds, (
            f"Total elapsed {total_elapsed} < hold time {hold_seconds}. "
            "Lock may not be blocking correctly."
        )


# ---------------------------------------------------------------------------
# Test: Multi-process OCC correctness
# ---------------------------------------------------------------------------


class TestMultiProcessOCC:
    """Two MemoryManager instances writing concurrently to the same agent."""

    def test_two_processes_writing_concurrently_no_version_drift(self, tmp_path):
        """Two processes writing 50 times each should result in version 101.

        This is the core test for the issue: without _refresh_write_count and
        file locks, each process would have its own in-memory counter starting
        from 0, resulting in version 50 (last write wins) instead of 100.

        Uses multiprocessing.Process instead of Pool to avoid serialization
        overhead that can create artificial contention patterns.
        """
        agent_id = "agent-mp-occ"
        writes_per_process = 50

        # Initialize the agent memory
        mgr_init = MemoryManager(base_path=str(tmp_path))
        mgr_init.write_agent_memory(agent_id, SlockMemory(role="worker", key_knowledge="init"))
        assert mgr_init._write_counts.get(agent_id, 0) == 1
        mgr_init.shutdown()

        # Two processes, each writing 50 times
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()

        p_a = ctx.Process(
            target=_mp_write_agent_memory_for_queue,
            args=(str(tmp_path), agent_id, "procA", writes_per_process, result_queue)
        )
        p_b = ctx.Process(
            target=_mp_write_agent_memory_for_queue,
            args=(str(tmp_path), agent_id, "procB", writes_per_process, result_queue)
        )

        p_a.start()
        p_b.start()

        p_a.join(timeout=120)
        p_b.join(timeout=120)

        assert not p_a.is_alive(), "Process A timed out"
        assert not p_b.is_alive(), "Process B timed out"

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"

        # Read final state
        mgr_final = MemoryManager(base_path=str(tmp_path))
        final_mem = mgr_final.read_agent_memory(agent_id)
        final_version = final_mem._version
        mgr_final.shutdown()

        # Version should be 1 (init) + 50 + 50 = 101
        expected_version = 1 + writes_per_process * 2
        assert final_version == expected_version, (
            f"Expected version {expected_version}, got {final_version}. "
            f"Results: {results}. "
            "Version drift detected - OCC not working across processes."
        )

        # All key_knowledge entries should be present
        kk_lines = [l for l in final_mem.key_knowledge.split("\n") if l.strip()]
        # init + 50 procA + 50 procB = 101 entries
        assert len(kk_lines) == expected_version, (
            f"Expected {expected_version} key_knowledge entries, got {len(kk_lines)}. "
            "Some writes may have been lost."
        )

        # Check both processes' entries are present
        procA_entries = [l for l in kk_lines if l.startswith("procA-")]
        procB_entries = [l for l in kk_lines if l.startswith("procB-")]
        assert len(procA_entries) == writes_per_process, (
            f"Expected {writes_per_process} procA entries, got {len(procA_entries)}"
        )
        assert len(procB_entries) == writes_per_process, (
            f"Expected {writes_per_process} procB entries, got {len(procB_entries)}"
        )
