"""Unit tests for slock_engine/memory_manager.py — three-layer memory system."""

from __future__ import annotations

import os

import pytest

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory


class TestMemoryManagerL1:
    """L1: Agent private memory."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        result = mm.read_agent_memory("agent-unknown")
        assert result.role == ""
        assert result.key_knowledge == ""
        assert result.active_context == ""

    def test_write_and_read_round_trip(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(role="Coder", key_knowledge="Python", active_context="Building API")
        mm.write_agent_memory("a1", mem)
        restored = mm.read_agent_memory("a1")
        assert restored.role == "Coder"
        assert restored.key_knowledge == "Python"
        assert restored.active_context == "Building API"

    def test_update_agent_context_appends(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(active_context="First entry")
        mm.write_agent_memory("a1", mem)
        mm.update_agent_context("a1", "Second entry")
        restored = mm.read_agent_memory("a1")
        assert "First entry" in restored.active_context
        assert "Second entry" in restored.active_context

    def test_update_agent_context_on_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.update_agent_context("a2", "New context")
        restored = mm.read_agent_memory("a2")
        assert restored.active_context == "New context"

    def test_agent_memory_path(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        path = mm._agent_memory_path("agent-42")
        assert path.endswith(os.path.join("agents", "agent-42", "MEMORY.md"))


class TestMemoryManagerL2:
    """L2: Group shared memory."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        assert mm.read_group_memory("ch-unknown") == ""

    def test_write_and_read(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("ch1", "Shared knowledge base")
        assert mm.read_group_memory("ch1") == "Shared knowledge base"

    def test_append_group_memory(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("ch1", "Line 1")
        mm.append_group_memory("ch1", "Line 2")
        content = mm.read_group_memory("ch1")
        assert "Line 1" in content
        assert "Line 2" in content

    def test_append_to_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.append_group_memory("ch2", "First entry")
        assert mm.read_group_memory("ch2") == "First entry"


class TestMemoryManagerL3:
    """L3: Global knowledge base."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        assert mm.read_global_wiki() == ""

    def test_write_and_read(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_global_wiki("Global knowledge")
        assert mm.read_global_wiki() == "Global knowledge"

    def test_append_global_wiki(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_global_wiki("Entry A")
        mm.append_global_wiki("Entry B")
        content = mm.read_global_wiki()
        assert "Entry A" in content
        assert "Entry B" in content


class TestMemoryManagerIsolation:
    """Verify layer isolation — groups don't leak into each other."""

    def test_different_groups_isolated(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("g1", "Group 1 data")
        mm.write_group_memory("g2", "Group 2 data")
        assert mm.read_group_memory("g1") == "Group 1 data"
        assert mm.read_group_memory("g2") == "Group 2 data"

    def test_ensure_directories_creates_structure(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.ensure_directories(agent_id="a1", channel_id="ch1")
        assert os.path.isdir(os.path.join(str(tmp_path), "agents", "a1"))
        assert os.path.isdir(os.path.join(str(tmp_path), "groups", "ch1", "tasks"))
        assert os.path.isdir(os.path.join(str(tmp_path), "global"))

    def test_cross_chat_id_access_returns_empty(self, tmp_path):
        """AC-10: L2 shared memory of chat_id_A is not accessible via chat_id_B."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("chat_id_A", "Secret data for group A")
        result_b = mm.read_group_memory("chat_id_B")
        assert result_b == ""
        assert mm.read_group_memory("chat_id_A") == "Secret data for group A"

    def test_cross_chat_append_does_not_leak(self, tmp_path):
        """AC-10: Appending to group B does not affect group A."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("group_alpha", "Alpha original")
        mm.append_group_memory("group_beta", "Beta new entry")
        assert mm.read_group_memory("group_alpha") == "Alpha original"
        assert mm.read_group_memory("group_beta") == "Beta new entry"

    def test_many_groups_no_cross_contamination(self, tmp_path):
        """AC-10: 10 groups each with unique data — no cross-contamination."""
        mm = MemoryManager(base_path=str(tmp_path))
        for i in range(10):
            mm.write_group_memory(f"chat_{i}", f"data_for_group_{i}")

        for i in range(10):
            content = mm.read_group_memory(f"chat_{i}")
            assert content == f"data_for_group_{i}"
            for j in range(10):
                if j != i:
                    assert f"data_for_group_{j}" not in content

    def test_agent_memory_isolated_from_group_memory(self, tmp_path):
        """Agent L1 memory and group L2 memory are fully separate namespaces."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_agent_memory("agent_x", SlockMemory(role="Coder", key_knowledge="Python", active_context="task"))
        mm.write_group_memory("agent_x", "This is group data")
        agent_mem = mm.read_agent_memory("agent_x")
        assert agent_mem.role == "Coder"
        assert mm.read_group_memory("agent_x") == "This is group data"
