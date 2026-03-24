import json
import logging
import os
import threading
import time
from typing import Generic, Optional, TypeVar

from .acp import ACPEventRenderer
from .agent_session import SyncSession, close_session_safely
from .config import get_settings
from .deep_engine.models import EngineRunState
from .utils.engine_identity import resolve_engine_identity
from .utils.gc_monitor import get_gc_monitor

logger = logging.getLogger(__name__)

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
    ):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
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
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

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
                logger.debug("关闭ACP session失败: %s", e)
            self._session = None
        self._project = None
        self._run_state = EngineRunState.IDLE
        get_gc_monitor(
            memory_threshold_percent=self._gc_threshold_default,
        ).check_and_collect(label=self._gc_label)

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
        for key in self._chat_keys.get(chat_id, ()):
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
