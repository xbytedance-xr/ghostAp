"""Slock Engine data models.

Core dataclasses and enums for the multi-Agent collaboration engine.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentStatus(Enum):
    """Agent lifecycle state machine."""

    IDLE = "idle"
    WAKING = "waking"
    THINKING = "thinking"
    RUNNING = "running"
    CHECKING = "checking"
    SENDING = "sending"


class TaskStatus(Enum):
    """Task lifecycle states."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


# Agent role color mapping for card rendering
AGENT_ROLE_COLORS: dict[str, str] = {
    "coder": "blue",
    "writer": "green",
    "reviewer": "orange",
    "tester": "purple",
    "planner": "red",
    "architect": "indigo",
    "custom": "grey",
}


@dataclass
class AgentIdentity:
    """Agent identity definition — persisted as identity.yaml."""

    agent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    emoji: str = "🤖"
    agent_type: str = "coco"  # coco/claude/codex/gemini/ttadk
    model_name: str = ""
    system_prompt: str = ""
    role: str = "custom"  # coder/writer/reviewer/tester/planner/architect/custom
    permissions: list[str] = field(default_factory=lambda: ["shell", "file_write", "git"])
    memory_path: str = ""
    owner_group: str = ""  # chat_id of owning group
    created_at: float = field(default_factory=time.time)

    @property
    def display_name(self) -> str:
        return f"{self.emoji} {self.name}" if self.name else f"{self.emoji} Agent"

    @property
    def card_color(self) -> str:
        return AGENT_ROLE_COLORS.get(self.role, "grey")

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "emoji": self.emoji,
            "agent_type": self.agent_type,
            "model_name": self.model_name,
            "system_prompt": self.system_prompt,
            "role": self.role,
            "permissions": self.permissions,
            "memory_path": self.memory_path,
            "owner_group": self.owner_group,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentIdentity:
        return cls(
            agent_id=data.get("agent_id", str(uuid.uuid4())),
            name=data.get("name", ""),
            emoji=data.get("emoji", "🤖"),
            agent_type=data.get("agent_type", "coco"),
            model_name=data.get("model_name", ""),
            system_prompt=data.get("system_prompt", ""),
            role=data.get("role", "custom"),
            permissions=data.get("permissions", ["shell", "file_write", "git"]),
            memory_path=data.get("memory_path", ""),
            owner_group=data.get("owner_group", ""),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class SlockTask:
    """A task that can be claimed and executed by an Agent."""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    status: TaskStatus = TaskStatus.TODO
    claimed_by: Optional[str] = None  # agent_id
    claimed_at: Optional[float] = None
    created_in: str = ""  # channel_id
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "status": self.status.value,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "created_in": self.created_in,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SlockTask:
        return cls(
            task_id=data.get("task_id", str(uuid.uuid4())),
            content=data.get("content", ""),
            status=TaskStatus(data.get("status", "todo")),
            claimed_by=data.get("claimed_by"),
            claimed_at=data.get("claimed_at"),
            created_in=data.get("created_in", ""),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class SlockChannel:
    """A Feishu group with slock mode activated."""

    channel_id: str = ""
    name: str = ""
    agents: list[str] = field(default_factory=list)  # agent_id list
    shared_memory_path: str = ""
    team_name: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "agents": self.agents,
            "shared_memory_path": self.shared_memory_path,
            "team_name": self.team_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SlockChannel:
        return cls(
            channel_id=data.get("channel_id", ""),
            name=data.get("name", ""),
            agents=data.get("agents", []),
            shared_memory_path=data.get("shared_memory_path", ""),
            team_name=data.get("team_name", ""),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class SlockMemory:
    """Three-section memory structure for an Agent."""

    role: str = ""  # Role definition
    key_knowledge: str = ""  # Long-term knowledge
    active_context: str = ""  # Active working context

    def to_markdown(self) -> str:
        sections = []
        if self.role:
            sections.append(f"# Role\n{self.role}")
        if self.key_knowledge:
            sections.append(f"# Key Knowledge\n{self.key_knowledge}")
        if self.active_context:
            sections.append(f"# Active Context\n{self.active_context}")
        return "\n\n".join(sections) if sections else ""

    @classmethod
    def from_markdown(cls, content: str) -> SlockMemory:
        role = ""
        key_knowledge = ""
        active_context = ""

        if not content.strip():
            return cls()

        sections: dict[str, str] = {}
        current_section = ""
        current_lines: list[str] = []

        for line in content.split("\n"):
            if line.startswith("# "):
                if current_section:
                    sections[current_section] = "\n".join(current_lines).strip()
                current_section = line[2:].strip().lower()
                current_lines = []
            else:
                current_lines.append(line)

        if current_section:
            sections[current_section] = "\n".join(current_lines).strip()

        role = sections.get("role", "")
        key_knowledge = sections.get("key knowledge", "")
        active_context = sections.get("active context", "")

        return cls(role=role, key_knowledge=key_knowledge, active_context=active_context)


@dataclass
class SkillProfile:
    """Skill profile for automatic task assignment scoring."""

    tag: str = ""
    success_rate: float = 50.0  # 0-100
    total_tasks: int = 0
    last_active: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "success_rate": self.success_rate,
            "total_tasks": self.total_tasks,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SkillProfile:
        return cls(
            tag=data.get("tag", ""),
            success_rate=data.get("success_rate", 50.0),
            total_tasks=data.get("total_tasks", 0),
            last_active=data.get("last_active", 0.0),
        )
