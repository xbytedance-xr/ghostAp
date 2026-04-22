import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Generic, Optional, Protocol, TypeVar, runtime_checkable

from .acp import ACPEventRenderer
from .agent_session import SyncSession, close_session_safely
from .config import get_settings
from .utils.engine_identity import resolve_engine_identity
from .utils.errors import get_error_detail
from .utils.gc_monitor import get_gc_monitor

logger = logging.getLogger(__name__)


class EngineRunState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


class ReviewPerspective(Enum):
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
        return {
            ReviewPerspective.DESIGNER: "🎨 视觉/交互建议",
        }.get(self, "❌ 有建议")


@dataclass
class PerspectiveReview:
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


@runtime_checkable
class HasOnError(Protocol):
    """Protocol for callback objects that may carry an ``on_error`` handler."""

    on_error: Optional[Callable[[str], None]]


T = TypeVar("T", bound="BaseEngine")


class BaseEngine:

    _state_filename: str = ".engine_state.json"
    _gc_label: str = "Engine"
    _gc_threshold_default: float = 85.0

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str = "coco",
        engine_name: str = "Coco",
        model_name: Optional[str] = None,
        settings: Optional[object] = None,
    ):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = settings if settings is not None else get_settings()
        self.engine_name = engine_name
        self._agent_type = agent_type
        self._model_name = model_name

        self._session: Optional[SyncSession] = None
        self._project = None
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._lock = threading.RLock()

    @property
    def project(self):
        with self._lock:
            return self._project

    @property
    def run_state(self) -> EngineRunState:
        with self._lock:
            return self._run_state

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._run_state != EngineRunState.IDLE

    def _close_session_safely(self) -> None:
        close_session_safely(self._session)
        self._session = None

    def stop(self):
        with self._lock:
            self._run_state = EngineRunState.STOPPING
            session = self._session
        if session:
            try:
                session.cancel()
            except Exception:
                pass

    def cleanup(self):
        if self._run_state != EngineRunState.IDLE:
            self._run_state = EngineRunState.STOPPING
            if self._session:
                try:
                    self._session.cancel()
                except Exception:
                    pass
            return
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("关闭ACP session失败: %s", get_error_detail(e))
            self._session = None
        self._project = None
        self._run_state = EngineRunState.IDLE
        mem_snapshot = getattr(self, "_mem_snapshot", None)
        gc_kwargs: dict = {"label": self._gc_label}
        if mem_snapshot is not None:
            gc_kwargs["mem_snapshot"] = mem_snapshot
        get_gc_monitor(
            memory_threshold_percent=self._gc_threshold_default,
        ).check_and_collect(**gc_kwargs)

    def save_state(self, filepath: Optional[str] = None) -> str:
        if not self._project:
            raise ValueError("没有项目状态可保存")
        if not filepath:
            filepath = os.path.join(self.root_path, self._state_filename)
        state = {
            "chat_id": self.chat_id,
            "root_path": self.root_path,
            "project": self._project.to_dict(),
            "saved_at": time.time(),
        }
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
        return filepath

    def inject_guidance(self, text: str) -> None:
        self._pending_guidance = text

    def _format_engine_error(
        self,
        error: Exception,
        label: str,
        *,
        is_timeout: bool = False,
        callbacks: Optional[HasOnError] = None,
    ) -> str:
        """Shared error formatting: build message, log, fire callback.

        Args:
            error: The caught exception.
            label: Human-readable prefix (e.g. "Spec执行", "Loop恢复").
            is_timeout: True for TimeoutError (warning), False for generic (error).
            callbacks: An object with an optional ``on_error`` callable attribute.

        Returns:
            The formatted user-facing error message.
        """
        detail = get_error_detail(error)
        kind = "超时" if is_timeout else "异常"
        error_msg = f"{label}{kind}: {detail}"
        project_name = getattr(self._project, "name", None) or "unknown"
        log_fn = logger.warning if is_timeout else logger.error
        log_fn("[%s:%s] %s", self.engine_name, project_name, error_msg)
        if callbacks is not None and callbacks.on_error is not None:
            callbacks.on_error(error_msg)
        return error_msg

    def get_rendered_content(self) -> str:
        return self._renderer.get_final_content()


class BaseEngineManager(Generic[T]):

    def __init__(self):
        self._engines: dict[str, T] = {}
        self._chat_keys: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def _add_index(self, chat_id: str, key: str) -> None:
        self._chat_keys.setdefault(chat_id, set()).add(key)

    def _remove_index(self, chat_id: str, key: str) -> None:
        keys = self._chat_keys.get(chat_id)
        if keys:
            keys.discard(key)
            if not keys:
                del self._chat_keys[chat_id]

    def _iter_chat_engines(self, chat_id: str):
        # Snapshot keys under lock to avoid RuntimeError: dictionary changed size during iteration
        with self._lock:
            keys = list(self._chat_keys.get(chat_id, ()))
        for key in keys:
            engine = self._engines.get(key)
            if engine:
                yield engine

    def _resolve_identity(self, engine_name: str) -> tuple[str, str, Optional[str]]:
        from .mode import InteractionMode
        from .ttadk import get_ttadk_manager

        normalized = (engine_name or "").strip().lower()
        ttadk_tool = None
        ttadk_model = None
        if normalized == "ttadk":
            mode = InteractionMode.TTADK
            try:
                ttadk_manager = get_ttadk_manager()
                ttadk_tool = ttadk_manager.get_current_tool()
                ttadk_model = ttadk_manager.get_current_model()
            except Exception:
                ttadk_tool = None
                ttadk_model = None
        elif normalized.startswith("claude"):
            mode = InteractionMode.CLAUDE
        elif normalized.startswith("aiden"):
            mode = InteractionMode.AIDEN
        elif normalized.startswith("codex"):
            mode = InteractionMode.CODEX
        elif normalized.startswith("gemini"):
            mode = InteractionMode.GEMINI
        else:
            mode = InteractionMode.COCO

        identity = resolve_engine_identity(
            mode=mode,
            ttadk_tool_name=ttadk_tool,
            ttadk_model_name=ttadk_model,
        )
        return identity.engine_name, identity.agent_type, identity.model_name

    def _create_engine(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str,
        engine_name: str,
        model_name: Optional[str],
    ) -> T:
        raise NotImplementedError

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> T:
        key = f"{chat_id}:{root_path}"
        resolved_engine_name, agent_type, model_name = self._resolve_identity(engine_name)

        with self._lock:
            if key not in self._engines:
                engine = self._create_engine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=agent_type,
                    engine_name=resolved_engine_name,
                    model_name=model_name,
                )
                self._engines[key] = engine
                self._add_index(chat_id, key)
            else:
                existing = self._engines[key]
                if existing.engine_name.lower() != resolved_engine_name.lower() and not existing.is_running:
                    existing.cleanup()
                    engine = self._create_engine(
                        chat_id=chat_id,
                        root_path=root_path,
                        agent_type=agent_type,
                        engine_name=resolved_engine_name,
                        model_name=model_name,
                    )
                    self._engines[key] = engine
            return self._engines[key]

    def get(self, chat_id: str, root_path: str) -> Optional[T]:
        key = f"{chat_id}:{root_path}"
        return self._engines.get(key)

    def get_active_engine(self, chat_id: str) -> Optional[T]:
        for engine in self._iter_chat_engines(chat_id):
            if engine.is_running:
                return engine
        return None

    def get_active_engines(self, chat_id: str) -> list[T]:
        return [e for e in self._iter_chat_engines(chat_id) if e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[T]:
        if chat_id is None:
            return list(self._engines.values())
        return list(self._iter_chat_engines(chat_id))

    def cleanup_all(self):
        with self._lock:
            next_engines: dict[str, T] = {}
            for key, engine in self._engines.items():
                engine.cleanup()
                if engine.is_running:
                    next_engines[key] = engine
            self._engines = next_engines
            self._chat_keys.clear()
            for key in next_engines:
                chat_id = key.partition(":")[0]
                self._chat_keys.setdefault(chat_id, set()).add(key)
