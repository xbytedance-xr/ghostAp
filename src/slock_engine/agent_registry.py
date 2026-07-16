"""Agent Registry — manages agent identities with file-system persistence.

Thread-safe registry for registering, finding, and removing agents.
Persists agent identities as JSON files under ~/.ghostap/slock/agents/.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
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

_STORAGE_LOCKS_GUARD = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
_STORAGE_LOCKS: dict[str, threading.RLock] = {}
_STORAGE_LOCK_DEPTH = threading.local()


def _storage_lock_for(base_path: str) -> threading.RLock:
    with _STORAGE_LOCKS_GUARD:
        return _STORAGE_LOCKS.setdefault(base_path, threading.RLock())  # leaf lock: never held while acquiring a LockLevel lock


def _normalize_at_token(value: str) -> str:
    """Normalize an at-token candidate for case-insensitive comparison.

    Collapses whitespace, strips a leading '@', and case-folds. Used by both
    the inbound mention matcher (TaskRouter._extract_mention) and the
    outbound text renderer so that '@关羽', '@ 关羽 ', '关羽' all map to
    the same token.
    """
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    return cleaned.casefold()


class DuplicateAgentNameError(Exception):
    """Raised when registering an agent with a name that already exists in the channel."""
    pass


class AmbiguousAgentNameError(LookupError):
    """Raised when a name resolves to multiple agents in the requested scope."""


class MoveResult(Enum):
    """Structured result codes for move_agent operations."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    NOT_IN_SOURCE = "not_in_source"
    DUPLICATE_NAME = "duplicate_name"
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
    previous: dict | None = None
    revision: str = ""
    previous_revision: str | None = None


@dataclass(frozen=True)
class _MaterializedAgent:
    identity: AgentIdentity
    revision: str


@dataclass(frozen=True)
class _IndexedAgent:
    identity: AgentIdentity
    revision: str
    materialized: _MaterializedAgent | None = None


class AgentRegistry:
    """Thread-safe registry for slock agent identities.

    ``agent_registry.v1.json`` is the durable admission authority. Every
    mutation replaces it under ``.agent_registry.lock`` (``flock``), so
    processes agree on case-folded channel/name uniqueness before an async
    compatibility copy is written. Pre-index installations are rebuilt once
    from identity files on their first mutation.

    Compatibility identity copies are persisted under:
        {base_path}/agents/{agent_id}/identity.json
    """

    def __init__(
        self,
        base_path: str,
        *,
        mutation_guard: LegacyMutationGuard,
        _synchronous_name_mutations: bool = False,
    ):
        configured_base = base_path or default_slock_storage_base()
        self._base_path = os.path.realpath(os.path.abspath(os.path.expanduser(configured_base)))
        self._storage_lock = _storage_lock_for(self._base_path)
        self._mutation_guard = mutation_guard
        self._synchronous_name_mutations = _synchronous_name_mutations
        self._agents: dict[str, AgentIdentity] = {}
        self._revisions: dict[str, str] = {}
        self._materialized: dict[str, _MaterializedAgent] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._loaded = False
        self._persist_queue: list[_PersistRequest] = []
        self._inflight_requests: list[_PersistRequest] = []
        self._admission_open = True
        self._persist_thread: Optional[threading.Thread] = None
        self._require_crash_durable = False

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
            _synchronous_name_mutations=True,
        )

    @property
    def base_path(self) -> str:
        return self._base_path

    def _agents_dir(self) -> str:
        return os.path.join(self._base_path, "agents")

    def _agent_file(self, agent_id: str) -> str:
        return os.path.join(self._agents_dir(), agent_id, "identity.json")

    def _index_file(self) -> str:
        return os.path.join(self._base_path, "agent_registry.v1.json")

    @contextmanager
    def _open_agents_directory(self, *, create: bool):
        """Open the managed agents root without following any symlink."""
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not os.path.isdir(self._base_path):
            if not create:
                yield None
                return
            os.makedirs(self._base_path, exist_ok=True)
        base_fd = os.open(self._base_path, directory_flags | nofollow)
        try:
            try:
                agents_fd = os.open(
                    "agents",
                    directory_flags | nofollow,
                    dir_fd=base_fd,
                )
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                os.mkdir("agents", mode=0o700, dir_fd=base_fd)
                os.fsync(base_fd)
                agents_fd = os.open(
                    "agents",
                    directory_flags | nofollow,
                    dir_fd=base_fd,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise OSError("unsafe agents directory symlink") from exc
                raise
            try:
                yield agents_fd
            finally:
                os.close(agents_fd)
        finally:
            os.close(base_fd)

    @contextmanager
    def _open_agent_directory(self, agent_id: str, *, create: bool):
        """Open one anchored agent directory with component-only containment."""
        if not agent_id or os.path.basename(agent_id) != agent_id:
            raise OSError("unsafe agent directory containment")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        with self._open_agents_directory(create=create) as agents_fd:
            if agents_fd is None:
                yield None
                return
            try:
                agent_fd = os.open(
                    agent_id,
                    directory_flags | nofollow,
                    dir_fd=agents_fd,
                )
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                os.mkdir(agent_id, mode=0o700, dir_fd=agents_fd)
                os.fsync(agents_fd)
                agent_fd = os.open(
                    agent_id,
                    directory_flags | nofollow,
                    dir_fd=agents_fd,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise OSError("unsafe agent directory symlink") from exc
                raise
            try:
                yield agent_fd
            finally:
                os.close(agent_fd)

    @contextmanager
    def _storage_mutation_lock(self):
        """Linearize storage-authority mutations across instances and processes."""
        import fcntl

        with self._storage_lock:
            depths = getattr(_STORAGE_LOCK_DEPTH, "paths", None)
            if depths is None:
                depths = {}
                _STORAGE_LOCK_DEPTH.paths = depths
            depth = depths.get(self._base_path, 0)
            if depth:
                depths[self._base_path] = depth + 1
                try:
                    yield
                finally:
                    depths[self._base_path] -= 1
                return
            os.makedirs(self._base_path, exist_ok=True)
            lock_path = os.path.join(self._base_path, ".agent_registry.lock")
            with open(lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                depths[self._base_path] = 1
                try:
                    yield
                finally:
                    depths.pop(self._base_path, None)
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _reload_from_disk(self) -> None:
        """Reload durable state, then overlay accepted writes awaiting persistence."""
        accepted = [*self._inflight_requests, *self._persist_queue]
        indexed = self._read_index_records()
        if indexed is None:
            self._agents = self._scan_agents_dir()
            self._revisions = {
                agent_id: self._legacy_revision(agent.to_dict())
                for agent_id, agent in self._agents.items()
            }
            self._materialized = {
                agent_id: _MaterializedAgent(
                    AgentIdentity.from_dict(agent.to_dict()),
                    self._revisions[agent_id],
                )
                for agent_id, agent in self._agents.items()
            }
        else:
            self._load_index_records(indexed)
        self._loaded = True
        if indexed is not None:
            return
        for request in accepted:
            self._agents[request.agent.agent_id] = request.agent
            self._revisions[request.agent.agent_id] = request.revision

    def _ensure_loaded(self) -> None:
        """Load/migrate under flock so a cold reader cannot publish stale state."""
        if self._loaded:
            return
        with self._storage_mutation_lock():
            with self._lock:
                if self._loaded:
                    return
                indexed = self._read_index_records()
                if indexed is None:
                    self._agents = self._scan_agents_dir()
                    self._revisions = {
                        agent_id: self._legacy_revision(agent.to_dict())
                        for agent_id, agent in self._agents.items()
                    }
                    self._materialized = {
                        agent_id: _MaterializedAgent(
                            AgentIdentity.from_dict(agent.to_dict()),
                            self._revisions[agent_id],
                        )
                        for agent_id, agent in self._agents.items()
                    }
                    self._write_index_to_disk()
                else:
                    self._load_index_records(indexed)
                self._loaded = True

    def _scan_agents_dir(self) -> dict[str, AgentIdentity]:
        """Rebuild the v1 index view from pre-index identity directories."""
        agents: dict[str, AgentIdentity] = {}
        with self._open_agents_directory(create=False) as agents_fd:
            if agents_fd is None:
                return agents
            entries: list[str] = []
            for entry in os.listdir(agents_fd):
                entry_stat = os.stat(
                    entry,
                    dir_fd=agents_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise OSError("unsafe agent directory symlink")
                if stat.S_ISDIR(entry_stat.st_mode):
                    entries.append(entry)
        for entry in entries:
            try:
                with self._open_agent_directory(entry, create=False) as agent_fd:
                    if agent_fd is None:
                        continue
                    agent = self._read_compat_identity_from_fd(agent_fd, entry)
                    if agent is not None:
                        agents[agent.agent_id] = agent
            except OSError:
                raise
            except Exception as exc:
                logger.warning("Failed to load agent %s: %s", entry, str(exc))
        return agents

    def _read_index_records(self) -> dict[str, _IndexedAgent] | None:
        index_file = self._index_file()
        if not os.path.isfile(index_file):
            return None
        try:
            with open(index_file, "r", encoding="utf-8") as file:
                document = json.load(file)
            if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(
                document.get("agents"), dict
            ):
                raise ValueError("unsupported registry index shape")
            agents: dict[str, _IndexedAgent] = {}
            for agent_id, record in document["agents"].items():
                if (
                    isinstance(record, dict)
                    and isinstance(record.get("revision"), str)
                    and isinstance(record.get("identity"), dict)
                ):
                    revision = record["revision"]
                    payload = record["identity"]
                    materialized_payload = record.get("materialized")
                    materialization_known = "materialized" in record
                else:
                    payload = record
                    revision = self._legacy_revision(payload)
                    materialized_payload = None
                    materialization_known = False
                agent = AgentIdentity.from_dict(payload)
                if agent.agent_id != agent_id:
                    raise ValueError("registry index key does not match agent_id")
                if not revision:
                    raise ValueError("registry index revision is empty")
                materialized: _MaterializedAgent | None = None
                if isinstance(materialized_payload, dict):
                    materialized_revision = materialized_payload.get("revision")
                    materialized_identity = AgentIdentity.from_dict(
                        materialized_payload.get("identity")
                    )
                    if not isinstance(materialized_revision, str) or not materialized_revision:
                        raise ValueError("materialized revision is empty")
                    materialized = _MaterializedAgent(
                        materialized_identity,
                        materialized_revision,
                    )
                elif not materialization_known:
                    # Upgrade pre-materialization v1 shapes from the actual
                    # compatibility copy. It may lag the accepted current.
                    compat_identity = self._read_compat_identity(agent_id)
                    if compat_identity is not None:
                        compat_revision = (
                            revision
                            if compat_identity.to_dict() == agent.to_dict()
                            else self._legacy_revision(compat_identity.to_dict())
                        )
                        materialized = _MaterializedAgent(
                            compat_identity,
                            compat_revision,
                        )
                agents[agent_id] = _IndexedAgent(agent, revision, materialized)
            return agents
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("cannot read durable agent registry index") from exc

    def _read_index_from_disk(self) -> dict[str, AgentIdentity] | None:
        indexed = self._read_index_records()
        if indexed is None:
            return None
        return {agent_id: record.identity for agent_id, record in indexed.items()}

    def _load_index_records(self, indexed: dict[str, _IndexedAgent]) -> None:
        self._agents = {
            agent_id: record.identity for agent_id, record in indexed.items()
        }
        self._revisions = {
            agent_id: record.revision for agent_id, record in indexed.items()
        }
        self._materialized = {
            agent_id: record.materialized
            for agent_id, record in indexed.items()
            if record.materialized is not None
        }

    @staticmethod
    def _legacy_revision(payload: dict) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"legacy-{hashlib.sha256(canonical).hexdigest()}"

    def _write_index_to_disk(self) -> None:
        """Atomically persist the admission/index state while holding storage flock."""
        os.makedirs(self._base_path, exist_ok=True)
        index_file = self._index_file()
        document = {
            "version": 1,
            "agents": {
                agent_id: {
                    "revision": self._revisions.setdefault(
                        agent_id, uuid.uuid4().hex
                    ),
                    "identity": agent.to_dict(),
                    "materialized": (
                        {
                            "revision": self._materialized[agent_id].revision,
                            "identity": self._materialized[
                                agent_id
                            ].identity.to_dict(),
                        }
                        if agent_id in self._materialized
                        else None
                    ),
                }
                for agent_id, agent in sorted(self._agents.items())
            },
        }
        self._durably_replace_json(index_file, document)

    def _durably_replace_json(self, path: str, payload: dict) -> None:
        tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
        try:
            AgentRegistry._fsync_directory(os.path.dirname(path))
        except OSError:
            try:
                with open(path, "r", encoding="utf-8") as file:
                    published = json.load(file)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                published = None
            if self._require_crash_durable or published != payload:
                raise
            logger.warning(
                "Directory fsync failed after visible JSON replace; "
                "continuing with committed-visible semantics: %s",
                path,
            )

    def register(self, agent: AgentIdentity) -> AgentIdentity:
        """Register a new agent and persist to disk."""
        with self._mutation_guard.write_lease("register") as validated_epoch:
            with self._storage_mutation_lock():
                previous: dict | None = None
                previous_revision: str | None = None
                with self._lock:
                    self._assert_admission_open()
                    self._mutation_guard.assert_writable(
                        "register admission",
                        validated_epoch=validated_epoch,
                    )
                    self._reload_from_disk()
                    existing = self._agents.get(agent.agent_id)
                    effective_name = agent.name or (existing.name if existing is not None else "")
                    effective_channels = self._agent_channels(agent)
                    if existing is not None:
                        effective_channels.update(self._agent_channels(existing))
                    self._assert_name_available(
                        effective_name,
                        effective_channels,
                        exclude_agent_id=agent.agent_id,
                    )
                    if existing is not None:
                        previous = existing.to_dict()
                        previous_revision = self._revisions.get(agent.agent_id)
                        agent = self._merge_agent(existing, agent)
                    else:
                        agent = self._normalize_groups(agent)
                    self._agents[agent.agent_id] = agent
                    self._revisions[agent.agent_id] = uuid.uuid4().hex
                    try:
                        self._write_index_to_disk()
                    except OSError:
                        self._restore_cached_snapshot(agent.agent_id, previous)
                        self._restore_revision(
                            agent.agent_id, previous_revision
                        )
                        raise
                    if self._synchronous_name_mutations:
                        synchronous_request = self._make_persist_request(
                            "register",
                            agent,
                            validated_epoch=validated_epoch,
                            previous=previous,
                            revision=self._revisions[agent.agent_id],
                            previous_revision=previous_revision,
                        )
                    else:
                        synchronous_request = self._persist(
                            "register",
                            agent,
                            validated_epoch,
                            previous=previous,
                            revision=self._revisions[agent.agent_id],
                            previous_revision=previous_revision,
                        )
                if synchronous_request is not None:
                    try:
                        self._persist_request(synchronous_request)
                    except OSError:
                        with self._lock:
                            self._restore_cached_snapshot(agent.agent_id, previous)
                            self._restore_revision(
                                agent.agent_id, previous_revision
                            )
                            self._write_index_to_disk()
                        raise
        return agent

    def get(self, agent_id: str) -> Optional[AgentIdentity]:
        """Find agent by ID."""
        self._ensure_loaded()
        with self._lock:
            return self._agents.get(agent_id)

    def find_by_name(self, name: str, channel_id: Optional[str] = None) -> Optional[AgentIdentity]:
        """Find agent by display name, optionally scoped to a channel."""
        self._ensure_loaded()
        with self._lock:
            name_key = name.casefold()
            matches = [
                agent
                for agent in self._agents.values()
                if agent.name.casefold() == name_key
                and (channel_id is None or self._belongs_to_channel(agent, channel_id))
            ]
            if len(matches) > 1:
                scope = channel_id or "all channels"
                raise AmbiguousAgentNameError(
                    f"Agent name '{name}' is ambiguous in {scope}: "
                    f"{', '.join(sorted(agent.agent_id for agent in matches))}"
                )
            return matches[0] if matches else None

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
        self._ensure_loaded()
        with self._lock:
            candidates = [
                agent
                for agent in self._agents.values()
                if channel_id is None or self._belongs_to_channel(agent, channel_id)
            ]
            id_matches = [
                agent for agent in candidates if agent.agent_id.casefold() == normalized
            ]
            if len(id_matches) > 1:
                raise AmbiguousAgentNameError(
                    f"Agent ID token '{token}' is ambiguous: "
                    f"{', '.join(sorted(agent.agent_id for agent in id_matches))}"
                )
            if id_matches:
                return id_matches[0]
            name_matches = [
                agent
                for agent in candidates
                if _normalize_at_token(agent.name) == normalized
            ]
            if len(name_matches) > 1:
                scope = channel_id or "all channels"
                raise AmbiguousAgentNameError(
                    f"Agent token '{token}' is ambiguous in {scope}: "
                    f"{', '.join(sorted(agent.agent_id for agent in name_matches))}"
                )
            return name_matches[0] if name_matches else None

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
        self._ensure_loaded()
        with self._lock:
            if channel_id is None:
                return list(self._agents.values())
            return [a for a in self._agents.values() if self._belongs_to_channel(a, channel_id)]

    def remove(self, agent_id: str) -> bool:
        """Remove an agent from registry and delete its identity file."""
        with self._mutation_guard.write_lease("remove") as validated_epoch:
            with self._storage_mutation_lock():
                with self._lock:
                    self._assert_admission_open()
                    self._mutation_guard.assert_writable(
                        "remove admission",
                        validated_epoch=validated_epoch,
                    )
                    self._reload_from_disk()
                    if agent_id not in self._agents:
                        return False
                    previous = self._agents[agent_id].to_dict()
                    previous_revision = self._revisions.get(agent_id)
                    del self._agents[agent_id]
                    self._revisions.pop(agent_id, None)
                    previous_materialized = self._materialized.pop(
                        agent_id, None
                    )
                    try:
                        self._write_index_to_disk()
                    except OSError:
                        self._restore_cached_snapshot(agent_id, previous)
                        self._restore_revision(agent_id, previous_revision)
                        if previous_materialized is not None:
                            self._materialized[agent_id] = previous_materialized
                        raise
                try:
                    self._durably_remove_identity(agent_id)
                except OSError as e:
                    # Ordinary post-unlink uncertainty is normalized inside
                    # the helper, so exceptions here are safe to roll back.
                    with self._lock:
                        self._restore_cached_snapshot(agent_id, previous)
                        self._restore_revision(agent_id, previous_revision)
                        if previous_materialized is not None:
                            self._materialized[agent_id] = previous_materialized
                        self._write_index_to_disk()
                    logger.warning("Failed to remove identity file for %s: %s", agent_id, str(e))
                    return False
                return True

    def update(self, agent: AgentIdentity) -> bool:
        """Update an existing agent's identity."""
        with self._mutation_guard.write_lease("update") as validated_epoch:
            with self._storage_mutation_lock():
                with self._lock:
                    self._assert_admission_open()
                    self._mutation_guard.assert_writable(
                        "update admission",
                        validated_epoch=validated_epoch,
                    )
                    self._reload_from_disk()
                    if agent.agent_id not in self._agents:
                        return False
                    self._assert_name_available(
                        agent.name,
                        self._agent_channels(agent),
                        exclude_agent_id=agent.agent_id,
                    )
                    previous = self._agents[agent.agent_id].to_dict()
                    previous_revision = self._revisions.get(agent.agent_id)
                    self._agents[agent.agent_id] = agent
                    self._revisions[agent.agent_id] = uuid.uuid4().hex
                    try:
                        self._write_index_to_disk()
                    except OSError:
                        self._restore_cached_snapshot(agent.agent_id, previous)
                        self._restore_revision(
                            agent.agent_id, previous_revision
                        )
                        raise
                    if self._synchronous_name_mutations:
                        synchronous_request = self._make_persist_request(
                            "update",
                            agent,
                            validated_epoch=validated_epoch,
                            previous=previous,
                            revision=self._revisions[agent.agent_id],
                            previous_revision=previous_revision,
                        )
                    else:
                        synchronous_request = self._persist(
                            "update",
                            agent,
                            validated_epoch,
                            previous=previous,
                            revision=self._revisions[agent.agent_id],
                            previous_revision=previous_revision,
                        )
                if synchronous_request is not None:
                    try:
                        self._persist_request(synchronous_request)
                    except OSError:
                        with self._lock:
                            self._restore_cached_snapshot(agent.agent_id, previous)
                            self._restore_revision(
                                agent.agent_id, previous_revision
                            )
                            self._write_index_to_disk()
                        raise
            return True

    def move_agent(self, agent_id: str, source_channel_id: str, target_channel_id: str) -> MoveOutcome:
        """Atomically move an agent from source channel to target channel.

        Uses copy-on-write: snapshots the agent state before mutation, and
        rolls back if persistence fails. Returns a structured MoveOutcome
        with explicit error codes.
        """
        with self._mutation_guard.write_lease("move_agent") as validated_epoch:
            with self._storage_mutation_lock():
                with self._lock:
                    self._assert_admission_open()
                    self._mutation_guard.assert_writable(
                        "move_agent admission",
                        validated_epoch=validated_epoch,
                    )
                    self._reload_from_disk()
                    agent = self._agents.get(agent_id)
                    if agent is None:
                        return MoveOutcome(status=MoveResult.NOT_FOUND)
                    if not self._belongs_to_channel(agent, source_channel_id):
                        return MoveOutcome(status=MoveResult.NOT_IN_SOURCE)
                    try:
                        self._assert_name_available(
                            agent.name,
                            {target_channel_id},
                            exclude_agent_id=agent.agent_id,
                        )
                    except DuplicateAgentNameError as exc:
                        return MoveOutcome(status=MoveResult.DUPLICATE_NAME, error_msg=str(exc))
                    snapshot = agent.to_dict()
                    previous_revision = self._revisions.get(agent_id)
                    groups = [g for g in (agent.member_groups or []) if g != source_channel_id]
                    if target_channel_id not in groups:
                        groups.append(target_channel_id)
                    agent.member_groups = groups
                    agent.owner_group = target_channel_id
                    self._revisions[agent_id] = uuid.uuid4().hex
                    try:
                        self._write_index_to_disk()
                    except OSError as exc:
                        self._restore_cached_snapshot(agent_id, snapshot)
                        self._restore_revision(agent_id, previous_revision)
                        return MoveOutcome(
                            status=MoveResult.PERSIST_FAILED,
                            error_msg=str(exc),
                        )
                    request = self._make_persist_request(
                        "move_agent",
                        agent,
                        validated_epoch=validated_epoch,
                        previous=snapshot,
                        revision=self._revisions[agent_id],
                        previous_revision=previous_revision,
                    )
                try:
                    self._persist_request(request)
                except OSError as e:
                    with self._lock:
                        agent.member_groups = snapshot.get("member_groups", [])
                        agent.owner_group = snapshot.get("owner_group", "")
                        self._restore_revision(agent_id, previous_revision)
                        self._write_index_to_disk()
                    logger.warning(
                        "move_agent: persist failed, rolled back agent=%s error=%s",
                        agent_id, str(e),
                    )
                    return MoveOutcome(status=MoveResult.PERSIST_FAILED, error_msg=str(e))
            # Defensive: verify persisted identity and L1 memory path
            memory_path = os.path.join(self._agents_dir(), agent_id, "MEMORY.md")
            identity_exists = self._read_compat_identity(agent_id) is not None
            logger.debug(
                "move_agent: persisted agent=%s to=%s identity_exists=%s memory_path=%s",
                agent_id,
                target_channel_id,
                identity_exists,
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
        previous: dict | None = None,
        revision: str | None = None,
        previous_revision: str | None = None,
    ) -> _PersistRequest:
        accepted_revision = (
            revision
            or self._revisions.get(agent.agent_id)
            or uuid.uuid4().hex
        )
        return _PersistRequest(
            operation,
            AgentIdentity.from_dict(agent.to_dict()),
            validated_epoch,
            previous,
            accepted_revision,
            previous_revision,
        )

    def _persist_request(self, request: _PersistRequest) -> None:
        with self._mutation_guard.write_lease(
            f"{request.operation} persistence",
            validated_epoch=request.validated_epoch,
        ):
            with self._storage_mutation_lock():
                with self._lock:
                    indexed = self._read_index_records()
                    should_write = indexed is None or self._request_is_current_or_local_prefix(
                        request,
                        indexed,
                    )
                if should_write:
                    self._write_agent_to_disk(request.agent)
                    if indexed is not None:
                        with self._lock:
                            latest = self._read_index_records()
                            if latest is not None:
                                self._load_index_records(latest)
                                self._materialized[request.agent.agent_id] = (
                                    _MaterializedAgent(
                                        request.agent,
                                        request.revision,
                                    )
                                )
                                try:
                                    self._write_index_to_disk()
                                except OSError:
                                    self._compensate_materialization_failure(
                                        request,
                                        latest,
                                    )
                                    raise

    def _compensate_materialization_failure(
        self,
        request: _PersistRequest,
        indexed: dict[str, _IndexedAgent],
    ) -> None:
        """Restore the last durable copy/index after materialized CAS fails."""
        current = indexed.get(request.agent.agent_id)
        if current is None:
            self._durably_remove_identity(request.agent.agent_id)
            return
        fallback = current.materialized
        if fallback is None:
            self._durably_remove_identity(request.agent.agent_id)
        else:
            self._write_agent_to_disk(fallback.identity)
        self._load_index_records(indexed)
        if current.revision == request.revision:
            if fallback is None:
                self._agents.pop(request.agent.agent_id, None)
                self._revisions.pop(request.agent.agent_id, None)
                self._materialized.pop(request.agent.agent_id, None)
            else:
                self._agents[request.agent.agent_id] = fallback.identity
                self._revisions[request.agent.agent_id] = fallback.revision
                self._materialized[request.agent.agent_id] = fallback
        self._write_index_to_disk()

    def _request_is_current_or_local_prefix(
        self,
        request: _PersistRequest,
        indexed: dict[str, _IndexedAgent],
    ) -> bool:
        """Reject stale cross-instance copies while preserving local queue order."""
        current = indexed.get(request.agent.agent_id)
        if current is None:
            return False
        if current.revision == request.revision:
            return True
        accepted = [*self._inflight_requests, *self._persist_queue]
        try:
            position = next(
                index for index, item in enumerate(accepted) if item is request
            )
        except StopIteration:
            return False
        return any(
            later.agent.agent_id == request.agent.agent_id
            and later.revision == current.revision
            for later in accepted[position + 1 :]
        )

    def cutover_authority(
        self,
        advance: Callable[[], AuthoritySnapshot],
    ) -> AuthoritySnapshot:
        """Flush every accepted legacy write before durable authority advance."""

        pending: tuple[_PersistRequest, ...] = ()

        def drain_and_advance() -> AuthoritySnapshot:
            nonlocal pending
            with self._storage_mutation_lock():
                previous_durability = self._require_crash_durable
                self._require_crash_durable = True
                try:
                    with self._lock:
                        self._admission_open = False
                        index_existed = os.path.isfile(self._index_file())
                        self._reload_from_disk()
                        pending = tuple(
                            [*self._inflight_requests, *self._persist_queue]
                        )
                        indexed_agents = tuple(self._agents.values())
                        current_ids = frozenset(self._agents)
                    if index_existed:
                        for orphan_id in self._compat_identity_ids() - current_ids:
                            self._durably_remove_identity(orphan_id)
                    for agent in indexed_agents:
                        self._write_agent_to_disk(agent)
                    with self._lock:
                        self._materialized = {
                            agent_id: _MaterializedAgent(
                                AgentIdentity.from_dict(agent.to_dict()),
                                self._revisions[agent_id],
                            )
                            for agent_id, agent in self._agents.items()
                        }
                        self._write_index_to_disk()
                    return advance()
                finally:
                    self._require_crash_durable = previous_durability

        def clear_flushed_requests() -> None:
            with self._lock:
                pending_ids = {id(request) for request in pending}
                self._persist_queue = [
                    request
                    for request in self._persist_queue
                    if id(request) not in pending_ids
                ]
                self._inflight_requests = [
                    request
                    for request in self._inflight_requests
                    if id(request) not in pending_ids
                ]

        def reopen_admission() -> None:
            with self._lock:
                self._admission_open = True

        return self._mutation_guard.cutover(
            drain_and_advance,
            on_success=clear_flushed_requests,
            on_finish=reopen_admission,
        )

    def _assert_admission_open(self) -> None:
        if not self._admission_open:
            raise RuntimeError("legacy registry admission is closed for cutover")

    def _restore_requests_from_disk(
        self,
        requests: tuple[_PersistRequest, ...],
    ) -> None:
        """Rebuild affected cache entries from disk plus remaining accepted writes.

        The caller must hold ``self._lock`` and must already have removed every
        discarded request from ``_inflight_requests``.  Inflight requests
        precede queued requests because that is their original acceptance
        order.
        """
        agent_ids = tuple(dict.fromkeys(
            request.agent.agent_id for request in requests
        ))
        indexed = self._read_index_records()
        for agent_id in agent_ids:
            if indexed is not None:
                indexed_record = indexed.get(agent_id)
                if indexed_record is None:
                    self._agents.pop(agent_id, None)
                    self._revisions.pop(agent_id, None)
                else:
                    self._agents[agent_id] = indexed_record.identity
                    self._revisions[agent_id] = indexed_record.revision
                continue
            identity = self._read_compat_identity(agent_id)
            if identity is None:
                self._agents.pop(agent_id, None)
                continue
            self._agents[agent_id] = identity
        if indexed is not None:
            return
        affected = set(agent_ids)
        for accepted in [*self._inflight_requests, *self._persist_queue]:
            if accepted.agent.agent_id in affected:
                self._agents[accepted.agent.agent_id] = accepted.agent
                self._revisions[accepted.agent.agent_id] = accepted.revision

    def _discard_inflight_and_reconcile(self, request: _PersistRequest) -> None:
        """Atomically discard one failed request and rebuild its cache entry."""
        with self._storage_mutation_lock():
            with self._lock:
                self._inflight_requests = [
                    item for item in self._inflight_requests if item is not request
                ]
                try:
                    indexed = self._read_index_records()
                    indexed_record = (
                        indexed.get(request.agent.agent_id)
                        if indexed is not None
                        else None
                    )
                    if (
                        indexed is not None
                        and indexed_record is not None
                        and indexed_record.revision == request.revision
                    ):
                        self._load_index_records(indexed)
                        materialized = indexed_record.materialized
                        if materialized is None:
                            self._agents.pop(request.agent.agent_id, None)
                            self._revisions.pop(request.agent.agent_id, None)
                            self._materialized.pop(
                                request.agent.agent_id, None
                            )
                        else:
                            self._agents[request.agent.agent_id] = materialized.identity
                            self._revisions[request.agent.agent_id] = (
                                materialized.revision
                            )
                            self._materialized[
                                request.agent.agent_id
                            ] = materialized
                        self._write_index_to_disk()
                    self._restore_requests_from_disk((request,))
                except RuntimeError as exc:
                    self._agents.pop(request.agent.agent_id, None)
                    self._loaded = False
                    logger.warning(
                        "Failed to reconcile discarded registry write for %s: %s",
                        request.agent.agent_id,
                        exc,
                    )

    def _identity_matches(self, agent: AgentIdentity) -> bool:
        """Return whether cutover/background persistence materialized this admission."""
        identity = self._read_compat_identity(agent.agent_id)
        return identity is not None and identity.to_dict() == agent.to_dict()

    def _read_compat_identity(self, agent_id: str) -> AgentIdentity | None:
        with self._open_agent_directory(agent_id, create=False) as agent_fd:
            if agent_fd is None:
                return None
            return self._read_compat_identity_from_fd(agent_fd, agent_id)

    def _read_compat_identity_from_fd(
        self,
        agent_fd: int,
        agent_id: str,
    ) -> AgentIdentity | None:
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            identity_fd = os.open(
                "identity.json",
                file_flags,
                dir_fd=agent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise OSError("unsafe compatibility identity symlink") from exc
            raise
        try:
            identity_stat = os.fstat(identity_fd)
            if not stat.S_ISREG(identity_stat.st_mode):
                raise OSError("unsafe compatibility identity type")
            try:
                with os.fdopen(identity_fd, "r", encoding="utf-8") as file:
                    identity_fd = -1
                    identity = AgentIdentity.from_dict(json.load(file))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                return None
        finally:
            if identity_fd >= 0:
                os.close(identity_fd)
        if identity.agent_id != agent_id:
            raise OSError("unsafe compatibility identity agent_id mismatch")
        return identity

    def _restore_cached_snapshot(
        self,
        agent_id: str,
        snapshot: dict | None,
    ) -> None:
        if snapshot is None:
            self._agents.pop(agent_id, None)
            return
        self._agents[agent_id] = AgentIdentity.from_dict(snapshot)

    def _restore_revision(
        self,
        agent_id: str,
        revision: str | None,
    ) -> None:
        if revision is None:
            self._revisions.pop(agent_id, None)
        else:
            self._revisions[agent_id] = revision

    def _persist(
        self,
        operation: str,
        agent: AgentIdentity,
        validated_epoch: int,
        *,
        previous: dict | None = None,
        revision: str | None = None,
        previous_revision: str | None = None,
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
                previous=previous,
                revision=revision,
                previous_revision=previous_revision,
            )

        self._persist_queue.append(
            self._make_persist_request(
                operation,
                agent,
                validated_epoch=validated_epoch,
                previous=previous,
                revision=revision,
                previous_revision=previous_revision,
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
                try:
                    self._persist_request(request)
                except StaleAuthorityEpoch as exc:
                    logger.warning("Discarded stale registry write: %s", str(exc))
                    self._discard_inflight_and_reconcile(request)
                except OSError as exc:
                    logger.warning(
                        "Failed background registry write for %s: %s",
                        request.agent.agent_id,
                        exc,
                    )
                    self._discard_inflight_and_reconcile(request)
                else:
                    with self._lock:
                        self._inflight_requests = [
                            item
                            for item in self._inflight_requests
                            if item is not request
                        ]

    def _write_agent_to_disk(self, agent: AgentIdentity) -> None:
        """Durably replace a compatibility identity before reporting success."""
        payload = agent.to_dict()
        with self._open_agent_directory(agent.agent_id, create=True) as agent_fd:
            if agent_fd is None:  # pragma: no cover - create=True contract
                raise OSError("cannot create managed agent directory")
            try:
                existing_identity = os.stat(
                    "identity.json",
                    dir_fd=agent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing_identity = None
            if existing_identity is not None and not stat.S_ISREG(
                existing_identity.st_mode
            ):
                raise OSError("unsafe compatibility identity symlink or type")
            tmp_name = (
                f".identity.json.tmp.{os.getpid()}.{threading.get_ident()}"
            )
            file_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            replaced = False
            try:
                file_fd = os.open(
                    tmp_name,
                    file_flags,
                    0o600,
                    dir_fd=agent_fd,
                )
                with os.fdopen(file_fd, "w", encoding="utf-8") as file:
                    json.dump(
                        payload,
                        file,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(
                    tmp_name,
                    "identity.json",
                    src_dir_fd=agent_fd,
                    dst_dir_fd=agent_fd,
                )
                replaced = True
                try:
                    os.fsync(agent_fd)
                except OSError:
                    observed = self._read_compat_identity_from_fd(
                        agent_fd,
                        agent.agent_id,
                    )
                    if (
                        self._require_crash_durable
                        or observed is None
                        or observed.to_dict() != payload
                    ):
                        raise
                    logger.warning(
                        "Directory fsync failed after visible identity replace; "
                        "continuing with committed-visible semantics: %s",
                        agent.agent_id,
                    )
            finally:
                if not replaced:
                    try:
                        os.unlink(tmp_name, dir_fd=agent_fd)
                    except FileNotFoundError:
                        pass

    def _durably_remove_identity(self, agent_id: str) -> None:
        """Durably unlink a compatibility copy and any now-empty agent dir."""
        if not agent_id or os.path.basename(agent_id) != agent_id:
            raise OSError("unsafe agent directory containment")
        agents_dir = self._agents_dir()
        if not os.path.isdir(agents_dir):
            return
        agents_real = os.path.realpath(agents_dir)
        candidate = os.path.join(agents_real, agent_id)
        candidate_real = os.path.realpath(candidate)
        try:
            contained = os.path.commonpath([agents_real, candidate_real]) == agents_real
        except ValueError:
            contained = False
        if not contained or candidate_real != os.path.abspath(candidate):
            raise OSError("unsafe agent directory symlink or containment")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        agents_fd = os.open(agents_real, directory_flags | nofollow)
        try:
            try:
                agent_fd = os.open(
                    agent_id,
                    directory_flags | nofollow,
                    dir_fd=agents_fd,
                )
            except FileNotFoundError:
                return
            try:
                try:
                    identity_stat = os.stat(
                        "identity.json",
                        dir_fd=agent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    identity_stat = None
                if identity_stat is not None:
                    if not stat.S_ISREG(identity_stat.st_mode):
                        raise OSError("unsafe compatibility identity symlink")
                    os.remove("identity.json", dir_fd=agent_fd)
                    self._fsync_after_visible_change(
                        agent_fd,
                        f"identity unlink for {agent_id}",
                    )
                empty = not os.listdir(agent_fd)
            finally:
                os.close(agent_fd)
            if empty:
                os.rmdir(agent_id, dir_fd=agents_fd)
                self._fsync_after_visible_change(
                    agents_fd,
                    f"agent directory removal for {agent_id}",
                )
        finally:
            os.close(agents_fd)

    def _compat_identity_ids(self) -> set[str]:
        agents_dir = self._agents_dir()
        if not os.path.isdir(agents_dir):
            return set()
        identities: set[str] = set()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        agents_fd = os.open(
            agents_dir,
            directory_flags | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            with os.scandir(agents_dir) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise OSError("unsafe agent directory symlink")
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    agent_fd = os.open(
                        entry.name,
                        directory_flags | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=agents_fd,
                    )
                    try:
                        try:
                            identity_stat = os.stat(
                                "identity.json",
                                dir_fd=agent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(identity_stat.st_mode):
                            raise OSError("unsafe compatibility identity symlink")
                        identities.add(entry.name)
                    finally:
                        os.close(agent_fd)
        finally:
            os.close(agents_fd)
        return identities

    @staticmethod
    def _fsync_directory(path: str) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _fsync_after_visible_change(self, fd: int, label: str) -> None:
        try:
            os.fsync(fd)
        except OSError:
            if self._require_crash_durable:
                raise
            logger.warning(
                "Directory fsync failed after visible %s; continuing with "
                "committed-visible semantics",
                label,
            )

    def refresh_agent(self, agent_id: str) -> Optional[AgentIdentity]:
        """Re-read a single agent's identity from disk and update the in-memory cache.

        Use this after a cross-engine move to ensure the target engine's
        registry sees the updated membership without a full reload.
        Returns the refreshed AgentIdentity, or None if the file does not exist.
        """
        with self._storage_mutation_lock():
            with self._lock:
                indexed = self._read_index_records()
                if indexed is None:
                    self._agents = self._scan_agents_dir()
                    self._revisions = {
                        known_id: self._legacy_revision(agent.to_dict())
                        for known_id, agent in self._agents.items()
                    }
                    self._materialized = {
                        known_id: _MaterializedAgent(
                            AgentIdentity.from_dict(agent.to_dict()),
                            self._revisions[known_id],
                        )
                        for known_id, agent in self._agents.items()
                    }
                    self._write_index_to_disk()
                else:
                    self._load_index_records(indexed)
                self._loaded = True
                return self._agents.get(agent_id)

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

    @staticmethod
    def _agent_channels(agent: AgentIdentity) -> set[str]:
        return {
            channel_id
            for channel_id in [agent.owner_group, *(agent.member_groups or [])]
            if channel_id
        }

    def _assert_name_available(
        self,
        name: str,
        channel_ids: set[str],
        *,
        exclude_agent_id: str,
    ) -> None:
        if not name or not channel_ids:
            return
        name_key = name.casefold()
        for existing_agent in self._agents.values():
            if existing_agent.agent_id == exclude_agent_id:
                continue
            if existing_agent.name.casefold() != name_key:
                continue
            conflicting_channels = channel_ids & self._agent_channels(existing_agent)
            if conflicting_channels:
                channel_id = sorted(conflicting_channels)[0]
                raise DuplicateAgentNameError(
                    f"Agent name '{name}' already exists in channel {channel_id}"
                )

    def clear(self) -> None:
        """Clear in-memory cache (does not delete files)."""
        with self._lock:
            self._agents.clear()
            self._revisions.clear()
            self._materialized.clear()
            self._loaded = False
