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
    async def slow_probe(_tool_name, _cwd, _current_model):
        await asyncio.sleep(1)
        return []

    class FakeCocoManager:
        def get_current_model(self):
            return "gpt-4.1"

        def get_models(self):  # pragma: no cover - this would re-enter ACP probing.
            raise AssertionError("fetch_acp_models should not call coco get_models after probe timeout")

    monkeypatch.setattr("src.acp.helper.probe_acp_models", slow_probe)
    monkeypatch.setattr("src.coco_model.get_coco_model_manager", lambda: FakeCocoManager())

    started = time.monotonic()
    models = fetch_acp_models("coco", cwd="/tmp/ghostap", probe_timeout=0.1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert [m.name for m in models] == [m.name for m in DEFAULT_MODELS]
    assert [m.name for m in models if m.is_default] == ["gpt-4.1"]
