"""Three-layer memory management for Slock Engine.

L1: Agent private memory (MEMORY.md) — follows agent across groups
L2: Group shared memory (SHARED_MEMORY.md) — isolated per group
L3: Global knowledge base (WIKI.md) — accessible by all agents
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from .models import SkillProfile, SlockChannel, SlockMemory, SlockTask

logger = logging.getLogger(__name__)


def default_slock_storage_base() -> str:
    """Return the app-level Slock storage directory."""
    return os.path.expanduser("~/.ghostap/slock")


class MemoryManager:
    """Manages the three-layer memory system for slock agents.

    All file operations are protected by a threading.Lock to prevent
    concurrent write corruption.
    """

    def __init__(self, base_path: str = ""):
        self._base_path = base_path or default_slock_storage_base()
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    @property
    def base_path(self) -> str:
        return self._base_path

    # ------------------------------------------------------------------
    # L1: Agent Private Memory
    # ------------------------------------------------------------------

    def _agent_memory_path(self, agent_id: str) -> str:
        return os.path.join(self._base_path, "agents", agent_id, "MEMORY.md")

    def agent_memory_path(self, agent_id: str) -> str:
        """Return the canonical L1 memory path for an agent."""
        return self._agent_memory_path(agent_id)

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

    def redact_active_context_for_move(
        self, agent_id: str, source_channel_id: str, target_channel_id: str
    ) -> None:
        """Redact active_context during cross-group move to prevent source-group history leakage.

        Preserves role and key_knowledge unchanged. Replaces active_context with
        a single migration record line.  This is an irreversible operation —
        source-group conversation history is permanently removed from the L1 file.
        """
        memory = self.read_agent_memory(agent_id)
        migration_record = (
            f"[{time.strftime('%Y-%m-%d %H:%M')}] "
            f"Context redacted on move: {source_channel_id} → {target_channel_id}"
        )
        memory.active_context = migration_record
        self.write_agent_memory(agent_id, memory)
        logger.info(
            "L1 active_context redacted for move | agent=%s source=%s target=%s",
            agent_id, source_channel_id, target_channel_id,
        )

    def _skill_profile_path(self, agent_id: str) -> str:
        return os.path.join(self._base_path, "agents", agent_id, "skill_profile.json")

    def skill_profile_path(self, agent_id: str) -> str:
        """Return the canonical skill profile path for an agent."""
        return self._skill_profile_path(agent_id)

    def read_skill_profiles(self, agent_id: str) -> list[SkillProfile]:
        """Read persisted skill profiles for an agent."""
        import json

        path = self._skill_profile_path(agent_id)
        with self._lock:
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return [SkillProfile.from_dict(item) for item in data if isinstance(item, dict)]

    def write_skill_profiles(self, agent_id: str, profiles: list[SkillProfile]) -> None:
        """Persist skill profiles for an agent."""
        import json

        path = self._skill_profile_path(agent_id)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump([profile.to_dict() for profile in profiles], f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    def record_skill_feedback(
        self,
        agent_id: str,
        skill_tags: list[str],
        *,
        quality_score: float = 100.0,
    ) -> list[SkillProfile]:
        """Update an agent's persisted skill profile after a successful task."""
        tags = list(dict.fromkeys(tag for tag in skill_tags if tag))
        if not tags:
            return self.read_skill_profiles(agent_id)

        profiles = {profile.tag: profile for profile in self.read_skill_profiles(agent_id)}
        now = time.time()
        bounded_score = max(0.0, min(100.0, quality_score))
        for tag in tags:
            profile = profiles.get(tag, SkillProfile(tag=tag))
            previous_total = max(0, profile.total_tasks)
            profile.success_rate = (
                (profile.success_rate * previous_total + bounded_score)
                / (previous_total + 1)
            )
            profile.total_tasks = previous_total + 1
            profile.last_active = now
            profiles[tag] = profile

        ordered = sorted(profiles.values(), key=lambda profile: profile.tag)
        self.write_skill_profiles(agent_id, ordered)
        return ordered

    # ------------------------------------------------------------------
    # L2: Group Shared Memory
    # ------------------------------------------------------------------

    def _group_memory_path(self, channel_id: str) -> str:
        return os.path.join(self._base_path, "groups", channel_id, "SHARED_MEMORY.md")

    def group_memory_path(self, channel_id: str) -> str:
        """Return the canonical L2 shared memory path for a group."""
        return self._group_memory_path(channel_id)

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

    def global_wiki_path(self) -> str:
        """Return the canonical L3 global wiki path."""
        return self._global_wiki_path()

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

    def team_workspace_path(self, channel_id: str) -> str:
        """Return the canonical team workspace path for a group."""
        return os.path.join(self.get_group_base_path(channel_id), "workspace")

    def task_board_path(self, channel_id: str) -> str:
        """Return the persisted task board JSON path for a group."""
        return os.path.join(self.team_workspace_path(channel_id), ".task-board.json")

    def initialize_team_workspace(self, channel: SlockChannel, *, project_path: str = "") -> None:
        """Create the auditable team workspace files for a Slock channel."""
        import json

        workspace = self.team_workspace_path(channel.channel_id)
        directories = [
            os.path.join(workspace, "agents"),
            os.path.join(workspace, "shared", "artifacts"),
            os.path.join(workspace, "shared", "references"),
            os.path.join(workspace, "shared", "templates"),
            os.path.join(workspace, "project"),
        ]
        team_config_path = os.path.join(workspace, ".team-config.json")
        task_board_path = self.task_board_path(channel.channel_id)
        team_config = {
            "channel_id": channel.channel_id,
            "name": channel.name,
            "team_name": channel.team_name,
            "project_path": project_path,
            "created_at": channel.created_at,
        }
        with self._lock:
            for directory in directories:
                os.makedirs(directory, exist_ok=True)
            if not os.path.exists(team_config_path):
                with open(team_config_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(team_config, f, ensure_ascii=False, indent=2)
                os.replace(team_config_path + ".tmp", team_config_path)
            if not os.path.exists(task_board_path):
                with open(task_board_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump({"tasks": []}, f, ensure_ascii=False, indent=2)
                os.replace(task_board_path + ".tmp", task_board_path)
        self.ensure_default_agent_templates()

    # ------------------------------------------------------------------
    # L3: Agent template market
    # ------------------------------------------------------------------

    def agent_templates_dir(self) -> str:
        """Return the global Agent template directory."""
        return os.path.join(self._base_path, "global", "agent_templates")

    def agent_template_path(self, name: str) -> str:
        """Return a template JSON path by name."""
        safe_name = name.strip().lower().replace("/", "-")
        return os.path.join(self.agent_templates_dir(), f"{safe_name}.json")

    def ensure_default_agent_templates(self) -> None:
        """Seed built-in global Agent templates."""
        import json

        for name, template in self._default_agent_templates().items():
            path = self.agent_template_path(name)
            with self._lock:
                if os.path.exists(path):
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp_path = path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(template, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, path)

    def list_agent_templates(self) -> list[str]:
        """List available global Agent templates."""
        directory = self.agent_templates_dir()
        with self._lock:
            if not os.path.isdir(directory):
                return []
            return sorted(filename[:-5] for filename in os.listdir(directory) if filename.endswith(".json"))

    def read_agent_template(self, name: str) -> dict:
        """Read an Agent template from the global template market."""
        import json

        path = self.agent_template_path(name)
        with self._lock:
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else {}

    def write_agent_template(self, name: str, template: dict) -> None:
        """Write an Agent template to the global template market."""
        import json

        path = self.agent_template_path(name)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(template, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    @staticmethod
    def _default_agent_templates() -> dict[str, dict]:
        return {
            "onboarding": {
                "name": "onboarding",
                "tool_type": "coco",
                "model_name": "",
                "role": "writer",
                "emoji": "🧭",
                "system_prompt": (
                    "# Role: Onboarding Guide\n"
                    "Help new team members understand project context, commands, "
                    "team conventions, and current tasks without blocking execution."
                ),
                "key_knowledge": "Use this template for new team member guidance and project orientation.",
            },
            "coder": {
                "name": "coder",
                "tool_type": "codex",
                "model_name": "",
                "role": "coder",
                "emoji": "🔧",
                "system_prompt": "Implement production-ready code with focused tests and clear verification.",
            },
            "reviewer": {
                "name": "reviewer",
                "tool_type": "claude",
                "model_name": "",
                "role": "reviewer",
                "emoji": "🔍",
                "system_prompt": "Review changes for correctness, security, regressions, and missing tests.",
            },
        }

    def read_task_board(self, channel_id: str) -> list[SlockTask]:
        """Read the persisted task board for a group."""
        import json

        path = self.task_board_path(channel_id)
        with self._lock:
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        tasks = data.get("tasks", []) if isinstance(data, dict) else data
        return [SlockTask.from_dict(item) for item in tasks if isinstance(item, dict)]

    def write_task_board(self, channel_id: str, tasks: list[SlockTask]) -> None:
        """Persist the group task board."""
        import json

        path = self.task_board_path(channel_id)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"tasks": [task.to_dict() for task in tasks]}, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    # ------------------------------------------------------------------
    # Message Archive
    # ------------------------------------------------------------------

    def message_archive_path(self, channel_id: str) -> str:
        """Return the JSONL archive path for a channel's Slock messages."""
        return os.path.join(self._base_path, "archives", channel_id, "messages.jsonl")

    def append_message_archive(
        self,
        channel_id: str,
        *,
        sender_type: str,
        content: str,
        agent_id: str = "",
        agent_name: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        """Append a message record to the channel JSONL archive."""
        import json

        path = self.message_archive_path(channel_id)
        record = {
            "timestamp": time.time(),
            "channel_id": channel_id,
            "sender_type": sender_type,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "content": content,
            "metadata": metadata or {},
        }
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                f.write("\n")

    def ensure_directories(self, agent_id: Optional[str] = None, channel_id: Optional[str] = None) -> None:
        """Pre-create directories for an agent and/or channel."""
        with self._lock:
            if agent_id:
                os.makedirs(os.path.join(self._base_path, "agents", agent_id), exist_ok=True)
            if channel_id:
                os.makedirs(os.path.join(self._base_path, "groups", channel_id, "tasks"), exist_ok=True)
            os.makedirs(os.path.join(self._base_path, "global"), exist_ok=True)
