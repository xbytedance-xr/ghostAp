"""AC7/AC8: Performance — storage optimization and batch creation timing."""

import subprocess
import time
from pathlib import Path

import pytest

from src.worktree_engine.git_service import WorktreeGitService


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_content(path: Path, file_count: int = 20) -> None:
    """Initialize a repo with multiple files to have meaningful storage."""
    _run_git(path, "init")
    _run_git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    for i in range(file_count):
        (path / f"module_{i:03d}.py").write_text(
            f"# Module {i}\n" + "x = 1\n" * 50, encoding="utf-8"
        )
    _run_git(path, "add", "-A")
    _run_git(
        path,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "init with content",
    )


def _get_dir_size(path: Path) -> int:
    """Get total size of a directory in bytes."""
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


@pytest.mark.slow
def test_batch_creation_performance(tmp_path):
    """AC8: Creating 10 worktrees should take <= 10 seconds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_content(repo, file_count=20)

    service = WorktreeGitService()

    start = time.monotonic()
    _, units = service.create_units(root_path=str(repo), count=10)
    elapsed = time.monotonic() - start

    assert len(units) == 10
    for unit in units:
        assert Path(unit.worktree_path).exists()

    # Each worktree should have independent files
    for unit in units:
        wt = Path(unit.worktree_path)
        assert (wt / "module_000.py").exists()

    assert elapsed <= 10.0, f"Batch creation took {elapsed:.2f}s, exceeds 10s limit"


@pytest.mark.slow
def test_worktrees_share_git_objects(tmp_path):
    """AC7: Multiple worktrees should share git objects via symlinks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_content(repo, file_count=30)

    service = WorktreeGitService()

    # Measure repo size before
    repo_size_before = _get_dir_size(repo)

    _, units = service.create_units(root_path=str(repo), count=10)

    # Each worktree's .git should be a file (symlink reference), not a full copy
    for unit in units:
        wt = Path(unit.worktree_path)
        git_file = wt / ".git"
        assert git_file.exists()
        # In git worktrees, .git is a file containing "gitdir: ..." not a directory
        assert git_file.is_file(), f"{git_file} should be a file, not a directory"

    # Total size of all worktrees should be much less than 10x repo size
    total_wt_size = sum(_get_dir_size(Path(u.worktree_path)) for u in units)
    # Worktrees share objects, so total should be significantly less than 10x
    # The .git objects are shared, only working tree files are duplicated
    assert total_wt_size < repo_size_before * 10, (
        f"Total worktree size ({total_wt_size}) is not significantly less than 10x repo ({repo_size_before * 10})"
    )


@pytest.mark.slow
def test_optimize_storage_runs_without_error(tmp_path):
    """optimize_storage should run gc + repack without errors."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_content(repo, file_count=10)

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(repo), count=5)

    # Should not raise
    service.optimize_storage(str(repo))

    # Repo should still be functional
    result = _run_git(repo, "status", "--porcelain")
    assert result.returncode == 0


@pytest.mark.slow
def test_each_worktree_independent_operations(tmp_path):
    """AC8: Each worktree should support independent file operations."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_content(repo, file_count=5)

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(repo), count=10)

    # Write and commit in each worktree independently
    for i, unit in enumerate(units):
        wt = Path(unit.worktree_path)
        (wt / f"independent_{i}.py").write_text(f"# unit {i}\n", encoding="utf-8")
        _run_git(wt, "add", f"independent_{i}.py")
        _run_git(
            wt,
            "-c", "user.name=Tester",
            "-c", "user.email=tester@example.com",
            "commit", "-m", f"independent commit {i}",
        )

    # Verify each worktree has only its own independent file
    for i, unit in enumerate(units):
        wt = Path(unit.worktree_path)
        assert (wt / f"independent_{i}.py").exists()
        # Other worktrees' files should NOT be here
        for j in range(len(units)):
            if j != i:
                assert not (wt / f"independent_{j}.py").exists()
