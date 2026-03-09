from pathlib import Path


def test_normalize_ttadk_cwd_none_and_empty():
    from src.utils.path import normalize_ttadk_cwd

    assert normalize_ttadk_cwd(None) is None
    assert normalize_ttadk_cwd("") is None
    assert normalize_ttadk_cwd("   ") is None


def test_normalize_ttadk_cwd_dot_is_absolute(tmp_path: Path, monkeypatch):
    from src.utils.path import normalize_ttadk_cwd

    # ensure '.' resolves to an absolute path (under current process cwd)
    monkeypatch.chdir(tmp_path)
    out = normalize_ttadk_cwd(".")
    assert out is not None
    assert Path(out).is_absolute()
    assert Path(out) == tmp_path.resolve()


def test_normalize_ttadk_cwd_relative_is_absolute(tmp_path: Path, monkeypatch):
    from src.utils.path import normalize_ttadk_cwd

    monkeypatch.chdir(tmp_path)
    (tmp_path / "p").mkdir(parents=True, exist_ok=True)
    out = normalize_ttadk_cwd("p")
    assert out is not None
    assert Path(out).is_absolute()
    assert Path(out) == (tmp_path / "p").resolve()
