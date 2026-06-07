"""WorkflowHistory — lightweight persistence for past workflow run records.

Storage: ``{root_path}/.ghostap/workflow_history.json``
Format: JSON list of HistoryEntry dicts, most recent first, capped at 50 entries.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HISTORY_FILENAME = ".ghostap/workflow_history.json"
_MAX_ENTRIES = 50


class HistoryEntry:
    """One workflow run record."""

    __slots__ = (
        "workflow_id",
        "name",
        "status",
        "started_at",
        "finished_at",
        "total_tokens",
        "total_agents",
        "phases_count",
        "error",
    )

    def __init__(
        self,
        workflow_id: str,
        name: str,
        status: str,
        started_at: float,
        finished_at: Optional[float] = None,
        total_tokens: int = 0,
        total_agents: int = 0,
        phases_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.name = name
        self.status = status
        self.started_at = started_at
        self.finished_at = finished_at
        self.total_tokens = total_tokens
        self.total_agents = total_agents
        self.phases_count = phases_count
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_tokens": self.total_tokens,
            "total_agents": self.total_agents,
            "phases_count": self.phases_count,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEntry":
        return cls(
            workflow_id=data.get("workflow_id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            started_at=data.get("started_at", 0.0),
            finished_at=data.get("finished_at"),
            total_tokens=data.get("total_tokens", 0),
            total_agents=data.get("total_agents", 0),
            phases_count=data.get("phases_count", 0),
            error=data.get("error"),
        )


class WorkflowHistory:
    """Read/append workflow run history for a project root."""

    def __init__(self, root_path: str) -> None:
        self._path = Path(root_path) / _HISTORY_FILENAME
        self._lock = threading.Lock()

    def record(self, project: Any) -> None:
        """Append a completed/failed workflow run from a WorkflowProject model."""
        entry = HistoryEntry(
            workflow_id=project.workflow_id or "",
            name=project.name or "unnamed",
            status=project.status.value if hasattr(project.status, "value") else str(project.status),
            started_at=project.started_at or time.time(),
            finished_at=project.finished_at,
            total_tokens=project.metrics.total_tokens if project.metrics else 0,
            total_agents=project.metrics.total_agents if project.metrics else 0,
            phases_count=len(project.phases) if project.phases else 0,
            error=(project.error or "")[:120] if project.error else None,
        )

        with self._lock:
            entries = self._load()
            entries.insert(0, entry)
            # Cap at max entries
            entries = entries[:_MAX_ENTRIES]
            self._save(entries)

    def list_recent(self, limit: int = 10) -> list[HistoryEntry]:
        """Return the N most recent history entries."""
        with self._lock:
            entries = self._load()
        return entries[:limit]

    def _load(self) -> list[HistoryEntry]:
        """Load history from disk. Returns empty list on error."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [HistoryEntry.from_dict(d) for d in data]
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Failed to load workflow history: %s", exc)
        return []

    def _save(self, entries: list[HistoryEntry]) -> None:
        """Persist history to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.write_text(
                json.dumps([e.to_dict() for e in entries], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Failed to save workflow history: %s", exc)
