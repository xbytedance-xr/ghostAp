"""Unit tests for MemoryManager.get_agent_memory_summary.

Covers:
- Multiple agents' memory summary correctly aggregated
- Empty agent list returns empty list
- Non-existent agent ID handling
- Agent name lookup from registry and identity.json
- Role preview truncation
- Error handling for failed reads
"""

from __future__ import annotations

import json
import os

import pytest

from src.slock_engine.agent_registry import AgentRegistry
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, SlockMemory


@pytest.fixture()
def mm(tmp_path):
    """Create a MemoryManager rooted at a temporary directory."""
    return MemoryManager(base_path=str(tmp_path))


@pytest.fixture()
def registry(tmp_path):
    """Create an AgentRegistry rooted at a temporary directory."""
    return AgentRegistry(base_path=str(tmp_path))


class TestGetAgentMemorySummary:
    """Tests for MemoryManager.get_agent_memory_summary."""

    def test_multiple_agents_summary_aggregated(self, mm: MemoryManager, tmp_path):
        """Multiple agents' memory summaries are correctly aggregated."""
        # Create agent 1 with memory
        agent1_id = "agent_coder"
        mm.write_agent_memory(
            agent1_id,
            SlockMemory(
                role="Senior Python Developer specializing in backend systems",
                key_knowledge="Django\nFastAPI\nPostgreSQL",
                active_context="Working on authentication module",
                archived_context="Previous project: e-commerce platform",
            ),
        )

        # Create agent 2 with memory
        agent2_id = "agent_tester"
        mm.write_agent_memory(
            agent2_id,
            SlockMemory(
                role="QA Engineer focused on integration testing",
                key_knowledge="pytest\nSelenium\nCI/CD",
                active_context="Testing the new payment flow",
                archived_context="",
            ),
        )

        # Get summary for both agents
        summaries = mm.get_agent_memory_summary([agent1_id, agent2_id])

        assert len(summaries) == 2

        # Verify agent 1 summary
        s1 = summaries[0]
        assert s1["agent_id"] == agent1_id
        assert s1["agent_name"] == agent1_id  # No registry, fallback to ID
        assert "Senior Python Developer" in s1["role_preview"]
        assert s1["key_knowledge_len"] == len("Django\nFastAPI\nPostgreSQL")
        assert s1["active_context_len"] == len("Working on authentication module")
        assert s1["archived_context_len"] == len("Previous project: e-commerce platform")
        assert s1["last_updated"] != ""
        assert s1["version"] == 1  # First write

        # Verify agent 2 summary
        s2 = summaries[1]
        assert s2["agent_id"] == agent2_id
        assert "QA Engineer" in s2["role_preview"]
        assert s2["key_knowledge_len"] == len("pytest\nSelenium\nCI/CD")
        assert s2["archived_context_len"] == 0
        assert s2["version"] == 1

    def test_empty_agent_list_returns_empty(self, mm: MemoryManager):
        """Empty agent ID list returns empty list."""
        summaries = mm.get_agent_memory_summary([])
        assert summaries == []

    def test_nonexistent_agent_handled(self, mm: MemoryManager):
        """Non-existent agent ID returns summary with empty/default values."""
        summaries = mm.get_agent_memory_summary(["nonexistent_agent"])

        assert len(summaries) == 1
        s = summaries[0]
        assert s["agent_id"] == "nonexistent_agent"
        assert s["agent_name"] == "nonexistent_agent"
        assert s["role_preview"] == ""
        assert s["key_knowledge_len"] == 0
        assert s["active_context_len"] == 0
        assert s["archived_context_len"] == 0
        assert s["last_updated"] == ""  # No file, no mtime
        assert s["version"] == 0
        # No error for non-existent agent (read_agent_memory returns empty SlockMemory)

    def test_agent_name_from_registry(self, mm: MemoryManager, registry: AgentRegistry, tmp_path):
        """Agent name is looked up from registry when provided."""
        agent_id = "agent_with_name"

        # Register agent with name
        agent = AgentIdentity(
            agent_id=agent_id,
            name="Alice the Coder",
            agent_type="coco",
            owner_group="test_channel",
        )
        registry.register(agent)

        # Write memory
        mm.write_agent_memory(
            agent_id,
            SlockMemory(role="Developer", key_knowledge="Python"),
        )

        # Get summary with registry
        summaries = mm.get_agent_memory_summary([agent_id], registry=registry)

        assert len(summaries) == 1
        assert summaries[0]["agent_name"] == "Alice the Coder"

    def test_agent_name_from_identity_json(self, mm: MemoryManager, tmp_path):
        """Agent name is read from identity.json when registry not provided."""
        agent_id = "agent_identity_file"

        # Create identity.json manually
        identity_dir = os.path.join(str(tmp_path), "agents", agent_id)
        os.makedirs(identity_dir, exist_ok=True)
        identity_path = os.path.join(identity_dir, "identity.json")
        with open(identity_path, "w", encoding="utf-8") as f:
            json.dump({
                "agent_id": agent_id,
                "name": "Bob from Identity",
                "emoji": "🤖",
            }, f)

        # Write memory
        mm.write_agent_memory(
            agent_id,
            SlockMemory(role="Tester"),
        )

        # Get summary without registry
        summaries = mm.get_agent_memory_summary([agent_id])

        assert len(summaries) == 1
        assert summaries[0]["agent_name"] == "Bob from Identity"

    def test_role_preview_truncated_at_100_chars(self, mm: MemoryManager):
        """Role preview is truncated to 100 chars with ellipsis."""
        agent_id = "agent_long_role"
        long_role = "A" * 150

        mm.write_agent_memory(agent_id, SlockMemory(role=long_role))

        summaries = mm.get_agent_memory_summary([agent_id])

        assert len(summaries) == 1
        preview = summaries[0]["role_preview"]
        # 100 chars + "..." = 103
        assert len(preview) == 103
        assert preview.endswith("...")
        assert preview.startswith("A" * 100)

    def test_role_preview_not_truncated_if_short(self, mm: MemoryManager):
        """Role preview is not truncated if under 100 chars."""
        agent_id = "agent_short_role"
        short_role = "Short role description"

        mm.write_agent_memory(agent_id, SlockMemory(role=short_role))

        summaries = mm.get_agent_memory_summary([agent_id])

        assert len(summaries) == 1
        assert summaries[0]["role_preview"] == short_role
        assert "..." not in summaries[0]["role_preview"]

    def test_version_increments_with_writes(self, mm: MemoryManager):
        """Version number reflects the number of writes."""
        agent_id = "agent_version"

        # First write
        mm.write_agent_memory(agent_id, SlockMemory(role="v1"))
        summaries = mm.get_agent_memory_summary([agent_id])
        assert summaries[0]["version"] == 1

        # Second write
        mm.write_agent_memory(agent_id, SlockMemory(role="v2"))
        summaries = mm.get_agent_memory_summary([agent_id])
        assert summaries[0]["version"] == 2

    def test_mixed_existing_and_nonexistent_agents(self, mm: MemoryManager):
        """Mix of existing and non-existent agents returns all summaries."""
        # Create existing agent
        existing_id = "existing_agent"
        mm.write_agent_memory(
            existing_id,
            SlockMemory(role="Real Agent", key_knowledge="knowledge"),
        )

        # Query with mix
        summaries = mm.get_agent_memory_summary([
            existing_id,
            "nonexistent_1",
            "nonexistent_2",
        ])

        assert len(summaries) == 3

        # Existing agent has data
        assert summaries[0]["agent_id"] == existing_id
        assert summaries[0]["role_preview"] == "Real Agent"
        assert summaries[0]["key_knowledge_len"] > 0

        # Non-existent agents have defaults
        assert summaries[1]["agent_id"] == "nonexistent_1"
        assert summaries[1]["role_preview"] == ""
        assert summaries[2]["agent_id"] == "nonexistent_2"

    def test_last_updated_is_iso_format(self, mm: MemoryManager):
        """last_updated is in ISO format."""
        import re

        agent_id = "agent_iso_time"
        mm.write_agent_memory(agent_id, SlockMemory(role="test"))

        summaries = mm.get_agent_memory_summary([agent_id])
        last_updated = summaries[0]["last_updated"]

        # ISO format pattern: YYYY-MM-DDTHH:MM:SS
        iso_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        assert iso_pattern.match(last_updated), (
            f"Expected ISO format timestamp, got: {last_updated}"
        )

    def test_summary_order_matches_input(self, mm: MemoryManager):
        """Summaries are returned in the same order as input agent_ids."""
        agent_ids = ["agent_c", "agent_a", "agent_b"]

        for aid in agent_ids:
            mm.write_agent_memory(aid, SlockMemory(role=f"Role for {aid}"))

        summaries = mm.get_agent_memory_summary(agent_ids)

        assert [s["agent_id"] for s in summaries] == agent_ids

    def test_empty_memory_fields(self, mm: MemoryManager):
        """Agent with empty memory fields returns zero lengths."""
        agent_id = "agent_empty"
        mm.write_agent_memory(agent_id, SlockMemory())

        summaries = mm.get_agent_memory_summary([agent_id])

        assert len(summaries) == 1
        s = summaries[0]
        assert s["key_knowledge_len"] == 0
        assert s["active_context_len"] == 0
        assert s["archived_context_len"] == 0
        assert s["role_preview"] == ""


class TestAgentNameLookup:
    """Tests for _lookup_agent_name fallback behavior."""

    def test_registry_takes_precedence(self, mm: MemoryManager, tmp_path):
        """Registry name takes precedence over identity.json when they differ."""
        agent_id = "agent_precedence"

        # Create identity.json with one name
        identity_dir = os.path.join(str(tmp_path), "agents", agent_id)
        os.makedirs(identity_dir, exist_ok=True)
        with open(os.path.join(identity_dir, "identity.json"), "w") as f:
            json.dump({"agent_id": agent_id, "name": "Identity Name"}, f)

        # Create a mock registry that returns a different name (without writing to disk)
        class MockAgent:
            name = "Registry Name"

        class MockRegistry:
            def get(self, aid):
                if aid == agent_id:
                    return MockAgent()
                return None

        # Write memory
        mm.write_agent_memory(agent_id, SlockMemory(role="test"))

        # With mock registry: should use registry name
        summaries = mm.get_agent_memory_summary([agent_id], registry=MockRegistry())
        assert summaries[0]["agent_name"] == "Registry Name"

        # Without registry: should use identity.json name
        summaries_no_reg = mm.get_agent_memory_summary([agent_id])
        assert summaries_no_reg[0]["agent_name"] == "Identity Name"

    def test_fallback_to_agent_id(self, mm: MemoryManager):
        """When no registry and no identity.json, falls back to agent_id."""
        agent_id = "agent_no_name"
        mm.write_agent_memory(agent_id, SlockMemory(role="test"))

        summaries = mm.get_agent_memory_summary([agent_id])
        assert summaries[0]["agent_name"] == agent_id

    def test_registry_get_returns_none(self, mm: MemoryManager, tmp_path):
        """When registry.get returns None, falls back to identity.json."""
        agent_id = "agent_registry_none"

        # Create identity.json
        identity_dir = os.path.join(str(tmp_path), "agents", agent_id)
        os.makedirs(identity_dir, exist_ok=True)
        with open(os.path.join(identity_dir, "identity.json"), "w") as f:
            json.dump({"agent_id": agent_id, "name": "Fallback Name"}, f)

        mm.write_agent_memory(agent_id, SlockMemory(role="test"))

        # Create a mock registry that returns None
        class MockRegistry:
            def get(self, agent_id):
                return None

        summaries = mm.get_agent_memory_summary([agent_id], registry=MockRegistry())
        assert summaries[0]["agent_name"] == "Fallback Name"


class TestErrorHandling:
    """Tests for error handling in get_agent_memory_summary."""

    def test_exception_during_summary_caught(self, mm: MemoryManager, tmp_path):
        """Exceptions during summary generation are caught and returned as error field."""
        from unittest.mock import patch

        agent_id = "agent_error"
        mm.write_agent_memory(agent_id, SlockMemory(role="test"))

        # Patch _get_single_agent_summary to raise an exception
        with patch.object(
            mm,
            "_get_single_agent_summary",
            side_effect=RuntimeError("Simulated read failure"),
        ):
            summaries = mm.get_agent_memory_summary([agent_id])

        assert len(summaries) == 1
        s = summaries[0]
        assert s["agent_id"] == agent_id
        assert s["error"] == "Simulated read failure"
        assert s["key_knowledge_len"] == 0
        assert s["version"] == 0

    def test_mixed_success_and_error(self, mm: MemoryManager, tmp_path):
        """Some agents succeed, some fail — all returned in order."""
        from unittest.mock import MagicMock

        # Create two agents
        mm.write_agent_memory("agent_ok", SlockMemory(role="Good Agent"))
        mm.write_agent_memory("agent_bad", SlockMemory(role="Bad Agent"))

        # Patch to make only the second agent fail
        original_method = mm._get_single_agent_summary

        def mock_get_single(agent_id, registry=None):
            if agent_id == "agent_bad":
                raise RuntimeError("Failed to read")
            return original_method(agent_id, registry)

        mm._get_single_agent_summary = MagicMock(side_effect=mock_get_single)

        summaries = mm.get_agent_memory_summary(["agent_ok", "agent_bad"])

        assert len(summaries) == 2
        assert summaries[0]["agent_id"] == "agent_ok"
        assert "error" not in summaries[0]
        assert summaries[1]["agent_id"] == "agent_bad"
        assert summaries[1]["error"] == "Failed to read"
