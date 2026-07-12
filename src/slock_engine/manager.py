"""SlockEngineManager — manages SlockEngine instances per chat+project."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

from ..engine_base import BaseEngineManager
from .engine import SlockEngine
from .memory_manager import default_slock_storage_base

logger = logging.getLogger(__name__)
_LARK_CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9_]+$")


class SlockEngineManager(BaseEngineManager["SlockEngine"]):
    """Manages SlockEngine instances per chat+project.

    Thread-safe: all dict mutations are protected by _lock.
    Uses secondary index (_chat_keys) for efficient per-chat lookups.
    """

    def __init__(self, storage_base_path: str = "") -> None:
        super().__init__()
        self._managed_chats: set[str] = set()
        self._dissolving_chats: set[str] = set()
        self._reserved_team_names: set[str] = set()
        self._storage_base_path = storage_base_path or default_slock_storage_base()
        self._blocked_team_names = self._load_pending_cleanup_names()

    def register_managed_chat(self, chat_id: str) -> None:
        """Declare a chat_id as managed by the slock engine (event routing)."""
        with self._lock:
            self._managed_chats.add(chat_id)

    def unregister_managed_chat(self, chat_id: str) -> None:
        """Remove a chat_id from slock management."""
        with self._lock:
            self._managed_chats.discard(chat_id)

    def claim_dissolve(self, chat_id: str) -> bool:
        """Claim the one in-flight dissolve transaction allowed per chat."""
        with self._lock:
            if chat_id in self._dissolving_chats:
                return False
            self._dissolving_chats.add(chat_id)
            return True

    def release_dissolve(self, chat_id: str) -> None:
        with self._lock:
            self._dissolving_chats.discard(chat_id)

    def reserve_team_name(self, team_name: str) -> bool:
        """Atomically reject active or concurrently-created duplicate names."""
        normalized = (team_name or "").strip().casefold()
        if not normalized:
            return False
        with self._lock:
            if normalized in self._reserved_team_names or normalized in self._blocked_team_names:
                return False
            for engine in self._engines.values():
                channel = engine.channel
                if channel and (channel.team_name or channel.name or "").strip().casefold() == normalized:
                    return False
            self._reserved_team_names.add(normalized)
            return True

    def release_team_name(self, team_name: str) -> None:
        normalized = (team_name or "").strip().casefold()
        with self._lock:
            self._reserved_team_names.discard(normalized)

    def block_team_name_for_cleanup(
        self,
        team_name: str,
        chat_id: str,
        delete_state: str,
    ) -> bool:
        """Persist a residual-group tombstone and block same-name creation.

        Returns whether the block reached durable storage. The in-memory block
        is installed first, so callers remain safe even if persistence fails.
        """
        normalized = (team_name or "").strip().casefold()
        if not normalized:
            return False
        with self._lock:
            self._blocked_team_names.add(normalized)

        records_dir = os.path.join(self._storage_base_path, "pending_cleanup")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        record_path = os.path.join(records_dir, f"{digest}.json")
        temp_path = f"{record_path}.tmp.{os.getpid()}.{time.time_ns()}"
        record = {
            "version": 1,
            "team_name": (team_name or "").strip(),
            "chat_id": chat_id,
            "delete_state": delete_state,
            "created_at_ns": time.time_ns(),
        }
        try:
            os.makedirs(records_dir, mode=0o700, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, record_path)
            return True
        except OSError:
            logger.exception(
                "block_team_name_for_cleanup: cannot persist residual group chat=%s",
                chat_id,
            )
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return False

    def _load_pending_cleanup_names(self) -> set[str]:
        """Load durable residual-group name blocks, ignoring unsafe records."""
        records_dir = os.path.join(self._storage_base_path, "pending_cleanup")
        if not os.path.isdir(records_dir) or os.path.islink(records_dir):
            return set()
        blocked: set[str] = set()
        try:
            entries = os.listdir(records_dir)
        except OSError:
            logger.exception("cannot list Slock pending-cleanup records")
            return blocked
        for entry in entries:
            if not re.fullmatch(r"[0-9a-f]{64}\.json", entry):
                continue
            path = os.path.join(records_dir, entry)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, json.JSONDecodeError):
                logger.warning("skipping invalid Slock cleanup record: %s", path)
                continue
            normalized = str(record.get("team_name") or "").strip().casefold()
            expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if normalized and entry == f"{expected}.json":
                blocked.add(normalized)
        return blocked

    def archive_managed_chat_marker(self, chat_id: str) -> Optional[str]:
        """Atomically retire an active marker so restart cannot revive it."""
        if not isinstance(chat_id, str) or not _LARK_CHAT_ID_RE.fullmatch(chat_id):
            raise ValueError("invalid chat_id")
        groups_dir = os.path.realpath(os.path.join(self._storage_base_path, "groups"))
        group_dir = os.path.join(groups_dir, chat_id)
        if os.path.islink(group_dir) or os.path.commonpath((groups_dir, os.path.realpath(group_dir))) != groups_dir:
            raise ValueError("unsafe chat marker path")
        marker_path = os.path.join(group_dir, ".slock_channel.json")
        if not os.path.isfile(marker_path):
            return None
        archived_path = os.path.join(
            group_dir,
            f".slock_channel.dissolved.{time.time_ns()}.json",
        )
        os.replace(marker_path, archived_path)
        return archived_path

    def restore_archived_chat_marker(self, chat_id: str, archived_path: str) -> None:
        """Restore a retired marker as compensation for a failed teardown."""
        if not isinstance(chat_id, str) or not _LARK_CHAT_ID_RE.fullmatch(chat_id):
            raise ValueError("invalid chat_id")
        group_dir = os.path.realpath(os.path.join(self._storage_base_path, "groups", chat_id))
        if os.path.dirname(os.path.realpath(archived_path)) != group_dir:
            raise ValueError("unsafe archived marker path")
        if not os.path.isfile(archived_path) or os.path.islink(archived_path):
            raise FileNotFoundError(archived_path)
        os.replace(archived_path, os.path.join(group_dir, ".slock_channel.json"))

    def is_managed_chat(self, chat_id: str) -> bool:
        """Check if a chat_id is managed by the slock engine."""
        with self._lock:
            return chat_id in self._managed_chats

    def _create_engine(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str,
        engine_name: str,
        model_name: Optional[str],
    ) -> SlockEngine:
        return SlockEngine(
            chat_id=chat_id,
            root_path=root_path,
            agent_type=agent_type,
            engine_name=engine_name,
            model_name=model_name,
            memory_base_path=self._storage_base_path,
        )

    def remove(self, chat_id: str, root_path: str) -> None:
        """Remove and cleanup a slock engine instance."""
        key = f"{chat_id}:{root_path}"
        with self._lock:
            if key in self._engines:
                self._engines[key].cleanup()
                del self._engines[key]
                self._remove_index(chat_id, key)

    def discard_engine_for_recovery(self, chat_id: str, root_path: str) -> None:
        """Drop a possibly half-shutdown instance before compensation rebuild."""
        key = f"{chat_id}:{root_path}"
        with self._lock:
            self._engines.pop(key, None)
            self._remove_index(chat_id, key)

    def get_activated_engine(self, chat_id: str) -> Optional[SlockEngine]:
        """Get the slock engine for a chat if it has an activated channel."""
        for engine in self._iter_chat_engines(chat_id):
            if engine.channel is not None:
                return engine
        return None

    def is_slock_active(self, chat_id: str) -> bool:
        """Check if slock mode is active for a given chat."""
        return self.get_activated_engine(chat_id) is not None

    def list_activated_engines(self) -> list[SlockEngine]:
        """List all engines with an activated slock channel."""
        return [engine for engine in self.list_engines() if engine.channel is not None]

    def find_team(self, name: str) -> Optional[SlockEngine]:
        """Find an activated team by team name, channel name, or chat id."""
        needle = (name or "").strip().lower()
        if not needle:
            return None
        for engine in self.list_activated_engines():
            channel = engine.channel
            if channel is None:
                continue
            candidates = {
                channel.channel_id.lower(),
                (channel.name or "").lower(),
                (channel.team_name or "").lower(),
            }
            if needle in candidates:
                return engine
        return None

    def restore_from_disk(self, root_path: str) -> int:
        """Scan app-level Slock group markers and restore engines.

        For each valid marker file, rebuilds a SlockEngine, activates the
        channel, and registers the chat for event routing.

        Returns the number of successfully restored engines.
        """
        from .models import SlockChannel

        marker_dirs = [os.path.join(self._storage_base_path, "groups")]
        existing_dirs = [path for path in marker_dirs if os.path.isdir(path)]
        if not existing_dirs:
            return 0

        restored = 0
        for slock_dir in existing_dirs:
            try:
                entries = os.listdir(slock_dir)
            except OSError as e:
                logger.warning("restore_from_disk: cannot list %s: %s", slock_dir, repr(e))
                continue

            for entry in entries:
                channel_dir = os.path.join(slock_dir, entry)
                if (
                    not _LARK_CHAT_ID_RE.fullmatch(entry)
                    or not os.path.isdir(channel_dir)
                    or os.path.islink(channel_dir)
                ):
                    continue

                marker_path = os.path.join(channel_dir, ".slock_channel.json")
                if not os.path.isfile(marker_path) or os.path.islink(marker_path):
                    continue

                try:
                    with open(marker_path, "r", encoding="utf-8") as f:
                        marker = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(
                        "restore_from_disk: skipping corrupted marker %s: %s",
                        marker_path, e,
                    )
                    continue

                channel_id = marker.get("channel_id")
                if not channel_id:
                    logger.warning(
                        "restore_from_disk: marker missing channel_id: %s", marker_path
                    )
                    continue
                if (
                    not isinstance(channel_id, str)
                    or not _LARK_CHAT_ID_RE.fullmatch(channel_id)
                    or channel_id != entry
                ):
                    logger.warning(
                        "restore_from_disk: skipping non-Lark chat marker: %s",
                        marker_path,
                    )
                    continue

                # Skip if already managed (idempotent)
                if self.is_managed_chat(channel_id):
                    continue

                try:
                    engine_root_path = self._resolve_marker_root_path(
                        marker,
                        channel_dir,
                    )
                    engine = self.get_or_create(
                        channel_id, engine_root_path, engine_name="Slock"
                    )
                    channel = SlockChannel(
                        channel_id=channel_id,
                        name=marker.get("name", ""),
                        team_name=marker.get("team_name", ""),
                        owner_id=marker.get("owner_id", ""),
                    )
                    engine.activate_channel(channel)
                    self.register_managed_chat(channel_id)
                    restored += 1
                    logger.info(
                        "restore_from_disk: restored slock engine for chat=%s team=%s",
                        channel_id, channel.team_name,
                    )
                except Exception as e:
                    logger.error(
                        "restore_from_disk: failed to restore %s: %s",
                        channel_id, e,
                    )

        if restored:
            logger.info("restore_from_disk: restored %d slock engine(s)", restored)
        return restored

    @staticmethod
    def _resolve_marker_root_path(marker: dict, channel_dir: str) -> str:
        persisted_candidates = [marker.get("root_path")]
        config_path = os.path.join(channel_dir, "workspace", ".team-config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                team_config = json.load(f)
            persisted_candidates.append(team_config.get("project_path"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            pass
        for candidate in persisted_candidates:
            if isinstance(candidate, str) and os.path.isabs(candidate) and os.path.isdir(candidate):
                return os.path.realpath(candidate)
        raise ValueError("persisted Slock project root is unavailable")

    def _build_snapshot(self, engine: SlockEngine):
        """Build EngineSnapshot with Slock-specific fields."""
        from src.card.engine_snapshot import EngineSnapshot

        channel = engine.channel
        agents = engine.registry.list_agents(
            channel_id=channel.channel_id if channel else None
        )
        return EngineSnapshot(
            engine_name=engine.engine_name,
            root_path=engine.root_path,
            is_running=engine.is_running,
            ext={
                "channel": channel,
                "agent_count": len(agents),
                "team_name": channel.team_name if channel else "",
            },
        )
