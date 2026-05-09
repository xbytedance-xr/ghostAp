import asyncio
import time

from src.acp.helper import fetch_acp_models
from src.coco_model.manager import DEFAULT_MODELS


def test_fetch_acp_models_times_out_and_returns_current_model(monkeypatch):
    async def slow_probe(_tool_name, _cwd, _current_model):
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr("src.acp.helper.probe_acp_models", slow_probe)

    started = time.monotonic()
    models = fetch_acp_models(
        "codex",
        cwd="/tmp/ghostap",
        current_model="current-fast-fallback",
        probe_timeout=0.1,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert [m.name for m in models] == ["current-fast-fallback"]
    assert models[0].is_default is True


def test_fetch_coco_models_timeout_uses_static_defaults(monkeypatch):
    """When probe times out AND CocoModelManager only has static defaults
    cached, fetch_acp_models must degrade to DEFAULT_MODELS (no infinite
    re-probe loop). The dedicated cached-model-bypass test below covers the
    happy path where manager has real ACP models cached."""

    async def slow_probe(_tool_name, _cwd, _current_model):
        await asyncio.sleep(1)
        return []

    from src.coco_model.models import CocoModel, ModelListResult

    class FakeCocoManager:
        def get_current_model(self):
            return "gpt-4.1"

        def get_models(self):
            # Return the same static defaults — fetch_acp_models should treat
            # this as "probe needed" and fall through, not loop.
            return ModelListResult(
                models=[CocoModel(name=m.name, description=m.description) for m in DEFAULT_MODELS],
                cached=False,
            )

    monkeypatch.setattr("src.acp.helper.probe_acp_models", slow_probe)
    monkeypatch.setattr("src.coco_model.get_coco_model_manager", lambda: FakeCocoManager())

    started = time.monotonic()
    models = fetch_acp_models("coco", cwd="/tmp/ghostap", probe_timeout=0.1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert [m.name for m in models] == [m.name for m in DEFAULT_MODELS]
    assert [m.name for m in models if m.is_default] == ["gpt-4.1"]


def test_fetch_coco_models_uses_manager_cache_when_real_models_present(monkeypatch):
    """When CocoModelManager already cached real ACP models, fetch_acp_models
    must return those (and skip the fresh probe) so /wt and /coco share the
    same model list source — exactly what the user asked for."""

    real_models = ["GPT-5.2", "GPT-5.4", "Gemini-3.1-Pro-Preview", "Test-O-New"]

    async def probe_should_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("probe should be skipped when manager has real models")

    from src.coco_model.models import CocoModel, ModelListResult

    class FakeCocoManager:
        def get_current_model(self):
            return "GPT-5.4"

        def get_models(self):
            return ModelListResult(
                models=[CocoModel(name=name, description=name) for name in real_models],
                cached=True,
            )

    monkeypatch.setattr("src.acp.helper.probe_acp_models", probe_should_not_run)
    monkeypatch.setattr("src.coco_model.get_coco_model_manager", lambda: FakeCocoManager())

    started = time.monotonic()
    models = fetch_acp_models("coco", cwd="/tmp/ghostap")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert [m.name for m in models] == real_models
    assert [m.name for m in models if m.is_default] == ["GPT-5.4"]
