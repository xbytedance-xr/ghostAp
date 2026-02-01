import logging
import time
import json
import os
import uuid
from typing import Optional, Callable, AsyncGenerator, Union
from dataclasses import dataclass
from ..coco.session import CocoSession, CocoSessionManager
from ..claude.session import ClaudeSession, ClaudeSessionManager
from ..config import get_settings

logger = logging.getLogger(__name__)
from .models import (
    DeepProject,
    DeepProjectStatus,
    DeepTask,
    DeepTaskStatus,
    ParsedRequirement,
    ExecutionResult,
    ExecutionContext,
    ProgressUpdate,
)
from .parser import RequirementParser
from .planner import TaskPlanner
from .executor import TaskExecutor, AISession

# Session manager 的 Union 类型
AISessionManager = Union[CocoSessionManager, ClaudeSessionManager]


@dataclass
class DeepEngineCallbacks:
    on_planning_start: Optional[Callable[[str], None]] = None
    on_planning_done: Optional[Callable[[DeepProject], None]] = None
    on_task_start: Optional[Callable[[DeepTask, int, int], None]] = None
    on_task_progress: Optional[Callable[[DeepTask, str], None]] = None
    on_task_done: Optional[Callable[[DeepTask, ExecutionResult], None]] = None
    on_context_adapted: Optional[Callable[[DeepTask, str, str], None]] = None  # (task, reason, prompt_preview)
    on_project_done: Optional[Callable[[DeepProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class DeepEngine:
    def __init__(self, chat_id: str, root_path: str, session_manager: Optional[AISessionManager] = None, engine_name: str = "Coco"):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name

        self._session_manager = session_manager or CocoSessionManager()
        self._parser = RequirementParser()
        self._planner = TaskPlanner()

        self._project: Optional[DeepProject] = None
        self._executor: Optional[TaskExecutor] = None
        self._ai_session: Optional[AISession] = None
        self._execution_context = ExecutionContext()
        self._is_running = False
        self._should_stop = False

    @property
    def project(self) -> Optional[DeepProject]:
        return self._project

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def execution_context(self) -> ExecutionContext:
        return self._execution_context

    def inject_context(self, message: str):
        """线程安全地注入用户上下文，供 ws_client 调用。"""
        self._execution_context.inject_user_context(message)
        logger.info("上下文已注入: %s...", message[:100])

    def _ensure_ai_session(self) -> AISession:
        if self._ai_session is None:
            # Claude CLI 要求 session_id 必须为合法 UUID；Coco 无此限制
            # 统一使用 UUID 格式，兼容两种后端
            session_id = str(uuid.uuid4())
            self._ai_session = self._session_manager.start_session(
                chat_id=self.chat_id,
                session_id=session_id
            )
        return self._ai_session

    def _ensure_executor(self) -> TaskExecutor:
        if self._executor is None:
            ai_session = self._ensure_ai_session()
            self._executor = TaskExecutor(ai_session, self.root_path)
        return self._executor

    def plan(self, requirement_text: str, callbacks: Optional[DeepEngineCallbacks] = None) -> DeepProject:
        callbacks = callbacks or DeepEngineCallbacks()

        if callbacks.on_planning_start:
            callbacks.on_planning_start(requirement_text)

        project_name = os.path.basename(self.root_path) or "deep_project"
        self._project = DeepProject.create(name=project_name, root_path=self.root_path)
        self._project.status = DeepProjectStatus.PLANNING

        try:
            logger.info("开始解析需求...")
            requirement = self._parser.parse(requirement_text)
            self._project.set_requirement(requirement)

            logger.info("开始规划任务...")
            tasks = self._planner.plan(requirement)
            self._project.set_tasks(tasks)

            self._project.status = DeepProjectStatus.IDLE

            if callbacks.on_planning_done:
                callbacks.on_planning_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"规划失败: {str(e)}"
            logger.error("%s", error_msg)
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

                # ① 上下文感知 prompt 适配（仅当有新上下文时触发 LLM）
                if self._execution_context.has_meaningful_context():
                    try:
                        context_prompt = self._execution_context.build_context_prompt()
                        was_adapted, adapted_prompt, reason = self._planner.adapt_task_prompt(task, context_prompt)
                        self._execution_context.consume_new_context_flag()
                        if was_adapted:
                            task.original_prompt = task.prompt
                            task.prompt = adapted_prompt
                            task.adapted_prompt = adapted_prompt
                            self._execution_context.record_adaptation(task.task_id, reason)
                            logger.info("任务 %s 指令已适配: %s", task.title, reason)
                            if callbacks.on_context_adapted:
                                preview = adapted_prompt[:200]
                                callbacks.on_context_adapted(task, reason, preview)
                    except Exception as e:
                        logger.error("任务适配异常，使用原始 prompt: %s", e)
                        self._execution_context.consume_new_context_flag()

                if callbacks.on_task_start:
                    callbacks.on_task_start(task, current_index, total_tasks)

                def on_chunk(content: str):
                    if callbacks.on_task_progress:
                        callbacks.on_task_progress(task, content)

                # ② 执行任务
                result = executor.execute(task, on_chunk=on_chunk)

                if callbacks.on_task_done:
                    callbacks.on_task_done(task, result)

                # ③ 记录结果到上下文
                summary = result.output[-200:] if result.output else (result.error or "")
                self._execution_context.add_result(
                    task.task_id, task.title, result.success, summary
                )

                # ④ 智能失败处理（使用 replan 替代盲目重试）
                if not result.success and task.retry_count < task.max_retries:
                    try:
                        context_prompt = self._execution_context.build_context_prompt()
                        replanned = self._planner.replan_task(
                            task, result.error or "未知错误", context_prompt
                        )
                        task.prompt = replanned.prompt
                        task.status = DeepTaskStatus.PENDING
                        logger.info("任务 %s 重规划后重试", task.title)
                        continue
                    except Exception as e:
                        logger.error("任务重规划异常: %s", e)
                        # 重规划失败，保持原有 fail 逻辑
                        if task.status == DeepTaskStatus.FAILED:
                            self._skip_dependent_tasks(task)
                elif not result.success and task.status == DeepTaskStatus.FAILED:
                    logger.warning("任务 %s 失败，跳过后续依赖任务", task.title)
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
            logger.error("%s", error_msg)
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
            logger.error("加载状态失败: %s", e)
            return False

    def cleanup(self):
        if self._ai_session:
            self._session_manager.end_session(self.chat_id)
            self._ai_session = None
        self._executor = None
        self._project = None
        self._is_running = False


class DeepEngineManager:
    def __init__(self):
        self._engines: dict[str, DeepEngine] = {}
        self._coco_session_manager = CocoSessionManager()
        self._claude_session_manager = ClaudeSessionManager()

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> DeepEngine:
        key = f"{chat_id}:{root_path}"
        if key not in self._engines:
            if engine_name.lower().startswith("claude"):
                session_manager = self._claude_session_manager
            else:
                session_manager = self._coco_session_manager
            self._engines[key] = DeepEngine(
                chat_id=chat_id,
                root_path=root_path,
                session_manager=session_manager,
                engine_name=engine_name,
            )
        else:
            # 如果已有 engine 但 engine_name 不同，重建以使用正确的后端
            existing = self._engines[key]
            if existing.engine_name.lower() != engine_name.lower() and not existing.is_running:
                existing.cleanup()
                if engine_name.lower().startswith("claude"):
                    session_manager = self._claude_session_manager
                else:
                    session_manager = self._coco_session_manager
                self._engines[key] = DeepEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    session_manager=session_manager,
                    engine_name=engine_name,
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
