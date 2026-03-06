import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .models import TTADKTool, TTADKModel, ToolListResult, ModelListResult
from .model_fetcher import TTADKModelFetcher
from ..config import get_settings

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

TOOL_DESCRIPTIONS = {
    "claude": "Claude AI Assistant",
    "cursor": "Cursor AI Editor",
    "gemini": "Google Gemini AI",
    "codex": "OpenAI Codex",
    "coco": "Coco AI Assistant",
    "tmates": "Tmates AI",
    "trae": "Trae IDE AI",
    "opencode": "OpenCode AI",
}

MODEL_KEYS = ("models", "model_list", "available_models", "ai_models", "llm_models", "llms")
TOOL_KEYS = ("tools", "ai_tools", "providers", "toolkits")


class TTADKManager:
    def __init__(self, default_tool: Optional[str] = None, default_model: Optional[str] = None):
        self._lock = threading.Lock()
        self._current_tool: Optional[str] = default_tool
        self._current_model: Optional[str] = default_model
        self._known_tools: set[str] = set()
        self._known_models: set[str] = set()
        self._initialized = False
        # 模型获取器和缓存
        self._model_fetcher = TTADKModelFetcher()
        self._tool_models_cache: dict[str, list[TTADKModel]] = {}
        self._cache_time: dict[str, float] = {}
        self._cache_ttl = 300  # 缓存 TTL（秒）

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            settings = get_settings()
            if not self._current_tool:
                self._current_tool = settings.ttadk_default_tool or "coco"
            if not self._current_model:
                self._current_model = settings.ttadk_default_model or None
            self._initialized = True

    def get_tools(self, cwd: Optional[str] = None) -> ToolListResult:
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
        tool_names = self._load_tool_names_from_settings()
        if not tool_names:
            tool_names = [t.name for t in DEFAULT_TOOLS]
        self._known_tools = {str(name) for name in tool_names}
        for name in tool_names:
            tools.append(
                TTADKTool(
                    name=name,
                    description=TOOL_DESCRIPTIONS.get(name, "AI Tool"),
                    is_default=(name == self._current_tool),
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
            configured = set(self._load_tool_names_from_settings())
            known_names = {t.name for t in DEFAULT_TOOLS} | self._known_tools | configured
            if tool_name not in known_names:
                logger.warning("Unknown tool: %s", tool_name)
                return False
            self._current_tool = tool_name
            logger.info("Switched TTADK tool to: %s", tool_name)
            return True

    def get_models(self, cwd: Optional[str] = None, tool_name: Optional[str] = None) -> ModelListResult:
        """获取当前工具或指定工具的模型列表"""
        self._ensure_initialized()
        current_tool = tool_name or self._current_tool

        if not current_tool:
            return ModelListResult(models=list(DEFAULT_MODELS))

        with self._lock:
            # 检查缓存
            if self._is_cache_valid(current_tool):
                models = self._tool_models_cache.get(current_tool, [])
                if models:
                    return ModelListResult(models=list(models), cached=True)

            try:
                # 尝试从 sync 获取
                if cwd:
                    synced = self._load_models_from_sync(cwd, current_tool)
                    if synced:
                        self._tool_models_cache[current_tool] = synced
                        self._cache_time[current_tool] = time.time()
                        self._known_models = {m.name for m in synced}
                        return ModelListResult(models=list(synced), cached=False)

                # 尝试从终端交互获取
                fetched = self._model_fetcher.fetch_tool_models(current_tool)
                if fetched:
                    self._tool_models_cache[current_tool] = fetched
                    self._cache_time[current_tool] = time.time()
                    self._known_models = {m.name for m in fetched}
                    return ModelListResult(models=list(fetched), cached=False)

                # 失败时返回默认模型列表
                return ModelListResult(models=list(DEFAULT_MODELS), cached=False)

            except Exception as e:
                logger.error("Failed to load TTADK models: %s", e)
                return ModelListResult(
                    models=list(DEFAULT_MODELS),
                    cached=False,
                    error=str(e),
                )

    def _is_cache_valid(self, tool_name: str) -> bool:
        """检查指定工具的模型缓存是否有效"""
        if tool_name not in self._tool_models_cache:
            return False
        cache_time = self._cache_time.get(tool_name, 0)
        return (time.time() - cache_time) < self._cache_ttl

    def invalidate_model_cache(self, tool_name: Optional[str] = None) -> None:
        """使模型缓存失效"""
        if tool_name:
            self._tool_models_cache.pop(tool_name, None)
            self._cache_time.pop(tool_name, None)
            self._model_fetcher.invalidate_cache(tool_name)
        else:
            self._tool_models_cache.clear()
            self._cache_time.clear()
            self._model_fetcher.invalidate_cache()

    def _load_tool_names_from_settings(self) -> list[str]:
        path = Path.home() / ".ttadk" / "setting.json"
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            commands = data.get("ai_tool_commands")
            if isinstance(commands, dict):
                return [str(k) for k in commands.keys()]
        except Exception as e:
            logger.debug("Failed to read ttadk setting.json: %s", e)
        return []

    def _load_models_from_sync(self, cwd: str, tool_name: Optional[str] = None) -> list[TTADKModel]:
        data = self._run_ttadk_sync(cwd)
        if not data:
            return []
        return self._extract_models_from_sync(data, tool_name or self._current_tool, self._current_model)

    def _run_ttadk_sync(self, cwd: str) -> Optional[object]:
        try:
            p = subprocess.run(
                ["ttadk", "sync", "-d", "-f", "json"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except Exception as e:
            logger.debug("ttadk sync failed: %s", e)
            return None
        if p.returncode != 0:
            logger.debug("ttadk sync rc=%d stderr=%s", p.returncode, (p.stderr or "").strip())
            return None
        payload = (p.stdout or "").strip()
        if not payload:
            return None
        try:
            return json.loads(payload)
        except Exception:
            # Best-effort: extract first JSON object
            start = payload.find("{")
            end = payload.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(payload[start:end + 1])
                except Exception:
                    return None
        return None

    def _extract_models_from_sync(
        self,
        data: object,
        tool_name: Optional[str],
        current_model: Optional[str],
    ) -> list[TTADKModel]:
        # Tool-specific lookup
        if isinstance(data, dict) and tool_name:
            for key in TOOL_KEYS:
                container = data.get(key)
                models = self._extract_models_from_tool_container(container, tool_name, current_model)
                if models:
                    return models

        # Fallback: search anywhere
        models = self._extract_models_from_container(data, current_model)
        return models

    def _extract_models_from_tool_container(
        self,
        container: object,
        tool_name: str,
        current_model: Optional[str],
    ) -> list[TTADKModel]:
        if isinstance(container, dict):
            if tool_name in container:
                return self._extract_models_from_container(container.get(tool_name), current_model)
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tool") or item.get("id")
                    if name == tool_name:
                        return self._extract_models_from_container(item, current_model)
        return []

    def _extract_models_from_container(
        self,
        container: object,
        current_model: Optional[str],
    ) -> list[TTADKModel]:
        if isinstance(container, dict):
            for key in MODEL_KEYS:
                if key in container:
                    models = self._normalize_models(container.get(key), current_model)
                    if models:
                        return models
            for value in container.values():
                models = self._extract_models_from_container(value, current_model)
                if models:
                    return models
        elif isinstance(container, list):
            models = self._normalize_models(container, current_model)
            if models:
                return models
            for item in container:
                models = self._extract_models_from_container(item, current_model)
                if models:
                    return models
        return []

    def _normalize_models(self, raw: object, current_model: Optional[str]) -> list[TTADKModel]:
        if isinstance(raw, list):
            if raw and all(isinstance(x, str) for x in raw):
                return [
                    TTADKModel(name=name, description=name, is_default=(name == current_model))
                    for name in raw
                ]
            models: list[TTADKModel] = []
            for item in raw:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("id") or item.get("model") or item.get("model_name")
                    if not name:
                        continue
                    desc = item.get("description") or item.get("label") or str(name)
                    models.append(
                        TTADKModel(
                            name=str(name),
                            description=str(desc),
                            is_default=(str(name) == current_model),
                        )
                    )
            return models
        if isinstance(raw, dict):
            # Map of name -> details
            models: list[TTADKModel] = []
            for name, item in raw.items():
                if isinstance(item, dict):
                    desc = item.get("description") or item.get("label") or str(name)
                else:
                    desc = str(item)
                models.append(
                    TTADKModel(
                        name=str(name),
                        description=str(desc),
                        is_default=(str(name) == current_model),
                    )
                )
            return models
        return []

    def get_current_model(self) -> Optional[str]:
        self._ensure_initialized()
        with self._lock:
            return self._current_model

    def set_model(self, model_name: str) -> bool:
        self._ensure_initialized()
        with self._lock:
            known_names = {m.name for m in DEFAULT_MODELS} | self._known_models
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
