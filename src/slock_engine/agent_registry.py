"""Agent Registry — manages agent identities with file-system persistence.

Thread-safe registry for registering, finding, and removing agents.
Persists agent identities as JSON files under ~/.ghostap/slock/agents/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from src.autonomous.workforce.authority import (
    AuthorityMode,
    AuthoritySnapshot,
    LegacyMutationGuard,
    StaleAuthorityEpoch,
)

from .memory_manager import default_slock_storage_base
from .models import AgentIdentity

logger = logging.getLogger(__name__)


def _normalize_at_token(value: str) -> str:
    """Normalize an at-token candidate for case-insensitive comparison.

    Collapses whitespace, strips a leading '@', and lowercases. Used by both
    the inbound mention matcher (TaskRouter._extract_mention) and the
    outbound text renderer so that '@关羽', '@ 关羽 ', '关羽' all map to
    the same token.
    """
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    return cleaned.lower()


class DuplicateAgentNameError(Exception):
    """Raised when registering an agent with a name that already exists in the channel."""
    pass


class MoveResult(Enum):
    """Structured result codes for move_agent operations."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    NOT_IN_SOURCE = "not_in_source"
    PERSIST_FAILED = "persist_failed"


@dataclass
class MoveOutcome:
    """Structured outcome of a move_agent operation."""

    status: MoveResult
    error_msg: str | None = None

    @property
    def success(self) -> bool:
        return self.status == MoveResult.SUCCESS


@dataclass(frozen=True)
class _PersistRequest:
    operation: str
    agent: AgentIdentity
    validated_epoch: int


class AgentRegistry:
    """Thread-safe registry for slock agent identities.

    Agents are persisted as identity.json files under:
        {base_path}/agents/{agent_id}/identity.json
    """

    def __init__(
        self,
        base_path: str,
        *,
        mutation_guard: LegacyMutationGuard,
    ):
        self._base_path = base_path or default_slock_storage_base()
        self._mutation_guard = mutation_guard
        self._agents: dict[str, AgentIdentity] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._loaded = False
        self._persist_queue: list[_PersistRequest] = []
        self._inflight_requests: list[_PersistRequest] = []
        self._persist_thread: Optional[threading.Thread] = None

    @classmethod
    def legacy(cls, base_path: str = "") -> AgentRegistry:
        """Build an explicitly legacy-writable compatibility registry."""
        snapshot = AuthoritySnapshot(
            epoch=0,
            mode=AuthorityMode.LEGACY_WRITE,
        )
        return cls(
            base_path or default_slock_storage_base(),
            mutation_guard=LegacyMutationGuard(
                lambda: snapshot,
                expected_epoch=0,
            ),
        )

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

    def _load_single_agent(self, agent_id: str) -> Optional[AgentIdentity]:
        """Load a single agent from disk without full scan (on-demand)."""
        if agent_id in self._agents:
            return self._agents[agent_id]
        identity_file = self._agent_file(agent_id)
        if not os.path.isfile(identity_file):
            return None
        try:
            with open(identity_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            agent = AgentIdentity.from_dict(data)
            self._agents[agent.agent_id] = agent
            return agent
        except Exception as e:
            logger.warning("Failed to load agent %s: %s", agent_id, str(e))
            return None

    def register(self, agent: AgentIdentity) -> AgentIdentity:
        """Register a new agent and persist to disk."""
        with self._mutation_guard.write_lease("register") as validated_epoch:
            synchronous_request = None
            previous: dict | None = None
            with self._lock:
                self._ensure_loaded()
                # Uniqueness check: (channel_id, name) must be unique
                if agent.name and agent.owner_group:
                    for existing_agent in self._agents.values():
                        if (
                            existing_agent.agent_id != agent.agent_id
                            and existing_agent.name.lower() == agent.name.lower()
                            and self._belongs_to_channel(existing_agent, agent.owner_group)
                        ):
                            raise DuplicateAgentNameError(
                                f"Agent name '{agent.name}' already exists in channel {agent.owner_group}"
                            )
                existing = self._agents.get(agent.agent_id)
                if existing is not None:
                    previous = existing.to_dict()
                    agent = self._merge_agent(existing, agent)
                else:
                    agent = self._normalize_groups(agent)
                self._agents[agent.agent_id] = agent
                synchronous_request = self._persist(
                    "register",
                    agent,
                    validated_epoch,
                )
            if synchronous_request is not None:
                try:
                    self._persist_request(synchronous_request)
                except OSError:
                    with self._lock:
                        self._restore_cached_snapshot(agent.agent_id, previous)
                    raise
        return agent

    def get(self, agent_id: str) -> Optional[AgentIdentity]:
        """Find agent by ID."""
        with self._lock:
            # Fast path: already in memory
            if agent_id in self._agents:
                return self._agents[agent_id]
            # Try on-demand single load before full scan
            if not self._loaded:
                loaded = self._load_single_agent(agent_id)
                if loaded:
                    return loaded
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

    def find_by_at_token(
        self, token: str, channel_id: Optional[str] = None
    ) -> Optional[AgentIdentity]:
        """Find agent by an at-token (display name, agent_id, or normalized variant).

        Matches against name first, then agent_id. Used by mention extraction so
        that ``@<name>``, ``<at>`` markup inner text, and explicit agent_id refs
        all resolve to the same agent. Channel-scoped when channel_id given.
        """
        normalized = _normalize_at_token(token)
        if not normalized:
            return None
        with self._lock:
            self._ensure_loaded()
            for agent in self._agents.values():
                if channel_id is not None and not self._belongs_to_channel(agent, channel_id):
                    continue
                if _normalize_at_token(agent.name) == normalized:
                    return agent
                if agent.agent_id.lower() == normalized:
                    return agent
            return None

    @staticmethod
    def format_at_for_text(agent: AgentIdentity) -> str:
        """Render an agent reference for inclusion in plain-text messages.

        Slock virtual agents share one Feishu bot identity, so a real Feishu
        ``<at user_id="ou_xxx">`` markup would not highlight them. We emit a
        bold ``@name`` instead so the reference is at least visible to humans
        and round-trippable through ``find_by_at_token``.
        """
        name = agent.name or agent.agent_id
        return f"**@{name}**"

    def list_agents(self, channel_id: Optional[str] = None) -> list[AgentIdentity]:
        """List all agents, optionally filtered by channel."""
        with self._lock:
            self._ensure_loaded()
            if channel_id is None:
                return list(self._agents.values())
            return [a for a in self._agents.values() if self._belongs_to_channel(a, channel_id)]

    def remove(self, agent_id: str) -> bool:
        """Remove an agent from registry and delete its identity file."""
        with self._mutation_guard.write_lease("remove"):
            with self._lock:
                self._ensure_loaded()
                if agent_id not in self._agents:
                    return False
                previous = self._agents[agent_id].to_dict()
                del self._agents[agent_id]
                identity_file = self._agent_file(agent_id)
                if os.path.exists(identity_file):
                    try:
                        os.remove(identity_file)
                    except OSError as e:
                        self._restore_cached_snapshot(agent_id, previous)
                        logger.warning("Failed to remove identity file for %s: %s", agent_id, str(e))
                        return False
                # Clean up empty agent directory
                agent_dir = os.path.dirname(identity_file)
                try:
                    if os.path.isdir(agent_dir) and not os.listdir(agent_dir):
                        os.rmdir(agent_dir)
                except OSError:
                    pass  # Directory not empty or permission issue — skip silently
                return True

    def update(self, agent: AgentIdentity) -> bool:
        """Update an existing agent's identity."""
        with self._mutation_guard.write_lease("update") as validated_epoch:
            synchronous_request = None
            previous: dict | None = None
            with self._lock:
                self._ensure_loaded()
                if agent.agent_id not in self._agents:
                    return False
                previous = self._agents[agent.agent_id].to_dict()
                self._agents[agent.agent_id] = agent
                synchronous_request = self._persist(
                    "update",
                    agent,
                    validated_epoch,
                )
            if synchronous_request is not None:
                try:
                    self._persist_request(synchronous_request)
                except OSError:
                    with self._lock:
                        self._restore_cached_snapshot(agent.agent_id, previous)
                    raise
            return True

    def move_agent(self, agent_id: str, source_channel_id: str, target_channel_id: str) -> MoveOutcome:
        """Atomically move an agent from source channel to target channel.

        Uses copy-on-write: snapshots the agent state before mutation, and
        rolls back if persistence fails. Returns a structured MoveOutcome
        with explicit error codes.
        """
        with self._mutation_guard.write_lease("move_agent") as validated_epoch:
            with self._lock:
                self._ensure_loaded()
                agent = self._agents.get(agent_id)
                if agent is None:
                    return MoveOutcome(status=MoveResult.NOT_FOUND)
                # Verify agent belongs to source channel
                if not self._belongs_to_channel(agent, source_channel_id):
                    return MoveOutcome(status=MoveResult.NOT_IN_SOURCE)
                # Snapshot for rollback
                snapshot = agent.to_dict()
                # Mutate in-place
                groups = [g for g in (agent.member_groups or []) if g != source_channel_id]
                if target_channel_id not in groups:
                    groups.append(target_channel_id)
                agent.member_groups = groups
                agent.owner_group = target_channel_id
                request = self._make_persist_request(
                    "move_agent",
                    agent,
                    validated_epoch=validated_epoch,
                )
            # Persist — rollback on failure (synchronous for atomicity guarantee)
            try:
                self._persist_request(request)
            except OSError as e:
                with self._lock:
                    # Rollback: restore agent fields from snapshot
                    agent.member_groups = snapshot.get("member_groups", [])
                    agent.owner_group = snapshot.get("owner_group", "")
                logger.warning(
                    "move_agent: persist failed, rolled back agent=%s error=%s",
                    agent_id, str(e),
                )
                return MoveOutcome(status=MoveResult.PERSIST_FAILED, error_msg=str(e))
            # Defensive: verify persisted identity and L1 memory path
            identity_file = self._agent_file(agent_id)
            memory_path = os.path.join(self._agents_dir(), agent_id, "MEMORY.md")
            logger.debug(
                "move_agent: persisted agent=%s to=%s identity_exists=%s memory_path=%s",
                agent_id,
                target_channel_id,
                os.path.isfile(identity_file),
                memory_path,
            )
            return MoveOutcome(status=MoveResult.SUCCESS)

    MAX_PERSIST_QUEUE_SIZE: int = 256

    def _make_persist_request(
        self,
        operation: str,
        agent: AgentIdentity,
        *,
        validated_epoch: int,
    ) -> _PersistRequest:
        return _PersistRequest(operation, agent, validated_epoch)

    def _persist_request(self, request: _PersistRequest) -> None:
        with self._mutation_guard.write_lease(
            f"{request.operation} persistence",
            validated_epoch=request.validated_epoch,
        ):
            self._write_agent_to_disk(request.agent)

    def cutover_authority(
        self,
        advance: Callable[[], AuthoritySnapshot],
    ) -> AuthoritySnapshot:
        """Discard unpersisted legacy state, then advance writer authority."""

        def discard_and_advance() -> AuthoritySnapshot:
            with self._lock:
                pending = tuple(
                    [*self._persist_queue, *self._inflight_requests]
                )
                self._persist_queue.clear()
                self._restore_requests_from_disk(pending)
                return advance()

        return self._mutation_guard.cutover(discard_and_advance)

    def _restore_requests_from_disk(
        self,
        requests: tuple[_PersistRequest, ...],
    ) -> None:
        for agent_id in dict.fromkeys(
            request.agent.agent_id for request in requests
        ):
            identity_file = self._agent_file(agent_id)
            if not os.path.isfile(identity_file):
                self._agents.pop(agent_id, None)
                continue
            try:
                with open(identity_file, "r", encoding="utf-8") as file:
                    self._agents[agent_id] = AgentIdentity.from_dict(
                        json.load(file)
                    )
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"cannot restore legacy agent before cutover: {agent_id}"
                ) from exc

    def _restore_cached_snapshot(
        self,
        agent_id: str,
        snapshot: dict | None,
    ) -> None:
        if snapshot is None:
            self._agents.pop(agent_id, None)
            return
        self._agents[agent_id] = AgentIdentity.from_dict(snapshot)

    def _persist(
        self,
        operation: str,
        agent: AgentIdentity,
        validated_epoch: int,
    ) -> _PersistRequest | None:
        """Schedule agent identity write to background thread (caller must hold _lock).

        Backpressure: when the queue exceeds MAX_PERSIST_QUEUE_SIZE, falls back to
        synchronous disk write in the caller's thread to prevent unbounded memory growth.
        """
        if len(self._persist_queue) >= self.MAX_PERSIST_QUEUE_SIZE:
            # Backpressure: write synchronously to avoid unbounded queue growth
            logger.warning(
                "persist_queue at capacity (%d), falling back to synchronous write for agent %s",
                self.MAX_PERSIST_QUEUE_SIZE, agent.agent_id,
            )
            return self._make_persist_request(
                operation,
                agent,
                validated_epoch=validated_epoch,
            )

        self._persist_queue.append(
            self._make_persist_request(
                operation,
                agent,
                validated_epoch=validated_epoch,
            )
        )
        if self._persist_thread is None or not self._persist_thread.is_alive():
            self._persist_thread = threading.Thread(
                target=self._flush_persist_queue,
                name="slock-registry-persist",
                daemon=True,
            )
            self._persist_thread.start()
        return None

    def _flush_persist_queue(self) -> None:
        """Background worker: drain persist queue and write to disk."""
        while True:
            with self._lock:
                if not self._persist_queue:
                    return
                batch = list(self._persist_queue)
                self._persist_queue.clear()
                self._inflight_requests.extend(batch)
            for request in batch:
                reconciled = True
                try:
                    self._persist_request(request)
                except StaleAuthorityEpoch as exc:
                    logger.warning("Discarded stale registry write: %s", exc)
                except OSError as exc:
                    logger.warning(
                        "Failed background registry write for %s: %s",
                        request.agent.agent_id,
                        exc,
                    )
                    with self._lock:
                        try:
                            self._restore_requests_from_disk((request,))
                        except RuntimeError:
                            reconciled = False
                finally:
                    if reconciled:
                        with self._lock:
                            self._inflight_requests = [
                                item
                                for item in self._inflight_requests
                                if item is not request
                            ]

    def _write_agent_to_disk(self, agent: AgentIdentity) -> None:
        """Write a single agent identity to disk (atomic)."""
        identity_file = self._agent_file(agent.agent_id)
        os.makedirs(os.path.dirname(identity_file), exist_ok=True)
        tmp_path = identity_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(agent.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, identity_file)

    def refresh_agent(self, agent_id: str) -> Optional[AgentIdentity]:
        """Re-read a single agent's identity from disk and update the in-memory cache.

        Use this after a cross-engine move to ensure the target engine's
        registry sees the updated membership without a full reload.
        Returns the refreshed AgentIdentity, or None if the file does not exist.
        """
        with self._lock:
            self._ensure_loaded()
            identity_file = self._agent_file(agent_id)
            if not os.path.isfile(identity_file):
                # Agent was removed from disk — evict from cache if present
                self._agents.pop(agent_id, None)
                return None
            try:
                with open(identity_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                agent = AgentIdentity.from_dict(data)
                self._agents[agent.agent_id] = agent
                return agent
            except Exception as e:
                logger.warning("refresh_agent failed for %s: %s", agent_id, str(e))
                return None

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
        existing.notes_path = incoming.notes_path or existing.notes_path
        existing.workspace_path = incoming.workspace_path or existing.workspace_path
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
