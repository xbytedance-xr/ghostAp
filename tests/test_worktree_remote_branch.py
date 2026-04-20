"""AC2: Remote branch association — create worktree tracking a remote branch."""

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


def _init_bare_with_branch(bare_path: Path, branch: str, filename: str) -> str:
    """Create a bare repo with a branch and return the tip commit hash."""
    # Use a temp working copy to set up the bare repo
    work = bare_path.parent / "work-setup"
    work.mkdir()
    _run_git(work, "init")
    _run_git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(work, "add", "seed.txt")
    _run_git(
        work,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "seed",
    )
    # Create the target branch
    _run_git(work, "checkout", "-b", branch)
    (work / filename).write_text(f"from {branch}\n", encoding="utf-8")
    _run_git(work, "add", filename)
    _run_git(
        work,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", f"add {filename} on {branch}",
    )
    tip = _run_git(work, "rev-parse", "HEAD").stdout.strip()
    # Push to bare
    _run_git(bare_path.parent, "git", "clone", "--bare", str(work), str(bare_path)) if not bare_path.exists() else None
    if not bare_path.exists():
        # Create bare repo from work
        bare_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare_path)],
        check=True, capture_output=True, text=True,
    )
    return tip


def test_create_with_remote_branch(tmp_path):
    """Create a worktree tracking a remote branch, HEAD should match remote tip."""
    bare = tmp_path / "remote.git"
    feature_branch = "feature-x"

    # Set up bare repo with a feature branch
    work = tmp_path / "work-setup"
    work.mkdir()
    _run_git(work, "init")
    _run_git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(work, "add", "seed.txt")
    _run_git(
        work,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "seed",
    )
    _run_git(work, "checkout", "-b", feature_branch)
    (work / "feature.py").write_text("# feature\n", encoding="utf-8")
    _run_git(work, "add", "feature.py")
    _run_git(
        work,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "add feature",
    )
    remote_tip = _run_git(work, "rev-parse", "HEAD").stdout.strip()

    # Create bare from work
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        check=True, capture_output=True, text=True,
    )

    # Clone to a local repo and add origin
    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True, text=True,
    )

    # Create worktree with remote_branch
    service = WorktreeGitService()
    wt_path = tmp_path / "wt-feature"

    service.create_worktree(
        str(local), "ghostap/wt/01-feature", str(wt_path), "main",
        custom_path=str(wt_path),
        remote_branch=f"origin/{feature_branch}",
    )

    assert wt_path.exists()
    # HEAD of the new worktree should match the remote tip
    wt_head = _run_git(wt_path, "rev-parse", "HEAD").stdout.strip()
    assert wt_head == remote_tip

    # feature.py should exist in the worktree
    assert (wt_path / "feature.py").exists()


def test_create_with_remote_branch_without_origin_prefix(tmp_path):
    """remote_branch without 'origin/' prefix should also work."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "work-setup"
    work.mkdir()
    _run_git(work, "init")
    _run_git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _run_git(work, "add", "a.txt")
    _run_git(
        work,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "init",
    )
    _run_git(work, "checkout", "-b", "dev")
    (work / "dev.py").write_text("dev\n", encoding="utf-8")
    _run_git(work, "add", "dev.py")
    _run_git(
        work,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", "dev commit",
    )
    remote_tip = _run_git(work, "rev-parse", "HEAD").stdout.strip()

    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        check=True, capture_output=True, text=True,
    )
    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True, text=True,
    )

    service = WorktreeGitService()
    wt_path = tmp_path / "wt-dev"

    service.create_worktree(
        str(local), "ghostap/wt/01-dev", str(wt_path), "main",
        custom_path=str(wt_path),
        remote_branch="dev",  # no origin/ prefix
    )

    wt_head = _run_git(wt_path, "rev-parse", "HEAD").stdout.strip()
    assert wt_head == remote_tip
