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

from ..acp import ACPEvent, ACPEventType, ACPEventRenderer
from ..agent_session import SyncSession, create_engine_session
from ..config import get_settings
from .models import (
    DeepProject,
    DeepProjectStatus,
    EngineRunState,
    ProgressUpdate,
)
from .progress import DeepProgress

logger = logging.getLogger(__name__)

_STATUS_ICONS = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}


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

        self._session: Optional[SyncSession] = None
        self._project: Optional[DeepProject] = None
        self._progress = DeepProgress()
        self._renderer = ACPEventRenderer()
        self._run_state = EngineRunState.IDLE
        self._pending_context: list[str] = []
        self._context_lock = threading.Lock()
        self._planning_done_fired: bool = False

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

    def _make_on_event(self, callbacks: DeepEngineCallbacks) -> Callable[[ACPEvent], None]:
        """Create the on_event callback shared by plan_and_execute and resume."""
        def on_event(event: ACPEvent):
            if self._run_state == EngineRunState.STOPPING:
                if self._session:
                    self._session.cancel()
                return

            self._renderer.process_event(event)

            # Transition: planning -> executing
            # Some backends (Claude CLI) only emit TEXT_CHUNK; for ACP backend,
            # tool calls/plan updates are strong signals that execution has started.
            if self._project and self._project.status == DeepProjectStatus.PLANNING:
                marker_hit = False
                try:
                    if event.event_type == ACPEventType.TEXT_CHUNK:
                        txt = self._renderer.text_content or ""
                        # Heuristic markers for CLI backend
                        if "### 执行过程" in txt or "## 执行过程" in txt or "开始执行" in txt:
                            marker_hit = True
                except Exception:
                    marker_hit = False

                if marker_hit or event.event_type in (
                    ACPEventType.PLAN_UPDATE,
                    ACPEventType.TOOL_CALL_START,
                    ACPEventType.TOOL_CALL_UPDATE,
                    ACPEventType.TOOL_CALL_DONE,
                ):
                    # Mark as executing and fire "planning done" once.
                    self._project.start()
                    if (not self._planning_done_fired) and callbacks.on_planning_done:
                        self._planning_done_fired = True
                        callbacks.on_planning_done(self._project)

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
        return on_event

    def _close_session_safely(self) -> None:
        """Close existing ACP session, ignoring errors."""
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("关闭旧ACP session失败: %s", e)
            self._session = None

    def plan_and_execute(
        self,
        requirement_text: str,
        callbacks: Optional[DeepEngineCallbacks] = None,
    ) -> DeepProject:
        """Single ACP prompt drives the entire deep execution."""
        callbacks = callbacks or DeepEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._planning_done_fired = False

        project_name = os.path.basename(self.root_path) or "deep_project"
        self._project = DeepProject.create(name=project_name, root_path=self.root_path)
        self._project.status = DeepProjectStatus.PLANNING
        self._project.started_at = time.time()

        if callbacks.on_planning_start:
            callbacks.on_planning_start(requirement_text)

        logger.info("[Deep:%s] ACP执行开始, 需求长度=%d, 路径=%s, agent=%s",
                     project_name, len(requirement_text), self.root_path, self._agent_type)

        try:
            # Create session
            self._session = create_engine_session(agent_type=self._agent_type, cwd=self.root_path)

            # Build deep prompt — let agent plan and execute autonomously
            prompt = self._build_deep_prompt(requirement_text)

            on_event = self._make_on_event(callbacks)
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
                batch = list(self._pending_context)
                self._pending_context.clear()
            if not batch:
                break
            ctx = "\n\n---\n\n".join(batch)
            logger.info("[Deep] 发送注入的上下文(%d条): %s...", len(batch), ctx[:100])
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
1. 必须先输出清晰的【分析】与【执行计划】，再开始调用工具执行
2. 计划需要拆成可验证的步骤（建议 3~8 步），每步一句话描述产物/验证点
3. 执行时严格按计划逐步推进；如需调整计划，先解释原因并更新计划
4. 每个关键步骤完成后做一次自检/验证（单测/运行/静态检查等，按项目能力选择）
5. 完成后输出总结：做了什么、改了哪些文件、如何验证

## 输出格式（强制）
### 分析
- ...

### 执行计划
1. ...
2. ...

### 执行过程
（从这里开始再调用工具）
"""

    def inject_context(self, message: str):
        """Inject user context — will be sent as follow-up prompt after current execution.

        Multiple calls accumulate; all pending messages are drained together.
        """
        with self._context_lock:
            self._pending_context.append(message)
        logger.info("[Deep] 上下文已注入(待发送, 队列=%d): %s...", len(self._pending_context), message[:100])

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
            # Close old session before opening new one (prevent resource leak)
            self._close_session_safely()
            self._session = create_engine_session(agent_type=self._agent_type, cwd=self.root_path)

            resume_prompt = """你之前的执行被暂停了。请继续完成剩余的任务。
检查之前的进度，对未完成的部分继续实现。
完成后输出总结报告。"""

            on_event = self._make_on_event(callbacks)
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

    # Static status messages (no f-string allocation per call)
    _STATUS_MESSAGES: dict[DeepProjectStatus, str] = {
        DeepProjectStatus.IDLE: "等待开始",
        DeepProjectStatus.PLANNING: "正在规划任务...",
        DeepProjectStatus.PAUSED: "已暂停",
        DeepProjectStatus.COMPLETED: "全部完成",
    }

    def get_progress(self) -> Optional[ProgressUpdate]:
        if not self._project:
            return None

        status = self._project.status
        if status == DeepProjectStatus.EXECUTING:
            message = f"执行中 (工具调用: {len(self._progress.tool_calls)})"
        elif status == DeepProjectStatus.FAILED:
            message = f"执行失败: {self._project.error or '未知错误'}"
        else:
            message = self._STATUS_MESSAGES.get(status, "未知状态")

        return ProgressUpdate(
            project_id=self._project.project_id,
            current_task=None,
            completed_count=self._progress.completed_steps,
            total_count=self._progress.total_steps or 1,
            status=status,
            message=message,
        )

    def get_task_summary(self) -> str:
        if not self._project:
            return "暂无任务"

        lines = [f"📊 **{self._project.name}** 执行进度\n"]

        if self._progress.plan_entries:
            lines.append(self._progress.progress_bar)
            lines.append("")
            for entry in self._progress.plan_entries:
                icon = _STATUS_ICONS.get(entry["status"], "⬜")
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
    """Manages DeepEngine instances per chat+project.

    Thread-safe: all dict mutations are protected by _lock.
    Uses a secondary index (_chat_keys) to avoid O(n) full-table scans.
    """

    def __init__(self):
        self._engines: dict[str, DeepEngine] = {}
        self._chat_keys: dict[str, set[str]] = {}  # chat_id → set of keys
        self._lock = threading.Lock()

    def _add_index(self, chat_id: str, key: str) -> None:
        self._chat_keys.setdefault(chat_id, set()).add(key)

    def _remove_index(self, chat_id: str, key: str) -> None:
        keys = self._chat_keys.get(chat_id)
        if keys:
            keys.discard(key)
            if not keys:
                del self._chat_keys[chat_id]

    def get_or_create(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> DeepEngine:
        key = f"{chat_id}:{root_path}"
        agent_type = "claude" if engine_name.lower().startswith("claude") else "coco"

        with self._lock:
            if key not in self._engines:
                self._engines[key] = DeepEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=agent_type,
                    engine_name=engine_name,
                )
                self._add_index(chat_id, key)
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
        with self._lock:
            if key in self._engines:
                self._engines[key].cleanup()
                del self._engines[key]
                self._remove_index(chat_id, key)

    def _iter_chat_engines(self, chat_id: str):
        """Yield engines belonging to a chat (O(k) where k = engines per chat)."""
        for key in self._chat_keys.get(chat_id, ()):
            engine = self._engines.get(key)
            if engine:
                yield engine

    def get_active_engine(self, chat_id: str) -> Optional[DeepEngine]:
        for engine in self._iter_chat_engines(chat_id):
            if engine.is_running:
                return engine
        return None

    def get_active_engines(self, chat_id: str) -> list[DeepEngine]:
        return [e for e in self._iter_chat_engines(chat_id) if e.is_running]

    def list_engines(self, chat_id: Optional[str] = None) -> list[DeepEngine]:
        if chat_id is None:
            return list(self._engines.values())
        return list(self._iter_chat_engines(chat_id))

    def find_by_deep_project_id(self, chat_id: str, deep_project_id: str) -> Optional[DeepEngine]:
        if not deep_project_id:
            return None
        # Single pass instead of double traversal
        for engine in self._iter_chat_engines(chat_id):
            try:
                if engine.project and engine.project.project_id == deep_project_id:
                    return engine
            except Exception:
                continue
        return None

    def cleanup_all(self):
        with self._lock:
            for engine in self._engines.values():
                engine.cleanup()
            self._engines.clear()
            self._chat_keys.clear()
