"""Three-layer memory management for Slock Engine.

L1: Agent private memory (MEMORY.md) — follows agent across groups
L2: Group shared memory (SHARED_MEMORY.md) — isolated per group
L3: Global knowledge base (WIKI.md) — accessible by all agents
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from .models import SlockMemory

logger = logging.getLogger(__name__)


class MemoryManager:
    """Manages the three-layer memory system for slock agents.

    All file operations are protected by a threading.Lock to prevent
    concurrent write corruption.
    """

    def __init__(self, base_path: str = ""):
        self._base_path = base_path or os.path.expanduser("~/.ghostap/slock")
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    @property
    def base_path(self) -> str:
        return self._base_path

    # ------------------------------------------------------------------
    # L1: Agent Private Memory
    # ------------------------------------------------------------------

    def _agent_memory_path(self, agent_id: str) -> str:
        return os.path.join(self._base_path, "agents", agent_id, "MEMORY.md")

    def read_agent_memory(self, agent_id: str) -> SlockMemory:
        """Read L1 agent private memory."""
        path = self._agent_memory_path(agent_id)
        with self._lock:
            if not os.path.exists(path):
                return SlockMemory()
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        return SlockMemory.from_markdown(content)

    def write_agent_memory(self, agent_id: str, memory: SlockMemory) -> None:
        """Write L1 agent private memory."""
        path = self._agent_memory_path(agent_id)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            content = memory.to_markdown()
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)

    def update_agent_context(self, agent_id: str, context_update: str) -> None:
        """Append to the active context section of L1 memory."""
        memory = self.read_agent_memory(agent_id)
        if memory.active_context:
            memory.active_context += f"\n{context_update}"
        else:
            memory.active_context = context_update
        self.write_agent_memory(agent_id, memory)

    # ------------------------------------------------------------------
    # L2: Group Shared Memory
    # ------------------------------------------------------------------

    def _group_memory_path(self, channel_id: str) -> str:
        return os.path.join(self._base_path, "groups", channel_id, "SHARED_MEMORY.md")

    def read_group_memory(self, channel_id: str) -> str:
        """Read L2 group shared memory."""
        path = self._group_memory_path(channel_id)
        with self._lock:
            if not os.path.exists(path):
                return ""
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

    def write_group_memory(self, channel_id: str, content: str) -> None:
        """Write L2 group shared memory."""
        path = self._group_memory_path(channel_id)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)

    def append_group_memory(self, channel_id: str, entry: str) -> None:
        """Append to L2 group shared memory."""
        current = self.read_group_memory(channel_id)
        if current:
            content = f"{current}\n{entry}"
        else:
            content = entry
        self.write_group_memory(channel_id, content)

    # ------------------------------------------------------------------
    # L3: Global Knowledge Base
    # ------------------------------------------------------------------

    def _global_wiki_path(self) -> str:
        return os.path.join(self._base_path, "global", "WIKI.md")

    def read_global_wiki(self) -> str:
        """Read L3 global knowledge base."""
        path = self._global_wiki_path()
        with self._lock:
            if not os.path.exists(path):
                return ""
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

    def write_global_wiki(self, content: str) -> None:
        """Write L3 global knowledge base."""
        path = self._global_wiki_path()
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)

    def append_global_wiki(self, entry: str) -> None:
        """Append to L3 global knowledge base."""
        current = self.read_global_wiki()
        if current:
            content = f"{current}\n{entry}"
        else:
            content = entry
        self.write_global_wiki(content)

    # ------------------------------------------------------------------
    # Isolation verification
    # ------------------------------------------------------------------

    def get_group_base_path(self, channel_id: str) -> str:
        """Return the base directory for a group's memory — useful for isolation checks."""
        return os.path.join(self._base_path, "groups", channel_id)

    def ensure_directories(self, agent_id: Optional[str] = None, channel_id: Optional[str] = None) -> None:
        """Pre-create directories for an agent and/or channel."""
        with self._lock:
            if agent_id:
                os.makedirs(os.path.join(self._base_path, "agents", agent_id), exist_ok=True)
            if channel_id:
                os.makedirs(os.path.join(self._base_path, "groups", channel_id, "tasks"), exist_ok=True)
            os.makedirs(os.path.join(self._base_path, "global"), exist_ok=True)
