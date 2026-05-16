from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import DeleteWarning, WorktreeInfo, WorktreeUnit

logger = logging.getLogger(__name__)


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

    def build_branch_name(self, index: int, *, session_slug: str = "") -> str:
        if session_slug:
            return f"ghostap/wt/{_slugify(session_slug, default='session')}/{index:02d}-unit"
        return f"ghostap/wt/{index:02d}-unit"

    def build_unit_id(self, index: int, *, session_slug: str = "") -> str:
        if session_slug:
            return f"wt-{_slugify(session_slug, default='session')}-{index:02d}"
        return f"wt-{index:02d}"

    # Forbidden path prefixes for custom_path safety validation
    _FORBIDDEN_PREFIXES = ("/etc", "/usr", "/var", "/sys", "/proc", "/dev", "/boot", "/sbin", "/bin")

    def _validate_custom_path(self, custom_path: str) -> None:
        """Validate a user-supplied custom worktree path for safety."""
        if ".." in Path(custom_path).parts:
            raise WorktreeGitError(f"自定义路径包含 '..'，拒绝操作: {custom_path}")
        # macOS 上 `/etc`、`/var` 等是 `/private/...` 的符号链接，仅比对 abspath 会漏掉系统
        # 目录；同时比对 abspath 与 realpath，并把禁区前缀扩展到其 realpath 形式。
        raw_abs = os.path.abspath(custom_path)
        real_abs = os.path.realpath(custom_path)
        # 系统级 tempdir（如 macOS 上的 `/var/folders/...`、Linux 上的 `/tmp/...`）属用户
        # 可写区，必须放行，避免在 `/var` 禁区下误伤合法 tempdir。
        sys_tmp = os.path.realpath(tempfile.gettempdir())
        for cand in (raw_abs, real_abs):
            if cand == sys_tmp or cand.startswith(sys_tmp + "/"):
                return
        forbidden_markers: set[str] = set()
        for prefix in self._FORBIDDEN_PREFIXES:
            forbidden_markers.add(prefix)
            forbidden_markers.add(os.path.realpath(prefix))
        for marker in forbidden_markers:
            for cand in (raw_abs, real_abs):
                if cand == marker or cand.startswith(marker + "/"):
                    raise WorktreeGitError(f"自定义路径指向系统目录，拒绝操作: {custom_path}")

    def create_worktree(
        self,
        repo_root: str,
        branch_name: str,
        worktree_path: str,
        base_ref: str,
        *,
        custom_path: Optional[str] = None,
        remote_branch: Optional[str] = None,
    ) -> None:
        if custom_path:
            self._validate_custom_path(custom_path)
            worktree_path = str(Path(custom_path).resolve())
        # Fetch remote branch if specified
        if remote_branch:
            # Strip 'origin/' prefix if present to get the bare branch name for fetch
            fetch_ref = remote_branch
            if fetch_ref.startswith("origin/"):
                fetch_ref = fetch_ref[len("origin/"):]
            self._run_git(repo_root, "fetch", "origin", fetch_ref, check=False)
            base_ref = f"origin/{fetch_ref}"
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
        custom_base_dir: Optional[str] = None,
        session_slug: str = "",
    ) -> tuple[GitRepoState, list[WorktreeUnit]]:
        t0 = time.monotonic()
        repo = self.ensure_local_repo(root_path)
        # Single fetch upfront to avoid per-worktree fetch overhead
        if repo.remote_lines:
            self._run_git(repo.repo_root, "fetch", "--all", check=False)
            logger.debug("git fetch --all completed in %.2fs", time.monotonic() - t0)
        if custom_base_dir:
            self._validate_custom_path(custom_base_dir)
            worktree_parent = Path(custom_base_dir).resolve()
        else:
            worktree_parent = Path(self.build_worktree_parent(repo.repo_root))
        units: list[WorktreeUnit] = []
        base_ref = base_branch or repo.base_branch or "HEAD"
        for index in range(1, count + 1):
            unit_id = self.build_unit_id(index, session_slug=session_slug)
            branch_name = self.build_branch_name(index, session_slug=session_slug)
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
        # Lightweight gc after batch creation
        self._run_git(repo.repo_root, "gc", "--auto", check=False)
        elapsed = time.monotonic() - t0
        logger.info("create_units: %d worktrees created in %.2fs", count, elapsed)
        return repo, units

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------

    def check_worktree_safety(
        self,
        repo_root: str,
        worktree_path: str,
        base_branch: Optional[str] = None,
    ) -> DeleteWarning:
        """Check if a worktree has uncommitted changes or unmerged branches."""
        warning = DeleteWarning()

        # Check uncommitted changes
        status_result = self._run_git(worktree_path, "status", "--porcelain", check=False)
        dirty_lines = [
            line.strip()
            for line in (status_result.stdout or "").splitlines()
            if line.strip()
        ]
        if dirty_lines:
            warning.has_uncommitted = True
            warning.uncommitted_files = dirty_lines

        # Check unmerged commits (commits in worktree branch not in base)
        if base_branch:
            branch_result = self._run_git(
                worktree_path, "rev-parse", "--abbrev-ref", "HEAD", check=False
            )
            wt_branch = (branch_result.stdout or "").strip()
            if wt_branch and wt_branch != base_branch:
                log_result = self._run_git(
                    worktree_path,
                    "log",
                    f"{base_branch}..{wt_branch}",
                    "--oneline",
                    check=False,
                )
                unmerged_lines = [
                    line.strip()
                    for line in (log_result.stdout or "").splitlines()
                    if line.strip()
                ]
                if unmerged_lines:
                    warning.has_unmerged = True
                    warning.unmerged_branch = wt_branch

        return warning

    # ------------------------------------------------------------------
    # Merge / cleanup
    # ------------------------------------------------------------------

    def _assert_clean_worktree(self, repo_root: str) -> None:
        """Raise if the repo has uncommitted changes (staged or unstaged)."""
        status = self._run_git(repo_root, "status", "--porcelain", check=False)
        if (status.stdout or "").strip():
            raise WorktreeGitError("仓库工作区有未提交的修改，请先提交或 stash 后再操作")

    def commit_worktree_changes(self, worktree_path: str, message: str) -> bool:
        """Commit dirty worktree changes so the branch can be merged."""
        status = self._run_git(worktree_path, "status", "--porcelain", check=False)
        if not (status.stdout or "").strip():
            return False
        self._run_git(worktree_path, "add", "-A", check=False)
        diff = self._run_git(worktree_path, "diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            return False
        self._run_git(
            worktree_path,
            "-c",
            "user.name=GhostAP",
            "-c",
            "user.email=ghostap@local",
            "commit",
            "-m",
            message,
        )
        return True

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
        result = self._run_git(repo_root, "merge", "--no-ff", "-X", "theirs", "--", branch_name, check=False)
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

    def remove_worktree(
        self,
        repo_root: str,
        worktree_path: str,
        *,
        force: bool = True,
        base_branch: Optional[str] = None,
    ) -> Optional[DeleteWarning]:
        """Remove a worktree directory.

        When *force* is ``False``, checks for uncommitted changes and
        unmerged branches first.  Returns a :class:`DeleteWarning` if
        the worktree is not safe to delete (caller should re-call with
        ``force=True`` to confirm).  Returns ``None`` on success.
        """
        self._validate_worktree_path(repo_root, worktree_path)
        if not force:
            warning = self.check_worktree_safety(repo_root, worktree_path, base_branch)
            if not warning.is_safe:
                return warning
        self._run_git(repo_root, "worktree", "remove", "--force", worktree_path, check=False)
        self._run_git(repo_root, "worktree", "prune", check=False)
        # Best-effort cleanup of leftover directory
        wt = Path(worktree_path)
        if wt.exists():
            import shutil as _shutil

            _shutil.rmtree(wt, ignore_errors=True)
        return None

    def remove_branch(self, repo_root: str, branch_name: str) -> None:
        """Force-delete a local branch."""
        self._run_git(repo_root, "branch", "-D", branch_name, check=False)

    # ------------------------------------------------------------------
    # List / sync / optimize
    # ------------------------------------------------------------------

    def list_worktrees(self, repo_root: str) -> list[WorktreeInfo]:
        """List all worktrees using ``git worktree list --porcelain``."""
        result = self._run_git(repo_root, "worktree", "list", "--porcelain", check=False)
        if result.returncode != 0:
            return []

        # Determine current active worktree
        cwd = str(Path.cwd().resolve())

        entries: list[WorktreeInfo] = []
        current: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                # End of entry block
                if current.get("worktree"):
                    wt_path = current["worktree"]
                    branch = current.get("branch", "").replace("refs/heads/", "")
                    is_active = str(Path(wt_path).resolve()) == cwd
                    # Get last commit time
                    last_updated = ""
                    time_result = self._run_git(
                        wt_path, "log", "-1", "--format=%ci", check=False
                    )
                    if time_result.returncode == 0 and (time_result.stdout or "").strip():
                        last_updated = (time_result.stdout or "").strip()
                    else:
                        # Fallback to directory mtime
                        try:
                            mtime = Path(wt_path).stat().st_mtime
                            import datetime
                            last_updated = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                        except OSError:
                            pass
                    entries.append(WorktreeInfo(
                        path=wt_path,
                        branch=branch,
                        commit=current.get("HEAD", ""),
                        is_active=is_active,
                        last_updated=last_updated,
                    ))
                current = {}
                continue
            if line.startswith("worktree "):
                current["worktree"] = line[len("worktree "):]
            elif line.startswith("HEAD "):
                current["HEAD"] = line[len("HEAD "):]
            elif line.startswith("branch "):
                current["branch"] = line[len("branch "):]
            elif line == "bare":
                current["bare"] = "true"
            elif line == "detached":
                current["detached"] = "true"

        # Handle last entry (porcelain output may not end with blank line)
        if current.get("worktree"):
            wt_path = current["worktree"]
            branch = current.get("branch", "").replace("refs/heads/", "")
            is_active = str(Path(wt_path).resolve()) == cwd
            last_updated = ""
            time_result = self._run_git(wt_path, "log", "-1", "--format=%ci", check=False)
            if time_result.returncode == 0 and (time_result.stdout or "").strip():
                last_updated = (time_result.stdout or "").strip()
            entries.append(WorktreeInfo(
                path=wt_path,
                branch=branch,
                commit=current.get("HEAD", ""),
                is_active=is_active,
                last_updated=last_updated,
            ))

        return entries

    def sync_worktree(
        self,
        repo_root: str,
        worktree_path: str,
        branch: Optional[str] = None,
        *,
        force: bool = False,
    ) -> Optional[DeleteWarning]:
        """Sync a worktree to the latest remote state.

        Executes: fetch → checkout → reset --hard → clean -fd.

        If *force* is ``False`` and the worktree has uncommitted changes,
        returns a :class:`DeleteWarning` instead of proceeding.
        Returns ``None`` on success.
        """
        if not force:
            warning = self.check_worktree_safety(repo_root, worktree_path)
            if warning.has_uncommitted:
                return warning

        # Determine branch if not given
        if not branch:
            branch_result = self._run_git(
                worktree_path, "rev-parse", "--abbrev-ref", "HEAD", check=False
            )
            branch = (branch_result.stdout or "").strip() or "main"

        self._run_git(worktree_path, "fetch", "origin", check=False)
        self._run_git(worktree_path, "checkout", branch, check=False)
        # Reset to remote tracking branch if available, otherwise just the branch
        remote_ref = f"origin/{branch}"
        ref_check = self._run_git(worktree_path, "rev-parse", "--verify", remote_ref, check=False)
        reset_target = remote_ref if ref_check.returncode == 0 else branch
        self._run_git(worktree_path, "reset", "--hard", reset_target)
        self._run_git(worktree_path, "clean", "-fd", check=False)
        return None

    def optimize_storage(self, repo_root: str) -> None:
        """Run aggressive gc + repack to minimize shared object storage."""
        self._run_git(repo_root, "gc", "--aggressive", "--prune=now", check=False)
        self._run_git(repo_root, "repack", "-a", "-d", check=False)
        logger.info("optimize_storage: gc + repack completed for %s", repo_root)
