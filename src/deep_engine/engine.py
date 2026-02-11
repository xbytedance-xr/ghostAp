"""ACP-driven Deep Engine — leverages agent's own planning capabilities.

Instead of parsing requirements → planning tasks → executing one-by-one,
the new Deep Engine sends a single comprehensive prompt to the agent and
monitors its plan/tool-call/text progress via ACP events.
"""

import logging
import threading
import time
import json
import os
from typing import Optional, Callable
from dataclasses import dataclass

from ..acp import ACPSessionManager, SyncACPSession, start_session_with_retry, ACPEvent, ACPEventType, ACPEventRenderer
from ..config import get_settings
from .models import (
    DeepProject,
    DeepProjectStatus,
    EngineRunState,
    ProgressUpdate,
)
from .progress import DeepProgress

logger = logging.getLogger(__name__)


@dataclass
class DeepEngineCallbacks:
    """Callbacks for deep engine lifecycle events."""
    on_planning_start: Optional[Callable[[str], None]] = None
    on_planning_done: Optional[Callable[[DeepProject], None]] = None
    on_event: Optional[Callable[[ACPEvent], None]] = None
    on_text: Optional[Callable[[str], None]] = None
    on_project_done: Optional[Callable[[DeepProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None


class DeepEngine:
    """ACP-driven Deep Engine — the agent plans and executes autonomously."""

    def __init__(self, chat_id: str, root_path: str, agent_type: str = "coco", engine_name: str = "Coco"):
        self.chat_id = chat_id
        self.root_path = os.path.expanduser(root_path)
        self.settings = get_settings()
        self.engine_name = engine_name
        self._agent_type = agent_type

        self._session: Optional[SyncACPSession] = None
        self._project: Optional[DeepProject] = None
        self._progress = DeepProgress()
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._pending_context: Optional[str] = None
        self._context_lock = threading.Lock()

    @property
    def project(self) -> Optional[DeepProject]:
        return self._project

    @property
    def run_state(self) -> EngineRunState:
        return self._run_state

    @property
    def is_running(self) -> bool:
        return self._run_state != EngineRunState.IDLE

    @property
    def progress(self) -> DeepProgress:
        return self._progress

    def plan_and_execute(
        self,
        requirement_text: str,
        callbacks: Optional[DeepEngineCallbacks] = None,
    ) -> DeepProject:
        """Single ACP prompt drives the entire deep execution."""
        callbacks = callbacks or DeepEngineCallbacks()
        self._run_state = EngineRunState.RUNNING

        project_name = os.path.basename(self.root_path) or "deep_project"
        self._project = DeepProject.create(name=project_name, root_path=self.root_path)
        self._project.status = DeepProjectStatus.PLANNING

        if callbacks.on_planning_start:
            callbacks.on_planning_start(requirement_text)

        logger.info("[Deep:%s] ACP执行开始, 需求长度=%d, 路径=%s, agent=%s",
                     project_name, len(requirement_text), self.root_path, self._agent_type)

        try:
            # Create ACP session (with retry and progressive timeout)
            self._session = start_session_with_retry(
                agent_type=self._agent_type, cwd=self.root_path,
                startup_timeout=self.settings.acp_startup_timeout,
            )

            # Build deep prompt — let agent plan and execute autonomously
            prompt = self._build_deep_prompt(requirement_text)

            self._project.status = DeepProjectStatus.EXECUTING
            self._project.start()

            if callbacks.on_planning_done:
                callbacks.on_planning_done(self._project)

            # Track progress via ACP events
            def on_event(event: ACPEvent):
                if self._run_state == EngineRunState.STOPPING:
                    if self._session:
                        self._session.cancel()
                    return

                self._renderer.process_event(event)

                match event.event_type:
                    case ACPEventType.PLAN_UPDATE:
                        if event.plan:
                            self._progress.update_plan(event.plan)
                    case ACPEventType.TOOL_CALL_DONE:
                        if event.tool_call:
                            self._progress.record_tool(event.tool_call)
                    case ACPEventType.TEXT_CHUNK:
                        if event.text:
                            self._progress.append_text(event.text)

                if callbacks.on_event:
                    callbacks.on_event(event)
                if event.event_type == ACPEventType.TEXT_CHUNK and callbacks.on_text:
                    callbacks.on_text(event.text or "")

            timeout = self.settings.coco_execution_timeout if self._agent_type == "coco" else self.settings.claude_execution_timeout
            result = self._session.send_prompt(prompt, on_event=on_event, timeout=timeout)

            # Process pending context injections as follow-up prompts
            result = self._drain_pending_context(on_event, timeout, result)

            # Determine final status
            if self._run_state == EngineRunState.STOPPING:
                self._project.pause()
                logger.info("[Deep:%s] 执行已暂停", project_name)
            elif result.stop_reason in ("end_turn", "max_turn_requests"):
                self._project.complete()
                logger.info("[Deep:%s] 执行完成, 工具调用=%d, 修改文件=%d, 总耗时=%.1fs",
                             project_name, len(self._progress.tool_calls),
                             len(self._progress.modified_files),
                             self._project.duration() or 0)
            elif result.stop_reason == "cancelled":
                self._project.pause()
            else:
                self._project.fail(f"意外停止: {result.stop_reason}")

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

            return self._project

        except Exception as e:
            error_msg = f"执行异常: {str(e)}"
            logger.error("[Deep:%s] %s", project_name, error_msg)
            if self._project:
                self._project.fail(error_msg)
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        finally:
            self._run_state = EngineRunState.IDLE

    def _drain_pending_context(self, on_event, timeout, last_result):
        """Send any pending context injections as follow-up prompts in the same session."""
        while self._run_state == EngineRunState.RUNNING:
            with self._context_lock:
                ctx = self._pending_context
                self._pending_context = None
            if not ctx:
                break
            logger.info("[Deep] 发送注入的上下文: %s...", ctx[:100])
            follow_up = f"""用户提供了额外的上下文/指导信息，请据此继续执行：

{ctx}

请根据以上信息调整你的执行方案并继续。"""
            last_result = self._session.send_prompt(follow_up, on_event=on_event, timeout=timeout)
        return last_result

    def _build_deep_prompt(self, requirement: str) -> str:
        """Build the deep prompt — let agent autonomously plan and execute."""
        return f"""你是一个专业的软件工程师。请完成以下需求：

## 需求
{requirement}

## 工作目录
{self.root_path}

## 要求
1. 先分析需求，制定执行计划
2. 按计划逐步实现，每步完成后验证
3. 确保代码质量和测试覆盖
4. 完成后输出总结报告
"""

    def inject_context(self, message: str):
        """Inject user context — will be sent as follow-up prompt after current execution."""
        with self._context_lock:
            self._pending_context = message
        logger.info("[Deep] 上下文已注入(待发送): %s...", message[:100])

    def stop(self):
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def pause(self):
        if self._project:
            self._project.pause()
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def resume(self, callbacks: Optional[DeepEngineCallbacks] = None) -> Optional[DeepProject]:
        """Resume a paused deep execution by loading the ACP session and sending a continuation prompt."""
        if not self._project or self._project.status != DeepProjectStatus.PAUSED:
            return self._project

        callbacks = callbacks or DeepEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._project.status = DeepProjectStatus.EXECUTING

        try:
            # Start a new ACP session with retry
            self._session = start_session_with_retry(
                agent_type=self._agent_type, cwd=self.root_path,
                startup_timeout=self.settings.acp_startup_timeout,
            )

            resume_prompt = """你之前的执行被暂停了。请继续完成剩余的任务。
检查之前的进度，对未完成的部分继续实现。
完成后输出总结报告。"""

            def on_event(event: ACPEvent):
                if self._run_state == EngineRunState.STOPPING:
                    if self._session:
                        self._session.cancel()
                    return
                self._renderer.process_event(event)
                match event.event_type:
                    case ACPEventType.PLAN_UPDATE:
                        if event.plan:
                            self._progress.update_plan(event.plan)
                    case ACPEventType.TOOL_CALL_DONE:
                        if event.tool_call:
                            self._progress.record_tool(event.tool_call)
                    case ACPEventType.TEXT_CHUNK:
                        if event.text:
                            self._progress.append_text(event.text)
                if callbacks.on_event:
                    callbacks.on_event(event)

            timeout = self.settings.coco_execution_timeout if self._agent_type == "coco" else self.settings.claude_execution_timeout
            result = self._session.send_prompt(resume_prompt, on_event=on_event, timeout=timeout)
            result = self._drain_pending_context(on_event, timeout, result)

            if self._run_state == EngineRunState.STOPPING:
                self._project.pause()
            elif result.stop_reason in ("end_turn", "max_turn_requests"):
                self._project.complete()
            else:
                self._project.fail(f"意外停止: {result.stop_reason}")

            if callbacks.on_project_done:
                callbacks.on_project_done(self._project)

        except Exception as e:
            error_msg = f"恢复执行异常: {str(e)}"
            logger.error("[Deep:%s] %s", self._project.name, error_msg)
            self._project.fail(error_msg)
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        finally:
            self._run_state = EngineRunState.IDLE

        return self._project

    def get_progress(self) -> Optional[ProgressUpdate]:
        if not self._project:
            return None

        status_messages = {
            DeepProjectStatus.IDLE: "等待开始",
            DeepProjectStatus.PLANNING: "正在规划任务...",
            DeepProjectStatus.EXECUTING: f"执行中 (工具调用: {len(self._progress.tool_calls)})",
            DeepProjectStatus.PAUSED: "已暂停",
            DeepProjectStatus.COMPLETED: "全部完成",
            DeepProjectStatus.FAILED: f"执行失败: {self._project.error or '未知错误'}",
        }

        return ProgressUpdate(
            project_id=self._project.project_id,
            current_task=None,
            completed_count=self._progress.completed_steps,
            total_count=self._progress.total_steps or 1,
            status=self._project.status,
            message=status_messages.get(self._project.status, "未知状态"),
        )

    def get_task_summary(self) -> str:
        if not self._project:
            return "暂无任务"

        lines = [f"📊 **{self._project.name}** 执行进度\n"]

        if self._progress.plan_entries:
            lines.append(self._progress.progress_bar)
            lines.append("")
            for entry in self._progress.plan_entries:
                icon = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(entry["status"], "⬜")
                lines.append(f"{icon} {entry['content']}")
        else:
            lines.append(f"🔧 工具调用: {len(self._progress.tool_calls)} 次")

        if self._progress.modified_files:
            lines.append(f"\n📝 修改文件: {len(self._progress.modified_files)} 个")

        if self._project.duration():
            lines.append(f"\n⏱️ 总耗时: {self._project.duration():.1f}s")

        return "\n".join(lines)

    def get_rendered_content(self) -> str:
        """Return the current rendered output from the ACP event renderer."""
        return self._renderer.get_final_content()

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

        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)

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
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("关闭ACP session失败: %s", e)
            self._session = None
        self._project = None
        self._run_state = EngineRunState.IDLE


class DeepEngineManager:
    """Manages DeepEngine instances per chat+project."""

    def __init__(self):
        self._engines: dict[str, DeepEngine] = {}

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> DeepEngine:
        key = f"{chat_id}:{root_path}"
        agent_type = "claude" if engine_name.lower().startswith("claude") else "coco"

        if key not in self._engines:
            self._engines[key] = DeepEngine(
                chat_id=chat_id,
                root_path=root_path,
                agent_type=agent_type,
                engine_name=engine_name,
            )
        else:
            existing = self._engines[key]
            if existing.engine_name.lower() != engine_name.lower() and not existing.is_running:
                existing.cleanup()
                self._engines[key] = DeepEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=agent_type,
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

    def get_active_engines(self, chat_id: str) -> list[DeepEngine]:
        return [e for k, e in self._engines.items()
                if k.startswith(f"{chat_id}:") and e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[DeepEngine]:
        if chat_id is None:
            return list(self._engines.values())
        prefix = f"{chat_id}:"
        return [e for k, e in self._engines.items() if k.startswith(prefix)]

    def find_by_deep_project_id(self, chat_id: str, deep_project_id: str) -> Optional[DeepEngine]:
        if not deep_project_id:
            return None
        for engine in self.get_active_engines(chat_id) + self.list_engines(chat_id):
            try:
                if engine.project and engine.project.project_id == deep_project_id:
                    return engine
            except Exception:
                continue
        return None

    def cleanup_all(self):
        for engine in self._engines.values():
            engine.cleanup()
        self._engines.clear()
