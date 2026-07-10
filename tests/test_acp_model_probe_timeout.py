import asyncio
import time

import src.acp.helper as _helper_mod
from src.acp.helper import fetch_acp_models
from src.coco_model.manager import DEFAULT_MODELS


def test_fetch_acp_models_times_out_and_returns_current_model(monkeypatch, tmp_path):
    async def slow_probe(_tool_name, _cwd, _current_model):
        await asyncio.sleep(1)
        return []

    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
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


def test_fetch_codex_models_current_model_marks_default_without_collapsing_cache(monkeypatch, tmp_path):
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
            {"slug": "gpt-5.4", "display_name": "GPT-5.4", "visibility": "list", "priority": 1},
            {"slug": "gpt-5.3", "display_name": "GPT-5.3", "visibility": "hidden", "priority": 2}
          ]
        }
        """,
        encoding="utf-8",
    )

    _helper_mod._acp_probe_cache.clear()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("src.acp.helper.probe_acp_models", probe_should_not_run)

    models = fetch_acp_models(
        "codex",
        cwd="/tmp/ghostap",
        current_model="gpt-5.4",
        probe_timeout=0.1,
    )

    assert [m.name for m in models] == ["gpt-5.5", "gpt-5.4"]
    assert [m.name for m in models if m.is_default] == ["gpt-5.4"]


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
    _helper_mod._acp_neg_cache.clear()

    models = fetch_acp_models("aiden", cwd="/tmp/ghostap", current_model="model-a")

    assert [m.name for m in models] == ["model-a", "model-b"]
    # Verify cache was populated (keyed by (tool, cwd))
    key = _helper_mod._probe_key("aiden", "/tmp/ghostap")
    assert key in _helper_mod._acp_probe_cache
    _ts, cached_models = _helper_mod._acp_probe_cache[key]
    assert [m.name for m in cached_models] == ["model-a", "model-b"]


def test_fetch_acp_models_non_coco_uses_cache_on_probe_failure(monkeypatch):
    """When probe fails for non-coco tool, cached result is used as fallback."""
    from src.ttadk.models import ACPModelOption

    # Pre-populate cache
    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_neg_cache.clear()
    _helper_mod._acp_probe_cache[_helper_mod._probe_key("codex", "/tmp/ghostap")] = (
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
    _helper_mod._acp_neg_cache.clear()
    expired_ts = _helper_mod._time.time() - _helper_mod._ACP_PROBE_CACHE_TTL - 10
    _helper_mod._acp_probe_cache[_helper_mod._probe_key("gemini", "/tmp/ghostap")] = (
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


# ---------------------------------------------------------------------------
# Single-flight coalescing + negative cache tests
# (the fix for "model selection is slow": concurrent probes were not
#  de-duplicated, and a tool that times out every time — e.g. claude — was
#  re-probed on every card click)
# ---------------------------------------------------------------------------


def test_fetch_acp_models_concurrent_probes_coalesce_to_single_call(monkeypatch):
    """Concurrent lookups for the same (tool, cwd) run the probe only once."""
    import threading

    from src.ttadk.models import ACPModelOption

    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_neg_cache.clear()
    _helper_mod._acp_probe_inflight.clear()

    call_count = 0
    release = threading.Event()

    async def slow_probe(_tool_name, _cwd, _current_model):
        nonlocal call_count
        call_count += 1
        # Hold the leader here so the other threads pile up as waiters.
        release.wait(timeout=5)
        return [ACPModelOption(name="m-1", description="M1", is_default=True)]

    monkeypatch.setattr("src.acp.helper.probe_acp_models", slow_probe)

    results: list[list] = []
    results_lock = threading.Lock()

    def worker():
        models = fetch_acp_models("aiden", cwd="/tmp/ghostap", probe_timeout=5.0)
        with results_lock:
            results.append(models)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    # Give the leader a moment to enter the probe, then let it finish.
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert call_count == 1, f"probe should run once, ran {call_count} times"
    assert len(results) == 5
    for r in results:
        assert [m.name for m in r] == ["m-1"]


def test_fetch_acp_models_negative_cache_skips_reprobe(monkeypatch):
    """After an empty/timed-out probe, the tool is not re-probed within TTL."""
    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_neg_cache.clear()
    _helper_mod._acp_probe_inflight.clear()

    call_count = 0

    async def empty_probe(_tool_name, _cwd, _current_model):
        nonlocal call_count
        call_count += 1
        return []

    monkeypatch.setattr("src.acp.helper.probe_acp_models", empty_probe)

    first = fetch_acp_models("claude", cwd="/tmp/ghostap", probe_timeout=1.0)
    second = fetch_acp_models("claude", cwd="/tmp/ghostap", probe_timeout=1.0)
    # A current_model on a neg-cache hit degrades to that model (no live probe).
    third = fetch_acp_models("claude", cwd="/tmp/ghostap", current_model="x", probe_timeout=1.0)

    assert first == []
    assert second == []
    assert [m.name for m in third] == ["x"]
    # Only the first call should have hit the live probe; the rest are served
    # by the negative cache.
    assert call_count == 1, f"expected 1 live probe, got {call_count}"


def test_fetch_acp_models_negative_cache_expires_and_reprobes(monkeypatch):
    """Once the negative-cache TTL passes, the tool is probed again."""
    from src.ttadk.models import ACPModelOption

    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_neg_cache.clear()
    _helper_mod._acp_probe_inflight.clear()

    call_count = 0

    async def probe(_tool_name, _cwd, _current_model):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        return [ACPModelOption(name="recovered", description="R", is_default=True)]

    monkeypatch.setattr("src.acp.helper.probe_acp_models", probe)

    assert fetch_acp_models("gemini", cwd="/tmp/ghostap", probe_timeout=1.0) == []
    # Force the negative-cache entry to look expired.
    key = _helper_mod._probe_key("gemini", "/tmp/ghostap")
    _helper_mod._acp_neg_cache[key] = (
        _helper_mod._time.time() - _helper_mod._ACP_NEG_CACHE_TTL - 1
    )

    models = fetch_acp_models("gemini", cwd="/tmp/ghostap", probe_timeout=1.0)
    assert [m.name for m in models] == ["recovered"]
    assert call_count == 2


def test_fetch_acp_models_cache_hit_returns_independent_copies(monkeypatch):
    """A cache hit must not let one caller's is_default leak into the cache
    or into another caller's list."""
    from src.ttadk.models import ACPModelOption

    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_neg_cache.clear()
    _helper_mod._acp_probe_inflight.clear()

    async def probe(_tool_name, _cwd, _current_model):
        return [
            ACPModelOption(name="alpha", description="A", is_default=True),
            ACPModelOption(name="beta", description="B", is_default=False),
        ]

    monkeypatch.setattr("src.acp.helper.probe_acp_models", probe)

    # Prime the cache.
    fetch_acp_models("aiden", cwd="/tmp/ghostap", probe_timeout=1.0)

    a = fetch_acp_models("aiden", cwd="/tmp/ghostap", current_model="beta")
    b = fetch_acp_models("aiden", cwd="/tmp/ghostap", current_model="alpha")

    assert [m.name for m in a if m.is_default] == ["beta"]
    assert [m.name for m in b if m.is_default] == ["alpha"]
    # The two result lists are independent objects.
    assert a[0] is not b[0]
    # The shared cache entry is untouched by per-caller default marking.
    key = _helper_mod._probe_key("aiden", "/tmp/ghostap")
    _ts, cached = _helper_mod._acp_probe_cache[key]
    assert [m.name for m in cached if m.is_default] == ["alpha"]


def test_fetch_acp_models_different_cwd_probed_separately(monkeypatch):
    """Cache/single-flight are keyed by (tool, cwd), so distinct cwds probe
    independently."""
    from src.ttadk.models import ACPModelOption

    _helper_mod._acp_probe_cache.clear()
    _helper_mod._acp_neg_cache.clear()
    _helper_mod._acp_probe_inflight.clear()

    seen_cwds: list[str] = []

    async def probe(_tool_name, cwd, _current_model):
        seen_cwds.append(cwd)
        return [ACPModelOption(name=f"m-{cwd}", description="M", is_default=True)]

    monkeypatch.setattr("src.acp.helper.probe_acp_models", probe)

    fetch_acp_models("aiden", cwd="/repo/a", probe_timeout=1.0)
    fetch_acp_models("aiden", cwd="/repo/b", probe_timeout=1.0)
    # Repeat first cwd — should be served from cache, not re-probed.
    fetch_acp_models("aiden", cwd="/repo/a", probe_timeout=1.0)

    assert seen_cwds == ["/repo/a", "/repo/b"]


# ---------------------------------------------------------------------------
# config_options model extraction tests (traex-style providers)
# ---------------------------------------------------------------------------


def test_extract_models_from_config_options_basic():
    """Models are extracted from config_options when available_models is empty."""
    from src.acp.helper import _extract_models_from_config_options

    class MockOption:
        def __init__(self, name, value, description=None):
            self.name = name
            self.value = value
            self.description = description
            self.field_meta = None

    class MockRoot:
        def __init__(self, category, current_value, options):
            self.category = category
            self.current_value = current_value
            self.options = options
            self.field_meta = None
            self.id = "model"
            self.name = "Model"
            self.type = "select"

    class MockConfigOption:
        def __init__(self, root):
            self.root = root

    class MockResp:
        def __init__(self, config_options):
            self.config_options = config_options

    options = [
        MockOption("Test-O-New-Thinking", "c_o_new_thinking", "200K context"),
        MockOption("GPT-5.5", "gpt-5.5", "272K context"),
        MockOption("Doubao-Seed-Code", "Doubao_1_6", "116K context"),
    ]
    root = MockRoot("model", "c_o_new_thinking", options)
    resp = MockResp([MockConfigOption(root)])

    result = _extract_models_from_config_options(resp, "c_o_new_thinking")

    assert len(result) == 3
    assert result[0].name == "c_o_new_thinking"
    assert result[0].description == "Test-O-New-Thinking"
    assert result[0].is_default is True
    assert result[1].name == "gpt-5.5"
    assert result[1].is_default is False


def test_extract_models_from_config_options_skips_non_model_category():
    """Only config_options with category='model' are used."""
    from src.acp.helper import _extract_models_from_config_options

    class MockOption:
        def __init__(self, name, value):
            self.name = name
            self.value = value
            self.description = None
            self.field_meta = None

    class MockRoot:
        def __init__(self, category, current_value, options):
            self.category = category
            self.current_value = current_value
            self.options = options

    class MockConfigOption:
        def __init__(self, root):
            self.root = root

    class MockResp:
        def __init__(self, config_options):
            self.config_options = config_options

    mode_root = MockRoot("mode", "default", [MockOption("Default", "default")])
    resp = MockResp([MockConfigOption(mode_root)])

    result = _extract_models_from_config_options(resp, "")
    assert result == []


def test_probe_acp_models_falls_back_to_config_options(monkeypatch):
    """probe_acp_models returns models from config_options when
    available_models is empty (traex behavior)."""
    from src.ttadk.models import ACPModelOption

    _helper_mod._acp_probe_cache.clear()

    async def fake_probe(_tool_name, _cwd, _current_model):
        class FakeOption:
            def __init__(self, name, value, description=None):
                self.name = name
                self.value = value
                self.description = description
                self.field_meta = None

        class FakeRoot:
            category = "model"
            current_value = "c_o_new_thinking"
            options = [
                FakeOption("Test-O-New", "c_o_new_thinking", "200K context"),
                FakeOption("GPT-5.5", "gpt-5.5", "272K context"),
            ]

        class FakeConfigOption:
            root = FakeRoot()

        return [
            ACPModelOption(name="c_o_new_thinking", description="Test-O-New", is_default=True),
            ACPModelOption(name="gpt-5.5", description="GPT-5.5", is_default=False),
        ]

    monkeypatch.setattr("src.acp.helper.probe_acp_models", fake_probe)

    models = fetch_acp_models("traex", cwd="/tmp/ghostap", probe_timeout=5.0)

    assert len(models) == 2
    assert models[0].name == "c_o_new_thinking"
    assert models[0].is_default is True


def test_probe_acp_models_initializes_lazy_providers_for_traex(monkeypatch):
    """Traex model probing must not depend on some earlier code path having
    already initialized the ACP provider registry."""
    from src.acp.helper import probe_acp_models
    from src.acp.providers import _reset_providers_for_testing

    _reset_providers_for_testing()

    class FakeModels:
        available_models = []
        current_model_id = "c_o_new_thinking/medium"

    class FakeOption:
        def __init__(self, name, value):
            self.name = name
            self.value = value
            self.description = ""

    class FakeRoot:
        category = "model"
        current_value = "c_o_new_thinking"
        options = [
            FakeOption("Test-O-New-Thinking", "c_o_new_thinking"),
            FakeOption("GPT-5.5", "gpt-5.5"),
        ]

    class FakeConfigOption:
        root = FakeRoot()

    class FakeResponse:
        models = FakeModels()
        config_options = [FakeConfigOption()]

    class FakeConn:
        async def initialize(self, protocol_version):
            return None

        async def new_session(self, cwd):
            return FakeResponse()

    class FakeSpawn:
        async def __aenter__(self):
            return FakeConn(), object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_spawn_agent_process(_client, cmd, *args, **_kwargs):
        assert cmd == "traex"
        assert args == ("acp", "serve")
        return FakeSpawn()

    monkeypatch.setattr("src.acp.helper.spawn_agent_process", fake_spawn_agent_process)

    models = asyncio.run(probe_acp_models("traex", cwd="/tmp/ghostap"))

    assert [m.name for m in models] == ["c_o_new_thinking", "gpt-5.5"]
    assert [m.name for m in models if m.is_default] == ["c_o_new_thinking"]
