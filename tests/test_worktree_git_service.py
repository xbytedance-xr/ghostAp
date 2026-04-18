import subprocess
from pathlib import Path

from src.project.context import ProjectContext
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
