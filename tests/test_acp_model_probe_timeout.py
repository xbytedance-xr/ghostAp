import asyncio
import time

from src.acp.helper import fetch_acp_models
from src.coco_model.manager import DEFAULT_MODELS
import src.acp.helper as _helper_mod


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


def test_fetch_codex_models_uses_local_codex_cache_without_live_probe(monkeypatch, tmp_path):
    async def probe_should_not_run(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("codex fallback should not require live model probe")

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    (codex_home / "models_cache.json").write_text(
        """
        {
          "models": [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 0},
            {"slug": "gpt-5.4", "display_name": "GPT-5.4", "visibility": "list", "priority": 1}
          ]
        }
        """,
        encoding="utf-8",
    )

    _helper_mod._acp_probe_cache.clear()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("src.acp.helper.probe_acp_models", probe_should_not_run)

    models = fetch_acp_models("codex", cwd="/tmp/ghostap", probe_timeout=0.1)

    assert [m.name for m in models] == ["gpt-5.5", "gpt-5.4"]
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


# ---------------------------------------------------------------------------
# Non-coco ACP probe cache tests
# ---------------------------------------------------------------------------


def test_fetch_acp_models_non_coco_caches_successful_probe(monkeypatch):
    """Successful probe for non-coco tools is cached in _acp_probe_cache."""
    from src.ttadk.models import ACPModelOption

    probe_results = [
        ACPModelOption(name="model-a", description="A", is_default=True),
        ACPModelOption(name="model-b", description="B", is_default=False),
    ]

    async def fake_probe(_tool_name, _cwd, _current_model):
        return probe_results

    monkeypatch.setattr("src.acp.helper.probe_acp_models", fake_probe)
    # Clear cache before test
    _helper_mod._acp_probe_cache.clear()

    models = fetch_acp_models("aiden", cwd="/tmp/ghostap", current_model="model-a")

    assert [m.name for m in models] == ["model-a", "model-b"]
    # Verify cache was populated
    assert "aiden" in _helper_mod._acp_probe_cache
    _ts, cached_models = _helper_mod._acp_probe_cache["aiden"]
    assert [m.name for m in cached_models] == ["model-a", "model-b"]


def test_fetch_acp_models_non_coco_uses_cache_on_probe_failure(monkeypatch):
    """When probe fails for non-coco tool, cached result is used as fallback."""
    from src.ttadk.models import ACPModelOption

    # Pre-populate cache
    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_probe_cache["codex"] = (
        _helper_mod._time.time(),
        [
            ACPModelOption(name="cached-1", description="C1", is_default=True),
            ACPModelOption(name="cached-2", description="C2", is_default=False),
        ],
    )

    async def failing_probe(_tool_name, _cwd, _current_model):
        raise RuntimeError("network error")

    monkeypatch.setattr("src.acp.helper.probe_acp_models", failing_probe)

    models = fetch_acp_models("codex", cwd="/tmp/ghostap", current_model="cached-2")

    assert [m.name for m in models] == ["cached-1", "cached-2"]
    # current_model should be re-marked as default
    assert [m.name for m in models if m.is_default] == ["cached-2"]


def test_fetch_acp_models_non_coco_expired_cache_not_used(monkeypatch):
    """Expired cache entries are not returned; falls back to current_model."""
    from src.ttadk.models import ACPModelOption

    # Pre-populate cache with expired entry (TTL + 10s ago)
    _helper_mod._acp_probe_cache.clear()
    expired_ts = _helper_mod._time.time() - _helper_mod._ACP_PROBE_CACHE_TTL - 10
    _helper_mod._acp_probe_cache["gemini"] = (
        expired_ts,
        [ACPModelOption(name="old-model", description="Old", is_default=True)],
    )

    async def failing_probe(_tool_name, _cwd, _current_model):
        raise RuntimeError("timeout")

    monkeypatch.setattr("src.acp.helper.probe_acp_models", failing_probe)

    models = fetch_acp_models("gemini", cwd="/tmp/ghostap", current_model="fallback-model")

    # Should not return expired cache; should fall back to current_model only
    assert [m.name for m in models] == ["fallback-model"]
    assert models[0].is_default is True
