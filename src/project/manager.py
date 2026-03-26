import fcntl
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from ..card.shared import THEMES
from ..config import get_settings
from .context import ProjectContext, ProjectStatus

logger = logging.getLogger(__name__)


class ProjectManager:
    def __init__(self, storage_path: Optional[str] = None):
        self._projects: dict[str, ProjectContext] = {}
        self._active_project: dict[str, str] = {}
        self._lock = threading.Lock()
        self._color_index = 0

        if storage_path:
            self._storage_path = Path(storage_path)
        else:
            self._storage_path = Path.home() / ".ghostap" / "projects.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

        self._load_projects()

    @contextmanager
    def _file_lock(self, exclusive: bool):
        lock_path = Path(f"{self._storage_path}.lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _write_atomic(self, payload: dict):
        tmp_path = Path(f"{self._storage_path}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._storage_path)
            try:
                dir_fd = os.open(self._storage_path.parent, os.O_DIRECTORY)
            except OSError:
                return
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _get_next_theme(self) -> tuple[str, str]:
        theme_list = list(THEMES.values())
        theme = theme_list[self._color_index % len(theme_list)]
        self._color_index += 1
        return theme.color, theme.emoji

    def create_project(
        self,
        project_id: str,
        project_name: str,
        root_path: str,
        chat_id: Optional[str] = None,
    ) -> tuple[bool, str, Optional[ProjectContext]]:
        with self._lock:
            if project_id in self._projects:
                return False, f"项目 {project_id} 已存在", None

            expanded_path = os.path.expanduser(root_path)
            if not os.path.isdir(expanded_path):
                try:
                    os.makedirs(expanded_path, exist_ok=True)
                except Exception as e:
                    return False, f"无法创建目录 {expanded_path}: {e}", None

            theme_color, emoji_prefix = self._get_next_theme()
            settings = get_settings()
            yolo_enabled = bool(getattr(settings, "ttadk_yolo_default_enabled", False))

            ctx = ProjectContext(
                project_id=project_id,
                project_name=project_name,
                root_path=expanded_path,
                working_dir=expanded_path,
                status=ProjectStatus.ACTIVE,
                theme_color=theme_color,
                emoji_prefix=emoji_prefix,
                ttadk_yolo_enabled=yolo_enabled,
            )

            self._projects[project_id] = ctx

            if chat_id:
                self._active_project[chat_id] = project_id

            self._save_projects()
            return True, f"项目 {project_name} 创建成功", ctx

    def get_project(self, project_id: str) -> Optional[ProjectContext]:
        return self._projects.get(project_id)

    def get_all_projects(self, sort_by_recent: bool = True) -> list[ProjectContext]:
        projects = list(self._projects.values())
        if sort_by_recent:
            projects.sort(key=lambda p: p.last_active, reverse=True)
        return projects

    def find_project_by_path(self, path: str) -> Optional[ProjectContext]:
        expanded = os.path.expanduser(os.path.abspath(path))
        for ctx in self._projects.values():
            if ctx.root_path == expanded:
                return ctx
        return None

    def get_or_create_project_for_path(
        self,
        path: str,
        chat_id: Optional[str] = None,
    ) -> tuple[ProjectContext, bool]:
        expanded = os.path.expanduser(os.path.abspath(path))

        existing = self.find_project_by_path(expanded)
        if existing:
            if chat_id:
                self.set_active_project(chat_id, existing.project_id)
            existing.touch()
            self._save_projects()
            return existing, False

        basename = os.path.basename(expanded.rstrip(os.sep))
        if not basename:
            basename = "root"

        project_id = basename.lower().replace(" ", "_").replace("-", "_")
        original_id = project_id
        counter = 1
        while project_id in self._projects:
            project_id = f"{original_id}_{counter}"
            counter += 1

        success, msg, ctx = self.create_project(
            project_id=project_id,
            project_name=basename,
            root_path=expanded,
            chat_id=chat_id,
        )

        if success and ctx:
            return ctx, True
        else:
            raise RuntimeError(f"创建项目失败: {msg}")

    def search_projects(self, query: str) -> list[ProjectContext]:
        query_lower = query.lower()
        results = []
        for ctx in self._projects.values():
            if (
                query_lower in ctx.project_name.lower()
                or query_lower in ctx.project_id.lower()
                or query_lower in ctx.root_path.lower()
            ):
                results.append(ctx)
        results.sort(key=lambda p: p.last_active, reverse=True)
        return results

    def validate_project_path(self, project_id: str) -> tuple[bool, str]:
        ctx = self._projects.get(project_id)
        if not ctx:
            return False, f"项目 {project_id} 不存在"

        if not os.path.isdir(ctx.root_path):
            return False, f"项目路径不存在: {ctx.root_path}"

        return True, ctx.root_path

    def get_active_project(self, chat_id: str) -> Optional[ProjectContext]:
        project_id = self._active_project.get(chat_id)
        if project_id:
            return self._projects.get(project_id)
        return None

    def set_active_project(self, chat_id: str, project_id: str) -> tuple[bool, str]:
        with self._lock:
            if project_id not in self._projects:
                return False, f"项目 {project_id} 不存在"

            old_project_id = self._active_project.get(chat_id)
            if old_project_id and old_project_id in self._projects:
                old_ctx = self._projects[old_project_id]
                if old_ctx.status == ProjectStatus.ACTIVE:
                    old_ctx.status = ProjectStatus.IDLE

            self._active_project[chat_id] = project_id
            ctx = self._projects[project_id]
            ctx.status = ProjectStatus.ACTIVE
            ctx.touch()

            self._save_projects()
            return True, f"已切换到项目 {ctx.project_name}"

    def close_project(self, project_id: str) -> tuple[bool, str]:
        with self._lock:
            if project_id not in self._projects:
                return False, f"项目 {project_id} 不存在"

            ctx = self._projects[project_id]
            ctx.status = ProjectStatus.CLOSED

            for chat_id, active_id in list(self._active_project.items()):
                if active_id == project_id:
                    del self._active_project[chat_id]

            del self._projects[project_id]
            self._save_projects()
            return True, f"项目 {ctx.project_name} 已关闭"

    def update_working_dir(self, project_id: str, new_dir: str) -> tuple[bool, str]:
        with self._lock:
            ctx = self._projects.get(project_id)
            if not ctx:
                return False, f"项目 {project_id} 不存在"

            expanded = os.path.expanduser(new_dir)
            if not os.path.isabs(expanded):
                expanded = os.path.normpath(os.path.join(ctx.working_dir, expanded))

            if not os.path.isdir(expanded):
                return False, f"目录不存在: {expanded}"

            ctx.working_dir = expanded
            ctx.touch()
            self._save_projects()
            return True, expanded

    def find_project_by_name(self, name: str) -> Optional[ProjectContext]:
        name_lower = name.lower()
        for ctx in self._projects.values():
            if ctx.project_name.lower() == name_lower or ctx.project_id.lower() == name_lower:
                return ctx
        for ctx in self._projects.values():
            if name_lower in ctx.project_name.lower() or name_lower in ctx.project_id.lower():
                return ctx
        return None

    def _save_projects(self):
        try:
            data = {
                "projects": {pid: ctx.to_snapshot() for pid, ctx in self._projects.items()},
                "active_project": self._active_project,
                "color_index": self._color_index,
            }
            with self._file_lock(True):
                self._write_atomic(data)
        except Exception as e:
            logger.error("保存项目数据失败: %s", e)

    def _load_projects(self):
        if not self._storage_path.exists():
            return

        try:
            with self._file_lock(True):
                with open(self._storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

            for pid, snap in data.get("projects", {}).items():
                try:
                    ctx = ProjectContext.from_snapshot(snap)
                    if ctx.status == ProjectStatus.CLOSED:
                        continue
                    ctx.status = ProjectStatus.IDLE
                    self._projects[pid] = ctx
                except Exception as e:
                    logger.error("加载项目 %s 失败: %s", pid, e)

            self._active_project = data.get("active_project", {})
            self._color_index = data.get("color_index", 0)
        except Exception as e:
            corrupt_path = Path(f"{self._storage_path}.corrupt.{int(time.time())}")
            try:
                if self._storage_path.exists():
                    os.replace(self._storage_path, corrupt_path)
                    logger.error("加载项目数据失败，已备份损坏文件到: %s", corrupt_path)
            except Exception:
                logger.error("加载项目数据失败: %s", e)
