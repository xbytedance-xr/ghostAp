"""AC3/AC4: Safe delete — uncommitted changes warning, unmerged branches warning, force delete."""

import subprocess
from pathlib import Path

from src.worktree_engine.git_service import WorktreeGitService


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


def _create_worktree(service: WorktreeGitService, repo: Path, tmp_path: Path) -> tuple[str, str]:
    """Create a single worktree and return (worktree_path, branch_name)."""
    wt_parent = Path(service.build_worktree_parent(str(repo)))
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = str(wt_parent / "wt-01")
    branch = "ghostap/wt/01-unit"
    service.create_worktree(str(repo), branch, wt_path, "main")
    return wt_path, branch


# ---- AC3: Uncommitted changes ----


def test_delete_with_uncommitted_changes_returns_warning(tmp_path):
    """Uncommitted file in worktree → remove_worktree(force=False) returns DeleteWarning."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    wt_path, branch = _create_worktree(service, repo, tmp_path)

    # Create an uncommitted file
    (Path(wt_path) / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    warning = service.remove_worktree(str(repo), wt_path, force=False, base_branch="main")

    assert warning is not None
    assert warning.has_uncommitted is True
    assert any("dirty.txt" in f for f in warning.uncommitted_files)
    # Directory should still exist
    assert Path(wt_path).exists()


def test_delete_force_with_uncommitted_deletes(tmp_path):
    """force=True should delete even with uncommitted changes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    wt_path, branch = _create_worktree(service, repo, tmp_path)

    # Create an uncommitted file
    (Path(wt_path) / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    warning = service.remove_worktree(str(repo), wt_path, force=True)

    assert warning is None
    assert not Path(wt_path).exists()


def test_delete_clean_worktree_without_force(tmp_path):
    """Clean worktree with force=False should delete successfully."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    wt_path, branch = _create_worktree(service, repo, tmp_path)

    warning = service.remove_worktree(str(repo), wt_path, force=False, base_branch="main")

    assert warning is None
    assert not Path(wt_path).exists()


# ---- AC4: Unmerged branches ----


def test_delete_with_unmerged_commits_returns_warning(tmp_path):
    """Worktree branch with unmerged commits → DeleteWarning.has_unmerged=True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    wt_path, branch = _create_worktree(service, repo, tmp_path)

    # Make a commit in the worktree branch (not merged to main)
    wt = Path(wt_path)
    (wt / "feature.py").write_text("# feature\n", encoding="utf-8")
    _run_git(wt, "add", "feature.py")
    _run_git(
        wt,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "add feature",
    )

    warning = service.remove_worktree(str(repo), wt_path, force=False, base_branch="main")

    assert warning is not None
    assert warning.has_unmerged is True
    assert warning.unmerged_branch == branch
    # Directory should still exist
    assert Path(wt_path).exists()


# ---- check_worktree_safety directly ----


def test_check_safety_clean_worktree(tmp_path):
    """Clean worktree returns is_safe=True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    wt_path, _ = _create_worktree(service, repo, tmp_path)

    warning = service.check_worktree_safety(str(repo), wt_path, "main")

    assert warning.is_safe is True
    assert warning.has_uncommitted is False
    assert warning.has_unmerged is False
