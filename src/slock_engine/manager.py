"""SlockEngineManager — manages SlockEngine instances per chat+project."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from ..engine_base import BaseEngineManager
from .engine import SlockEngine
from .memory_manager import default_slock_storage_base

logger = logging.getLogger(__name__)


class SlockEngineManager(BaseEngineManager["SlockEngine"]):
    """Manages SlockEngine instances per chat+project.

    Thread-safe: all dict mutations are protected by _lock.
    Uses secondary index (_chat_keys) for efficient per-chat lookups.
    """

    def __init__(self, storage_base_path: str = "") -> None:
        super().__init__()
        self._managed_chats: set[str] = set()
        self._storage_base_path = storage_base_path or default_slock_storage_base()

    def register_managed_chat(self, chat_id: str) -> None:
        """Declare a chat_id as managed by the slock engine (event routing)."""
        with self._lock:
            self._managed_chats.add(chat_id)

    def unregister_managed_chat(self, chat_id: str) -> None:
        """Remove a chat_id from slock management."""
        with self._lock:
            self._managed_chats.discard(chat_id)

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
                if not os.path.isdir(channel_dir):
                    continue

                marker_path = os.path.join(channel_dir, ".slock_channel.json")
                if not os.path.isfile(marker_path):
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

                # Skip if already managed (idempotent)
                if self.is_managed_chat(channel_id):
                    continue

                try:
                    engine = self.get_or_create(
                        channel_id, root_path, engine_name="Slock"
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
