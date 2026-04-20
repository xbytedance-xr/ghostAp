"""AC1: Custom path creation — nonexistent path, existing path, security validation."""

import subprocess
from pathlib import Path

import pytest

from src.worktree_engine.git_service import WorktreeGitError, WorktreeGitService


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    _run_git(path, "init")
    _run_git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    (path / "README.txt").write_text("hello\n", encoding="utf-8")
    _run_git(path, "add", "README.txt")
    _run_git(
        path,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "init",
    )


def test_create_with_nonexistent_custom_path(tmp_path):
    """Pass a nonexistent custom_path — directory should be recursively created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    custom = tmp_path / "custom" / "deep" / "wt"
    assert not custom.exists()

    service.create_worktree(
        str(repo), "ghostap/wt/01-unit", str(custom), "main",
        custom_path=str(custom),
    )

    assert custom.exists()
    # Verify it's a valid git worktree
    result = _run_git(custom, "rev-parse", "--is-inside-work-tree")
    assert result.stdout.strip() == "true"


def test_create_with_existing_empty_custom_path(tmp_path):
    """Pass an existing empty custom_path — should work identically."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    custom = tmp_path / "existing-empty"
    custom.mkdir(parents=True)

    service.create_worktree(
        str(repo), "ghostap/wt/01-unit", str(custom), "main",
        custom_path=str(custom),
    )

    assert custom.exists()
    result = _run_git(custom, "rev-parse", "--is-inside-work-tree")
    assert result.stdout.strip() == "true"


def test_custom_path_security_rejects_dotdot(tmp_path):
    """Path with '..' should be rejected."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    bad_path = str(tmp_path / "foo" / ".." / "bar")

    with pytest.raises(WorktreeGitError, match="\\.\\."):
        service.create_worktree(
            str(repo), "ghostap/wt/01-unit", "ignored", "main",
            custom_path=bad_path,
        )


def test_custom_path_security_rejects_system_dirs(tmp_path):
    """Paths under /etc, /usr, etc. should be rejected."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    for bad in ["/etc/ghostap-wt", "/usr/local/wt", "/var/ghostap"]:
        with pytest.raises(WorktreeGitError, match="系统目录"):
            service.create_worktree(
                str(repo), "ghostap/wt/01-unit", "ignored", "main",
                custom_path=bad,
            )


def test_create_units_with_custom_base_dir(tmp_path):
    """create_units with custom_base_dir places worktrees under specified dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    custom_dir = tmp_path / "my-worktrees"

    _, units = service.create_units(
        root_path=str(repo), count=2, custom_base_dir=str(custom_dir),
    )

    assert len(units) == 2
    for unit in units:
        assert str(custom_dir.resolve()) in unit.worktree_path
        assert Path(unit.worktree_path).exists()
