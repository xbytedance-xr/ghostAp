"""Document materializers and memory compatibility facade for canonical employees."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DataKind
from .projection import DataProjectionState


@dataclass(frozen=True)
class DocumentPath:
    """Resolved canonical path for a materialized document."""

    agent_id: str
    kind: DataKind
    relative: str
    absolute: Path


class EmployeeDocumentMaterializer:
    """Writes canonical employee documents from projection state."""

    def __init__(self, agents_root: str | Path) -> None:
        self._root = Path(agents_root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def resolve_path(self, agent_id: str, kind: DataKind, source_id: str = "") -> DocumentPath:
        if kind is DataKind.L1_MEMORY:
            relative = "memory/MEMORY.md"
        elif kind is DataKind.MEMORY_SUMMARY:
            safe_id = hashlib.sha256(source_id.encode()).hexdigest()[:16]
            relative = f"memory/summary_{safe_id}.md"
        elif kind is DataKind.SKILL_PROFILE:
            relative = "skill_profile.json"
        elif kind is DataKind.REASONING:
            safe_id = hashlib.sha256(source_id.encode()).hexdigest()
            relative = f"reasoning/{safe_id}.json"
        else:
            raise ValueError(f"unsupported kind: {kind}")
        return DocumentPath(
            agent_id=agent_id,
            kind=kind,
            relative=relative,
            absolute=self._root / agent_id / relative,
        )

    def materialize(
        self,
        agent_id: str,
        kind: DataKind,
        source_id: str,
        content: bytes,
        content_hash: str,
    ) -> DocumentPath:
        """Write one document file atomically with fsync."""
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise ValueError("content hash verification failed")
        doc_path = self.resolve_path(agent_id, kind, source_id)
        target = doc_path.absolute
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_name = f".{target.stem}-{secrets.token_hex(8)}.tmp"
        temp_path = target.parent / temp_name
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(str(temp_path), flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(temp_path), str(target))
        self._fsync_dir(target.parent)
        return doc_path

    def materialize_from_state(
        self,
        state: DataProjectionState,
        agent_id: str,
        blob_reader: Any,
    ) -> list[DocumentPath]:
        """Rebuild all documents for one agent from projected metadata + blob reads."""
        paths: list[DocumentPath] = []
        for doc_id, metadata in state.employee_documents.items():
            if metadata.agent_id != agent_id or metadata.tombstoned:
                continue
            latest_key = (
                metadata.tenant_key,
                metadata.agent_id,
                metadata.kind.value,
                metadata.source_id,
            )
            if state.latest_employee_document.get(latest_key) != doc_id:
                continue
            from ..journal.blob_store import BlobRef
            ref = BlobRef.from_dict(metadata.blob_ref)
            content = blob_reader(ref)
            path = self.materialize(
                agent_id=agent_id,
                kind=metadata.kind,
                source_id=metadata.source_id,
                content=content,
                content_hash=metadata.content_hash,
            )
            paths.append(path)
        return paths

    def verify(self, agent_id: str, kind: DataKind, source_id: str, expected_hash: str) -> bool:
        """Check if file on disk matches expected content hash."""
        doc_path = self.resolve_path(agent_id, kind, source_id)
        target = doc_path.absolute
        if not target.exists():
            return False
        try:
            file_stat = os.stat(str(target), follow_symlinks=False)
            if not stat.S_ISREG(file_stat.st_mode):
                return False
        except OSError:
            return False
        content = target.read_bytes()
        return hashlib.sha256(content).hexdigest() == expected_hash

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class EmployeeMemoryFacade:
    """Compatibility facade that reads canonical first, falls back to legacy.

    For canonical employees: reads from materialized projection path.
    For legacy virtual agents: delegates to existing MemoryManager.
    """

    def __init__(
        self,
        *,
        materializer: EmployeeDocumentMaterializer,
        state: DataProjectionState,
        legacy_base_path: str | Path | None = None,
    ) -> None:
        self._materializer = materializer
        self._state = state
        self._legacy_base = Path(legacy_base_path) if legacy_base_path else None

    def read_l1(self, agent_id: str, tenant_key: str) -> str | None:
        """Read full L1 memory. Canonical first, legacy fallback."""
        canonical = self._read_canonical(agent_id, DataKind.L1_MEMORY, "l1_memory")
        if canonical is not None:
            return canonical
        legacy = self._read_legacy_l1(agent_id)
        if legacy is not None and canonical is not None:
            raise MemoryConflictError(
                f"canonical and legacy L1 both exist for {agent_id}"
            )
        return legacy

    def read_memory_summary(
        self, agent_id: str, tenant_key: str, chat_id: str, thread_root_id: str = ""
    ) -> str | None:
        """Read chat-scoped memory summary."""
        from .models import EmployeeDataDocumentV1
        source_id = EmployeeDataDocumentV1.memory_summary_source_id(
            chat_id=chat_id, thread_root_id=thread_root_id
        )
        return self._read_canonical(agent_id, DataKind.MEMORY_SUMMARY, source_id)

    def read_skill_profile(self, agent_id: str) -> dict | None:
        """Read skill profile JSON."""
        content = self._read_canonical(agent_id, DataKind.SKILL_PROFILE, "skill_profile")
        if content is None:
            return self._read_legacy_skill(agent_id)
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None

    def is_canonical(self, agent_id: str) -> bool:
        """Check if agent has any canonical documents."""
        for key in self._state.latest_employee_document:
            if key[1] == agent_id:
                return True
        return False

    def _read_canonical(self, agent_id: str, kind: DataKind, source_id: str) -> str | None:
        doc_path = self._materializer.resolve_path(agent_id, kind, source_id)
        target = doc_path.absolute
        if not target.exists():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _read_legacy_l1(self, agent_id: str) -> str | None:
        if self._legacy_base is None:
            return None
        path = self._legacy_base / "agents" / agent_id / "memory" / "MEMORY.md"
        if not path.exists():
            path = self._legacy_base / "agents" / agent_id / "MEMORY.md"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _read_legacy_skill(self, agent_id: str) -> dict | None:
        if self._legacy_base is None:
            return None
        path = self._legacy_base / "agents" / agent_id / "skill_profile.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


class MemoryConflictError(RuntimeError):
    """Canonical and legacy memory both exist with different content."""
