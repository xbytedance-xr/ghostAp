import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from src.coco_model import (
    CocoModel,
    CocoModelManager,
    ModelListResult,
    get_coco_model_manager,
)
from src.coco_model.manager import DEFAULT_MODELS


@pytest.fixture(autouse=True)
def _mock_acp_probe():
    # Tests in this module don't need real ACP subprocess spawn; block it to keep suite fast.
    with patch.object(CocoModelManager, "_load_models_via_acp", return_value=[]):
        yield


class TestCocoModel:
    def test_dataclass_fields(self):
        model = CocoModel(name="gpt-5.2", description="Test", is_default=True)
        assert model.name == "gpt-5.2"
        assert model.description == "Test"
        assert model.is_default is True

    def test_defaults(self):
        model = CocoModel(name="test")
        assert model.description == ""
        assert model.is_default is False


class TestModelListResult:
    def test_dataclass_fields(self):
        models = [CocoModel(name="test")]
        result = ModelListResult(models=models, cached=True, error="err")
        assert result.models == models
        assert result.cached is True
        assert result.error == "err"

    def test_defaults(self):
        result = ModelListResult()
        assert result.models == []
        assert result.cached is False
        assert result.error is None


class TestCocoModelManager:
    def test_get_models_returns_default_models_when_acp_empty(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            with patch.object(manager, "_load_models_via_acp", return_value=[]):
                result = manager.get_models()
        assert len(result.models) == len(DEFAULT_MODELS)
        assert result.error is None

    def test_get_models_caches_result(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            result1 = manager.get_models()
            result2 = manager.get_models()
        assert result1.cached is False
        assert result2.cached is True

    def test_cache_invalidation(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            manager.get_models()
            manager.invalidate_cache()
            result = manager.get_models()
        assert result.cached is False

    def test_set_model_updates_current(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            with patch.object(
                manager,
                "get_models",
                return_value=ModelListResult(models=[CocoModel(name="claude-3-opus")], cached=False),
            ):
                assert manager.set_model("claude-3-opus") is True
                assert manager.get_current_model() == "claude-3-opus"

    def test_set_model_unknown_returns_false(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            assert manager.set_model("unknown-model") is False

    def test_set_model_invalidates_cache(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            manager.get_models()
            with patch.object(
                manager,
                "get_models",
                return_value=ModelListResult(models=[CocoModel(name="claude-3-opus")], cached=False),
            ):
                manager.set_model("claude-3-opus")
            result = manager.get_models()
        assert result.cached is False
        assert manager.get_current_model() == "claude-3-opus"

    def test_reads_model_from_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("model:\n  name: gpt-4.1\n")
            f.flush()
            config_path = Path(f.name)

        try:
            manager = CocoModelManager()
            manager._config_path = config_path
            assert manager.get_current_model() == "gpt-4.1"
        finally:
            config_path.unlink()

    def test_thread_safety(self):
        manager = CocoModelManager()
        results = []
        errors = []

        def worker():
            try:
                for _ in range(10):
                    manager.get_models()
                    manager.invalidate_cache()
                    manager.set_model("gpt-5.2")
                results.append(True)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 5

    def test_load_models_prefers_acp_available_models(self):
        manager = CocoModelManager()
        with patch.object(manager, "_load_models_via_acp") as mock_acp:
            mock_acp.return_value = [
                CocoModel(name="acp-a", description="A", is_default=True),
                CocoModel(name="acp-b", description="B", is_default=False),
            ]
            models = manager._load_models()
        assert [m.name for m in models] == ["acp-a", "acp-b"]

    def test_load_models_falls_back_to_defaults_when_acp_empty(self):
        manager = CocoModelManager()
        with patch.object(manager, "_load_models_via_acp", return_value=[]):
            models = manager._load_models()
        assert len(models) == len(DEFAULT_MODELS)
        assert [m.name for m in models] == [m.name for m in DEFAULT_MODELS]

    def test_set_model_accepts_acp_discovered_model(self):
        manager = CocoModelManager()
        with patch.object(manager, "_config_path", Path("/nonexistent/path")):
            with patch.object(
                manager,
                "get_models",
                return_value=ModelListResult(models=[CocoModel(name="acp-only-model")], cached=False),
            ):
                assert manager.set_model("acp-only-model") is True
                assert manager.get_current_model() == "acp-only-model"


class TestGetCocoModelManager:
    def test_returns_singleton(self):
        import src.coco_model.manager as mgr

        mgr._manager = None
        m1 = get_coco_model_manager()
        m2 = get_coco_model_manager()
        assert m1 is m2
