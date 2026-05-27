"""Tests for dual-version OCC validation.

Verifies that version numbers are embedded in MEMORY.md as HTML comments,
and that _restore_write_counts can recover from .version file loss by
reading the embedded version from the Markdown file.
"""

import os
import threading
import time

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory


class TestOccDualVersion:
    """Test suite for dual-version OCC validation."""

    def test_slock_memory_to_markdown_includes_version(self) -> None:
        """SlockMemory.to_markdown should include version comment when _version > 0."""
        memory = SlockMemory(
            role="You are a coder.",
            key_knowledge="Use Python 3.10+",
            _version=42,
        )
        md = memory.to_markdown()

        assert "<!-- version: 42 -->" in md
        assert "# Role" in md
        assert "# Key Knowledge" in md

    def test_slock_memory_to_markdown_no_version_when_zero(self) -> None:
        """SlockMemory.to_markdown should NOT include version comment when _version == 0."""
        memory = SlockMemory(
            role="You are a coder.",
            _version=0,
        )
        md = memory.to_markdown()

        assert "<!-- version:" not in md
        assert "# Role" in md

    def test_slock_memory_from_markdown_extracts_version(self) -> None:
        """SlockMemory.from_markdown should extract embedded version."""
        md_content = """# Role
You are a coder.

# Key Knowledge
Use Python 3.10+

<!-- version: 42 -->
"""
        memory = SlockMemory.from_markdown(md_content)

        assert memory._version == 42
        assert memory.role == "You are a coder."
        assert memory.key_knowledge == "Use Python 3.10+"

    def test_slock_memory_from_markdown_no_version_comment(self) -> None:
        """SlockMemory.from_markdown should handle content without version comment."""
        md_content = """# Role
You are a coder.
"""
        memory = SlockMemory.from_markdown(md_content)

        assert memory._version == 0
        assert memory.role == "You are a coder."

    def test_slock_memory_from_markdown_version_comment_ignored_in_content(self) -> None:
        """Version comment should not appear in parsed content sections."""
        md_content = """# Role
You are a coder.

<!-- version: 42 -->
"""
        memory = SlockMemory.from_markdown(md_content)

        assert memory.role == "You are a coder."
        assert "<!-- version" not in memory.role

    def test_memory_manager_writes_embedded_version(self, tmp_path) -> None:
        """MemoryManager should write embedded version to MEMORY.md."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent_001"

        # Initialize agent workspace
        mm.initialize_agent_workspace(agent_id)

        # Write memory (this should increment version and embed it)
        memory = SlockMemory(role="Test role", key_knowledge="Test knowledge")
        mm.write_agent_memory(agent_id, memory)

        # Read the actual file and check for embedded version
        memory_path = mm.agent_memory_path(agent_id)
        with open(memory_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Version 1 after first write
        assert "<!-- version: 1 -->" in content

    def test_memory_manager_restore_from_embedded_version(self, tmp_path) -> None:
        """MemoryManager should restore write count from embedded version when .version is missing."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent_001"

        # Initialize and write memory multiple times
        mm.initialize_agent_workspace(agent_id)
        for i in range(5):
            memory = SlockMemory(role=f"Role version {i}")
            mm.write_agent_memory(agent_id, memory)

        # Verify .version file exists with correct value
        version_file = os.path.join(tmp_path, "agents", agent_id, ".version")
        with open(version_file, "r", encoding="utf-8") as f:
            assert f.read().strip() == "5"

        # Verify MEMORY.md has embedded version
        memory_path = mm.agent_memory_path(agent_id)
        with open(memory_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "<!-- version: 5 -->" in content

        # Delete .version file
        os.remove(version_file)
        assert not os.path.exists(version_file)

        # Create a new MemoryManager instance (simulates restart)
        mm2 = MemoryManager(base_path=str(tmp_path))

        # Verify write count was restored from embedded version
        assert mm2._write_counts.get(agent_id) == 5

    def test_memory_manager_uses_max_version(self, tmp_path) -> None:
        """MemoryManager should use max(.version, embedded) to prevent rollback."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent_001"

        mm.initialize_agent_workspace(agent_id)

        # Write memory a few times
        for i in range(3):
            memory = SlockMemory(role=f"Role {i}")
            mm.write_agent_memory(agent_id, memory)

        # Manually set .version to a lower value (simulates rollback)
        version_file = os.path.join(tmp_path, "agents", agent_id, ".version")
        with open(version_file, "w", encoding="utf-8") as f:
            f.write("1")

        # Create new MemoryManager
        mm2 = MemoryManager(base_path=str(tmp_path))

        # Should use max(1, 3) = 3
        assert mm2._write_counts.get(agent_id) == 3

    def test_roundtrip_memory_preserves_version(self, tmp_path) -> None:
        """Writing and reading memory should preserve version information."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent_001"

        mm.initialize_agent_workspace(agent_id)

        # Write initial memory
        memory1 = SlockMemory(role="First role")
        mm.write_agent_memory(agent_id, memory1)

        # Read it back
        read1 = mm.read_agent_memory(agent_id)
        assert read1._version == 1

        # Write again
        memory2 = SlockMemory(role="Second role")
        mm.write_agent_memory(agent_id, memory2)

        # Read again
        read2 = mm.read_agent_memory(agent_id)
        assert read2._version == 2


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
