import time
import json
import os
from typing import Optional, Callable, AsyncGenerator
from dataclasses import dataclass
from ..coco.session import CocoSession, CocoSessionManager
from ..config import get_settings
from .models import (
    DeepProject,
    DeepProjectStatus,
    DeepTask,
    DeepTaskStatus,
    ParsedRequirement,
    ExecutionResult,
    ProgressUpdate,
)
from .parser import RequirementParser
from .planner import TaskPlanner
from .executor import TaskExecutor


@dataclass
class DeepEngineCallbacks:
    on_planning_start: Optional[Callable[[str], None]] = None
    on_planning_done: Optional[Callable[[DeepProject], None]] = None
    on_task_start: Optional[Callable[[DeepTask, int, int], None]] = None
    on_task_progress: Optional[Callable[[DeepTask, str], None]] = None
    on_task_done: Optional[Callable[[DeepTask, ExecutionResult], None]] = None
    on_project_done: Optional[Callable[[DeepProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class DeepEngine:
    def __init__(self, chat_id: str, root_path: str, session_manager: Optional[CocoSessionManager] = None):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()

        self._session_manager = session_manager or CocoSessionManager()
        self._parser = RequirementParser()
        self._planner = TaskPlanner()

        self._project: Optional[DeepProject] = None
        self._executor: Optional[TaskExecutor] = None
        self._coco_session: Optional[CocoSession] = None
        self._is_running = False
        self._should_stop = False

    @property
    def project(self) -> Optional[DeepProject]:
        return self._project

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _ensure_coco_session(self) -> CocoSession:
        if self._coco_session is None:
            session_id = f"deep_{self.chat_id}_{int(time.time())}"
            self._coco_session = self._session_manager.start_session(
                chat_id=self.chat_id,
                session_id=session_id
            )
        return self._coco_session

    def _ensure_executor(self) -> TaskExecutor:
        if self._executor is None:
            coco_session = self._ensure_coco_session()
            self._executor = TaskExecutor(coco_session, self.root_path)
        return self._executor

    def plan(self, requirement_text: str, callbacks: Optional[DeepEngineCallbacks] = None) -> DeepProject:
        callbacks = callbacks or DeepEngineCallbacks()

        if callbacks.on_planning_start:
            callbacks.on_planning_start(requirement_text)

        project_name = os.path.basename(self.root_path) or "deep_project"
        self._project = DeepProject.create(name=project_name, root_path=self.root_path)
        self._project.status = DeepProjectStatus.PLANNING

        try:
            print(f"📋 开始解析需求...")
            requirement = self._parser.parse(requirement_text)
            self._project.set_requirement(requirement)

            print(f"📝 开始规划任务...")
            tasks = self._planner.plan(requirement)
            self._project.set_tasks(tasks)

            self._project.status = DeepProjectStatus.IDLE

            if callbacks.on_planning_done:
                callbacks.on_planning_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"规划失败: {str(e)}"
            print(f"❌ {error_msg}")
            self._project.fail(error_msg)

            if callbacks.on_error:
                callbacks.on_error(error_msg)

            return self._project

    def execute(self, callbacks: Optional[DeepEngineCallbacks] = None) -> DeepProject:
        if not self._project:
            raise ValueError("请先调用 plan() 方法进行任务规划")

        callbacks = callbacks or DeepEngineCallbacks()
        self._is_running = True
        self._should_stop = False
        self._project.start()

        executor = self._ensure_executor()
        total_tasks = self._project.total_count

        try:
            while not self._should_stop:
                task = self._project.get_next_task()
                if not task:
                    break

                current_index = self._project.completed_count + 1

                if callbacks.on_task_start:
                    callbacks.on_task_start(task, current_index, total_tasks)

                def on_chunk(content: str):
                    if callbacks.on_task_progress:
                        callbacks.on_task_progress(task, content)

                result = executor.execute(task, on_chunk=on_chunk)

                if callbacks.on_task_done:
                    callbacks.on_task_done(task, result)

                if not result.success and task.status == DeepTaskStatus.FAILED:
                    print(f"❌ 任务 {task.title} 失败，跳过后续依赖任务")
                    self._skip_dependent_tasks(task)

            if self._project.is_completed:
                if self._project.has_failures:
                    self._project.status = DeepProjectStatus.FAILED
                else:
                    self._project.complete()
            elif self._should_stop:
                self._project.pause()

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"执行异常: {str(e)}"
            print(f"❌ {error_msg}")
            self._project.fail(error_msg)

            if callbacks.on_error:
                callbacks.on_error(error_msg)

            return self._project

        finally:
            self._is_running = False

    def plan_and_execute(
        self,
        requirement_text: str,
        callbacks: Optional[DeepEngineCallbacks] = None
    ) -> DeepProject:
        self.plan(requirement_text, callbacks)

        if self._project and self._project.status != DeepProjectStatus.FAILED:
            return self.execute(callbacks)

        return self._project

    def stop(self):
        self._should_stop = True

    def pause(self):
        if self._project:
            self._project.pause()
        self._should_stop = True

    def resume(self, callbacks: Optional[DeepEngineCallbacks] = None) -> Optional[DeepProject]:
        if not self._project:
            return None

        if self._project.status == DeepProjectStatus.PAUSED:
            self._project.resume()
            return self.execute(callbacks)

        return self._project

    def _skip_dependent_tasks(self, failed_task: DeepTask):
        for task in self._project.tasks:
            if failed_task.task_id in task.dependencies:
                if task.status == DeepTaskStatus.PENDING:
                    task.skip(f"依赖任务 {failed_task.title} 失败")
                    self._skip_dependent_tasks(task)

    def get_progress(self) -> Optional[ProgressUpdate]:
        if not self._project:
            return None

        current_task = self._project.get_current_task()
        status_messages = {
            DeepProjectStatus.IDLE: "等待开始",
            DeepProjectStatus.PLANNING: "正在规划任务...",
            DeepProjectStatus.EXECUTING: f"正在执行: {current_task.title}" if current_task else "执行中",
            DeepProjectStatus.PAUSED: "已暂停",
            DeepProjectStatus.COMPLETED: "全部完成",
            DeepProjectStatus.FAILED: f"执行失败: {self._project.error or '未知错误'}",
        }

        return self._project.get_progress_update(
            message=status_messages.get(self._project.status, "未知状态")
        )

    def get_task_summary(self) -> str:
        if not self._project:
            return "暂无任务"

        lines = [f"📊 **{self._project.name}** 任务进度\n"]

        progress = self.get_progress()
        if progress:
            lines.append(f"{progress.progress_bar}\n")

        for task in self._project.tasks:
            status_emoji = {
                DeepTaskStatus.PENDING: "⏳",
                DeepTaskStatus.READY: "🔜",
                DeepTaskStatus.IN_PROGRESS: "🔄",
                DeepTaskStatus.COMPLETED: "✅",
                DeepTaskStatus.FAILED: "❌",
                DeepTaskStatus.SKIPPED: "⏭️",
                DeepTaskStatus.BLOCKED: "🚫",
            }.get(task.status, "❓")

            duration_str = ""
            if task.duration():
                duration_str = f" ({task.duration():.1f}s)"

            lines.append(f"{status_emoji} {task.order + 1}. {task.title}{duration_str}")

        if self._project.duration():
            lines.append(f"\n⏱️ 总耗时: {self._project.duration():.1f}s")

        return "\n".join(lines)

    def save_state(self, filepath: Optional[str] = None) -> str:
        if not self._project:
            raise ValueError("没有项目状态可保存")

        if not filepath:
            filepath = os.path.join(self.root_path, ".deep_engine_state.json")

        state = {
            "chat_id": self.chat_id,
            "root_path": self.root_path,
            "project": self._project.to_dict(),
            "saved_at": time.time(),
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

        return filepath

    def load_state(self, filepath: Optional[str] = None) -> bool:
        if not filepath:
            filepath = os.path.join(self.root_path, ".deep_engine_state.json")

        if not os.path.exists(filepath):
            return False

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                state = json.load(f)

            self._project = DeepProject.from_dict(state["project"])
            return True

        except Exception as e:
            print(f"加载状态失败: {e}")
            return False

    def cleanup(self):
        if self._coco_session:
            self._session_manager.end_session(self.chat_id)
            self._coco_session = None
        self._executor = None
        self._project = None
        self._is_running = False


class DeepEngineManager:
    def __init__(self):
        self._engines: dict[str, DeepEngine] = {}
        self._session_manager = CocoSessionManager()

    def get_or_create(self, chat_id: str, root_path: str) -> DeepEngine:
        key = f"{chat_id}:{root_path}"
        if key not in self._engines:
            self._engines[key] = DeepEngine(
                chat_id=chat_id,
                root_path=root_path,
                session_manager=self._session_manager
            )
        return self._engines[key]

    def get(self, chat_id: str, root_path: str) -> Optional[DeepEngine]:
        key = f"{chat_id}:{root_path}"
        return self._engines.get(key)

    def remove(self, chat_id: str, root_path: str):
        key = f"{chat_id}:{root_path}"
        if key in self._engines:
            self._engines[key].cleanup()
            del self._engines[key]

    def get_active_engine(self, chat_id: str) -> Optional[DeepEngine]:
        for key, engine in self._engines.items():
            if key.startswith(f"{chat_id}:") and engine.is_running:
                return engine
        return None

    def cleanup_all(self):
        for engine in self._engines.values():
            engine.cleanup()
        self._engines.clear()
