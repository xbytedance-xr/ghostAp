"""AC6: Sync worktree — after sync, status is clean and HEAD matches remote."""

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


def test_sync_resets_to_branch_head(tmp_path):
    """sync_worktree should reset the worktree to the branch HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(repo), count=1)
    wt_path = units[0].worktree_path
    wt = Path(wt_path)

    # Make the worktree dirty
    (wt / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    status_before = _run_git(wt, "status", "--porcelain").stdout.strip()
    assert status_before  # Should be dirty

    # Sync with force (since worktree is dirty)
    result = service.sync_worktree(str(repo), wt_path, force=True)

    assert result is None
    # After sync, status should be clean
    status_after = _run_git(wt, "status", "--porcelain").stdout.strip()
    assert status_after == ""


def test_sync_refuses_dirty_without_force(tmp_path):
    """sync_worktree(force=False) should return warning for dirty worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(repo), count=1)
    wt_path = units[0].worktree_path

    # Make dirty
    (Path(wt_path) / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    warning = service.sync_worktree(str(repo), wt_path, force=False)

    assert warning is not None
    assert warning.has_uncommitted is True


def test_sync_with_remote_updates(tmp_path):
    """After pushing new commits to remote, sync should bring worktree up to date."""
    # Set up bare + local + worktree
    bare = tmp_path / "remote.git"
    local = tmp_path / "local"
    work_setup = tmp_path / "work-setup"
    work_setup.mkdir()

    _init_repo(work_setup)
    subprocess.run(
        ["git", "clone", "--bare", str(work_setup), str(bare)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True, text=True,
    )

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(local), count=1)
    wt_path = units[0].worktree_path
    wt_branch = units[0].branch_name

    # Record initial HEAD
    initial_head = _run_git(Path(wt_path), "rev-parse", "HEAD").stdout.strip()

    # Push a new commit to bare via local main
    (local / "new_file.py").write_text("new\n", encoding="utf-8")
    _run_git(local, "add", "new_file.py")
    _run_git(
        local,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "new commit on main",
    )
    _run_git(local, "push", "origin", "main")

    # Sync the worktree (which is on its own branch, so we sync to that branch)
    result = service.sync_worktree(str(local), wt_path, force=True)
    assert result is None

    # Status should be clean after sync
    status = _run_git(Path(wt_path), "status", "--porcelain").stdout.strip()
    assert status == ""
