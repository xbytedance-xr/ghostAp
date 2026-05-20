"""Three-layer memory management for Slock Engine.

L1: Agent private memory (MEMORY.md) — follows agent across groups
L2: Group shared memory (SHARED_MEMORY.md) — isolated per group
L3: Global knowledge base (WIKI.md) — accessible by all agents
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Callable, Optional

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
        self._base_path = os.path.realpath(base_path or default_slock_storage_base())
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._llm_callback: Optional[Callable[[str], Optional[str]]] = None

    @property
    def base_path(self) -> str:
        return self._base_path

    def set_llm_callback(self, callback: Callable[[str], Optional[str]]) -> None:
        """Set an LLM callback for intelligent context summarization.

        The callback receives a prompt string and should return the LLM's
        response as a string, or None on failure.  It will be invoked while
        self._lock is held, so it MUST complete quickly (the caller is
        responsible for enforcing timeouts).
        """
        self._llm_callback = callback

    # ------------------------------------------------------------------
    # L1: Agent Private Memory
    # ------------------------------------------------------------------

    def _agent_memory_path(self, agent_id: str) -> str:
        return os.path.join(self._base_path, "agents", agent_id, "MEMORY.md")

    def agent_memory_path(self, agent_id: str) -> str:
        """Return the canonical L1 memory path for an agent."""
        return self._agent_memory_path(agent_id)

    def agent_notes_path(self, agent_id: str) -> str:
        """Return the canonical notes path for an agent."""
        return os.path.join(self._base_path, "agents", agent_id, "NOTES.md")

    def agent_workspace_path(self, agent_id: str) -> str:
        """Return the canonical workspace path for an agent."""
        return os.path.join(self._base_path, "agents", agent_id, "workspace")

    def initialize_agent_workspace(self, agent_id: str) -> dict[str, str]:
        """Create per-agent MEMORY/NOTES/workspace directories from the Slock spec."""
        memory_path = self.agent_memory_path(agent_id)
        notes_path = self.agent_notes_path(agent_id)
        workspace_path = self.agent_workspace_path(agent_id)
        with self._lock:
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)
            os.makedirs(os.path.join(workspace_path, "current-task"), exist_ok=True)
            os.makedirs(os.path.join(workspace_path, "history"), exist_ok=True)
            os.makedirs(os.path.join(self._base_path, "agents", agent_id, "reasoning"), exist_ok=True)
            if not os.path.exists(notes_path):
                with open(notes_path + ".tmp", "w", encoding="utf-8") as f:
                    f.write("# Notes\n")
                os.replace(notes_path + ".tmp", notes_path)
        return {
            "memory_path": memory_path,
            "notes_path": notes_path,
            "workspace_path": workspace_path,
        }

    def read_agent_memory(self, agent_id: str) -> SlockMemory:
        """Read L1 agent private memory."""
        with self._lock:
            return self._read_agent_memory_unlocked(agent_id)

    def _read_agent_memory_unlocked(self, agent_id: str) -> SlockMemory:
        path = self._agent_memory_path(agent_id)
        if not os.path.exists(path):
            return SlockMemory()
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return SlockMemory.from_markdown(content)

    def write_agent_memory(self, agent_id: str, memory: SlockMemory) -> None:
        """Write L1 agent private memory."""
        with self._lock:
            self._write_agent_memory_unlocked(agent_id, memory)

    def _write_agent_memory_unlocked(self, agent_id: str, memory: SlockMemory) -> None:
        path = self._agent_memory_path(agent_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = memory.to_markdown()
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)

    def update_agent_context(self, agent_id: str, context_update: str) -> None:
        """Append to the active context section of L1 memory."""
        with self._lock:
            memory = self._read_agent_memory_unlocked(agent_id)
            if memory.active_context:
                memory.active_context += f"\n{context_update}"
            else:
                memory.active_context = context_update
            self._write_agent_memory_unlocked(agent_id, memory)

    def redact_active_context_for_move(
        self, agent_id: str, source_channel_id: str, target_channel_id: str
    ) -> None:
        """Redact active_context during cross-group move to prevent source-group history leakage.

        Preserves role and key_knowledge unchanged. Replaces active_context with
        a single migration record line.  This is an irreversible operation —
        source-group conversation history is permanently removed from the L1 file.
        """
        with self._lock:
            memory = self._read_agent_memory_unlocked(agent_id)
            migration_record = (
                f"[{time.strftime('%Y-%m-%d %H:%M')}] "
                f"Context redacted on move: {source_channel_id} → {target_channel_id}"
            )
            memory.active_context = migration_record
            self._write_agent_memory_unlocked(agent_id, memory)
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

    def _reasoning_snapshot_path(self, agent_id: str, task_id: str) -> str:
        safe_task_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", task_id or "message").strip("_") or "message"
        return os.path.join(self._base_path, "agents", agent_id, "reasoning", f"{safe_task_id}.json")

    def write_agent_reasoning_snapshot(
        self,
        agent_id: str,
        task_id: str,
        *,
        prompt_summary: str,
        result_summary: str,
        tool_name: str = "",
        model_name: str = "",
    ) -> str:
        """Persist a user-visible execution summary for the reply-card reasoning action."""
        import json

        path = self._reasoning_snapshot_path(agent_id, task_id)
        record = {
            "agent_id": agent_id,
            "task_id": task_id,
            "prompt_summary": prompt_summary,
            "result_summary": result_summary,
            "tool_name": tool_name,
            "model_name": model_name,
            "created_at": time.time(),
        }
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        return path

    def read_agent_reasoning_snapshot(self, agent_id: str, task_id: str) -> dict:
        """Read a persisted reasoning/execution summary for an agent task."""
        import json

        path = self._reasoning_snapshot_path(agent_id, task_id)
        with self._lock:
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else {}

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
            return self._read_text_unlocked(path)

    def write_group_memory(self, channel_id: str, content: str) -> None:
        """Write L2 group shared memory."""
        path = self._group_memory_path(channel_id)
        with self._lock:
            self._write_text_unlocked(path, content)

    def append_group_memory(self, channel_id: str, entry: str) -> None:
        """Append to L2 group shared memory."""
        path = self._group_memory_path(channel_id)
        with self._lock:
            current = self._read_text_unlocked(path)
            content = f"{current}\n{entry}" if current else entry
            self._write_text_unlocked(path, content)

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
            return self._read_text_unlocked(path)

    def write_global_wiki(self, content: str) -> None:
        """Write L3 global knowledge base."""
        path = self._global_wiki_path()
        with self._lock:
            self._write_text_unlocked(path, content)

    def append_global_wiki(self, entry: str) -> None:
        """Append to L3 global knowledge base."""
        path = self._global_wiki_path()
        with self._lock:
            current = self._read_text_unlocked(path)
            content = f"{current}\n{entry}" if current else entry
            self._write_text_unlocked(path, content)

    def _read_text_unlocked(self, path: str) -> str:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _write_text_unlocked(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)

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
            "tester": {
                "name": "tester",
                "tool_type": "codex",
                "model_name": "",
                "role": "tester",
                "emoji": "🧪",
                "system_prompt": (
                    "Design and run focused tests, reproduce edge cases, and turn failures "
                    "into actionable regression coverage."
                ),
            },
            "planner": {
                "name": "planner",
                "tool_type": "coco",
                "model_name": "",
                "role": "planner",
                "emoji": "📋",
                "system_prompt": (
                    "Break broad goals into executable tasks, clarify dependencies, and keep "
                    "the team aligned on current priorities."
                ),
            },
            "architect": {
                "name": "architect",
                "tool_type": "gemini",
                "model_name": "",
                "role": "architect",
                "emoji": "🏗️",
                "system_prompt": (
                    "Evaluate system boundaries, interfaces, data flow, and long-term "
                    "evolution risks before implementation."
                ),
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
            # Rotation check: rotate if file exceeds 10000 lines or 5MB
            self._maybe_rotate_archive(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _maybe_rotate_archive(self, path: str) -> None:
        """Rotate archive file if it exceeds size/line limits.

        Must be called while self._lock is held.
        Limits: 10000 lines or 5MB. Rotates to .old (overwrites existing).
        """
        if not os.path.exists(path):
            return
        # Check file size first (cheaper than counting lines)
        try:
            file_size = os.path.getsize(path)
        except OSError:
            return

        MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5MB
        MAX_LINES = 10000

        needs_rotate = file_size >= MAX_SIZE_BYTES
        if not needs_rotate:
            # Count lines only if size check didn't trigger
            try:
                with open(path, "r", encoding="utf-8") as f:
                    line_count = sum(1 for _ in f)
                needs_rotate = line_count >= MAX_LINES
            except OSError:
                return

        if needs_rotate:
            old_path = path + ".old"
            try:
                os.replace(path, old_path)
                logger.info("Rotated archive %s -> %s (size=%d)", path, old_path, file_size)
            except OSError as exc:
                logger.warning("Failed to rotate archive %s: %s", path, str(exc))

    def ensure_directories(self, agent_id: Optional[str] = None, channel_id: Optional[str] = None) -> None:
        """Pre-create directories for an agent and/or channel."""
        with self._lock:
            if agent_id:
                os.makedirs(os.path.join(self._base_path, "agents", agent_id), exist_ok=True)
            if channel_id:
                os.makedirs(os.path.join(self._base_path, "groups", channel_id, "tasks"), exist_ok=True)
            os.makedirs(os.path.join(self._base_path, "global"), exist_ok=True)

    # ------------------------------------------------------------------
    # Memory Enhancement: Context Summarization
    # ------------------------------------------------------------------

    def summarize_context(self, agent_id: str, *, threshold: int = 4000) -> bool:
        """Summarize L1 active_context when it exceeds the threshold.

        Returns True if summarization was performed, False if not needed.
        Preserves original file as MEMORY.md.bak before overwriting.

        The LLM callback is invoked OUTSIDE the lock to avoid deadlock.
        """
        import shutil

        # Phase 1: read and backup under lock
        with self._lock:
            memory = self._read_agent_memory_unlocked(agent_id)
            if len(memory.active_context) <= threshold:
                return False

            original_len = len(memory.active_context)
            text_to_summarize = memory.active_context

            # Create backup: copy MEMORY.md to MEMORY.md.bak (atomic)
            memory_path = self._agent_memory_path(agent_id)
            backup_path = memory_path + ".bak"
            if os.path.exists(memory_path):
                shutil.copy2(memory_path, backup_path)

        # Phase 2: LLM summarization OUTSIDE lock (may be slow)
        compressed = self._summarize_text(text_to_summarize)

        # Phase 3: write back under lock
        with self._lock:
            memory = self._read_agent_memory_unlocked(agent_id)
            memory.active_context = compressed
            self._write_agent_memory_unlocked(agent_id, memory)

        logger.info(
            "L1 active_context summarized | agent=%s original_len=%d new_len=%d threshold=%d",
            agent_id, original_len, len(compressed), threshold,
        )
        return True

    def _summarize_text(self, text: str, *, max_output_chars: int = 1500) -> str:
        """Compress text using LLM summarization.

        If an LLM callback has been registered via `set_llm_callback`, it is
        invoked to produce a high-quality summary.  On any failure the method
        falls back to simple tail-truncation.
        """
        from datetime import datetime, timezone

        if len(text) <= max_output_chars:
            return text

        timestamp_marker = f"[Context summarized at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}]"

        # --- Attempt LLM-based summarization ---
        if self._llm_callback is not None:
            prompt = (
                "Summarize the following context into key facts, decisions, and "
                "important details. Preserve all role-relevant information, "
                "technical decisions, and action items. Output a concise summary "
                f"under {max_output_chars} characters.\n\n"
                f"{text}"
            )
            try:
                response = self._llm_callback(prompt)
                if response and isinstance(response, str) and len(response) <= max_output_chars:
                    logger.info(
                        "LLM summarization succeeded | original_len=%d summary_len=%d",
                        len(text), len(response),
                    )
                    return f"{timestamp_marker}\n\n{response}"
                else:
                    logger.warning(
                        "LLM summarization returned invalid response "
                        "(empty=%s, type=%s, len=%s); falling back to truncation",
                        not response,
                        type(response).__name__,
                        len(response) if isinstance(response, str) else "N/A",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM summarization failed with exception: %s; falling back to truncation",
                    exc,
                )

        # --- Fallback: keep the last max_output_chars with a prefix ---
        logger.debug(
            "Using truncation fallback for summarization | original_len=%d max_output_chars=%d",
            len(text), max_output_chars,
        )
        return f"{timestamp_marker}\n\n{text[-max_output_chars:]}"

    # ------------------------------------------------------------------
    # Memory Enhancement: Conversation Replay
    # ------------------------------------------------------------------

    def read_conversation_replay(self, channel_id: str, n_rounds: int = 5) -> list[dict]:
        """Read the most recent n_rounds of conversation from the message archive.

        A 'round' is defined as one user message + one agent response.
        Returns list of dicts with keys: sender_type, agent_name, content, timestamp.

        Uses tail-read (reverse scan from file end) to avoid loading the entire file.
        """
        import json

        path = self.message_archive_path(channel_id)
        target_lines = n_rounds * 2

        with self._lock:
            if not os.path.exists(path):
                return []
            try:
                lines = self._tail_read_lines(path, target_lines)
            except OSError:
                return []

        # Parse lines as JSON
        entries: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                entries.append({
                    "sender_type": record.get("sender_type", ""),
                    "agent_name": record.get("agent_name", ""),
                    "content": record.get("content", ""),
                    "timestamp": record.get("timestamp", 0.0),
                })
            except (json.JSONDecodeError, TypeError):
                continue

        return entries[-target_lines:] if len(entries) > target_lines else entries

    def _tail_read_lines(self, path: str, n: int, buf_size: int = 8192) -> list[str]:
        """Read the last n lines from a file using reverse seeking.

        Must be called while self._lock is held (or with external synchronization).
        """
        with open(path, "rb") as f:
            # Seek to end to get file size
            f.seek(0, 2)
            file_size = f.tell()

            if file_size == 0:
                return []

            lines: list[str] = []
            remaining = file_size

            while len(lines) <= n and remaining > 0:
                read_size = min(buf_size, remaining)
                remaining -= read_size
                f.seek(remaining)
                chunk = f.read(read_size).decode("utf-8", errors="replace")

                # Split and accumulate lines
                chunk_lines = chunk.split("\n")
                if lines:
                    # Merge the last piece of previous chunk with first of current
                    chunk_lines[-1] += lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            # Remove empty first element if file doesn't start mid-line
            if lines and not lines[0]:
                lines = lines[1:]

            # Remove trailing empty element from final newline
            if lines and not lines[-1]:
                lines = lines[:-1]

            # Return only last n lines
            return lines[-n:] if len(lines) > n else lines

    # ------------------------------------------------------------------
    # Memory Enhancement: Structured L2 Group Memory
    # ------------------------------------------------------------------

    # Standard L2 sections
    L2_SECTIONS: tuple[str, ...] = ("Decisions", "Blocking Issues", "Conventions")

    def read_group_memory_section(self, channel_id: str, section: str) -> str:
        """Read a specific section from L2 structured group memory."""
        with self._lock:
            path = self._group_memory_path(channel_id)
            content = self._read_text_unlocked(path)

        if not content:
            return ""

        # Parse the markdown, extract content under the given # Section header
        header_pattern = re.compile(r"^#\s+" + re.escape(section) + r"\s*$", re.MULTILINE)
        match = header_pattern.search(content)
        if not match:
            return ""

        # Find the start of content (after the header line)
        start = match.end()
        # Find the next top-level header or end of file
        next_header = re.search(r"^#\s+", content[start:], re.MULTILINE)
        if next_header:
            end = start + next_header.start()
        else:
            end = len(content)

        return content[start:end].strip()

    def append_group_memory_section(self, channel_id: str, section: str, entry: str) -> None:
        """Append an entry to a specific section in L2 structured group memory.

        If the section does not exist, it is created at the end of the file.
        """
        with self._lock:
            path = self._group_memory_path(channel_id)
            content = self._read_text_unlocked(path)

            header_pattern = re.compile(r"^#\s+" + re.escape(section) + r"\s*$", re.MULTILINE)
            match = header_pattern.search(content)

            if match:
                # Find the end of this section (next header or EOF)
                start = match.end()
                next_header = re.search(r"^#\s+", content[start:], re.MULTILINE)
                if next_header:
                    insert_pos = start + next_header.start()
                    # Insert the entry before the next header
                    new_content = (
                        content[:insert_pos].rstrip()
                        + f"\n{entry}\n\n"
                        + content[insert_pos:]
                    )
                else:
                    # Append at the end of file
                    new_content = content.rstrip() + f"\n{entry}\n"
            else:
                # Section does not exist — create it at the end
                new_content = content.rstrip() + f"\n\n# {section}\n{entry}\n"

            self._write_text_unlocked(path, new_content)

        logger.info(
            "L2 section appended | channel=%s section=%s entry_len=%d",
            channel_id, section, len(entry),
        )

    # ------------------------------------------------------------------
    # Memory Enhancement: Discussion Conclusion
    # ------------------------------------------------------------------

    def append_discussion_conclusion(
        self, channel_id: str, conclusion: str, *, section: str = "Decisions"
    ) -> None:
        """Write a discussion conclusion to the L2 structured memory.

        Appends the conclusion with a timestamp to the specified section.
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        formatted_entry = f"[{timestamp}] Discussion Conclusion: {conclusion}"
        self.append_group_memory_section(channel_id, section, formatted_entry)

    def sync_discussion_conclusion_to_agents(
        self, agent_ids: list[str], conclusion: str, *, trigger_reason: str = ""
    ) -> None:
        """Write discussion conclusion to L1 active_context of all participating agents.

        This creates a memory loop: discussion outcomes are remembered by each
        participant, influencing their future prompt construction.

        Args:
            agent_ids: List of participating agent IDs.
            conclusion: The discussion conclusion text.
            trigger_reason: Optional trigger reason for context.
        """
        import time as _time

        timestamp = _time.strftime("%Y-%m-%d %H:%M")
        for agent_id in agent_ids:
            context_entry = (
                f"[{timestamp}] Discussion conclusion"
                f"{' (' + trigger_reason + ')' if trigger_reason else ''}: "
                f"{conclusion[:500]}"
            )
            try:
                self.update_agent_context(agent_id, context_entry)
                logger.debug(
                    "Discussion conclusion synced to L1 | agent=%s entry_len=%d",
                    agent_id, len(context_entry),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to sync discussion conclusion to agent %s: %s",
                    agent_id, exc,
                )
