"""Three-layer memory management for Slock Engine.

L1: Agent private memory (MEMORY.md) — follows agent across groups
L2: Group shared memory (SHARED_MEMORY.md) — isolated per group
L3: Global knowledge base (WIKI.md) — accessible by all agents
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Optional

from .models import SkillProfile, SlockChannel, SlockMemory, SlockTask

logger = logging.getLogger(__name__)


def default_slock_storage_base() -> str:
    """Return the app-level Slock storage directory."""
    return os.path.expanduser("~/.ghostap/slock")


class _AuditLogWriter:
    """Async writer for global/AUDIT_LOG.md.

    append_audit_log must be cheap on the caller thread; this writer keeps the
    markdown table append on a single background consumer and drains on shutdown.
    """

    _HEADER = "| Timestamp | Operator | Action | Target | Detail |\n|---|---|---|---|---|\n"

    def __init__(self, base_path: str):
        self._base_path = base_path
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=1000)
        self._shutdown_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="slock-audit-log-writer")
        self._thread.start()

    def enqueue(self, row: str) -> None:
        self._queue.put(row, timeout=5)

    def shutdown(self) -> None:
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        self._queue.put(None, timeout=5)
        self._thread.join(timeout=10)

    def _audit_path(self) -> str:
        return os.path.join(self._base_path, "global", "AUDIT_LOG.md")

    def _run(self) -> None:
        while True:
            row = self._queue.get()
            try:
                if row is None:
                    return
                self._append_row(row)
            finally:
                self._queue.task_done()

    def _append_row(self, row: str) -> None:
        path = self._audit_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8") as f:
            if needs_header:
                f.write(self._HEADER)
            f.write(row)


class MemoryManager:
    """Manages the three-layer memory system for slock agents.

    All file operations are protected by a threading.Lock to prevent
    concurrent write corruption.
    """

    def __init__(self, base_path: str = ""):
        self._base_path = os.path.realpath(base_path or default_slock_storage_base())
        self._lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        self._agent_locks: dict[str, threading.RLock] = {}
        self._channel_locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        self._locks_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._llm_callback: Optional[Callable[[str], Optional[str]]] = None
        self._write_counts: dict[str, int] = {}
        self._file_lock_counts: dict[str, int] = {}
        self._audit_writer = _AuditLogWriter(self._base_path)
        self._memory_cache: dict[str, tuple[float, SlockMemory]] = {}
        self._restore_write_counts()

    @staticmethod
    def _sanitize_path_component(component: str) -> str:
        """Sanitize a path component to prevent directory traversal.

        Strips any characters that are not alphanumeric, underscore,
        colon, or hyphen. Dots are excluded to prevent '..' traversal.
        This matches the agent_id sanitization in models.py.
        """
        result = re.sub(r'[^A-Za-z0-9_:-]+', '_', component)
        if '..' in result or result.startswith('.'):
            raise ValueError(
                f"Path component '{component}' sanitizes to '{result}' "
                f"which contains '..' or starts with '.'"
            )
        return result

    def _safe_path(self, *parts: str) -> str:
        """Join path parts under base_path with traversal protection.

        Raises ValueError if the resolved path escapes base_path.
        """
        raw = os.path.join(self._base_path, *parts)
        resolved = os.path.realpath(raw)
        if not resolved.startswith(self._base_path + os.sep) and resolved != self._base_path:
            raise ValueError(
                f"Path traversal detected: component resolves to '{resolved}' "
                f"which is outside base '{self._base_path}'"
            )
        return resolved

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

    def _get_agent_lock(self, agent_id: str) -> threading.RLock:
        """Get or create a per-agent lock."""
        if agent_id in self._agent_locks:
            return self._agent_locks[agent_id]
        with self._locks_lock:
            if agent_id not in self._agent_locks:
                self._agent_locks[agent_id] = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
            return self._agent_locks[agent_id]

    def _get_channel_lock(self, channel_id: str) -> threading.RLock:
        """Get or create a per-channel lock."""
        if channel_id in self._channel_locks:
            return self._channel_locks[channel_id]
        with self._locks_lock:
            if channel_id not in self._channel_locks:
                self._channel_locks[channel_id] = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
            return self._channel_locks[channel_id]

    def shutdown(self) -> None:
        """Flush background writers. Safe to call more than once."""
        self._audit_writer.shutdown()

    def append_audit_log(self, *, operator_id: str, action: str, target: str, detail: str) -> None:
        """Append one audit row asynchronously to global/AUDIT_LOG.md."""
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def cell(value: str) -> str:
            return str(value).replace("|", "\\|").replace("\n", " ").strip()

        self._audit_writer.enqueue(
            f"| {cell(timestamp)} | {cell(operator_id)} | {cell(action)} | "
            f"{cell(target)} | {cell(detail)} |\n"
        )

    def _version_path(self, agent_id: str) -> str:
        safe_id = self._sanitize_path_component(agent_id)
        return self._safe_path("agents", safe_id, ".version")

    def _read_version_file(self, agent_id: str) -> int:
        path = self._version_path(agent_id)
        try:
            if not os.path.exists(path):
                return 0
            with open(path, "r", encoding="utf-8") as f:
                return max(0, int((f.read() or "0").strip()))
        except (OSError, ValueError):
            return 0

    def _read_embedded_version(self, agent_id: str) -> int:
        path = self._agent_memory_path(agent_id)
        try:
            if not os.path.exists(path):
                return 0
            with open(path, "r", encoding="utf-8") as f:
                return SlockMemory.from_markdown(f.read())._version
        except OSError:
            return 0

    def _refresh_write_count(self, agent_id: str) -> int:
        """Refresh in-memory OCC counter from disk using max(.version, embedded)."""
        version = max(self._read_version_file(agent_id), self._read_embedded_version(agent_id))
        self._write_counts[agent_id] = version
        return version

    def _restore_write_counts(self) -> None:
        agents_dir = self._safe_path("agents")
        if not os.path.isdir(agents_dir):
            return
        for agent_id in os.listdir(agents_dir):
            if os.path.isdir(os.path.join(agents_dir, agent_id)):
                self._refresh_write_count(agent_id)

    @contextmanager
    def _agent_file_lock(self, agent_id: str):
        """Cross-process advisory lock for one agent memory file."""
        import fcntl

        if self._file_lock_counts.get(agent_id, 0) > 0:
            self._file_lock_counts[agent_id] += 1
            try:
                yield
            finally:
                self._file_lock_counts[agent_id] -= 1
            return

        lock_path = self._safe_path("agents", self._sanitize_path_component(agent_id), ".lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._file_lock_counts[agent_id] = 1
            try:
                yield
            finally:
                self._file_lock_counts[agent_id] = 0
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # L1: Agent Private Memory
    # ------------------------------------------------------------------

    def _agent_memory_path(self, agent_id: str) -> str:
        safe_id = self._sanitize_path_component(agent_id)
        return self._safe_path("agents", safe_id, "MEMORY.md")

    def agent_memory_path(self, agent_id: str) -> str:
        """Return the canonical L1 memory path for an agent."""
        return self._agent_memory_path(agent_id)

    def agent_notes_path(self, agent_id: str) -> str:
        """Return the canonical notes path for an agent."""
        safe_id = self._sanitize_path_component(agent_id)
        return self._safe_path("agents", safe_id, "NOTES.md")

    def agent_workspace_path(self, agent_id: str) -> str:
        """Return the canonical workspace path for an agent."""
        safe_id = self._sanitize_path_component(agent_id)
        return self._safe_path("agents", safe_id, "workspace")

    def initialize_agent_workspace(self, agent_id: str) -> dict[str, str]:
        """Create per-agent MEMORY/NOTES/workspace directories from the Slock spec."""
        memory_path = self.agent_memory_path(agent_id)
        notes_path = self.agent_notes_path(agent_id)
        workspace_path = self.agent_workspace_path(agent_id)
        with self._get_agent_lock(agent_id):
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)
            os.makedirs(os.path.join(workspace_path, "current-task"), exist_ok=True)
            os.makedirs(os.path.join(workspace_path, "history"), exist_ok=True)
            safe_id = self._sanitize_path_component(agent_id)
            os.makedirs(self._safe_path("agents", safe_id, "reasoning"), exist_ok=True)
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
        with self._get_agent_lock(agent_id):
            return self._read_agent_memory_unlocked(agent_id)

    def _read_agent_memory_unlocked(self, agent_id: str) -> SlockMemory:
        path = self._agent_memory_path(agent_id)
        if not os.path.exists(path):
            return SlockMemory()
        mtime = os.path.getmtime(path)
        cached = self._memory_cache.get(agent_id)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        memory = SlockMemory.from_markdown(content)
        self._memory_cache[agent_id] = (mtime, memory)
        return memory

    def write_agent_memory(self, agent_id: str, memory: SlockMemory) -> None:
        """Write L1 agent private memory."""
        with self._get_agent_lock(agent_id):
            self._write_agent_memory_unlocked(agent_id, memory)
            self._enforce_l1_capacity(agent_id)

    def _write_agent_memory_async(self, agent_id: str, memory: SlockMemory) -> None:
        """Write L1 memory on a daemon thread using the same OCC merge path."""
        expected_version = self._write_counts.get(agent_id, 0)

        def _do_write() -> None:
            try:
                memory._version = expected_version
                self.write_agent_memory(agent_id, memory)
            except Exception as exc:
                logger.warning("Async memory write failed for %s: %s", agent_id, str(exc))

        thread = threading.Thread(
            target=_do_write,
            name=f"slock-memory-write-{agent_id[:12]}",
            daemon=True,
        )
        thread.start()

    def _write_agent_memory_unlocked(self, agent_id: str, memory: SlockMemory) -> None:
        with self._agent_file_lock(agent_id):
            current_version = self._refresh_write_count(agent_id)
            current = self._read_agent_memory_unlocked(agent_id)
            if memory._version and memory._version < current_version:
                memory = self._merge_agent_memory(current, memory)
            next_version = current_version + 1
            memory._version = next_version
            path = self._agent_memory_path(agent_id)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            content = memory.to_markdown()
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
            version_path = self._version_path(agent_id)
            with open(version_path + ".tmp", "w", encoding="utf-8") as f:
                f.write(str(next_version))
            os.replace(version_path + ".tmp", version_path)
            self._write_counts[agent_id] = next_version

    def _merge_agent_memory(self, current: SlockMemory, incoming: SlockMemory) -> SlockMemory:
        """Merge stale incoming writes with current disk state."""

        def merge_lines(existing: str, new: str) -> str:
            result = list(existing.splitlines()) if existing else []
            seen = set(result)
            for line in (new or "").splitlines():
                if line not in seen:
                    result.append(line)
                    seen.add(line)
            return "\n".join(result)

        role = incoming.role or current.role
        key_knowledge = merge_lines(current.key_knowledge, incoming.key_knowledge)
        if incoming.active_context and incoming.active_context != current.active_context:
            if incoming.active_context.startswith(current.active_context):
                delta = incoming.active_context[len(current.active_context):].strip()
            elif current.active_context.startswith(incoming.active_context):
                delta = ""
            else:
                delta = incoming.active_context
            active_context = current.active_context
            if delta:
                active_context = f"{current.active_context}\n\n## Recent Updates\n{delta}".strip()
        else:
            active_context = current.active_context or incoming.active_context
        archived_context = merge_lines(current.archived_context, incoming.archived_context)
        return SlockMemory(
            role=role,
            key_knowledge=key_knowledge,
            active_context=active_context,
            archived_context=archived_context,
            _version=current._version,
        )

    def update_agent_context(self, agent_id: str, context_update: str) -> None:
        """Append to the active context section of L1 memory."""
        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            if memory.active_context:
                memory.active_context += f"\n{context_update}"
            else:
                memory.active_context = context_update
            self._write_agent_memory_unlocked(agent_id, memory)
            self._enforce_l1_capacity(agent_id)

    def redact_active_context_for_move(
        self, agent_id: str, source_channel_id: str, target_channel_id: str
    ) -> None:
        """Redact active_context during cross-group move to prevent source-group history leakage.

        Preserves role and key_knowledge unchanged. Replaces active_context with
        a single migration record line.  This is an irreversible operation —
        source-group conversation history is permanently removed from the L1 file.
        """
        with self._get_agent_lock(agent_id):
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
        safe_id = self._sanitize_path_component(agent_id)
        return self._safe_path("agents", safe_id, "skill_profile.json")

    def skill_profile_path(self, agent_id: str) -> str:
        """Return the canonical skill profile path for an agent."""
        return self._skill_profile_path(agent_id)

    def read_skill_profiles(self, agent_id: str) -> list[SkillProfile]:
        """Read persisted skill profiles for an agent."""
        import json

        path = self._skill_profile_path(agent_id)
        with self._get_agent_lock(agent_id):
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return [SkillProfile.from_dict(item) for item in data if isinstance(item, dict)]

    def write_skill_profiles(self, agent_id: str, profiles: list[SkillProfile]) -> None:
        """Persist skill profiles for an agent."""
        import json

        path = self._skill_profile_path(agent_id)
        with self._get_agent_lock(agent_id):
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

    def evolve_agent_role(self, agent_id: str, agent_identity, *, task_threshold: int = 3) -> bool:
        """Evolve the agent's role description based on accumulated experience.

        Updates memory.role with a synthesized identity from skill profiles and
        task history. Only triggers after every `task_threshold` tasks.

        Returns True if role was updated.
        """
        profiles = self.read_skill_profiles(agent_id)
        if not profiles:
            return False

        total_tasks = sum(p.total_tasks for p in profiles)
        # Only evolve every N tasks
        if total_tasks < task_threshold or total_tasks % task_threshold != 0:
            return False

        # Build evolved role description from top skills
        top_skills = sorted(profiles, key=lambda p: p.total_tasks, reverse=True)[:5]
        skill_lines = [f"- {p.tag} ({p.total_tasks} tasks, {p.success_rate:.0f}% quality)" for p in top_skills]

        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            # Preserve any user-authored role prefix, append evolved section
            base_role = agent_identity.role if hasattr(agent_identity, 'role') else "custom"
            traits = ", ".join(agent_identity.personality_traits) if hasattr(agent_identity, 'personality_traits') and agent_identity.personality_traits else ""

            evolved_role = (
                f"Base: {base_role}"
                + (f" | Traits: {traits}" if traits else "")
                + f"\n\nEvolved expertise (auto-derived from {total_tasks} completed tasks):\n"
                + "\n".join(skill_lines)
            )
            memory.role = evolved_role
            self._write_agent_memory_unlocked(agent_id, memory)

        logger.info(
            "Agent role evolved | agent=%s total_tasks=%d top_skills=%s",
            agent_id, total_tasks, [p.tag for p in top_skills[:3]],
        )
        return True

    def _reasoning_snapshot_path(self, agent_id: str, task_id: str) -> str:
        safe_id = self._sanitize_path_component(agent_id)
        safe_task_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", task_id or "message").strip("_") or "message"
        return self._safe_path("agents", safe_id, "reasoning", f"{safe_task_id}.json")

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
        with self._get_agent_lock(agent_id):
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
        with self._get_agent_lock(agent_id):
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # L2: Group Shared Memory
    # ------------------------------------------------------------------

    def _group_memory_path(self, channel_id: str) -> str:
        safe_id = self._sanitize_path_component(channel_id)
        return self._safe_path("groups", safe_id, "SHARED_MEMORY.md")

    def group_memory_path(self, channel_id: str) -> str:
        """Return the canonical L2 shared memory path for a group."""
        return self._group_memory_path(channel_id)

    def read_group_memory(self, channel_id: str) -> str:
        """Read L2 group shared memory."""
        path = self._group_memory_path(channel_id)
        with self._get_channel_lock(channel_id):
            return self._read_text_unlocked(path)

    def write_group_memory(self, channel_id: str, content: str) -> None:
        """Write L2 group shared memory."""
        content = self._enforce_text_capacity(content, self._get_l2_max_size(), "L2")
        path = self._group_memory_path(channel_id)
        with self._get_channel_lock(channel_id):
            self._write_text_unlocked(path, content)

    def append_group_memory(self, channel_id: str, entry: str) -> None:
        """Append to L2 group shared memory."""
        path = self._group_memory_path(channel_id)
        with self._get_channel_lock(channel_id):
            current = self._read_text_unlocked(path)
            content = f"{current}\n{entry}" if current else entry
            content = self._enforce_text_capacity(content, self._get_l2_max_size(), "L2")
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
        with self._global_lock:
            return self._read_text_unlocked(path)

    def write_global_wiki(self, content: str) -> None:
        """Write L3 global knowledge base."""
        content = self._enforce_text_capacity(content, self._get_l3_max_size(), "L3")
        path = self._global_wiki_path()
        with self._global_lock:
            self._write_text_unlocked(path, content)

    def append_global_wiki(self, entry: str) -> None:
        """Append to L3 global knowledge base."""
        path = self._global_wiki_path()
        with self._global_lock:
            current = self._read_text_unlocked(path)
            content = f"{current}\n{entry}" if current else entry
            content = self._enforce_text_capacity(content, self._get_l3_max_size(), "L3")
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
    # Memory Capacity Management (B026)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_l1_max_size() -> int:
        from ..config import get_settings
        return get_settings().slock_l1_max_size

    @staticmethod
    def _get_l2_max_size() -> int:
        from ..config import get_settings
        return get_settings().slock_l2_max_size

    @staticmethod
    def _get_l3_max_size() -> int:
        from ..config import get_settings
        return get_settings().slock_l3_max_size

    def _enforce_l1_capacity(self, agent_id: str) -> None:
        """Enforce L1 memory capacity for an agent.

        Must be called while the per-agent RLock is held by write paths; this
        method may re-enter that lock while calibrating and truncating.

        Strategy:
        1. Check if the serialized memory exceeds slock_l1_max_size.
        2. If over, attempt LLM summarization via summarize_context().
        3. If still over after summarization (or summarize returned False),
           fall back to FIFO truncation of active_context.
        """
        max_size = self._get_l1_max_size()
        target_size = int(max_size * 0.7)
        path = self._agent_memory_path(agent_id)

        try:
            file_size = os.path.getsize(path)
        except OSError:
            return
        if file_size < max_size:
            return

        # Phase 1: check size
        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            content_size = len(memory.to_markdown().encode("utf-8"))
        if content_size <= max_size:
            return

        logger.info(
            "L1 capacity exceeded | agent=%s size=%d max=%d, attempting summarization",
            agent_id, content_size, max_size,
        )

        # Phase 2: attempt summarization (re-enters the same RLock)
        try:
            self.summarize_context(agent_id, threshold=0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "L1 summarize_context failed for agent=%s: %s; falling back to FIFO truncation",
                agent_id, exc,
            )

        # Phase 3: re-check and FIFO truncate if still over target
        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            content_size = len(memory.to_markdown().encode("utf-8"))
            if content_size <= target_size:
                return

            role, key_knowledge = self._preserve_critical_sections(memory)
            preserved = SlockMemory(role=role, key_knowledge=key_knowledge)
            preserved_overhead = len(preserved.to_markdown().encode("utf-8"))
            if preserved_overhead >= target_size:
                memory = preserved
            else:
                budget = max(0, target_size - preserved_overhead - 128)
                memory.role = role
                memory.key_knowledge = key_knowledge
                memory.archived_context = ""
                memory.active_context = self._tail_text_bytes(memory.active_context, budget)
            self._write_agent_memory_unlocked(agent_id, memory)

        logger.info(
            "L1 FIFO truncation applied | agent=%s new_context_len=%d",
            agent_id, len(memory.active_context),
        )

    def _tail_text_bytes(self, text: str, max_bytes: int) -> str:
        if max_bytes <= 0 or not text:
            return ""
        data = text.encode("utf-8")
        if len(data) <= max_bytes:
            return text
        return data[-max_bytes:].decode("utf-8", errors="ignore").lstrip()

    def _parse_memory_sections(self, markdown: str) -> dict[str, str]:
        """Parse SlockMemory markdown sections with tolerant header matching."""
        sections = {
            "role": "",
            "key_knowledge": "",
            "active_context": "",
            "archived_context": "",
        }
        aliases = {
            "role": "role",
            "key knowledge": "key_knowledge",
            "active context": "active_context",
            "archived context": "archived_context",
        }
        current = ""
        lines: list[str] = []
        for line in (markdown or "").splitlines():
            if line.startswith("#"):
                normalized = " ".join(line.lstrip("#").strip().lower().split())
                if normalized in aliases:
                    if current:
                        sections[current] = "\n".join(lines).strip()
                    current = aliases[normalized]
                    lines = []
                    continue
                if current:
                    sections[current] = "\n".join(lines).strip()
                current = ""
                lines = []
                continue
            if current:
                lines.append(line)
        if current:
            sections[current] = "\n".join(lines).strip()
        return sections

    def _preserve_critical_sections(self, memory: SlockMemory) -> tuple[str, str]:
        """Preserve role and the most recent key knowledge facts."""
        kk_lines = [line for line in (memory.key_knowledge or "").splitlines() if line.strip()]
        return memory.role.strip(), "\n".join(kk_lines[-3:]).strip()

    def _enforce_text_capacity(self, content: str, max_size: int, layer_label: str) -> str:
        """Enforce capacity on plain-text memory content (L2/L3).

        If content byte size exceeds max_size, FIFO truncate by keeping
        the tail portion that fits within max_size.

        Args:
            content: The text content to check.
            max_size: Maximum allowed size in bytes.
            layer_label: Label for logging (e.g. "L2", "L3").

        Returns:
            The content, possibly truncated to fit within max_size.
        """
        content_bytes = content.encode("utf-8")
        if len(content_bytes) <= max_size:
            return content

        logger.info(
            "%s capacity exceeded | size=%d max=%d, applying FIFO truncation",
            layer_label, len(content_bytes), max_size,
        )

        # Keep the tail that fits within max_size
        truncated_bytes = content_bytes[-max_size:]
        # Decode safely; skip leading partial UTF-8 character
        truncated = truncated_bytes.decode("utf-8", errors="ignore")

        logger.info(
            "%s FIFO truncation complete | original_size=%d new_size=%d",
            layer_label, len(content_bytes), len(truncated.encode("utf-8")),
        )
        return truncated

    # ------------------------------------------------------------------
    # Isolation verification
    # ------------------------------------------------------------------

    def get_group_base_path(self, channel_id: str) -> str:
        """Return the base directory for a group's memory — useful for isolation checks."""
        safe_id = self._sanitize_path_component(channel_id)
        return self._safe_path("groups", safe_id)

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
        with self._get_channel_lock(channel.channel_id):
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
        with self._get_channel_lock(channel_id):
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
        with self._get_channel_lock(channel_id):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"tasks": [task.to_dict() for task in tasks]}, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    # ------------------------------------------------------------------
    # Collaboration Plan Persistence
    # ------------------------------------------------------------------

    def _plans_path(self, channel_id: str) -> str:
        """Return the persisted plans JSON path for a channel."""
        return os.path.join(self.team_workspace_path(channel_id), ".plans.json")

    def write_plans(self, channel_id: str, plans: list) -> None:
        """Persist active collaboration plans for a channel (atomic write)."""
        import json


        path = self._plans_path(channel_id)
        with self._get_channel_lock(channel_id):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"plans": [p.to_dict() for p in plans]},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp_path, path)

    def read_plans(self, channel_id: str) -> list:
        """Read persisted collaboration plans for a channel."""
        import json

        from .models import CollaborationPlan

        path = self._plans_path(channel_id)
        with self._get_channel_lock(channel_id):
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        items = data.get("plans", []) if isinstance(data, dict) else []
        return [CollaborationPlan.from_dict(item) for item in items if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # Discussion Persistence (Task 14)
    # ------------------------------------------------------------------

    def _discussions_path(self, channel_id: str) -> str:
        """Return the persisted discussions JSON path for a channel."""
        return os.path.join(self.team_workspace_path(channel_id), ".discussions.json")

    def write_discussions(self, channel_id: str, discussions: list[dict]) -> None:
        """Persist active discussion threads for a channel."""
        import json

        path = self._discussions_path(channel_id)
        with self._get_channel_lock(channel_id):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(discussions, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)

    def read_discussions(self, channel_id: str) -> list[dict]:
        """Read persisted discussion threads for a channel."""
        import json

        path = self._discussions_path(channel_id)
        with self._get_channel_lock(channel_id):
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Message Archive
    # ------------------------------------------------------------------

    def message_archive_path(self, channel_id: str) -> str:
        """Return the JSONL archive path for a channel's Slock messages."""
        safe_id = self._sanitize_path_component(channel_id)
        return self._safe_path("archives", safe_id, "messages.jsonl")

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
        with self._get_channel_lock(channel_id):
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
                safe_agent = self._sanitize_path_component(agent_id)
                os.makedirs(self._safe_path("agents", safe_agent), exist_ok=True)
            if channel_id:
                safe_channel = self._sanitize_path_component(channel_id)
                os.makedirs(self._safe_path("groups", safe_channel, "tasks"), exist_ok=True)
            os.makedirs(os.path.join(self._base_path, "global"), exist_ok=True)

    # ------------------------------------------------------------------
    # Freshness Gate: message counting and retrieval since a timestamp
    # ------------------------------------------------------------------

    def count_messages_since(
        self,
        channel_id: str,
        since_ts: float,
        *,
        exclude_agent_id: str = "",
    ) -> int:
        """Count messages in the channel archive that arrived after since_ts.

        If exclude_agent_id is given, messages from that agent are not counted
        (an agent's own outgoing messages should not trigger freshness failure).
        """
        import json

        path = self.message_archive_path(channel_id)
        with self._get_channel_lock(channel_id):
            if not os.path.exists(path):
                return 0
            try:
                lines = self._tail_read_lines(path, 50)
            except OSError:
                return 0

        count = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            ts = record.get("timestamp", 0.0)
            if ts <= since_ts:
                continue
            if exclude_agent_id and record.get("agent_id") == exclude_agent_id:
                continue
            count += 1
        return count

    def get_messages_since(
        self,
        channel_id: str,
        since_ts: float,
        *,
        exclude_agent_id: str = "",
        limit: int = 5,
    ) -> list[dict]:
        """Return messages that arrived after since_ts (newest last, up to limit).

        Used by Freshness Gate to provide new context to the agent for draft re-evaluation.
        """
        import json

        path = self.message_archive_path(channel_id)
        with self._get_channel_lock(channel_id):
            if not os.path.exists(path):
                return []
            try:
                lines = self._tail_read_lines(path, 50)
            except OSError:
                return []

        results: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            ts = record.get("timestamp", 0.0)
            if ts <= since_ts:
                continue
            if exclude_agent_id and record.get("agent_id") == exclude_agent_id:
                continue
            results.append({
                "sender_type": record.get("sender_type", ""),
                "agent_name": record.get("agent_name", ""),
                "content": record.get("content", ""),
                "timestamp": ts,
            })
        return results[-limit:]

    # ------------------------------------------------------------------
    # Behavior Self-Convergence: track agent failure patterns
    # ------------------------------------------------------------------

    def record_task_outcome(
        self,
        agent_id: str,
        skill_tag: str,
        success: bool,
    ) -> None:
        """Record a task outcome for behavior convergence tracking.

        Stores recent outcomes per agent+skill in a lightweight JSONL file.
        When consecutive failures reach threshold, writes avoidance strategy
        into L1 memory.
        """
        import json

        safe_agent = self._sanitize_path_component(agent_id)
        outcomes_path = self._safe_path("agents", safe_agent, "outcomes.jsonl")
        record = {
            "timestamp": time.time(),
            "skill_tag": skill_tag,
            "success": success,
        }
        with self._get_agent_lock(agent_id):
            os.makedirs(os.path.dirname(outcomes_path), exist_ok=True)
            with open(outcomes_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_consecutive_failures(self, agent_id: str, skill_tag: str) -> int:
        """Count consecutive recent failures for a specific skill tag."""
        import json

        safe_agent = self._sanitize_path_component(agent_id)
        outcomes_path = self._safe_path("agents", safe_agent, "outcomes.jsonl")
        with self._get_agent_lock(agent_id):
            if not os.path.exists(outcomes_path):
                return 0
            try:
                lines = self._tail_read_lines(outcomes_path, 20)
            except OSError:
                return 0

        consecutive = 0
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if record.get("skill_tag") != skill_tag:
                continue
            if record.get("success"):
                break
            consecutive += 1
        return consecutive

    def _count_knowledge_references(self, knowledge_line: str, channel_id: str, exclude_agent_id: str) -> int:
        """Check how many other agents in the channel have matching knowledge.

        Scans all agent L1 memories in the agents directory and counts those
        whose key_knowledge contains the same line (stripped comparison).
        """
        agents_dir = self._safe_path("agents")
        if not os.path.isdir(agents_dir):
            return 0

        target = knowledge_line.strip()
        count = 0
        for entry in os.listdir(agents_dir):
            if entry == self._sanitize_path_component(exclude_agent_id):
                continue
            agent_dir = os.path.join(agents_dir, entry)
            if not os.path.isdir(agent_dir):
                continue
            memory_path = os.path.join(agent_dir, "MEMORY.md")
            if not os.path.exists(memory_path):
                continue
            try:
                with open(memory_path, "r", encoding="utf-8") as f:
                    content = f.read()
                mem = SlockMemory.from_markdown(content)
                for line in (mem.key_knowledge or "").splitlines():
                    if line.strip() == target:
                        count += 1
                        break
            except (OSError, ValueError):
                continue
        return count

    def check_and_promote_knowledge(self, agent_id: str, channel_id: str) -> None:
        """Promote L1 knowledge entries to L2 group shared memory when widespread.

        Reads the agent's L1 key_knowledge, and for each entry (lines starting
        with "- "), checks how many other agents have the same knowledge. If 3+
        agents share the entry, it is promoted to L2 group shared memory.
        """
        memory = self.read_agent_memory(agent_id)
        if not memory.key_knowledge:
            return

        existing_group_memory = self.read_group_memory(channel_id)

        for line in memory.key_knowledge.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            # Skip if already present in L2
            if stripped in existing_group_memory:
                continue
            ref_count = self._count_knowledge_references(stripped, channel_id, agent_id)
            if ref_count >= 2:  # 2 others + self = 3+ total
                timestamp = time.strftime("%Y-%m-%d %H:%M")
                promoted_entry = f"[{timestamp}] [Promoted] {stripped}"
                self.append_group_memory(channel_id, promoted_entry)
                logger.info(
                    "Knowledge promoted L1→L2 | agent=%s channel=%s refs=%d entry=%s",
                    agent_id, channel_id, ref_count + 1, stripped[:80],
                )

    def write_avoidance_strategy(self, agent_id: str, skill_tag: str, reason: str) -> None:
        """Write an avoidance strategy into agent L1 memory with expiry tracking.

        Avoidance strategies are not permanent — they expire after N consecutive
        successes in the avoided skill area, reflecting agent growth.
        """
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        entry = (
            f"\n## Behavioral Note (expires after 3 consecutive successes)\n"
            f"- Skill `{skill_tag}`: 连续失败多次，暂时回避此类任务或降低自评分。"
            f" 原因: {reason[:200]}\n"
            f"- 记录日期: {now_str} | 成功计数: 0/3 (达到3次自动解除)\n"
        )
        self.update_agent_context(agent_id, entry)

    def clear_expired_avoidance(self, agent_id: str, skill_tag: str) -> bool:
        """Clear an avoidance strategy after consecutive successes.

        Returns True if an avoidance entry was found and cleared.
        """
        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            if f"Skill `{skill_tag}`" in memory.active_context and "暂时回避" in memory.active_context:
                # Remove the avoidance note
                lines = memory.active_context.split("\n")
                cleaned_lines = []
                skip_block = False
                for line in lines:
                    if f"Skill `{skill_tag}`" in line and "暂时回避" in line:
                        skip_block = True
                        continue
                    if skip_block and (line.startswith("- 记录日期") or line.strip() == ""):
                        skip_block = False
                        continue
                    skip_block = False
                    cleaned_lines.append(line)
                memory.active_context = "\n".join(cleaned_lines)
                self._write_agent_memory_unlocked(agent_id, memory)
                logger.info(
                    "Avoidance strategy cleared: agent=%s skill=%s (consecutive successes)",
                    agent_id, skill_tag,
                )
                return True
        return False

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
        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            if len(memory.active_context) <= threshold:
                return False

            original_len = len(memory.active_context)
            text_to_summarize = memory.active_context
            original_version = memory._version

            # Create backup: copy MEMORY.md to MEMORY.md.bak (atomic)
            memory_path = self._agent_memory_path(agent_id)
            backup_path = memory_path + ".bak"
            if os.path.exists(memory_path):
                shutil.copy2(memory_path, backup_path)

        # Phase 2: LLM summarization OUTSIDE lock (may be slow)
        compressed = self._summarize_text(text_to_summarize)

        # Phase 3: write back under lock
        with self._get_agent_lock(agent_id):
            memory = self._read_agent_memory_unlocked(agent_id)
            if memory._version > original_version and memory.active_context != text_to_summarize:
                delta = ""
                if memory.active_context.startswith(text_to_summarize):
                    delta = memory.active_context[len(text_to_summarize):].strip()
                elif text_to_summarize not in memory.active_context:
                    delta = memory.active_context.strip()
                if delta:
                    compressed = f"{compressed}\n\n## Recent Updates\n{delta}"
            memory.active_context = compressed
            self._write_agent_memory_unlocked(agent_id, memory)

        logger.info(
            "L1 active_context summarized | agent=%s original_len=%d new_len=%d threshold=%d",
            agent_id, original_len, len(compressed), threshold,
        )
        return True

    def _summarize_with_preservation(self, memory: SlockMemory, max_output_chars: int) -> SlockMemory:
        """Return a summarized copy preserving critical role/knowledge fields."""
        role, key_knowledge = self._preserve_critical_sections(memory)
        return SlockMemory(
            role=role,
            key_knowledge=key_knowledge,
            active_context=self._summarize_text(memory.active_context, max_output_chars=max_output_chars),
            archived_context="",
            _version=memory._version,
        )

    def _summarize_text(self, text: str, *, max_output_chars: int = 1500) -> str:
        """Compress text using LLM summarization.

        If an LLM callback has been registered via `set_llm_callback`, it is
        invoked to produce a high-quality summary.  On any failure the method
        falls back to simple tail-truncation.

        [RATIONALE] marked paragraphs are prioritized for preservation in both
        the LLM prompt and the truncation fallback.
        """
        from datetime import datetime, timezone

        if len(text) <= max_output_chars:
            return text

        timestamp_marker = f"[Context summarized at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}]"

        # Extract and preserve [RATIONALE] sections
        rationale_sections: list[str] = []
        remaining_lines: list[str] = []
        in_rationale = False
        rationale_buffer: list[str] = []

        for line in text.split("\n"):
            if line.strip().startswith("[RATIONALE]"):
                in_rationale = True
                rationale_buffer = [line]
            elif in_rationale:
                # Rationale ends at blank line or next [RATIONALE]
                if line.strip() == "" or line.strip().startswith("[RATIONALE]"):
                    if rationale_buffer:
                        rationale_sections.append("\n".join(rationale_buffer))
                    rationale_buffer = []
                    in_rationale = False
                    if line.strip().startswith("[RATIONALE]"):
                        in_rationale = True
                        rationale_buffer = [line]
                    else:
                        remaining_lines.append(line)
                else:
                    rationale_buffer.append(line)
            else:
                remaining_lines.append(line)

        # Flush any remaining rationale
        if rationale_buffer:
            rationale_sections.append("\n".join(rationale_buffer))

        rationale_text = "\n\n".join(rationale_sections)
        remaining_text = "\n".join(remaining_lines)
        rationale_len = len(rationale_text)

        # --- Attempt LLM-based summarization ---
        if self._llm_callback is not None:
            # Build prompt with explicit instruction to preserve rationale
            rationale_instruction = (
                f"CRITICAL: The following sections marked [RATIONALE] MUST be preserved "
                f"VERBATIM in the output (do not summarize or rephrase them):\n\n"
                f"{rationale_text}\n\n"
                if rationale_sections else ""
            )
            prompt = (
                f"{rationale_instruction}"
                "Summarize the following context into key facts, decisions, and "
                "important details. Preserve all role-relevant information, "
                "technical decisions, and action items. Output a concise summary "
                f"under {max_output_chars} characters.\n\n"
                f"{remaining_text}"
            )
            try:
                response_box: list[object] = []
                error_box: list[BaseException] = []

                def run_callback() -> None:
                    try:
                        response_box.append(self._llm_callback(prompt))
                    except BaseException as exc:  # noqa: BLE001
                        error_box.append(exc)

                worker = threading.Thread(target=run_callback, daemon=True)
                worker.start()
                from ..config import get_settings as _get_settings
                summarize_timeout = getattr(_get_settings(), "slock_memory_summarize_timeout", 30.0)
                worker.join(timeout=summarize_timeout)
                if worker.is_alive():
                    logger.warning("LLM summarization timed out; falling back to truncation")
                    response = None
                elif error_box:
                    raise error_box[0]
                else:
                    response = response_box[0] if response_box else None
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

        # --- Fallback: keep rationale first, then tail of remaining ---
        logger.debug(
            "Using truncation fallback for summarization | original_len=%d max_output_chars=%d rationale_len=%d",
            len(text), max_output_chars, rationale_len,
        )

        # If no rationale sections, use original behavior: simple tail truncation
        if rationale_len == 0:
            return f"{timestamp_marker}\n\n{text[-max_output_chars:]}"

        # Reserve space for timestamp + rationale; fill rest with tail of remaining text
        timestamp_overhead = len(timestamp_marker) + 2  # +2 for "\n\n"
        available_for_content = max_output_chars - timestamp_overhead

        if rationale_len >= available_for_content:
            # Rationale alone exceeds budget; truncate rationale itself
            preserved = rationale_text[:available_for_content]
        else:
            remaining_budget = available_for_content - rationale_len - 2  # -2 for "\n\n"
            tail = remaining_text[-remaining_budget:] if remaining_budget > 0 else ""
            preserved = f"{rationale_text}\n\n{tail}" if tail else rationale_text

        return f"{timestamp_marker}\n\n{preserved}"

    def get_agent_memory_summary(self, agent_ids: list[str], registry=None) -> list[dict]:
        """Return compact memory summaries for UI/status surfaces."""
        summaries: list[dict] = []
        for agent_id in agent_ids:
            try:
                summaries.append(self._get_single_agent_summary(agent_id, registry=registry))
            except Exception as exc:  # noqa: BLE001
                summaries.append({
                    "agent_id": agent_id,
                    "agent_name": agent_id,
                    "role_preview": "",
                    "key_knowledge_len": 0,
                    "active_context_len": 0,
                    "archived_context_len": 0,
                    "last_updated": "",
                    "version": 0,
                    "error": str(exc),
                })
        return summaries

    def _get_single_agent_summary(self, agent_id: str, registry=None) -> dict:
        memory = self.read_agent_memory(agent_id)
        path = self._agent_memory_path(agent_id)
        last_updated = ""
        if os.path.exists(path):
            last_updated = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
        role_preview = memory.role
        if len(role_preview) > 100:
            role_preview = role_preview[:100] + "..."
        return {
            "agent_id": agent_id,
            "agent_name": self._lookup_agent_name(agent_id, registry=registry),
            "role_preview": role_preview,
            "key_knowledge_len": len(memory.key_knowledge),
            "active_context_len": len(memory.active_context),
            "archived_context_len": len(memory.archived_context),
            "last_updated": last_updated,
            "version": memory._version,
        }

    def _lookup_agent_name(self, agent_id: str, registry=None) -> str:
        if registry is not None:
            try:
                agent = registry.get(agent_id)
                name = getattr(agent, "name", "") if agent is not None else ""
                if name:
                    return name
            except Exception:
                pass
        import json

        identity_path = self._safe_path("agents", self._sanitize_path_component(agent_id), "identity.json")
        try:
            if os.path.exists(identity_path):
                with open(identity_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("name", "") if isinstance(data, dict) else ""
                if name:
                    return name
        except (OSError, ValueError, TypeError):
            pass
        return agent_id

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
        self, agent_ids: list[str], conclusion: str, *, trigger_reason: str = "", rationale: str = ""
    ) -> None:
        """Write discussion conclusion to L1 active_context of all participating agents.

        This creates a memory loop: discussion outcomes are remembered by each
        participant, influencing their future prompt construction.

        Args:
            agent_ids: List of participating agent IDs.
            conclusion: The discussion conclusion text.
            trigger_reason: Optional trigger reason for context.
            rationale: Optional rationale explaining why this conclusion was reached.
                Marked with [RATIONALE] for semantic retention during summarization.
        """
        import time as _time

        timestamp = _time.strftime("%Y-%m-%d %H:%M")
        for agent_id in agent_ids:
            decision_entry = (
                f"[{timestamp}] [DECISION]"
                f"{' (' + trigger_reason + ')' if trigger_reason else ''}: "
                f"{conclusion[:500]}"
            )
            context_parts = [
                f"[{timestamp}] Discussion conclusion"
                f"{' (' + trigger_reason + ')' if trigger_reason else ''}: "
                f"{conclusion[:500]}"
            ]
            if rationale:
                context_parts.append(f"[RATIONALE] {rationale}")
            context_entry = "\n".join(context_parts)
            try:
                memory = self.read_agent_memory(agent_id)
                if memory.key_knowledge:
                    memory.key_knowledge += f"\n{decision_entry}"
                else:
                    memory.key_knowledge = decision_entry
                self.write_agent_memory(agent_id, memory)
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
