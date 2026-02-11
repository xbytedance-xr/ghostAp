import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class EngineRunState(Enum):
    """DeepEngine 运行时状态，替代 _is_running/_should_stop 布尔组合。"""
    IDLE = "idle"           # 未运行
    RUNNING = "running"     # 正在执行
    STOPPING = "stopping"   # 已请求停止，等待当前任务完成


class DeepTaskStatus(Enum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class DeepProjectStatus(Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DeepTask:
    task_id: str
    title: str
    description: str
    prompt: str
    order: int = 0
    status: DeepTaskStatus = DeepTaskStatus.PENDING
    dependencies: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    original_prompt: Optional[str] = None
    adapted_prompt: Optional[str] = None

    @classmethod
    def create(cls, title: str, description: str, prompt: str, order: int = 0,
               dependencies: Optional[list[str]] = None) -> "DeepTask":
        return cls(
            task_id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            prompt=prompt,
            order=order,
            dependencies=dependencies or [],
        )

    def start(self):
        self.status = DeepTaskStatus.IN_PROGRESS
        self.started_at = time.time()

    def complete(self, result: str):
        self.status = DeepTaskStatus.COMPLETED
        self.completed_at = time.time()
        self.result = result

    def fail(self, error: str):
        self.retry_count += 1
        if self.retry_count >= self.max_retries:
            self.status = DeepTaskStatus.FAILED
            self.completed_at = time.time()
            self.error = error
        else:
            self.status = DeepTaskStatus.PENDING

    def skip(self, reason: str):
        self.status = DeepTaskStatus.SKIPPED
        self.completed_at = time.time()
        self.error = reason

    def is_ready(self, completed_tasks: set[str]) -> bool:
        if self.status != DeepTaskStatus.PENDING:
            return False
        return all(dep in completed_tasks for dep in self.dependencies)

    def duration(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "prompt": self.prompt,
            "order": self.order,
            "status": self.status.value,
            "dependencies": self.dependencies,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "original_prompt": self.original_prompt,
            "adapted_prompt": self.adapted_prompt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeepTask":
        task = cls(
            task_id=data["task_id"],
            title=data["title"],
            description=data["description"],
            prompt=data["prompt"],
            order=data.get("order", 0),
            status=DeepTaskStatus(data.get("status", "pending")),
            dependencies=data.get("dependencies", []),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            result=data.get("result"),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            original_prompt=data.get("original_prompt"),
            adapted_prompt=data.get("adapted_prompt"),
        )
        return task


@dataclass
class ParsedRequirement:
    original_text: str
    summary: str
    goals: list[str]
    constraints: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    estimated_complexity: str = "medium"
    parsed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "original_text": self.original_text,
            "summary": self.summary,
            "goals": self.goals,
            "constraints": self.constraints,
            "tech_stack": self.tech_stack,
            "estimated_complexity": self.estimated_complexity,
            "parsed_at": self.parsed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ParsedRequirement":
        return cls(
            original_text=data["original_text"],
            summary=data["summary"],
            goals=data["goals"],
            constraints=data.get("constraints", []),
            tech_stack=data.get("tech_stack", []),
            estimated_complexity=data.get("estimated_complexity", "medium"),
            parsed_at=data.get("parsed_at", time.time()),
        )


@dataclass
class ExecutionResult:
    task_id: str
    success: bool
    output: str
    duration: float
    error: Optional[str] = None
    executed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "output": self.output,
            "duration": self.duration,
            "error": self.error,
            "executed_at": self.executed_at,
        }


@dataclass
class ProgressUpdate:
    project_id: str
    current_task: Optional[DeepTask]
    completed_count: int
    total_count: int
    status: DeepProjectStatus
    message: str
    timestamp: float = field(default_factory=time.time)

    @property
    def progress_percent(self) -> float:
        if self.total_count == 0:
            return 0.0
        return (self.completed_count / self.total_count) * 100

    @property
    def progress_bar(self) -> str:
        percent = self.progress_percent
        filled = int(percent / 10)
        empty = 10 - filled
        return f"[{'█' * filled}{'░' * empty}] {percent:.0f}%"


@dataclass
class DeepProject:
    project_id: str
    name: str
    root_path: str
    requirement: Optional[ParsedRequirement] = None
    tasks: list[DeepTask] = field(default_factory=list)
    status: DeepProjectStatus = DeepProjectStatus.IDLE
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    current_task_index: int = 0
    error: Optional[str] = None

    @classmethod
    def create(cls, name: str, root_path: str) -> "DeepProject":
        return cls(
            project_id=str(uuid.uuid4())[:8],
            name=name,
            root_path=root_path,
        )

    def set_requirement(self, requirement: ParsedRequirement):
        self.requirement = requirement

    def set_tasks(self, tasks: list[DeepTask]):
        self.tasks = tasks
        for i, task in enumerate(self.tasks):
            task.order = i

    def start(self):
        self.status = DeepProjectStatus.EXECUTING
        self.started_at = time.time()

    def pause(self):
        self.status = DeepProjectStatus.PAUSED

    def resume(self):
        self.status = DeepProjectStatus.EXECUTING

    def complete(self):
        self.status = DeepProjectStatus.COMPLETED
        self.completed_at = time.time()

    def fail(self, error: str):
        self.status = DeepProjectStatus.FAILED
        self.completed_at = time.time()
        self.error = error

    def get_next_task(self) -> Optional[DeepTask]:
        completed_ids = {t.task_id for t in self.tasks if t.status == DeepTaskStatus.COMPLETED}
        for task in self.tasks:
            if task.is_ready(completed_ids):
                return task
        return None

    def get_current_task(self) -> Optional[DeepTask]:
        for task in self.tasks:
            if task.status == DeepTaskStatus.IN_PROGRESS:
                return task
        return None

    @property
    def completed_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == DeepTaskStatus.COMPLETED)

    @property
    def failed_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == DeepTaskStatus.FAILED)

    @property
    def pending_count(self) -> int:
        return sum(1 for t in self.tasks if t.status in (DeepTaskStatus.PENDING, DeepTaskStatus.READY))

    @property
    def total_count(self) -> int:
        return len(self.tasks)

    @property
    def is_completed(self) -> bool:
        return all(t.status in (DeepTaskStatus.COMPLETED, DeepTaskStatus.SKIPPED, DeepTaskStatus.FAILED)
                   for t in self.tasks)

    @property
    def has_failures(self) -> bool:
        return any(t.status == DeepTaskStatus.FAILED for t in self.tasks)

    def get_progress_update(self, message: str) -> ProgressUpdate:
        return ProgressUpdate(
            project_id=self.project_id,
            current_task=self.get_current_task(),
            completed_count=self.completed_count,
            total_count=self.total_count,
            status=self.status,
            message=message,
        )

    def duration(self) -> Optional[float]:
        if self.started_at:
            end = self.completed_at or time.time()
            return end - self.started_at
        return None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "root_path": self.root_path,
            "requirement": self.requirement.to_dict() if self.requirement else None,
            "tasks": [t.to_dict() for t in self.tasks],
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "current_task_index": self.current_task_index,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeepProject":
        project = cls(
            project_id=data["project_id"],
            name=data["name"],
            root_path=data["root_path"],
            status=DeepProjectStatus(data.get("status", "idle")),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            current_task_index=data.get("current_task_index", 0),
            error=data.get("error"),
        )
        if data.get("requirement"):
            project.requirement = ParsedRequirement.from_dict(data["requirement"])
        if data.get("tasks"):
            project.tasks = [DeepTask.from_dict(t) for t in data["tasks"]]
        return project


@dataclass
class ContextEntry:
    """上下文条目，记录执行过程中的各类事件。"""
    entry_type: str  # "task_result" | "user_injection" | "deviation" | "adaptation"
    content: str
    task_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "entry_type": self.entry_type,
            "content": self.content,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContextEntry":
        return cls(
            entry_type=data["entry_type"],
            content=data["content"],
            task_id=data.get("task_id"),
            timestamp=data.get("timestamp", time.time()),
        )


class ExecutionContext:
    """线程安全的执行上下文累积器。

    在 Deep Engine 执行过程中累积任务结果、用户注入、偏差记录等信息，
    供后续任务的 prompt 自适应调整使用。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list[ContextEntry] = []
        self._new_context_since_last_check = False

    def add_result(self, task_id: str, title: str, success: bool, summary: str):
        """记录任务执行结果。不设置 flag，因为 AI session 已有会话上下文。"""
        content = f"任务「{title}」{'成功' if success else '失败'}: {summary[:200]}"
        entry = ContextEntry(
            entry_type="task_result",
            content=content,
            task_id=task_id,
        )
        with self._lock:
            self._entries.append(entry)

    def inject_user_context(self, message: str):
        """注入用户上下文（跨线程安全）。设置 flag 触发 adaptation。"""
        entry = ContextEntry(
            entry_type="user_injection",
            content=message,
        )
        with self._lock:
            self._entries.append(entry)
            self._new_context_since_last_check = True

    def record_deviation(self, task_id: str, deviation: str):
        """记录偏差。"""
        entry = ContextEntry(
            entry_type="deviation",
            content=deviation,
            task_id=task_id,
        )
        with self._lock:
            self._entries.append(entry)

    def record_adaptation(self, task_id: str, description: str):
        """记录适配操作。"""
        entry = ContextEntry(
            entry_type="adaptation",
            content=description,
            task_id=task_id,
        )
        with self._lock:
            self._entries.append(entry)

    def has_meaningful_context(self) -> bool:
        """O(1) 检查是否有新上下文需要处理。"""
        with self._lock:
            return self._new_context_since_last_check

    def consume_new_context_flag(self):
        """消费 flag，避免重复触发。"""
        with self._lock:
            self._new_context_since_last_check = False

    def build_context_prompt(self, max_entries: int = 10) -> str:
        """构建上下文摘要给 LLM。"""
        with self._lock:
            entries = self._entries[-max_entries:] if max_entries > 0 else self._entries[:]

        if not entries:
            return ""

        lines = ["## 执行上下文\n"]
        for entry in entries:
            type_label = {
                "task_result": "📋 任务结果",
                "user_injection": "💬 用户指示",
                "deviation": "⚠️ 偏差",
                "adaptation": "🔄 已适配",
            }.get(entry.entry_type, entry.entry_type)

            task_ref = f" [{entry.task_id}]" if entry.task_id else ""
            lines.append(f"- {type_label}{task_ref}: {entry.content}")

        return "\n".join(lines)

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "entries": [e.to_dict() for e in self._entries],
                "new_context_flag": self._new_context_since_last_check,
            }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionContext":
        ctx = cls()
        ctx._entries = [ContextEntry.from_dict(e) for e in data.get("entries", [])]
        ctx._new_context_since_last_check = data.get("new_context_flag", False)
        return ctx
