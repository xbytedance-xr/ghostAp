import logging
import threading
from typing import Optional

from .models import TTADKTool, TTADKModel, ToolListResult, ModelListResult

logger = logging.getLogger(__name__)

DEFAULT_TOOLS = [
    TTADKTool(name="claude", description="Claude AI Assistant"),
    TTADKTool(name="cursor", description="Cursor AI Editor"),
    TTADKTool(name="gemini", description="Google Gemini AI"),
    TTADKTool(name="codex", description="OpenAI Codex"),
    TTADKTool(name="coco", description="Coco AI Assistant"),
    TTADKTool(name="tmates", description="Tmates AI"),
    TTADKTool(name="trae", description="Trae IDE AI"),
    TTADKTool(name="opencode", description="OpenCode AI"),
]

DEFAULT_MODELS = [
    TTADKModel(name="gpt-5.2", description="GPT-5.2"),
    TTADKModel(name="gpt-4.1", description="GPT-4.1"),
    TTADKModel(name="claude-3-opus", description="Claude 3 Opus"),
    TTADKModel(name="claude-3.5-sonnet", description="Claude 3.5 Sonnet"),
    TTADKModel(name="claude-3.7-sonnet", description="Claude 3.7 Sonnet"),
    TTADKModel(name="doubao-1.5-pro", description="Doubao 1.5 Pro"),
    TTADKModel(name="gemini-2.0-pro", description="Gemini 2.0 Pro"),
    TTADKModel(name="gemini-2.5-pro", description="Gemini 2.5 Pro"),
]


class TTADKManager:
    def __init__(self, default_tool: Optional[str] = None, default_model: Optional[str] = None):
        self._lock = threading.Lock()
        self._current_tool: Optional[str] = default_tool
        self._current_model: Optional[str] = default_model
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._initialized = True

    def get_tools(self) -> ToolListResult:
        self._ensure_initialized()
        with self._lock:
            try:
                tools = self._load_tools()
                return ToolListResult(tools=list(tools), cached=False)
            except Exception as e:
                logger.error("Failed to load TTADK tools: %s", e)
                return ToolListResult(
                    tools=list(DEFAULT_TOOLS),
                    cached=False,
                    error=str(e),
                )

    def _load_tools(self) -> list[TTADKTool]:
        tools = []
        for t in DEFAULT_TOOLS:
            tools.append(
                TTADKTool(
                    name=t.name,
                    description=t.description,
                    is_default=(t.name == self._current_tool),
                )
            )
        return tools

    def get_current_tool(self) -> Optional[str]:
        self._ensure_initialized()
        with self._lock:
            return self._current_tool

    def set_tool(self, tool_name: str) -> bool:
        self._ensure_initialized()
        with self._lock:
            known_names = {t.name for t in DEFAULT_TOOLS}
            if tool_name not in known_names:
                logger.warning("Unknown tool: %s", tool_name)
                return False
            self._current_tool = tool_name
            logger.info("Switched TTADK tool to: %s", tool_name)
            return True

    def get_models(self) -> ModelListResult:
        self._ensure_initialized()
        with self._lock:
            try:
                models = self._load_models()
                return ModelListResult(models=list(models), cached=False)
            except Exception as e:
                logger.error("Failed to load TTADK models: %s", e)
                return ModelListResult(
                    models=list(DEFAULT_MODELS),
                    cached=False,
                    error=str(e),
                )

    def _load_models(self) -> list[TTADKModel]:
        models = []
        for m in DEFAULT_MODELS:
            models.append(
                TTADKModel(
                    name=m.name,
                    description=m.description,
                    is_default=(m.name == self._current_model),
                )
            )
        return models

    def get_current_model(self) -> Optional[str]:
        self._ensure_initialized()
        with self._lock:
            return self._current_model

    def set_model(self, model_name: str) -> bool:
        self._ensure_initialized()
        with self._lock:
            known_names = {m.name for m in DEFAULT_MODELS}
            if model_name not in known_names:
                logger.warning("Unknown model: %s", model_name)
                return False
            self._current_model = model_name
            logger.info("Switched TTADK model to: %s", model_name)
            return True


_manager: Optional[TTADKManager] = None
_manager_lock = threading.Lock()


def get_ttadk_manager(default_tool: Optional[str] = None, default_model: Optional[str] = None) -> TTADKManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TTADKManager(default_tool=default_tool, default_model=default_model)
    return _manager
