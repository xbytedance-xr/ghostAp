import logging
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

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
            logger.warning("Failed to read coco config: %s", e)
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
                logger.error("Failed to load coco models: %s", e)
                return ModelListResult(
                    models=list(DEFAULT_MODELS),
                    cached=False,
                    error=str(e),
                )

    def _load_models(self) -> list[CocoModel]:
        current = self._current_model or self._read_model_from_config()
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
            self._cached_models = None
            self._cache_time = 0
            logger.info("Switched coco model to: %s", model_name)
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
