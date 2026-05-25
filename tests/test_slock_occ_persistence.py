"""Tests for OCC version persistence and Merge-on-Write (AC15, AC16)."""
import threading
import time

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory


class TestOCCPersistence:
    """AC15: Version numbers survive restart."""

    def test_write_counts_persisted_to_version_file(self, tmp_path):
        """After 3 writes, .version file contains value >= 3."""
        mgr = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent_1"
        for _ in range(3):
            mem = SlockMemory(role="tester", key_knowledge="k", active_context="ctx")
            mgr.write_agent_memory(agent_id, mem)
        assert mgr._write_counts[agent_id] >= 3
        # Check .version file exists
        version_path = tmp_path / "agents" / agent_id / ".version"
        assert version_path.exists()
        assert int(version_path.read_text().strip()) >= 3

    def test_write_counts_restored_on_restart(self, tmp_path):
        """New MemoryManager instance restores _write_counts from .version files."""
        mgr1 = MemoryManager(base_path=str(tmp_path))
        agent_id = "restart_agent"
        for _ in range(5):
            mem = SlockMemory(role="dev", key_knowledge="", active_context="data")
            mgr1.write_agent_memory(agent_id, mem)
        last_count = mgr1._write_counts[agent_id]
        assert last_count >= 5

        # Simulate restart
        mgr2 = MemoryManager(base_path=str(tmp_path))
        assert mgr2._write_counts.get(agent_id, 0) >= last_count


class TestMergeOnWrite:
    """AC16: Concurrent writes trigger merge instead of abandon."""

    def test_concurrent_summarize_merges_incremental(self, tmp_path):
        """When concurrent write happens during summarize, result includes Recent Updates."""
        mgr = MemoryManager(base_path=str(tmp_path))
        agent_id = "merge_agent"

        # Set up LLM callback that simulates slow processing
        def slow_llm(prompt: str):
            time.sleep(0.3)
            return "SUMMARY: compressed content"

        mgr.set_llm_callback(slow_llm)

        # Write initial large context
        large_context = "x" * 5000
        mem = SlockMemory(role="dev", key_knowledge="k", active_context=large_context)
        mgr.write_agent_memory(agent_id, mem)

        # Start summarize in background thread
        result = [None]
        def do_summarize():
            result[0] = mgr.summarize_context(agent_id, threshold=4000)
        t = threading.Thread(target=do_summarize)
        t.start()

        # Concurrent write while summarize is running (during LLM call)
        time.sleep(0.1)
        mem2 = mgr.read_agent_memory(agent_id)
        mem2.active_context = large_context + "\nNEW_INCREMENTAL_DATA"
        mgr.write_agent_memory(agent_id, mem2)

        t.join(timeout=5)
        assert result[0] is True

        # Verify final memory contains merged content
        final_mem = mgr.read_agent_memory(agent_id)
        assert "## Recent Updates" in final_mem.active_context
        assert "NEW_INCREMENTAL_DATA" in final_mem.active_context
        assert "SUMMARY" in final_mem.active_context
