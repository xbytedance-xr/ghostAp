import threading
import time
from pathlib import Path


def test_ttadk_cache_seed_models_from_error_persists_via_hook(monkeypatch, tmp_path: Path):
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel

    cache_path = tmp_path / "models_cache.json"
    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=cache_path, cache_ttl_s=300)

    called = {"n": 0}

    def _hook(*, cwd=None):
        called["n"] += 1

    c.set_persist_hook(_hook)
    names = c.seed_models_from_error("codex", "Invalid model. Available models: a,b")
    assert names == ["a", "b"]
    assert called["n"] == 1


def test_ttadk_cache_ttl_expire(monkeypatch, tmp_path: Path):
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel

    cache_path = tmp_path / "models_cache.json"
    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=cache_path, cache_ttl_s=0.01)
    c.set_persist_hook(lambda: None)

    # 写入缓存
    c.seed_models_from_invalid_model_runtime(tool_name="codex", available_models=["m1"], source="x")
    r1 = c.get_models(tool_name="codex")
    assert [m.name for m in r1.models] == ["m1"]
    assert r1.source in ("cache", "defaults", "unknown", "probe", "probe_seed", "runtime_invalid_model_seed")

    # 等待 TTL 过期后，再次 get_models 会走 fetcher（此处 fetcher 可能 fallback defaults），但不应再命中 cache
    time.sleep(0.02)
    r2 = c.get_models(tool_name="codex")
    assert r2.cached is False


def test_ttadk_cache_load_from_broken_file_recovers(monkeypatch, tmp_path: Path):
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel

    cache_path = tmp_path / "models_cache.json"
    cache_path.write_text("{broken", encoding="utf-8")

    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=cache_path, cache_ttl_s=300)
    c.set_persist_hook(lambda: None)
    c.load_from_file()
    # broken project cache should be removed
    assert not cache_path.exists()


def test_ttadk_cache_project_path_ssot_no_home_write(monkeypatch, tmp_path: Path):
    """硬门槛：给定 cwd 时只能写入项目目录，不得回退写入真实 HOME。"""
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel
    from pathlib import Path as _Path

    fake_home = tmp_path / "REAL_HOME"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_Path, "home", lambda: fake_home)

    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    project_abs = project.resolve()

    class _S:
        ttadk_models_cache_path = "{cwd}/.ghostap/ttadk/models_cache.json"
        ttadk_models_cache_read_legacy_home = False
        ttadk_models_cache_migrate_from_legacy_home = False

    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=None, cache_ttl_s=300, get_settings_fn=lambda: _S())
    # 写入内存并触发项目落盘
    c.seed_models_from_invalid_model_runtime(tool_name="codex", available_models=["m1"], source="x")
    r = c.get_models(tool_name="codex", cwd=str(project_abs))
    assert [m.name for m in r.models] == ["m1"]

    # 显式落盘到项目目录（get_models 命中内存缓存时不强制 I/O）
    c.save_to_file(cwd=str(project_abs))

    project_cache = project_abs / ".ghostap" / "ttadk" / "models_cache.json"
    assert project_cache.exists()

    # 真实 HOME 不应产生 ~/.ttadk/models_cache.json
    assert not (fake_home / ".ttadk" / "models_cache.json").exists()


def test_ttadk_cache_can_read_legacy_home_and_migrate(monkeypatch, tmp_path: Path):
    """兼容性：项目 cache 不存在时允许读取 legacy HOME cache，并迁移写入项目 cache。"""
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel
    from pathlib import Path as _Path
    import json

    fake_home = tmp_path / "REAL_HOME"
    (fake_home / ".ttadk").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_Path, "home", lambda: fake_home)

    legacy = fake_home / ".ttadk" / "models_cache.json"
    legacy.write_text(json.dumps({"codex": [{"name": "m-legacy", "description": "m-legacy"}]}), encoding="utf-8")

    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    project_abs = project.resolve()

    class _S:
        ttadk_models_cache_path = "{cwd}/.ghostap/ttadk/models_cache.json"
        ttadk_models_cache_read_legacy_home = True
        ttadk_models_cache_migrate_from_legacy_home = True

    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=None, cache_ttl_s=300, get_settings_fn=lambda: _S())
    # 触发按项目加载
    r = c.get_models(tool_name="codex", cwd=str(project_abs))
    assert "m-legacy" in [m.name for m in r.models]

    project_cache = project_abs / ".ghostap" / "ttadk" / "models_cache.json"
    assert project_cache.exists()


def test_ttadk_cache_invalidate(monkeypatch, tmp_path: Path):
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel

    cache_path = tmp_path / "models_cache.json"
    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=cache_path, cache_ttl_s=300)
    c.set_persist_hook(lambda: None)

    c.seed_models_from_invalid_model_runtime(tool_name="codex", available_models=["m1"], source="x")
    assert [m.name for m in c.get_models(tool_name="codex").models] == ["m1"]
    c.invalidate("codex")
    # 失效后不会从 cache 返回 m1（可能 fallback defaults）
    r = c.get_models(tool_name="codex")
    assert "m1" not in [m.name for m in r.models]


def test_ttadk_cache_thread_safety_seed(monkeypatch, tmp_path: Path):
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel

    cache_path = tmp_path / "models_cache.json"
    c = TTADKModelCache(default_models=[TTADKModel(name="d")], cache_file_path=cache_path, cache_ttl_s=300)
    c.set_persist_hook(lambda: None)

    errs: list[Exception] = []

    def _worker(i: int):
        try:
            tool = f"t{i % 5}"
            c.seed_models_from_invalid_model_runtime(tool_name=tool, available_models=[f"m{i}"], source="x")
            _ = c.get_models(tool_name=tool)
        except Exception as e:
            errs.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    assert not errs
