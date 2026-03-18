from __future__ import annotations

from pathlib import Path


def test_agent_session_create_engine_session_normalizes_dot_cwd(monkeypatch, tmp_path: Path):
    """入口级回归：传入 cwd='.' 时，create_engine_session 侧应归一化为绝对路径再交给底层启动。"""
    from src import agent_session as a

    monkeypatch.chdir(tmp_path)

    captured = {}

    def _fake_start_session_with_retry(*, agent_type, cwd, startup_timeout=None, model_name=None, **kwargs):
        captured["cwd"] = cwd

        class _S:
            def start(self, startup_timeout=None):
                return None

            def close(self):
                return None

        return _S()

    # 注意：create_engine_session 内部会 `from .acp.sync_adapter import start_session_with_retry`
    # 因此需要 patch 对应模块的符号。
    import src.acp.sync_adapter as sa

    monkeypatch.setattr(sa, "start_session_with_retry", _fake_start_session_with_retry)

    # 调用非 TTADK 分支即可触发 start_session_with_retry
    _ = a.create_engine_session(agent_type="coco", cwd=".", model_name=None)
    assert "cwd" in captured
    assert Path(str(captured["cwd"])) == tmp_path.resolve()


def test_ttadk_cache_does_not_write_repo_root_on_relative_cwd(monkeypatch, tmp_path: Path):
    """硬门槛：相对 cwd（例如 '.'）不应触发项目级落盘到仓库根目录。"""
    from src.ttadk.cache import TTADKModelCache
    from src.ttadk.models import TTADKModel

    # 将进程工作目录切到一个“模拟仓库根”的目录
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo_root)

    class _S:
        ttadk_models_cache_path = "{cwd}/.ghostap/ttadk/models_cache.json"
        ttadk_models_cache_read_legacy_home = False
        ttadk_models_cache_migrate_from_legacy_home = False

    c = TTADKModelCache(
        default_models=[TTADKModel(name="d")], cache_file_path=None, cache_ttl_s=300, get_settings_fn=lambda: _S()
    )
    c.seed_models_from_invalid_model_runtime(tool_name="codex", available_models=["m1"], source="x")

    # 若调用方错误传入 cwd='.'，cache 层不得落盘到 repo_root
    c.save_to_file(cwd=".")
    assert not (repo_root / ".ghostap" / "ttadk" / "models_cache.json").exists()
