from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import WorktreeSelectionItem, WorktreeUnit


class WorktreeGitError(RuntimeError):
    pass


def _slugify(value: str, *, default: str = "unit") -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return (text or default).lower()


@dataclass(frozen=True)
class GitRepoState:
    repo_root: str
    base_branch: str
    initialized: bool = False
    remote_lines: tuple[str, ...] = ()


class WorktreeGitService:
    def __init__(self, *, default_base_branch: str = "main") -> None:
        self._default_base_branch = default_base_branch

    def _run_git(self, cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        if check and result.returncode != 0:
            raise WorktreeGitError((result.stderr or result.stdout or "git command failed").strip())
        return result

    def _has_git_repo(self, root_path: str) -> bool:
        result = self._run_git(root_path, "rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and (result.stdout or "").strip() == "true"

    def _git_root(self, root_path: str) -> str:
        return (self._run_git(root_path, "rev-parse", "--show-toplevel").stdout or "").strip()

    def _current_branch(self, root_path: str) -> str:
        branch = (self._run_git(root_path, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()
        if branch and branch != "HEAD":
            return branch
        return self._default_base_branch

    def _remote_lines(self, root_path: str) -> tuple[str, ...]:
        result = self._run_git(root_path, "remote", "-v", check=False)
        if result.returncode != 0:
            return ()
        return tuple(line.strip() for line in (result.stdout or "").splitlines() if line.strip())

    def ensure_local_repo(self, root_path: str) -> GitRepoState:
        root_path = str(Path(root_path).resolve())
        initialized = False
        if not self._has_git_repo(root_path):
            self._run_git(root_path, "init")
            initialized = True
            self._run_git(root_path, "symbolic-ref", "HEAD", f"refs/heads/{self._default_base_branch}")
            self._run_git(root_path, "add", "-A", check=False)
            self._run_git(
                root_path,
                "-c",
                "user.name=GhostAP",
                "-c",
                "user.email=ghostap@local",
                "commit",
                "--allow-empty",
                "-m",
                "Initialize local worktree support",
            )
        repo_root = self._git_root(root_path)
        base_branch = self._current_branch(repo_root)
        return GitRepoState(
            repo_root=repo_root,
            base_branch=base_branch,
            initialized=initialized,
            remote_lines=self._remote_lines(repo_root),
        )

    def build_worktree_parent(self, repo_root: str) -> str:
        root = Path(repo_root).resolve()
        return str(root.parent / f".ghostap-worktrees-{root.name}")

    def build_branch_name(self, index: int) -> str:
        return f"ghostap/wt/{index:02d}-unit"

    def build_unit_id(self, index: int) -> str:
        return f"wt-{index:02d}"

    def create_worktree(self, repo_root: str, branch_name: str, worktree_path: str, base_ref: str) -> None:
        worktree = Path(worktree_path)
        if worktree.exists() and any(worktree.iterdir()):
            return
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch_check = self._run_git(repo_root, "rev-parse", "--verify", branch_name, check=False)
        if branch_check.returncode == 0:
            self._run_git(repo_root, "worktree", "add", worktree_path, branch_name)
            return
        self._run_git(repo_root, "worktree", "add", "-b", branch_name, worktree_path, base_ref)

    def ensure_remote_unchanged(self, repo_root: str, previous_remote_lines: Iterable[str]) -> None:
        current = tuple(self._remote_lines(repo_root))
        expected = tuple(str(line).strip() for line in previous_remote_lines if str(line).strip())
        if current != expected:
            raise WorktreeGitError("检测到 Git 远程配置发生变化，已拒绝继续 worktree 操作")

    def create_units(
        self,
        *,
        root_path: str,
        count: int,
        base_branch: Optional[str] = None,
    ) -> tuple[GitRepoState, list[WorktreeUnit]]:
        repo = self.ensure_local_repo(root_path)
        worktree_parent = Path(self.build_worktree_parent(repo.repo_root))
        units: list[WorktreeUnit] = []
        base_ref = base_branch or repo.base_branch or "HEAD"
        for index in range(1, count + 1):
            unit_id = self.build_unit_id(index)
            branch_name = self.build_branch_name(index)
            worktree_path = str((worktree_parent / unit_id).resolve())
            self.create_worktree(repo.repo_root, branch_name, worktree_path, base_ref)
            units.append(
                WorktreeUnit(
                    unit_id=unit_id,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                    status="ready",
                    summary="worktree 已准备完成",
                )
            )
        self.ensure_remote_unchanged(repo.repo_root, repo.remote_lines)
        return repo, units

    # ------------------------------------------------------------------
    # Merge / cleanup
    # ------------------------------------------------------------------

    def _assert_clean_worktree(self, repo_root: str) -> None:
        """Raise if the repo has uncommitted changes (staged or unstaged)."""
        status = self._run_git(repo_root, "status", "--porcelain", check=False)
        if (status.stdout or "").strip():
            raise WorktreeGitError("仓库工作区有未提交的修改，请先提交或 stash 后再操作")

    def merge_branch(
        self,
        repo_root: str,
        branch_name: str,
        base_branch: str,
    ) -> tuple[bool, list[str]]:
        """Merge *branch_name* into *base_branch* using ``--no-ff``.

        Returns ``(success, conflict_files)``.  On conflict the merge is
        aborted so the repo stays clean.
        """
        self._assert_clean_worktree(repo_root)
        # Ensure we are on base_branch
        self._run_git(repo_root, "checkout", base_branch)
        result = self._run_git(repo_root, "merge", "--no-ff", "--", branch_name, check=False)
        if result.returncode == 0:
            return True, []

        # Conflict – collect conflicting files then abort
        diff_result = self._run_git(repo_root, "diff", "--name-only", "--diff-filter=U", check=False)
        conflict_files = [
            line.strip()
            for line in (diff_result.stdout or "").splitlines()
            if line.strip()
        ]
        self._run_git(repo_root, "merge", "--abort", check=False)
        return False, conflict_files

    def _validate_worktree_path(self, repo_root: str, worktree_path: str) -> None:
        """Ensure *worktree_path* is under the expected worktree parent directory."""
        expected_parent = Path(self.build_worktree_parent(repo_root)).resolve()
        resolved = Path(worktree_path).resolve()
        if not str(resolved).startswith(str(expected_parent)):
            raise WorktreeGitError(f"worktree 路径 {worktree_path} 不在预期目录 {expected_parent} 下，拒绝操作")

    def remove_worktree(self, repo_root: str, worktree_path: str) -> None:
        """Remove a worktree directory (force) and prune stale entries."""
        self._validate_worktree_path(repo_root, worktree_path)
        self._run_git(repo_root, "worktree", "remove", "--force", worktree_path, check=False)
        self._run_git(repo_root, "worktree", "prune", check=False)
        # Best-effort cleanup of leftover directory
        wt = Path(worktree_path)
        if wt.exists():
            import shutil as _shutil

            _shutil.rmtree(wt, ignore_errors=True)

    def remove_branch(self, repo_root: str, branch_name: str) -> None:
        """Force-delete a local branch."""
        self._run_git(repo_root, "branch", "-D", branch_name, check=False)
