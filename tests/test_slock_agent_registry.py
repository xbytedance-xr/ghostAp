"""Unit tests for slock_engine/agent_registry.py — file-backed agent registry."""

from __future__ import annotations

import json
import os

from src.slock_engine.agent_registry import AgentRegistry
from src.slock_engine.models import AgentIdentity


class TestAgentRegistry:
    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Alice", "owner_group": "g1"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_register_and_get(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        agent = self._make_agent()
        reg.register(agent)
        found = reg.get("a1")
        assert found is not None
        assert found.name == "Alice"

    def test_get_nonexistent_returns_none(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        assert reg.get("nonexistent") is None

    def test_find_by_name(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Bob"))
        found = reg.find_by_name("bob")
        assert found is not None
        assert found.agent_id == "a1"

    def test_find_by_name_case_insensitive(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Charlie"))
        assert reg.find_by_name("CHARLIE") is not None
        assert reg.find_by_name("charlie") is not None

    def test_find_by_name_scoped_to_channel(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Dave", owner_group="g1"))
        reg.register(self._make_agent(agent_id="a2", name="Dave", owner_group="g2"))
        found = reg.find_by_name("Dave", channel_id="g2")
        assert found is not None
        assert found.agent_id == "a2"

    def test_find_by_at_token_matches_name_with_prefix_and_whitespace(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="关羽", owner_group="g1"))
        # Bare token, leading '@', surrounding whitespace, mixed case all collapse
        assert reg.find_by_at_token("关羽").agent_id == "a1"
        assert reg.find_by_at_token("@关羽").agent_id == "a1"
        assert reg.find_by_at_token("  @关羽  ").agent_id == "a1"

    def test_find_by_at_token_falls_back_to_agent_id(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="zhang-fei", name="张飞", owner_group="g1"))
        assert reg.find_by_at_token("zhang-fei").agent_id == "zhang-fei"
        assert reg.find_by_at_token("ZHANG-FEI").agent_id == "zhang-fei"

    def test_find_by_at_token_respects_channel_scope(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="赵云", owner_group="g1"))
        reg.register(self._make_agent(agent_id="a2", name="赵云", owner_group="g2"))
        assert reg.find_by_at_token("赵云", channel_id="g2").agent_id == "a2"
        assert reg.find_by_at_token("赵云", channel_id="g3") is None

    def test_find_by_at_token_returns_none_for_empty_or_unknown(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Alice"))
        assert reg.find_by_at_token("") is None
        assert reg.find_by_at_token("   ") is None
        assert reg.find_by_at_token("@") is None
        assert reg.find_by_at_token("nobody") is None

    def test_format_at_for_text_uses_name_when_present(self, tmp_path):
        agent = self._make_agent(agent_id="a1", name="赵云")
        assert AgentRegistry.format_at_for_text(agent) == "**@赵云**"

    def test_format_at_for_text_falls_back_to_agent_id(self, tmp_path):
        agent = self._make_agent(agent_id="anon", name="")
        assert AgentRegistry.format_at_for_text(agent) == "**@anon**"

    def test_list_agents_all(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="One", owner_group="g1"))
        reg.register(self._make_agent(agent_id="a2", name="Two", owner_group="g2"))
        all_agents = reg.list_agents()
        assert len(all_agents) == 2

    def test_list_agents_by_channel(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="One", owner_group="g1"))
        reg.register(self._make_agent(agent_id="a2", name="Two", owner_group="g2"))
        g1_agents = reg.list_agents(channel_id="g1")
        assert len(g1_agents) == 1
        assert g1_agents[0].agent_id == "a1"

    def test_remove(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="ToRemove"))
        assert reg.remove("a1") is True
        assert reg.get("a1") is None

    def test_remove_nonexistent_returns_false(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        assert reg.remove("nope") is False

    def test_update(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        agent = self._make_agent(agent_id="a1", name="Original")
        reg.register(agent)
        updated = AgentIdentity(agent_id="a1", name="Updated", owner_group="g1")
        assert reg.update(updated) is True
        found = reg.get("a1")
        assert found.name == "Updated"

    def test_update_nonexistent_returns_false(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        agent = self._make_agent(agent_id="nope")
        assert reg.update(agent) is False

    def test_persistence_across_instances(self, tmp_path):
        reg1 = AgentRegistry.legacy(base_path=str(tmp_path))
        reg1.register(self._make_agent(agent_id="a1", name="Persist"))
        if reg1._persist_thread:
            reg1._persist_thread.join(timeout=2)

        # New instance reads from disk
        reg2 = AgentRegistry.legacy(base_path=str(tmp_path))
        found = reg2.get("a1")
        assert found is not None
        assert found.name == "Persist"

    def test_clear_resets_memory_cache(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1"))
        if reg._persist_thread:
            reg._persist_thread.join(timeout=2)
        reg.clear()
        # After clear, lazy reload from disk on next access
        found = reg.get("a1")
        assert found is not None  # still on disk

    def test_identity_file_is_valid_json(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        agent = self._make_agent(agent_id="a1", name="JsonCheck")
        reg.register(agent)
        if reg._persist_thread:
            reg._persist_thread.join(timeout=2)
        identity_file = os.path.join(str(tmp_path), "agents", "a1", "identity.json")
        assert os.path.isfile(identity_file)
        with open(identity_file) as f:
            data = json.load(f)
        assert data["name"] == "JsonCheck"
