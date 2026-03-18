import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ProjectStatus(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    BUSY = "busy"
    SUSPENDED = "suspended"
    CLOSED = "closed"


@dataclass
class ConversationItem:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    message_id: Optional[str] = None


@dataclass
class Task:
    task_id: str
    task_type: str
    payload: dict
    status: str = "pending"
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class SessionSnapshot:
    session_id: str
    query_count: int
    last_query: str
    is_resumable: bool = True


# Backward-compatible aliases
CocoSessionSnapshot = SessionSnapshot
ClaudeSessionSnapshot = SessionSnapshot
TtadkSessionSnapshot = SessionSnapshot


@dataclass
class ProjectContext:
    project_id: str
    project_name: str
    root_path: str
    working_dir: str = ""
    status: ProjectStatus = ProjectStatus.IDLE
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    coco_session_snapshot: Optional[SessionSnapshot] = None
    coco_mode: bool = False

    claude_session_snapshot: Optional[SessionSnapshot] = None
    claude_mode: bool = False

    ttadk_session_snapshot: Optional[SessionSnapshot] = None
    ttadk_mode: bool = False

    task_queue: list[Task] = field(default_factory=list)
    current_task: Optional[Task] = None

    conversation_history: list[ConversationItem] = field(default_factory=list)
    max_history_size: int = 20

    theme_color: str = "green"
    emoji_prefix: str = "🟢"

    env_vars: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.working_dir:
            self.working_dir = self.root_path
        self.root_path = os.path.expanduser(self.root_path)
        self.working_dir = os.path.expanduser(self.working_dir)

    def touch(self):
        self.last_active = time.time()

    def add_conversation(self, role: str, content: str, message_id: Optional[str] = None):
        item = ConversationItem(role=role, content=content, message_id=message_id)
        self.conversation_history.append(item)
        if len(self.conversation_history) > self.max_history_size:
            self.conversation_history = self.conversation_history[-self.max_history_size :]
        self.touch()

    def set_coco_mode(self, enabled: bool, session_id: Optional[str] = None, query_count: int = 0):
        self.coco_mode = enabled
        if enabled and session_id:
            self.coco_session_snapshot = CocoSessionSnapshot(
                session_id=session_id, query_count=query_count, last_query="", is_resumable=True
            )
        elif not enabled and self.coco_session_snapshot:
            self.coco_session_snapshot.is_resumable = True

    def update_coco_snapshot(self, query: str, query_count: int):
        if self.coco_session_snapshot:
            self.coco_session_snapshot.last_query = query
            self.coco_session_snapshot.query_count = query_count

    def set_claude_mode(self, enabled: bool, session_id: Optional[str] = None, query_count: int = 0):
        self.claude_mode = enabled
        if enabled and session_id:
            self.claude_session_snapshot = ClaudeSessionSnapshot(
                session_id=session_id, query_count=query_count, last_query="", is_resumable=True
            )
        elif not enabled and self.claude_session_snapshot:
            self.claude_session_snapshot.is_resumable = True

    def update_claude_snapshot(self, query: str, query_count: int, session_id: Optional[str] = None):
        if self.claude_session_snapshot:
            self.claude_session_snapshot.last_query = query
            self.claude_session_snapshot.query_count = query_count
            if session_id:
                self.claude_session_snapshot.session_id = session_id

    def set_ttadk_mode(self, enabled: bool, session_id: Optional[str] = None, query_count: int = 0):
        self.ttadk_mode = enabled
        if enabled and session_id:
            self.ttadk_session_snapshot = TtadkSessionSnapshot(
                session_id=session_id, query_count=query_count, last_query="", is_resumable=True
            )
        elif not enabled and self.ttadk_session_snapshot:
            self.ttadk_session_snapshot.is_resumable = True

    def update_ttadk_snapshot(self, query: str, query_count: int, session_id: Optional[str] = None):
        if self.ttadk_session_snapshot:
            self.ttadk_session_snapshot.last_query = query
            self.ttadk_session_snapshot.query_count = query_count
            if session_id:
                self.ttadk_session_snapshot.session_id = session_id

    def get_status_emoji(self) -> str:
        status_map = {
            ProjectStatus.IDLE: "⚪",
            ProjectStatus.ACTIVE: self.emoji_prefix,
            ProjectStatus.BUSY: "🟡",
            ProjectStatus.SUSPENDED: "⏸️",
            ProjectStatus.CLOSED: "❌",
        }
        return status_map.get(self.status, "⚪")

    def to_snapshot(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "root_path": self.root_path,
            "working_dir": self.working_dir,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "coco_mode": self.coco_mode,
            "coco_session_snapshot": {
                "session_id": self.coco_session_snapshot.session_id,
                "query_count": self.coco_session_snapshot.query_count,
                "last_query": self.coco_session_snapshot.last_query,
                "is_resumable": self.coco_session_snapshot.is_resumable,
            }
            if self.coco_session_snapshot
            else None,
            "claude_mode": self.claude_mode,
            "claude_session_snapshot": {
                "session_id": self.claude_session_snapshot.session_id,
                "query_count": self.claude_session_snapshot.query_count,
                "last_query": self.claude_session_snapshot.last_query,
                "is_resumable": self.claude_session_snapshot.is_resumable,
            }
            if self.claude_session_snapshot
            else None,
            "ttadk_mode": self.ttadk_mode,
            "ttadk_session_snapshot": {
                "session_id": self.ttadk_session_snapshot.session_id,
                "query_count": self.ttadk_session_snapshot.query_count,
                "last_query": self.ttadk_session_snapshot.last_query,
                "is_resumable": self.ttadk_session_snapshot.is_resumable,
            }
            if self.ttadk_session_snapshot
            else None,
            "theme_color": self.theme_color,
            "emoji_prefix": self.emoji_prefix,
            "env_vars": self.env_vars,
            "conversation_history": [
                {
                    "role": item.role,
                    "content": item.content,
                    "timestamp": item.timestamp,
                    "message_id": item.message_id,
                }
                for item in self.conversation_history
            ],
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "ProjectContext":
        ctx = cls(
            project_id=data["project_id"],
            project_name=data["project_name"],
            root_path=data["root_path"],
            working_dir=data.get("working_dir", data["root_path"]),
            status=ProjectStatus(data.get("status", "idle")),
            created_at=data.get("created_at", time.time()),
            last_active=data.get("last_active", time.time()),
            coco_mode=data.get("coco_mode", False),
            claude_mode=data.get("claude_mode", False),
            ttadk_mode=data.get("ttadk_mode", False),
            theme_color=data.get("theme_color", "green"),
            emoji_prefix=data.get("emoji_prefix", "🟢"),
            env_vars=data.get("env_vars", {}),
        )
        if data.get("coco_session_snapshot"):
            snap = data["coco_session_snapshot"]
            ctx.coco_session_snapshot = CocoSessionSnapshot(
                session_id=snap["session_id"],
                query_count=snap["query_count"],
                last_query=snap["last_query"],
                is_resumable=snap.get("is_resumable", True),
            )
        if data.get("claude_session_snapshot"):
            snap = data["claude_session_snapshot"]
            ctx.claude_session_snapshot = ClaudeSessionSnapshot(
                session_id=snap["session_id"],
                query_count=snap["query_count"],
                last_query=snap["last_query"],
                is_resumable=snap.get("is_resumable", True),
            )
        if data.get("ttadk_session_snapshot"):
            snap = data["ttadk_session_snapshot"]
            ctx.ttadk_session_snapshot = TtadkSessionSnapshot(
                session_id=snap["session_id"],
                query_count=snap["query_count"],
                last_query=snap["last_query"],
                is_resumable=snap.get("is_resumable", True),
            )
        for item_data in data.get("conversation_history", []):
            ctx.conversation_history.append(
                ConversationItem(
                    role=item_data["role"],
                    content=item_data["content"],
                    timestamp=item_data.get("timestamp", time.time()),
                    message_id=item_data.get("message_id"),
                )
            )
        return ctx
