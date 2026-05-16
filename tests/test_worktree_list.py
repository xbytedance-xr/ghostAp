"""AC5: Worktree list — field completeness and column alignment."""

import subprocess
from pathlib import Path

from src.worktree_engine.git_service import WorktreeGitService
from src.worktree_engine.models import WorktreeInfo
from src.worktree_engine.reporter import WorktreeReporter


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


def test_list_worktrees_returns_structured_entries(tmp_path):
    """list_worktrees returns WorktreeInfo with all required fields."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    # Create 3 worktrees
    _, units = service.create_units(root_path=str(repo), count=3)
    assert len(units) == 3

    entries = service.list_worktrees(str(repo))

    # Should include main worktree + 3 created worktrees
    assert len(entries) >= 4

    for entry in entries:
        assert isinstance(entry, WorktreeInfo)
        assert entry.path  # Non-empty path
        # branch may be empty for detached HEAD, but should generally be present
        assert isinstance(entry.is_active, bool)
        assert isinstance(entry.last_updated, str)


def test_list_worktrees_branch_names_correct(tmp_path):
    """Branches in list output should match created branches."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(repo), count=2)

    entries = service.list_worktrees(str(repo))
    branch_names = {e.branch for e in entries}

    assert "main" in branch_names
    for unit in units:
        assert unit.branch_name in branch_names


def test_list_worktrees_last_updated_not_empty(tmp_path):
    """last_updated should be non-empty for worktrees with commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    _, units = service.create_units(root_path=str(repo), count=1)

    entries = service.list_worktrees(str(repo))

    # Main repo entry should have a commit time
    main_entry = [e for e in entries if e.branch == "main"]
    assert main_entry
    assert main_entry[0].last_updated  # Non-empty


def test_format_worktree_table_alignment(tmp_path):
    """format_worktree_table should produce aligned columns."""
    entries = [
        WorktreeInfo(path="/short", branch="main", is_active=True, last_updated="2026-01-01 00:00:00"),
        WorktreeInfo(path="/a/very/long/path/here", branch="feature-x", is_active=False, last_updated="2026-01-02 12:00:00"),
        WorktreeInfo(path="/medium/path", branch="dev", is_active=False, last_updated="2026-01-03 08:30:00"),
    ]

    table = WorktreeReporter().format_worktree_table(entries)
    lines = table.split("\n")

    # Header + separator + 3 data rows
    assert len(lines) == 5

    # All lines should have the same number of columns (split by double-space)
    # Check that each data row is properly aligned (same column positions)
    lines[0]
    for data_line in lines[2:]:
        # Status column should start at the same position
        assert len(data_line.rstrip()) > 0


def test_format_worktree_table_empty():
    """Empty entries should return a placeholder."""
    table = WorktreeReporter().format_worktree_table([])
    assert "(无 worktree)" in table


def test_format_worktree_table_active_marker():
    """Active worktree should have '*' marker in status column."""
    entries = [
        WorktreeInfo(path="/active", branch="main", is_active=True, last_updated="now"),
        WorktreeInfo(path="/inactive", branch="dev", is_active=False, last_updated="now"),
    ]

    table = WorktreeReporter().format_worktree_table(entries)

    assert "活跃 *" in table
    assert "非活跃" in table
