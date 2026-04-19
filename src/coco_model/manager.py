import logging
import threading
import time
from pathlib import Path
from typing import Optional

import yaml
from acp.stdio import spawn_agent_process

from .models import CocoModel, ModelListResult

logger = logging.getLogger(__name__)

DEFAULT_MODELS = [
    CocoModel(name="gpt-5.2", description="GPT-5.2"),
    CocoModel(name="gpt-4.1", description="GPT-4.1"),
    CocoModel(name="claude-3-opus", description="Claude 3 Opus"),
    CocoModel(name="claude-3.5-sonnet", description="Claude 3.5 Sonnet"),
    CocoModel(name="claude-3.7-sonnet", description="Claude 3.7 Sonnet"),
    CocoModel(name="doubao-1.5-pro", description="Doubao 1.5 Pro"),
]

CACHE_TTL_SECONDS = 300


class CocoModelManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._cached_models: Optional[list[CocoModel]] = None
        self._cache_time: float = 0
        self._current_model: Optional[str] = None
        self._config_path = Path.home() / "Library" / "Application Support" / "coco" / "coco.yaml"
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._current_model = self._read_model_from_config()
            self._initialized = True

    def _read_model_from_config(self) -> Optional[str]:
        try:
            if not self._config_path.exists():
                logger.debug("coco config not found: %s", self._config_path)
                return None
            with open(self._config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            if config and isinstance(config.get("model"), dict):
                return config["model"].get("name")
        except Exception as e:
            logger.warning("Failed to read coco config: %s", str(e) or repr(e))
        return None

    def _is_cache_valid(self) -> bool:
        return self._cached_models is not None and (time.time() - self._cache_time) < CACHE_TTL_SECONDS

    def get_models(self) -> ModelListResult:
        self._ensure_initialized()
        with self._lock:
            if self._is_cache_valid():
                return ModelListResult(
                    models=list(self._cached_models),
                    cached=True,
                )
            try:
                models = self._load_models()
                self._cached_models = models
                self._cache_time = time.time()
                return ModelListResult(models=list(models), cached=False)
            except Exception as e:
                logger.error("Failed to load coco models: %s", str(e) or repr(e))
                return ModelListResult(
                    models=list(DEFAULT_MODELS),
                    cached=False,
                    error=str(e) or repr(e),
                )

    def _load_models(self) -> list[CocoModel]:
        current = self._current_model or self._read_model_from_config()

        # ACP-first: use protocol-provided available models when possible.
        models_from_acp = self._load_models_via_acp(current_model=current)
        if models_from_acp:
            return models_from_acp

        models = []
        for m in DEFAULT_MODELS:
            models.append(
                CocoModel(
                    name=m.name,
                    description=m.description,
                    is_default=(m.name == current),
                )
            )
        return models

    def _load_models_via_acp(self, current_model: Optional[str]) -> list[CocoModel]:
        """Best-effort ACP model discovery for coco.

        For ACP-capable backends, model capabilities are exposed in new/load session
        responses (`SessionModelState.available_models`). We query once and map to
        CocoModel entries.
        """
        try:
            import asyncio
            import os

            from src.acp.client import GhostAPClient

            async def _probe() -> list[CocoModel]:
                env = os.environ.copy()
                env.pop("CLAUDECODE", None)

                client = GhostAPClient(on_event=lambda _ev: None, auto_approve=True)
                async with spawn_agent_process(client, "coco", "acp", "serve", env=env, cwd=str(Path.cwd())) as (
                    conn,
                    _proc,
                ):
                    await conn.initialize(protocol_version=1)
                    resp = await conn.new_session(cwd=str(Path.cwd()))
                    models_state = getattr(resp, "models", None)
                    available = list(getattr(models_state, "available_models", []) or [])
                    current_id = str(
                        getattr(models_state, "current_model_id", "") or getattr(models_state, "currentModelId", "")
                    )

                    out: list[CocoModel] = []
                    for item in available:
                        model_id = str(
                            getattr(item, "model_id", "") or getattr(item, "modelId", "") or getattr(item, "name", "")
                        ).strip()
                        if not model_id:
                            continue
                        desc = str(getattr(item, "description", "") or getattr(item, "name", "") or model_id)
                        out.append(
                            CocoModel(
                                name=model_id,
                                description=desc,
                                is_default=(model_id == (current_model or current_id)),
                            )
                        )
                    return out

            return asyncio.run(_probe())
        except Exception as e:
            logger.debug("Failed to load coco models via ACP: %s", str(e) or repr(e))
            return []

    def get_current_model(self) -> Optional[str]:
        self._ensure_initialized()
        with self._lock:
            return self._current_model

    def set_model(self, model_name: str) -> bool:
        self._ensure_initialized()
        normalized = (model_name or "").strip()
        if not normalized:
            logger.warning("Unknown model: %s", model_name)
            return False

        # ACP-first: validate against runtime-discovered model list.
        # Keep a DEFAULT_MODELS fallback for offline/failed discovery scenarios.
        result = self.get_models()
        known_names = {m.name for m in (result.models or []) if getattr(m, "name", "")}
        if not known_names:
            known_names = {m.name for m in DEFAULT_MODELS}

        if normalized not in known_names:
            logger.warning("Unknown model: %s", normalized)
            return False

        with self._lock:
            self._current_model = normalized
            self._cached_models = None
            self._cache_time = 0

        logger.info("Switched coco model to: %s", normalized)
        return True

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cached_models = None
            self._cache_time = 0


_manager: Optional[CocoModelManager] = None
_manager_lock = threading.Lock()


def get_coco_model_manager() -> CocoModelManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = CocoModelManager()
    return _manager
