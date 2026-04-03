from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ThreadContext:
    thread_root_id: str
    chat_id: str
    project_id: str
    mode: str = "smart"
    tool_name: Optional[str] = None
    model_name: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    @property
    def session_key_suffix(self) -> str:
        return f"t:{self.thread_root_id}"

    def touch(self) -> None:
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "thread_root_id": self.thread_root_id,
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "mode": self.mode,
            "tool_name": self.tool_name,
            "model_name": self.model_name,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThreadContext:
        return cls(
            thread_root_id=data["thread_root_id"],
            chat_id=data["chat_id"],
            project_id=data["project_id"],
            mode=data.get("mode", "smart"),
            tool_name=data.get("tool_name"),
            model_name=data.get("model_name"),
            created_at=data.get("created_at", time.time()),
            last_active=data.get("last_active", time.time()),
        )
