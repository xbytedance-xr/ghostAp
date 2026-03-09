"""Loop Engine 数据模型与枚举定义。

Loop Engine 采用迭代闭环策略，每轮迭代动态决策下一步操作，
直到产品诉求被完整满足或触发终止条件。
"""

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LoopProjectStatus(Enum):
    """Loop 项目状态机。"""

    IDLE = "idle"  # 未启动
    ANALYZING = "analyzing"  # 需求解析中
    RUNNING = "running"  # 迭代执行中
    PAUSED = "paused"  # 用户暂停
    COMPLETED = "completed"  # 全部标准满足
    ABORTED = "aborted"  # 触发终止条件


class IterationStatus(Enum):
    """单轮迭代状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class LoopRole(Enum):
    """迭代角色类型。"""

    ARCHITECT = "architect"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    TESTER = "tester"
    DEBUGGER = "debugger"
    INTEGRATOR = "integrator"
    DESIGNER = "designer"

    @property
    def display_name(self) -> str:
        return {
            LoopRole.ARCHITECT: "架构师",
            LoopRole.DEVELOPER: "开发者",
            LoopRole.REVIEWER: "审查者",
            LoopRole.TESTER: "测试者",
            LoopRole.DEBUGGER: "调试者",
            LoopRole.INTEGRATOR: "集成者",
            LoopRole.DESIGNER: "设计师",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            LoopRole.ARCHITECT: "🏗️",
            LoopRole.DEVELOPER: "💻",
            LoopRole.REVIEWER: "🔍",
            LoopRole.TESTER: "🧪",
            LoopRole.DEBUGGER: "🐛",
            LoopRole.INTEGRATOR: "🔗",
            LoopRole.DESIGNER: "🎨",
        }[self]


class ReviewPerspective(Enum):
    """多视角审查的视角类型。"""

    ARCHITECT = "architect"
    PRODUCT = "product"
    USER = "user"
    TESTER = "tester"
    DESIGNER = "designer"

    @property
    def display_name(self) -> str:
        return {
            ReviewPerspective.ARCHITECT: "架构师",
            ReviewPerspective.PRODUCT: "产品经理",
            ReviewPerspective.USER: "用户",
            ReviewPerspective.TESTER: "测试",
            ReviewPerspective.DESIGNER: "设计师",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            ReviewPerspective.ARCHITECT: "🏗️",
            ReviewPerspective.PRODUCT: "📦",
            ReviewPerspective.USER: "👤",
            ReviewPerspective.TESTER: "🧪",
            ReviewPerspective.DESIGNER: "🎨",
        }[self]

    @property
    def review_focus(self) -> str:
        return {
            ReviewPerspective.ARCHITECT: "代码结构、设计模式、可维护性、性能、安全性",
            ReviewPerspective.PRODUCT: "需求完整度、用户价值、边界场景、功能一致性",
            ReviewPerspective.USER: "易用性、文档、错误提示、交互体验、可理解性",
            ReviewPerspective.TESTER: "测试覆盖、边界条件、异常处理、回归风险、可测试性",
            ReviewPerspective.DESIGNER: "UI视觉(配色/层级)、交互体验(动效/流程)、移动端适配、美观度",
        }[self]

    @property
    def failure_label(self) -> str:
        """审查不通过时显示的特定文案，默认 '❌ 有建议'。"""
        return {
            ReviewPerspective.DESIGNER: "🎨 视觉/交互建议",
        }.get(self, "❌ 有建议")


class TerminationSignal(Enum):
    """终止信号类型。"""

    CONTINUE = "continue"  # 继续迭代
    COMPLETE = "complete"  # 所有标准满足
    CONVERGED = "converged"  # 收敛终止（连续N轮无进展）
    MAX_ITER = "max_iter"  # 达到最大迭代次数
    FATAL = "fatal"  # 不可恢复的错误
    USER_STOP = "user_stop"  # 用户主动停止


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LoopRequirement:
    """结构化的产品诉求。"""

    goal: str
    acceptance_criteria: list[str]
    constraints: list[str] = field(default_factory=list)
    context_summary: str = ""
    estimated_iterations: int = 6
    raw_text: str = ""
    parsed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "acceptance_criteria": self.acceptance_criteria,
            "constraints": self.constraints,
            "context_summary": self.context_summary,
            "estimated_iterations": self.estimated_iterations,
            "raw_text": self.raw_text,
            "parsed_at": self.parsed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoopRequirement":
        return cls(
            goal=data["goal"],
            acceptance_criteria=data["acceptance_criteria"],
            constraints=data.get("constraints", []),
            context_summary=data.get("context_summary", ""),
            estimated_iterations=data.get("estimated_iterations", 6),
            raw_text=data.get("raw_text", ""),
            parsed_at=data.get("parsed_at", time.time()),
        )


@dataclass
class RoleSelection:
    """角色选择结果。"""

    role: LoopRole
    reason: str
    focus: str


@dataclass
class PerspectiveReview:
    """单个视角的审查结果。"""

    perspective: ReviewPerspective
    passed: bool
    suggestions: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "perspective": self.perspective.value,
            "passed": self.passed,
            "suggestions": self.suggestions,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PerspectiveReview":
        return cls(
            perspective=ReviewPerspective(data["perspective"]),
            passed=data["passed"],
            suggestions=data.get("suggestions", []),
            summary=data.get("summary", ""),
        )


@dataclass
class ReviewResult:
    """多视角审查汇总结果。"""

    reviews: list[PerspectiveReview] = field(default_factory=list)
    iteration: int = 0

    @property
    def all_passed(self) -> bool:
        return len(self.reviews) > 0 and all(r.passed for r in self.reviews)

    @property
    def total_suggestions(self) -> int:
        return sum(len(r.suggestions) for r in self.reviews)

    @property
    def failed_perspectives(self) -> list[PerspectiveReview]:
        return [r for r in self.reviews if not r.passed]

    def suggestions_by_perspective(self) -> dict[ReviewPerspective, list[str]]:
        return {r.perspective: r.suggestions for r in self.reviews if r.suggestions}

    def to_dict(self) -> dict:
        return {
            "reviews": [r.to_dict() for r in self.reviews],
            "iteration": self.iteration,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewResult":
        return cls(
            reviews=[PerspectiveReview.from_dict(r) for r in data.get("reviews", [])],
            iteration=data.get("iteration", 0),
        )


@dataclass
class IterationRecord:
    """单轮迭代记录。"""

    iteration: int
    role: Optional[LoopRole] = None
    focus: str = ""
    status: IterationStatus = IterationStatus.RUNNING
    prompt: str = ""
    output: str = ""
    duration: float = 0.0
    criteria_progress: dict[int, bool] = field(default_factory=dict)
    summary: str = ""
    error: Optional[str] = None
    review_result: Optional[ReviewResult] = None
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def complete(self, output: str, summary: str, criteria_progress: dict[int, bool]):
        self.status = IterationStatus.SUCCESS
        self.output = output
        self.summary = summary
        self.criteria_progress = criteria_progress
        self.completed_at = time.time()
        self.duration = self.completed_at - self.started_at

    def fail(self, error: str, output: str = ""):
        self.status = IterationStatus.FAILED
        self.error = error
        self.output = output
        self.completed_at = time.time()
        self.duration = self.completed_at - self.started_at

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "role": self.role.value if self.role else None,
            "focus": self.focus,
            "status": self.status.value,
            "prompt": self.prompt,
            "output": self.output,
            "duration": self.duration,
            "criteria_progress": self.criteria_progress,
            "summary": self.summary,
            "error": self.error,
            "review_result": self.review_result.to_dict() if self.review_result else None,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IterationRecord":
        role_val = data.get("role")
        review_data = data.get("review_result")
        return cls(
            iteration=data.get("iteration", data.get("iteration_id", 0)),
            role=LoopRole(role_val) if role_val else None,
            focus=data.get("focus", ""),
            status=IterationStatus(data.get("status", "running")),
            prompt=data.get("prompt", ""),
            output=data.get("output", ""),
            duration=data.get("duration", 0.0),
            criteria_progress=data.get("criteria_progress", {}),
            summary=data.get("summary", ""),
            error=data.get("error"),
            review_result=ReviewResult.from_dict(review_data) if review_data else None,
            started_at=data.get("started_at", time.time()),
            completed_at=data.get("completed_at"),
        )


@dataclass
class CriteriaTracker:
    """验收标准追踪器。"""

    criteria: list[str] = field(default_factory=list)
    satisfied: dict[int, bool] = field(default_factory=dict)
    satisfied_at_iteration: dict[int, int] = field(default_factory=dict)

    def init_criteria(self, criteria: list[str]):
        self.criteria = criteria
        self.satisfied = {i: False for i in range(len(criteria))}
        self.satisfied_at_iteration = {}

    def update(self, criteria_id: int, is_satisfied: bool, iteration_id: int):
        if criteria_id < 0 or criteria_id >= len(self.criteria):
            return
        if is_satisfied and not self.satisfied.get(criteria_id, False):
            self.satisfied[criteria_id] = True
            self.satisfied_at_iteration[criteria_id] = iteration_id

    def batch_update(self, progress: dict[int, bool], iteration_id: int):
        for criteria_id, is_satisfied in progress.items():
            self.update(criteria_id, is_satisfied, iteration_id)

    @property
    def satisfied_count(self) -> int:
        return sum(1 for v in self.satisfied.values() if v)

    @property
    def total_count(self) -> int:
        return len(self.criteria)

    @property
    def is_all_satisfied(self) -> bool:
        return self.total_count > 0 and self.satisfied_count == self.total_count

    @property
    def unsatisfied_indices(self) -> list[int]:
        return [i for i, v in self.satisfied.items() if not v]

    @property
    def unsatisfied_criteria(self) -> list[str]:
        return [self.criteria[i] for i in self.unsatisfied_indices]

    @property
    def satisfied_criteria(self) -> list[str]:
        return [self.criteria[i] for i, v in self.satisfied.items() if v]

    def to_dict(self) -> dict:
        return {
            "criteria": self.criteria,
            "satisfied": self.satisfied,
            "satisfied_at_iteration": self.satisfied_at_iteration,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CriteriaTracker":
        tracker = cls()
        tracker.criteria = data.get("criteria", [])
        # JSON keys are strings, convert to int
        tracker.satisfied = {int(k): v for k, v in data.get("satisfied", {}).items()}
        tracker.satisfied_at_iteration = {
            int(k): v for k, v in data.get("satisfied_at_iteration", {}).items()
        }
        return tracker


@dataclass
class TerminationResult:
    """终止判断结果。"""

    signal: TerminationSignal
    reason: str
    summary: str = ""


@dataclass
class IterationState:
    """单轮迭代开始前的状态快照，供角色选择和 prompt 构建使用。"""

    iteration_number: int
    requirement: LoopRequirement
    criteria_tracker: CriteriaTracker
    recent_iterations: list[IterationRecord]
    context_summary: str
    user_guidance: Optional[str] = None
    consecutive_failures: int = 0
    last_role: Optional[LoopRole] = None


@dataclass
class LoopProject:
    """Loop 项目顶层容器。"""

    project_id: str
    name: str
    root_path: str
    requirement: Optional[LoopRequirement] = None
    iterations: list[IterationRecord] = field(default_factory=list)
    criteria_tracker: CriteriaTracker = field(default_factory=CriteriaTracker)
    status: LoopProjectStatus = LoopProjectStatus.IDLE
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    task_id: Optional[str] = None  # Human-readable task ID

    @classmethod
    def create(
        cls, name: str = "", root_path: str = "", chat_id: str = ""
    ) -> "LoopProject":
        if not name:
            name = os.path.basename(root_path) or "loop_project"
        return cls(
            project_id=str(uuid.uuid4())[:8],
            name=name,
            root_path=root_path,
        )

    def set_requirement(self, requirement: LoopRequirement):
        self.requirement = requirement
        self.criteria_tracker.init_criteria(requirement.acceptance_criteria)

    def start(self):
        self.status = LoopProjectStatus.RUNNING
        self.started_at = time.time()

    def pause(self):
        self.status = LoopProjectStatus.PAUSED

    def resume(self):
        self.status = LoopProjectStatus.RUNNING

    def complete(self):
        self.status = LoopProjectStatus.COMPLETED
        self.completed_at = time.time()

    def abort(self, reason: str):
        self.status = LoopProjectStatus.ABORTED
        self.completed_at = time.time()
        self.error = reason

    @property
    def current_iteration(self) -> int:
        return len(self.iterations)

    @property
    def satisfied_count(self) -> int:
        return self.criteria_tracker.satisfied_count

    @property
    def total_criteria(self) -> int:
        return self.criteria_tracker.total_count

    @property
    def is_all_satisfied(self) -> bool:
        return self.criteria_tracker.is_all_satisfied

    @property
    def success_count(self) -> int:
        return sum(1 for it in self.iterations if it.status == IterationStatus.SUCCESS)

    @property
    def failure_count(self) -> int:
        return sum(1 for it in self.iterations if it.status == IterationStatus.FAILED)

    @property
    def consecutive_failures(self) -> int:
        count = 0
        for it in reversed(self.iterations):
            if it.status == IterationStatus.FAILED:
                count += 1
            else:
                break
        return count

    @property
    def last_role(self) -> Optional[LoopRole]:
        if self.iterations:
            return self.iterations[-1].role
        return None

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
            "iterations": [it.to_dict() for it in self.iterations],
            "criteria_tracker": self.criteria_tracker.to_dict(),
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoopProject":
        project = cls(
            project_id=data["project_id"],
            name=data["name"],
            root_path=data["root_path"],
            status=LoopProjectStatus(data.get("status", "idle")),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            task_id=data.get("task_id"),
        )
        if data.get("requirement"):
            project.requirement = LoopRequirement.from_dict(data["requirement"])
        if data.get("iterations"):
            project.iterations = [
                IterationRecord.from_dict(it) for it in data["iterations"]
            ]
        if data.get("criteria_tracker"):
            project.criteria_tracker = CriteriaTracker.from_dict(
                data["criteria_tracker"]
            )
        return project


# ---------------------------------------------------------------------------
# Context Manager
# ---------------------------------------------------------------------------


class LoopContextManager:
    """线程安全的迭代上下文管理器。

    管理迭代历史摘要，构建上下文 prompt，处理用户引导注入。
    使用三级压缩策略: 远期(1-line) / 近期(brief) / 最新(full)。
    """

    def __init__(self, max_context_tokens: int = 8000):
        self._lock = threading.Lock()
        self._iterations: list[IterationRecord] = []
        self._user_guidances: list[str] = []
        self._max_tokens = max_context_tokens

    def record_iteration(self, record: IterationRecord):
        with self._lock:
            self._iterations.append(record)

    def inject_user_guidance(self, message: str):
        with self._lock:
            self._user_guidances.append(message)

    def has_user_guidance(self) -> bool:
        with self._lock:
            return len(self._user_guidances) > 0

    def consume_user_guidance(self) -> Optional[str]:
        with self._lock:
            if not self._user_guidances:
                return None
            guidances = "\n".join(self._user_guidances)
            self._user_guidances.clear()
            return guidances

    def build_context_prompt(self, recent_full: int = 1, recent_brief: int = 3) -> str:
        """构建上下文 prompt，使用三级压缩策略。

        Args:
            recent_full: 保留完整输出的最近轮数
            recent_brief: 保留简要摘要的最近轮数（不含 full 的）
        """
        with self._lock:
            iterations = self._iterations[:]

        if not iterations:
            return ""

        lines = ["## 迭代历史\n"]
        total = len(iterations)

        for i, record in enumerate(iterations):
            distance_from_end = total - 1 - i
            role_label = f"{record.role.emoji} {record.role.display_name}"
            status_emoji = "✅" if record.status == IterationStatus.SUCCESS else "❌"

            if distance_from_end < recent_full:
                # 最新轮: 完整输出
                lines.append(
                    f"### 第{record.iteration}轮 [{role_label}] {status_emoji}"
                )
                if record.summary:
                    lines.append(f"摘要: {record.summary}")
                if record.output:
                    output_preview = record.output[-500:]
                    lines.append(f"输出:\n```\n{output_preview}\n```")
            elif distance_from_end < recent_full + recent_brief:
                # 近期: 简要摘要
                summary = record.summary or "(无摘要)"
                lines.append(
                    f"- 第{record.iteration}轮 [{role_label}] {status_emoji}: {summary}"
                )
            else:
                # 远期: 一行摘要
                brief = (
                    record.summary[:80]
                    if record.summary
                    else f"{record.role.display_name} {'成功' if record.status == IterationStatus.SUCCESS else '失败'}"
                )
                lines.append(f"- #{record.iteration} {status_emoji} {brief}")

        return "\n".join(lines)

    def get_iteration_summaries(self) -> list[str]:
        with self._lock:
            return [it.summary for it in self._iterations if it.summary]

    @property
    def iteration_count(self) -> int:
        with self._lock:
            return len(self._iterations)
