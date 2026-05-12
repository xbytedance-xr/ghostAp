import subprocess
from pathlib import Path

from src.project.context import ProjectContext
from src.thread import get_thread_manager, set_current_thread_id
from src.worktree_engine.git_service import WorktreeGitService
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.selection import WorktreeToolOption


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
        "-c",
        "user.name=Tester",
        "-c",
        "user.email=tester@example.com",
        "commit",
        "-m",
        "init",
    )


def test_git_service_initializes_local_repo_and_creates_units(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "app.py").write_text("print('hi')\n", encoding="utf-8")

    project = ProjectContext(project_id="p1", project_name="P1", root_path=str(project_root))
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)
    manager.select_tool(project, WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco"))
    manager.add_pending_item(project, model_name="doubao-seed-1.6")
    manager.finalize_selection(project)

    state = manager.ensure_worktrees(project)

    assert state.git_initialized_locally is True
    assert state.git_root == str(project_root.resolve())
    assert len(state.units) == 1
    assert Path(state.units[0].worktree_path).exists()
    assert state.units[0].branch_name.startswith("ghostap/wt/")


def test_git_service_keeps_remote_config_unchanged_for_existing_repo(tmp_path):
    remote_root = tmp_path / "remote.git"
    project_root = tmp_path / "project"
    remote_root.mkdir()
    project_root.mkdir()
    _run_git(remote_root, "init", "--bare")
    _init_repo(project_root)
    _run_git(project_root, "remote", "add", "origin", str(remote_root))

    service = WorktreeGitService()
    repo_state = service.ensure_local_repo(str(project_root))
    before_remote = tuple(repo_state.remote_lines)

    project = ProjectContext(project_id="p2", project_name="P2", root_path=str(project_root))
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)
    manager.select_tool(project, WorktreeToolOption(provider="ttadk", tool_name="codex", display_name="Codex"))
    manager.add_pending_item(project, model_name="gpt-5.2")
    manager.finalize_selection(project)

    state = manager.ensure_worktrees(project)
    after_remote = tuple(service.ensure_local_repo(str(project_root)).remote_lines)

    assert state.git_initialized_locally is False
    assert len(state.units) == 1
    assert before_remote == after_remote
    assert Path(state.units[0].worktree_path).exists()


def test_git_service_creates_distinct_worktrees_for_multiple_selections(tmp_path):
    project_root = tmp_path / "project-multi"
    project_root.mkdir()
    _init_repo(project_root)

    project = ProjectContext(project_id="p3", project_name="P3", root_path=str(project_root))
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)
    manager.select_tool(project, WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco"))
    manager.add_pending_item(project, model_name="doubao-seed-1.6")
    manager.back_to_tool_selection(project)
    manager.select_tool(project, WorktreeToolOption(provider="ttadk", tool_name="codex", display_name="Codex"))
    manager.add_pending_item(project, model_name="gpt-5.2")
    manager.finalize_selection(project)

    state = manager.ensure_worktrees(project)

    assert len(state.units) == 2
    assert state.units[0].worktree_path != state.units[1].worktree_path
    assert state.units[0].branch_name != state.units[1].branch_name


def test_git_service_scopes_worktree_names_by_topic_session(tmp_path):
    project_root = tmp_path / "project-topic"
    project_root.mkdir()
    _init_repo(project_root)

    thread_mgr = get_thread_manager()
    thread_mgr.register("thread-a", "chat1", "p-topic", mode="worktree")
    thread_mgr.register("thread-b", "chat1", "p-topic", mode="worktree")
    project = ProjectContext(project_id="p-topic", project_name="P", root_path=str(project_root))
    manager = WorktreeManager(project_manager=None)
    try:
        set_current_thread_id("thread-a")
        manager.start_selection(project)
        manager.select_tool(project, WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco"))
        manager.add_pending_item(project, model_name="m1")
        manager.finalize_selection(project)
        state_a = manager.ensure_worktrees(project)

        set_current_thread_id("thread-b")
        manager.start_selection(project)
        manager.select_tool(project, WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco"))
        manager.add_pending_item(project, model_name="m1")
        manager.finalize_selection(project)
        state_b = manager.ensure_worktrees(project)

        assert state_a.units[0].branch_name != state_b.units[0].branch_name
        assert state_a.units[0].worktree_path != state_b.units[0].worktree_path
        assert "thread-a" in state_a.units[0].branch_name
        assert "thread-b" in state_b.units[0].branch_name
    finally:
        set_current_thread_id(None)
        thread_mgr.remove("thread-a")
        thread_mgr.remove("thread-b")


# ------------------------------------------------------------------
# merge_branch / remove_worktree / remove_branch
# ------------------------------------------------------------------


def _commit_file(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content, encoding="utf-8")
    _run_git(path, "add", filename)
    _run_git(
        path,
        "-c", "user.name=Tester",
        "-c", "user.email=tester@example.com",
        "commit", "-m", message,
    )


def test_merge_branch_success(tmp_path):
    """Fast-forward-free merge succeeds and returns (True, [])."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Create a feature branch, add a commit
    _run_git(repo, "checkout", "-b", "feature-a")
    _commit_file(repo, "a.txt", "aaa\n", "add a")
    _run_git(repo, "checkout", "main")

    service = WorktreeGitService()
    ok, conflicts = service.merge_branch(str(repo), "feature-a", "main")

    assert ok is True
    assert conflicts == []
    assert (repo / "a.txt").exists()


def test_merge_branch_conflict_aborts(tmp_path):
    """Conflicting merge returns (False, conflict_files) and aborts cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Diverge: main edits file one way, feature edits another
    _commit_file(repo, "shared.txt", "main content\n", "main edit")
    _run_git(repo, "checkout", "-b", "feature-b", "HEAD~1")
    _commit_file(repo, "shared.txt", "feature content\n", "feature edit")
    _run_git(repo, "checkout", "main")

    service = WorktreeGitService()
    ok, conflicts = service.merge_branch(str(repo), "feature-b", "main")

    assert ok is False
    assert "shared.txt" in conflicts
    # Repo should be clean after abort
    status = _run_git(repo, "status", "--porcelain")
    assert status.stdout.strip() == ""


def test_remove_worktree(tmp_path):
    """remove_worktree deletes the worktree directory and prunes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    # Use the expected parent directory so path validation passes
    wt_parent = Path(service.build_worktree_parent(str(repo)))
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = str(wt_parent / "wt-test")
    _run_git(repo, "worktree", "add", "-b", "wt-branch", wt_path, "main")
    assert Path(wt_path).exists()

    service.remove_worktree(str(repo), wt_path)

    assert not Path(wt_path).exists()


def test_remove_branch(tmp_path):
    """remove_branch force-deletes a local branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _run_git(repo, "branch", "to-delete")
    branches_before = _run_git(repo, "branch", "--list").stdout
    assert "to-delete" in branches_before

    service = WorktreeGitService()
    service.remove_branch(str(repo), "to-delete")

    branches_after = _run_git(repo, "branch", "--list").stdout
    assert "to-delete" not in branches_after


def test_remove_branch_nonexistent_is_noop(tmp_path):
    """Removing a branch that doesn't exist should not raise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    service = WorktreeGitService()
    # Should not raise
    service.remove_branch(str(repo), "nonexistent-branch")


def test_no_git_auto_init_and_create_worktree(tmp_path):
    """T8: Empty directory → auto git init → create worktree succeeds (AC5)."""
    project_root = tmp_path / "empty-project"
    project_root.mkdir()
    # Just write a file — no git repo
    (project_root / "main.py").write_text("pass\n", encoding="utf-8")

    service = WorktreeGitService()

    # ensure_local_repo should auto-init
    repo_state = service.ensure_local_repo(str(project_root))
    assert repo_state.initialized is True
    assert repo_state.repo_root == str(project_root.resolve())

    # Verify git is actually initialised
    result = _run_git(project_root, "rev-parse", "--is-inside-work-tree")
    assert result.stdout.strip() == "true"

    # Now create worktrees on the auto-initialised repo
    from src.worktree_engine.models import WorktreeSelectionItem

    selections = [
        WorktreeSelectionItem(
            provider="acp", tool_name="coco",
            display_name="Coco", model_name="doubao",
        ),
    ]
    _, units = service.create_units(
        root_path=str(project_root), count=len(selections),
    )
    assert len(units) == 1
    assert Path(units[0].worktree_path).exists()
    assert units[0].branch_name.startswith("ghostap/wt/")


def test_full_lifecycle_create_execute_merge_cleanup(tmp_path):
    """T7: create → write files → commit → merge → cleanup full lifecycle."""
    project_root = tmp_path / "lifecycle-project"
    project_root.mkdir()
    _init_repo(project_root)

    from src.worktree_engine.models import WorktreeSelectionItem

    service = WorktreeGitService()
    selections = [
        WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco"),
        WorktreeSelectionItem(provider="cli", tool_name="claude", display_name="Claude"),
    ]
    repo_state, units = service.create_units(
        root_path=str(project_root), count=len(selections),
    )

    # Verify worktrees exist
    wt_list_before = _run_git(project_root, "worktree", "list").stdout
    for unit in units:
        assert Path(unit.worktree_path).exists()
        assert unit.branch_name in wt_list_before

    # Simulate tool execution: write files and commit in each worktree
    # Since we didn't call dispatcher, units don't have tool_name yet.
    # We use selections to simulate which tool went to which unit.
    for i, unit in enumerate(units):
        tool_name = selections[i].tool_name
        wt = Path(unit.worktree_path)
        _commit_file(wt, f"{tool_name}.py", f"# {tool_name}\n", f"feat: {tool_name}")

    # Merge each branch back to base
    for unit in units:
        ok, conflicts = service.merge_branch(repo_state.repo_root, unit.branch_name, repo_state.base_branch)
        assert ok is True
        assert conflicts == []

    # Verify merged files exist on base branch
    _run_git(project_root, "checkout", repo_state.base_branch)
    assert (project_root / "coco.py").exists()
    assert (project_root / "claude.py").exists()

    # Cleanup
    for unit in units:
        service.remove_worktree(repo_state.repo_root, unit.worktree_path)
        service.remove_branch(repo_state.repo_root, unit.branch_name)

    # Verify worktrees are gone
    wt_list_after = _run_git(project_root, "worktree", "list").stdout
    for unit in units:
        assert unit.worktree_path not in wt_list_after


def test_no_remote_operations_during_lifecycle(tmp_path, monkeypatch):
    """T9: No push/fetch/pull operations during entire worktree lifecycle (AC6)."""
    project_root = tmp_path / "no-remote-project"
    project_root.mkdir()
    _init_repo(project_root)

    from src.worktree_engine.models import WorktreeSelectionItem
    from unittest.mock import patch

    # Track all git commands
    git_commands: list[list[str]] = []
    original_run = subprocess.run

    def tracking_run(args, **kwargs):
        if args and args[0] == "git":
            git_commands.append(list(args))
        return original_run(args, **kwargs)

    with patch("subprocess.run", side_effect=tracking_run):
        service = WorktreeGitService()
        selections = [
            WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco"),
        ]
        repo_state, units = service.create_units(
            root_path=str(project_root), count=len(selections),
        )

        # Simulate work and merge
        wt = Path(units[0].worktree_path)
        _commit_file(wt, "test.py", "pass\n", "add test")
        service.merge_branch(repo_state.repo_root, units[0].branch_name, repo_state.base_branch)

        # Cleanup
        service.remove_worktree(repo_state.repo_root, units[0].worktree_path)
        service.remove_branch(repo_state.repo_root, units[0].branch_name)

    # Assert no remote operations occurred
    remote_ops = {"push", "fetch", "pull"}
    for cmd in git_commands:
        git_subcmd = cmd[1] if len(cmd) > 1 else ""
        assert git_subcmd not in remote_ops, f"Unexpected remote operation: {cmd}"
