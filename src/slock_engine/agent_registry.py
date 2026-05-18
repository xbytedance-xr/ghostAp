"""Agent Registry — manages agent identities with file-system persistence.

Thread-safe registry for registering, finding, and removing agents.
Persists agent identities as YAML files under .ghostap/slock/agents/.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from .models import AgentIdentity

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Thread-safe registry for slock agent identities.

    Agents are persisted as identity.json files under:
        {base_path}/agents/{agent_id}/identity.json
    """

    def __init__(self, base_path: str = ""):
        self._base_path = base_path or os.path.expanduser("~/.ghostap/slock")
        self._agents: dict[str, AgentIdentity] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._loaded = False

    @property
    def base_path(self) -> str:
        return self._base_path

    def _agents_dir(self) -> str:
        return os.path.join(self._base_path, "agents")

    def _agent_file(self, agent_id: str) -> str:
        return os.path.join(self._agents_dir(), agent_id, "identity.json")

    def _ensure_loaded(self) -> None:
        """Lazy-load all agents from disk on first access."""
        if self._loaded:
            return
        agents_dir = self._agents_dir()
        if not os.path.isdir(agents_dir):
            self._loaded = True
            return
        for entry in os.listdir(agents_dir):
            identity_file = os.path.join(agents_dir, entry, "identity.json")
            if os.path.isfile(identity_file):
                try:
                    with open(identity_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    agent = AgentIdentity.from_dict(data)
                    self._agents[agent.agent_id] = agent
                except Exception as e:
                    logger.warning("Failed to load agent %s: %s", entry, str(e))
        self._loaded = True

    def register(self, agent: AgentIdentity) -> AgentIdentity:
        """Register a new agent and persist to disk."""
        with self._lock:
            self._ensure_loaded()
            existing = self._agents.get(agent.agent_id)
            if existing is not None:
                agent = self._merge_agent(existing, agent)
            else:
                agent = self._normalize_groups(agent)
            self._agents[agent.agent_id] = agent
            self._persist(agent)
        return agent

    def get(self, agent_id: str) -> Optional[AgentIdentity]:
        """Find agent by ID."""
        with self._lock:
            self._ensure_loaded()
            return self._agents.get(agent_id)

    def find_by_name(self, name: str, channel_id: Optional[str] = None) -> Optional[AgentIdentity]:
        """Find agent by display name, optionally scoped to a channel."""
        with self._lock:
            self._ensure_loaded()
            name_lower = name.lower()
            for agent in self._agents.values():
                if agent.name.lower() == name_lower:
                    if channel_id is None or self._belongs_to_channel(agent, channel_id):
                        return agent
            return None

    def list_agents(self, channel_id: Optional[str] = None) -> list[AgentIdentity]:
        """List all agents, optionally filtered by channel."""
        with self._lock:
            self._ensure_loaded()
            if channel_id is None:
                return list(self._agents.values())
            return [a for a in self._agents.values() if self._belongs_to_channel(a, channel_id)]

    def remove(self, agent_id: str) -> bool:
        """Remove an agent from registry and delete its identity file."""
        with self._lock:
            self._ensure_loaded()
            if agent_id not in self._agents:
                return False
            del self._agents[agent_id]
            identity_file = self._agent_file(agent_id)
            if os.path.exists(identity_file):
                try:
                    os.remove(identity_file)
                except OSError as e:
                    logger.warning("Failed to remove identity file for %s: %s", agent_id, str(e))
            return True

    def update(self, agent: AgentIdentity) -> bool:
        """Update an existing agent's identity."""
        with self._lock:
            self._ensure_loaded()
            if agent.agent_id not in self._agents:
                return False
            self._agents[agent.agent_id] = agent
            self._persist(agent)
            return True

    def _persist(self, agent: AgentIdentity) -> None:
        """Write agent identity to disk (caller must hold _lock)."""
        identity_file = self._agent_file(agent.agent_id)
        os.makedirs(os.path.dirname(identity_file), exist_ok=True)
        tmp_path = identity_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(agent.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, identity_file)

    @staticmethod
    def _normalize_groups(agent: AgentIdentity) -> AgentIdentity:
        """Ensure owner_group and member_groups agree for persisted identities."""
        groups = list(dict.fromkeys([*(agent.member_groups or []), agent.owner_group]))
        agent.member_groups = [g for g in groups if g]
        return agent

    @classmethod
    def _merge_agent(cls, existing: AgentIdentity, incoming: AgentIdentity) -> AgentIdentity:
        """Merge a known agent into another team without losing its original identity."""
        groups = list(dict.fromkeys([
            *(existing.member_groups or []),
            existing.owner_group,
            *(incoming.member_groups or []),
            incoming.owner_group,
        ]))
        groups = [g for g in groups if g]

        existing.name = incoming.name or existing.name
        existing.emoji = incoming.emoji or existing.emoji
        existing.agent_type = incoming.agent_type or existing.agent_type
        existing.model_name = incoming.model_name or existing.model_name
        existing.system_prompt = incoming.system_prompt or existing.system_prompt
        existing.role = incoming.role or existing.role
        existing.permissions = incoming.permissions or existing.permissions
        existing.memory_path = incoming.memory_path or existing.memory_path
        existing.member_groups = groups
        if not existing.owner_group:
            existing.owner_group = incoming.owner_group
        return cls._normalize_groups(existing)

    @staticmethod
    def _belongs_to_channel(agent: AgentIdentity, channel_id: str) -> bool:
        return agent.owner_group == channel_id or channel_id in (agent.member_groups or [])

    def clear(self) -> None:
        """Clear in-memory cache (does not delete files)."""
        with self._lock:
            self._agents.clear()
            self._loaded = False
