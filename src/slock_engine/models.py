"""Slock Engine data models.

Core dataclasses and enums for the multi-Agent collaboration engine.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Escalation resolution option constants (bilingual support)
RETRY_OPTIONS: frozenset[str] = frozenset({"Retry", "重试"})
SKIP_OPTIONS: frozenset[str] = frozenset({"Skip", "跳过"})
ABORT_OPTIONS: frozenset[str] = frozenset({"Abort", "中止"})


class AgentStatus(Enum):
    """Agent lifecycle state machine."""

    IDLE = "idle"
    WAKING = "waking"
    THINKING = "thinking"
    RUNNING = "running"
    CHECKING = "checking"
    SENDING = "sending"
    MOVING = "moving"
    DISCUSSING = "discussing"


class DiscussionStatus(Enum):
    """Discussion thread lifecycle states."""

    ACTIVE = "active"
    CONVERGED = "converged"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MANUALLY_STOPPED = "manually_stopped"


class TaskStatus(Enum):
    """Task lifecycle states."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


class EscalationLevel(Enum):
    """Escalation severity levels."""

    WARNING = "warning"      # Agent can continue but wants guidance
    BLOCKED = "blocked"      # Agent cannot proceed, needs admin decision
    CRITICAL = "critical"    # Fatal error, immediate admin attention needed


@dataclass
class EscalationRequest:
    """A request for admin intervention from an agent."""

    escalation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    agent_name: str = ""
    task_id: Optional[str] = None
    level: EscalationLevel = EscalationLevel.BLOCKED
    reason: str = ""
    context: str = ""  # Additional context (truncated conversation, error details)
    options: tuple[str, ...] = field(default_factory=tuple)  # Suggested resolution options (frozen)
    resolved: bool = False
    resolution: str = ""  # Admin's resolution choice
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    card_message_id: Optional[str] = None  # Feishu message_id of the escalation card (for update)

    def __post_init__(self) -> None:
        # Freeze options to tuple for thread-safety — prevents mutation after creation.
        if isinstance(self.options, list):
            object.__setattr__(self, "options", tuple(self.options))

    def to_dict(self) -> dict:
        return {
            "escalation_id": self.escalation_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "task_id": self.task_id,
            "level": self.level.value,
            "reason": self.reason,
            "context": self.context,
            "options": list(self.options),
            "resolved": self.resolved,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "card_message_id": self.card_message_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EscalationRequest":
        return cls(
            escalation_id=data.get("escalation_id", str(uuid.uuid4())),
            agent_id=data.get("agent_id", ""),
            agent_name=data.get("agent_name", ""),
            task_id=data.get("task_id"),
            level=EscalationLevel(data.get("level", "blocked")),
            reason=data.get("reason", ""),
            context=data.get("context", ""),
            options=data.get("options", []),
            resolved=data.get("resolved", False),
            resolution=data.get("resolution", ""),
            created_at=data.get("created_at", time.time()),
            resolved_at=data.get("resolved_at"),
            card_message_id=data.get("card_message_id"),
        )


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
    notes_path: str = ""
    workspace_path: str = ""
    owner_group: str = ""  # chat_id of owning group
    member_groups: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # Sanitize agent_id to prevent path traversal
        self.agent_id = re.sub(r'[^A-Za-z0-9_.:-]+', '_', self.agent_id)
        if self.owner_group and self.owner_group not in self.member_groups:
            self.member_groups.append(self.owner_group)

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
            "notes_path": self.notes_path,
            "workspace_path": self.workspace_path,
            "owner_group": self.owner_group,
            "member_groups": self.member_groups,
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
            notes_path=data.get("notes_path", ""),
            workspace_path=data.get("workspace_path", ""),
            owner_group=data.get("owner_group", ""),
            member_groups=data.get("member_groups", []),
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
    resolved_reason: Optional[str] = None  # Non-None for abnormal completion (e.g. "超时中止")

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "status": self.status.value,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "created_in": self.created_in,
            "created_at": self.created_at,
            "resolved_reason": self.resolved_reason,
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
            resolved_reason=data.get("resolved_reason"),
        )


@dataclass
class SlockChannel:
    """A Feishu group with slock mode activated."""

    channel_id: str = ""
    name: str = ""
    agents: list[str] = field(default_factory=list)  # agent_id list
    shared_memory_path: str = ""
    team_name: str = ""
    owner_id: str = ""  # User who created this team (for permission checks)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "agents": self.agents,
            "shared_memory_path": self.shared_memory_path,
            "team_name": self.team_name,
            "owner_id": self.owner_id,
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
            owner_id=data.get("owner_id", ""),
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

        _KNOWN_SECTIONS = {"role", "key knowledge", "active context"}

        sections: dict[str, str] = {}
        current_section = ""
        current_lines: list[str] = []

        for line in content.split("\n"):
            if line.startswith("# "):
                candidate = line[2:].strip().lower()
                if candidate in _KNOWN_SECTIONS:
                    if current_section:
                        sections[current_section] = "\n".join(current_lines).strip()
                    current_section = candidate
                    current_lines = []
                else:
                    # Not a known section header — treat as content
                    current_lines.append(line)
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


# ---------------------------------------------------------------------------
# Discussion Protocol Models
# ---------------------------------------------------------------------------


@dataclass
class DiscussionConfig:
    """Configuration for an inter-agent discussion session."""

    max_rounds: int = 3
    token_budget: int = 50000
    trigger_rules: list[str] = field(default_factory=lambda: ["coder->reviewer"])
    convergence_threshold: float = 0.85  # Similarity threshold to declare convergence
    discussion_timeout: int = 300  # Total discussion timeout in seconds

    def to_dict(self) -> dict:
        return {
            "max_rounds": self.max_rounds,
            "token_budget": self.token_budget,
            "trigger_rules": self.trigger_rules,
            "convergence_threshold": self.convergence_threshold,
            "discussion_timeout": self.discussion_timeout,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiscussionConfig:
        return cls(
            max_rounds=data.get("max_rounds", 3),
            token_budget=data.get("token_budget", 50000),
            trigger_rules=data.get("trigger_rules", ["coder->reviewer"]),
            convergence_threshold=data.get("convergence_threshold", 0.85),
            discussion_timeout=data.get("discussion_timeout", 300),
        )


@dataclass
class DiscussionMessage:
    """A single message within an inter-agent discussion thread."""

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sender_agent_id: str = ""
    receiver_agent_id: str = ""
    content: str = ""
    round_num: int = 0
    timestamp: float = field(default_factory=time.time)
    token_count: int = 0  # Estimated tokens consumed by this message

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "sender_agent_id": self.sender_agent_id,
            "sender_display_name": self.sender_agent_id,
            "receiver_agent_id": self.receiver_agent_id,
            "content": self.content,
            "round_num": self.round_num,
            "timestamp": self.timestamp,
            "token_count": self.token_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiscussionMessage:
        return cls(
            message_id=data.get("message_id", str(uuid.uuid4())),
            sender_agent_id=data.get("sender_agent_id", ""),
            receiver_agent_id=data.get("receiver_agent_id", ""),
            content=data.get("content", ""),
            round_num=data.get("round_num", 0),
            timestamp=data.get("timestamp", time.time()),
            token_count=data.get("token_count", 0),
        )


@dataclass
class DiscussionThread:
    """A discussion thread between multiple agents."""

    thread_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str = ""
    participants: list[str] = field(default_factory=list)  # agent_id list
    messages: list[DiscussionMessage] = field(default_factory=list)
    _status_value: DiscussionStatus = field(default=DiscussionStatus.ACTIVE, init=False, repr=False)
    _status_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    config: DiscussionConfig = field(default_factory=DiscussionConfig)
    trigger_reason: str = ""  # Why this discussion was triggered
    conclusion: str = ""  # Final conclusion after convergence
    total_tokens_used: int = 0
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def __init__(
        self,
        thread_id: str | None = None,
        channel_id: str = "",
        participants: list[str] | None = None,
        messages: list[DiscussionMessage] | None = None,
        status: DiscussionStatus = DiscussionStatus.ACTIVE,
        config: DiscussionConfig | None = None,
        trigger_reason: str = "",
        conclusion: str = "",
        total_tokens_used: int = 0,
        created_at: float | None = None,
        completed_at: Optional[float] = None,
    ) -> None:
        self.thread_id = thread_id if thread_id is not None else str(uuid.uuid4())
        self.channel_id = channel_id
        self.participants = participants if participants is not None else []
        self.messages = messages if messages is not None else []
        self._status_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._status_value = status
        self.config = config if config is not None else DiscussionConfig()
        self.trigger_reason = trigger_reason
        self.conclusion = conclusion
        self.total_tokens_used = total_tokens_used
        self.created_at = created_at if created_at is not None else time.time()
        self.completed_at = completed_at

    @property
    def status(self) -> DiscussionStatus:
        with self._status_lock:
            return self._status_value

    @status.setter
    def status(self, value: DiscussionStatus) -> None:
        with self._status_lock:
            self._status_value = value

    @property
    def current_round(self) -> int:
        if not self.messages:
            return 0
        return max(m.round_num for m in self.messages)

    @property
    def is_active(self) -> bool:
        return self.status == DiscussionStatus.ACTIVE

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "channel_id": self.channel_id,
            "participants": self.participants,
            "messages": [m.to_dict() for m in self.messages],
            "status": self.status.value,
            "config": self.config.to_dict(),
            "trigger_reason": self.trigger_reason,
            "conclusion": self.conclusion,
            "total_tokens_used": self.total_tokens_used,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiscussionThread:
        messages = [DiscussionMessage.from_dict(m) for m in data.get("messages", [])]
        config = DiscussionConfig.from_dict(data.get("config", {}))
        status_val = data.get("status", "active")
        try:
            status = DiscussionStatus(status_val)
        except ValueError:
            status = DiscussionStatus.ACTIVE
        return cls(
            thread_id=data.get("thread_id", str(uuid.uuid4())),
            channel_id=data.get("channel_id", ""),
            participants=data.get("participants", []),
            messages=messages,
            status=status,
            config=config,
            trigger_reason=data.get("trigger_reason", ""),
            conclusion=data.get("conclusion", ""),
            total_tokens_used=data.get("total_tokens_used", 0),
            created_at=data.get("created_at", time.time()),
            completed_at=data.get("completed_at"),
        )
