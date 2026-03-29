"""ACP-driven Deep Engine — leverages agent's own planning capabilities.

Instead of parsing requirements → planning tasks → executing one-by-one,
the new Deep Engine sends a single comprehensive prompt to the agent and
monitors its plan/tool-call/text progress via ACP events.
"""

import gc
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

from ..acp import ACPEvent, ACPEventType
from ..agent_session import create_engine_session
from ..engine_base import BaseEngine, BaseEngineManager
from ..utils.debug_utils import MemorySnapshot
from ..utils.gc_monitor import get_gc_monitor
from ..utils.trace import TraceContext
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

    on_analyzing_start: Optional[Callable[[str], None]] = None
    on_analyzing_done: Optional[Callable[[DeepProject], None]] = None
    on_event: Optional[Callable[[ACPEvent], None]] = None
    on_text: Optional[Callable[[str], None]] = None
    on_project_done: Optional[Callable[[DeepProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None

    @property
    def on_planning_start(self):
        return self.on_analyzing_start

    @on_planning_start.setter
    def on_planning_start(self, value):
        self.on_analyzing_start = value

    @property
    def on_planning_done(self):
        return self.on_analyzing_done

    @on_planning_done.setter
    def on_planning_done(self, value):
        self.on_analyzing_done = value


class DeepEngine(BaseEngine):
    """ACP-driven Deep Engine — the agent plans and executes autonomously."""

    _state_filename = ".deep_engine_state.json"
    _gc_label = "Deep"
    _gc_threshold_default = 80.0

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str = "coco",
        engine_name: str = "Coco",
        model_name: Optional[str] = None,
    ):
        super().__init__(chat_id, root_path, agent_type, engine_name, model_name)
        self._progress = DeepProgress()
        self._pending_context: list[str] = []
        self._planning_done_fired: bool = False
        self._last_mem_check: float = 0.0
        self._mem_snapshot = MemorySnapshot()

    @property
    def progress(self) -> DeepProgress:
        return self._progress

    def _check_memory_and_gc(self) -> None:
        """Backward-compatible memory check hook used by legacy tests/callers."""
        now = time.time()
        if now - self._last_mem_check < 5.0:
            return
        self._last_mem_check = now

        if psutil is None:
            return

        try:
            process = psutil.Process(os.getpid())
            mem_percent = process.memory_percent()
            threshold_raw = getattr(self.settings, "deep_memory_threshold", 80.0)
            threshold = threshold_raw if isinstance(threshold_raw, (int, float)) else 80.0
            if mem_percent >= float(threshold):
                gc.collect()
                process.memory_percent()
        except Exception:
            return

    def _make_on_event(self, callbacks: DeepEngineCallbacks) -> Callable[[ACPEvent], None]:
        """Create the on_event callback shared by plan_and_execute and resume."""
        gc_monitor = get_gc_monitor(memory_threshold_percent=self.settings.deep_memory_threshold)

        def on_event(event: ACPEvent):
            gc_monitor.check_and_collect(label="Deep", mem_snapshot=self._mem_snapshot)

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
                    if (not self._planning_done_fired) and callbacks.on_analyzing_done:
                        self._planning_done_fired = True
                        callbacks.on_analyzing_done(self._project)

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

    def plan_and_execute(
        self,
        requirement_text: str,
        callbacks: Optional[DeepEngineCallbacks] = None,
        task_id: Optional[str] = None,
        on_rate_limit: Optional[Callable[[int], None]] = None,
    ) -> DeepProject:
        """Single ACP prompt drives the entire deep execution."""
        callbacks = callbacks or DeepEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._planning_done_fired = False
        self._on_rate_limit = on_rate_limit

        project_name = os.path.basename(self.root_path) or "deep_project"
        self._project = DeepProject.create(name=project_name, root_path=self.root_path)
        self._project.task_id = task_id
        self._project.status = DeepProjectStatus.PLANNING
        self._project.started_at = time.time()

        if callbacks.on_analyzing_start:
            callbacks.on_analyzing_start(requirement_text)

        logger.info(
            "[Deep:%s] ACP执行开始, 需求长度=%d, 路径=%s, agent=%s",
            project_name,
            len(requirement_text),
            self.root_path,
            self._agent_type,
        )

        # Initialize TraceContext for the execution
        trace_ctx = TraceContext(trace_id=task_id or f"deep-{int(time.time())}")
        
        try:
            with trace_ctx:
                # Create session
                from ..utils.path import normalize_ttadk_cwd

                self._session = create_engine_session(
                    agent_type=self._agent_type,
                    cwd=normalize_ttadk_cwd(self.root_path) or self.root_path,
                    on_rate_limit=on_rate_limit,
                    model_name=self._model_name,
                )

                # Build deep prompt — let agent plan and execute autonomously
                prompt = self._build_deep_prompt(requirement_text)

                on_event = self._make_on_event(callbacks)
                if self._agent_type.startswith("ttadk_"):
                    timeout = self.settings.coco_execution_timeout
                else:
                    timeout = (
                        self.settings.coco_execution_timeout
                        if self._agent_type == "coco"
                        else self.settings.claude_execution_timeout
                    )
                def _before_retry(attempt: int, error: Exception):
                    # For Deep Engine, we clear the renderer and progress to avoid duplicated rendering
                    self._renderer.reset()
                    # Cannot fully reset self._progress since we might lose some previous steps,
                    # but typically deep engine retry happens early or we just append to text.
                    # Best effort clear for a fresh retry
                    self._planning_done_fired = False

                from ..utils.retry import RetryPolicy
                result = self._session.send_prompt_with_retry(
                    prompt, on_event=on_event, timeout=timeout,
                    retry_policy=RetryPolicy(max_retries=2, retry_delay=2.0),
                    before_retry=_before_retry
                )

                # Process pending context injections as follow-up prompts
                result = self._drain_pending_context(on_event, timeout, result)

                # Determine final status
                if self._run_state == EngineRunState.STOPPING:
                    self._project.pause()
                    logger.info("[Deep:%s] 执行已暂停", project_name)
                elif result.stop_reason in ("end_turn", "max_turn_requests"):
                    self._project.complete()
                    logger.info(
                        "[Deep:%s] 执行完成, 工具调用=%d, 修改文件=%d, 总耗时=%.1fs",
                        project_name,
                        len(self._progress.tool_calls),
                        len(self._progress.modified_files),
                        self._project.duration() or 0,
                    )
                elif result.stop_reason == "cancelled":
                    self._project.pause()
                else:
                    self._project.fail(f"意外停止: {result.stop_reason}")

                if callbacks.on_project_done:
                    callbacks.on_project_done(self._project)

                return self._project

        except Exception as e:
            from ..utils.errors import get_error_detail

            detail = get_error_detail(e)

            error_msg = f"执行异常: {detail}"
            logger.error("[Deep:%s] %s", project_name, error_msg)
            if self._project:
                self._project.fail(error_msg)
            if callbacks.on_error:
                callbacks.on_error(error_msg)
            return self._project

        finally:
            self._run_state = EngineRunState.IDLE
            get_gc_monitor(memory_threshold_percent=self.settings.deep_memory_threshold).check_and_collect(label="Deep", mem_snapshot=self._mem_snapshot)

    def _drain_pending_context(self, on_event, timeout, last_result):
        """Send any pending context injections as follow-up prompts in the same session."""
        while self._run_state == EngineRunState.RUNNING:
            with self._lock:
                batch = list(self._pending_context)
                self._pending_context.clear()
            if not batch:
                break
            ctx = "\n\n---\n\n".join(batch)
            logger.info("[Deep] 发送注入的上下文(%d条): %s...", len(batch), ctx[:100])
            follow_up = f"""用户提供了额外的上下文/指导信息，请据此继续执行：

{ctx}

请根据以上信息调整你的执行方案并继续。"""
            from ..utils.retry import RetryPolicy
            last_result = self._session.send_prompt_with_retry(
                follow_up, on_event=on_event, timeout=timeout,
                retry_policy=RetryPolicy(max_retries=1, retry_delay=2.0)
            )
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

    def inject_guidance(self, message: str):
        with self._lock:
            self._pending_context.append(message)
        logger.info("[Deep] 上下文已注入(待发送, 队列=%d): %s...", len(self._pending_context), message[:100])

    inject_context = inject_guidance

    def pause(self):
        if self._project:
            self._project.pause()
        self._run_state = EngineRunState.STOPPING
        if self._session:
            self._session.cancel()

    def resume(self, callbacks: Optional[DeepEngineCallbacks] = None) -> Optional[DeepProject]:
        """Resume a paused deep execution by loading the ACP session and sending a continuation prompt."""
        if not self._project or self._project.status not in (DeepProjectStatus.PAUSED, DeepProjectStatus.FAILED):
            return self._project

        callbacks = callbacks or DeepEngineCallbacks()
        self._run_state = EngineRunState.RUNNING
        self._project.status = DeepProjectStatus.EXECUTING

        try:
            # Close old session before opening new one (prevent resource leak)
            self._close_session_safely()

            from ..utils.path import normalize_ttadk_cwd

            self._session = create_engine_session(
                agent_type=self._agent_type,
                cwd=normalize_ttadk_cwd(self.root_path) or self.root_path,
                on_rate_limit=getattr(self, "_on_rate_limit", None),
                model_name=self._model_name,
            )

            resume_prompt = """你之前的执行被暂停了。请继续完成剩余的任务。
检查之前的进度，对未完成的部分继续实现。
完成后输出总结报告。"""

            on_event = self._make_on_event(callbacks)
            if self._agent_type.startswith("ttadk_"):
                timeout = self.settings.coco_execution_timeout
            else:
                timeout = (
                    self.settings.coco_execution_timeout
                    if self._agent_type == "coco"
                    else self.settings.claude_execution_timeout
                )
            from ..utils.retry import RetryPolicy
            result = self._session.send_prompt_with_retry(
                resume_prompt, on_event=on_event, timeout=timeout,
                retry_policy=RetryPolicy(max_retries=2, retry_delay=2.0)
            )
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
            from ..utils.errors import get_error_detail

            detail = get_error_detail(e)

            error_msg = f"恢复执行异常: {detail}"
            logger.error("[Deep:%s] %s", self._project.name, error_msg)
            self._project.fail(error_msg)
            if callbacks.on_error:
                callbacks.on_error(error_msg)

        finally:
            self._run_state = EngineRunState.IDLE
            get_gc_monitor(memory_threshold_percent=self.settings.deep_memory_threshold).check_and_collect(label="Deep", mem_snapshot=self._mem_snapshot)

        return self._project

    # Static status messages (no f-string allocation per call)
    _STATUS_MESSAGES: dict[DeepProjectStatus, str] = {
        DeepProjectStatus.IDLE: "等待开始",
        DeepProjectStatus.PLANNING: "正在规划任务...",
        DeepProjectStatus.PAUSED: "已暂停",
        DeepProjectStatus.COMPLETED: "全部完成",
    }

    def get_progress(self) -> Optional[ProgressUpdate]:
        with self._lock:
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
            completed_count=self._progress.completed_steps,
            total_count=self._progress.total_steps or 1,
            status=status,
            message=message,
        )

    def get_task_summary(self) -> str:
        with self._lock:
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

    def load_state(self, filepath: Optional[str] = None) -> bool:
        if not filepath:
            filepath = os.path.join(self.root_path, self._state_filename)

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
        super().cleanup()


class DeepEngineManager(BaseEngineManager["DeepEngine"]):
    """Manages DeepEngine instances per chat+project.

    Thread-safe: all dict mutations are protected by _lock.
    Uses a secondary index (_chat_keys) to avoid O(n) full-table scans.
    """

    def _create_engine(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str,
        engine_name: str,
        model_name: Optional[str],
    ) -> "DeepEngine":
        return DeepEngine(
            chat_id=chat_id,
            root_path=root_path,
            agent_type=agent_type,
            engine_name=engine_name,
            model_name=model_name,
        )

    def remove(self, chat_id: str, root_path: str):
        key = f"{chat_id}:{root_path}"
        with self._lock:
            if key in self._engines:
                self._engines[key].cleanup()
                del self._engines[key]
                self._remove_index(chat_id, key)

    def find_by_deep_project_id(self, chat_id: str, deep_project_id: str) -> Optional["DeepEngine"]:
        if not deep_project_id:
            return None
        for engine in self._iter_chat_engines(chat_id):
            try:
                if engine.project and engine.project.project_id == deep_project_id:
                    return engine
            except Exception:
                continue
        return None
