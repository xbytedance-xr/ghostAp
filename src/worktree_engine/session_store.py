from __future__ import annotations

import threading
from dataclasses import dataclass

from .models import WorktreeRuntimeState


@dataclass(frozen=True)
class WorktreeSessionKey:
    project_id: str
    chat_id: str
    thread_root_id: str

    @property
    def slug(self) -> str:
        raw = f"{self.project_id}-{self.chat_id}-{self.thread_root_id}"
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw)[:96]


class WorktreeSessionStore:
    """Thread-safe topic-scoped Worktree runtime state store."""

    def __init__(self) -> None:
        self._states: dict[WorktreeSessionKey, WorktreeRuntimeState] = {}
        self._lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock

    def get(self, key: WorktreeSessionKey) -> WorktreeRuntimeState | None:
        with self._lock:
            return self._states.get(key)

    def get_or_create(self, key: WorktreeSessionKey) -> WorktreeRuntimeState:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = WorktreeRuntimeState()
                self._states[key] = state
            return state

    def reset(self, key: WorktreeSessionKey) -> WorktreeRuntimeState:
        with self._lock:
            state = WorktreeRuntimeState()
            self._states[key] = state
            return state

    def put(self, key: WorktreeSessionKey, state: WorktreeRuntimeState) -> WorktreeRuntimeState:
        with self._lock:
            self._states[key] = state
            return state

    def remove(self, key: WorktreeSessionKey) -> WorktreeRuntimeState | None:
        with self._lock:
            return self._states.pop(key, None)

    def keys_for_project(self, project_id: str) -> list[WorktreeSessionKey]:
        with self._lock:
            return [key for key in self._states if key.project_id == project_id]
