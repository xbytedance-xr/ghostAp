"""Deep Engine handler — start, status, pause, resume, stop, update context."""

from __future__ import annotations

import logging
import os
import time
import json
from typing import TYPE_CHECKING, Optional

from lark_oapi.api.im.v1 import (
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from ...acp import ACPEvent, ACPEventType, ACPEventRenderer
from ...card import CardBuilder
from ...deep_engine import DeepEngineCallbacks
from ...deep_engine.models import DeepProject, DeepProjectStatus
from ...project import ContextSourceMode
from ...tasking import TaskSpec, TaskPriority
from ...utils.errors import fmt_error
from ...utils.text import append_duration_to_title, generate_task_id
from ..emoji import EmojiReaction
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class DeepHandler(BaseHandler):
    """Manages the full lifecycle of Deep Engine tasks."""

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_deep_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        text_lower = text.lower().strip()

        if text_lower == "/deep_status" or text_lower.startswith("/deep_status "):
            arg = text_lower[len("/deep_status"):].strip()
            if arg in ("all", "-a", "--all"):
                self.show_deep_board(message_id, chat_id)
            else:
                self.show_deep_status(message_id, chat_id, project)
        elif text_lower == "/stop_deep" or text_lower.startswith("/stop_deep "):
            arg = text_lower[len("/stop_deep"):].strip()
            if arg in ("all", "-a", "--all"):
                self.stop_all_deep_engines(message_id, chat_id)
            else:
                self.stop_deep_engine(message_id, chat_id, project)
        elif text_lower.startswith("/deep_update "):
            update_message = text[len("/deep_update "):].strip()
            self.update_deep_context(message_id, chat_id, update_message, project)
        elif text_lower == "/deep_update":
            self.reply_message(message_id, "📝 请提供上下文信息\n\n用法: `/deep_update <上下文描述>`\n\n例如: `/deep_update 数据库改用 PostgreSQL 而不是 SQLite`")
        elif text_lower.startswith("/deep "):
            requirement = text[6:].strip()
            self.start_deep_engine(message_id, chat_id, requirement, project)
        elif text_lower == "/deep":
            self.reply_message(message_id, "📝 请提供需求描述\n\n用法: `/deep <你的需求描述>`\n\n例如: `/deep 帮我写一个 Python 爬虫，爬取豆瓣电影 Top250`")
        else:
            self.reply_message(message_id, "❓ 未知的 Deep 命令\n\n可用命令:\n• `/deep <需求>` - 启动 Deep Agent\n• `/deep_update <上下文>` - 注入执行上下文\n• `/deep_status` - 查看当前项目进度\n• `/deep_status all` - 查看所有项目 Deep Agent 看板\n• `/stop_deep` - 停止当前项目任务\n• `/stop_deep all` - 停止所有项目任务")

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    def start_deep_engine(self, message_id: str, chat_id: str, requirement: str, project: Optional["ProjectContext"] = None):
        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("Deep Engine 自动创建项目: %s @ %s", project.project_name, project.root_path)
            except Exception as e:
                self.reply_message(message_id, fmt_error("创建项目", str(e)))
                return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        existing = self.ctx.deep_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_message(
                message_id,
                "⚠️ 当前项目已有 Deep Agent 任务在执行中\n\n发送 `/deep_status` 查看进度\n发送 `/stop_deep` 停止任务",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.progress_reporter

        planning_content = reporter.format_planning_start(requirement)
        planning_title = reporter.get_planning_start_title()
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            title=planning_title,
            content=f"{planning_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else planning_content,
            engine_name=engine_name,
            show_buttons=False,
        )
        initial_msg_id = self.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id)

        engine = self.ctx.deep_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        project_name = project.project_name if project else os.path.basename(root_path) or "deep"
        task_id = generate_task_id(project_name)

        _on_rate_limit = self.create_rate_limit_callback(chat_id, message_id, project, engine_name, request_id)

        def run_deep_engine():
            try:
                callbacks = self._create_deep_callbacks(
                    message_id,
                    chat_id,
                    project,
                    engine_name,
                    root_path=root_path,
                    initial_message_id=initial_msg_id,
                )
                engine.plan_and_execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)
            except Exception as e:
                logger.error("Deep Engine 执行异常: %s", e, exc_info=True)
                error_content = reporter.format_error(str(e))
                error_title = reporter.get_error_title()
                err_msg_type, err_card = CardBuilder.build_deep_card(
                    project=project,
                    title=error_title,
                    content=f"{error_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else error_content,
                    engine_name=engine_name,
                    show_buttons=False,
                )
                self.send_message(chat_id, err_card, err_msg_type, origin_message_id=message_id, request_id=request_id)

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:deep:{project.project_id if project else root_path}",
            name="deep_engine_run",
            task_type="deep_engine",
            project_id=project.project_id if project else None,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            task_id=task_id,
            priority=TaskPriority.NORMAL,
        )
        handle = self.scheduler.submit(spec, lambda ctx: run_deep_engine())
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(deep_engine_run): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    # ------------------------------------------------------------------
    # callbacks factory
    # ------------------------------------------------------------------
    def _convert_to_legacy_card(self, card_json_str: str) -> str:
        """Convert Schema 2.0 card to legacy structure for PATCH API."""
        try:
            card = json.loads(card_json_str)
            if isinstance(card, dict) and card.get("schema") == "2.0":
                # Convert to legacy structure: lift elements from body, remove schema
                new_card = {
                    "config": card.get("config", {}),
                    "header": card.get("header", {}),
                    "elements": card.get("body", {}).get("elements", [])
                }
                return json.dumps(new_card, ensure_ascii=False)
            return card_json_str
        except Exception:
            return card_json_str

    def _create_deep_callbacks(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        engine_name: str = "Coco",
        root_path: Optional[str] = None,
        initial_message_id: Optional[str] = None,
    ) -> DeepEngineCallbacks:
        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.progress_reporter

        thread_root_message_id: list[str | None] = [initial_message_id]
        current_status_message_id: list[str | None] = [initial_message_id]
        renderer = ACPEventRenderer()

        # Throttle streaming updates to avoid spamming Feishu
        last_stream_ts: float = 0.0
        last_stream_text_len: int = 0
        last_plan_ts: float = 0.0
        last_plan_content: str = ""

        def _send_deep_message(card_content: str, msg_type: str = "interactive", is_update: bool = False):
            """发送 deep 任务消息，在话题模式下确保所有消息都回复到同一个话题。"""
            # 1. Try update existing card
            if is_update and current_status_message_id[0]:
                legacy_content = self._convert_to_legacy_card(card_content)
                client = self.ctx.api_client_factory()
                
                # Simple exponential backoff for patch
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        req = PatchMessageRequest.builder() \
                            .message_id(current_status_message_id[0]) \
                            .request_body(PatchMessageRequestBody.builder()
                                .content(legacy_content)
                                .build()) \
                            .build()
                        resp = client.im.v1.message.patch(req)
                        if resp.success():
                            return
                        
                        logger.warning("Patch deep message failed (attempt %d/%d): %s %s", 
                                     attempt + 1, max_retries, resp.code, resp.msg)
                        
                        # Wait before retry
                        if attempt < max_retries - 1:
                            time.sleep(0.5 * (2 ** attempt))
                            
                    except Exception as e:
                        logger.error("Patch deep message error (attempt %d/%d): %s", 
                                   attempt + 1, max_retries, e)
                        if attempt < max_retries - 1:
                            time.sleep(0.5 * (2 ** attempt))

                # Patch failed, stop here if it was an update to an existing card
                logger.error("Failed to patch deep message after %d attempts. Skipping update.", max_retries)
                return

            # 2. Create new message (Only if not updating or no existing message)
            use_thread = self.settings.default_reply_mode == "thread"
            result_id = None
            if use_thread:
                reply_to = thread_root_message_id[0] or message_id
                result_id = self.reply_message(
                    reply_to, card_content, msg_type=msg_type,
                    origin_message_id=message_id, request_id=request_id,
                    reply_in_thread=True,
                )
                if thread_root_message_id[0] is None and result_id:
                    thread_root_message_id[0] = result_id
            else:
                result_id = self.send_message(chat_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id)

            if result_id:
                current_status_message_id[0] = result_id

        def on_planning_done(deep_project: DeepProject):
            content = f"🚀 ACP Deep 执行开始\n\n📂 **{deep_project.name}**\n🔗 路径: `{deep_project.root_path}`"
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title="🚀 开始执行",
                content=content,
                deep_project_id=deep_project.project_id, engine_name=engine_name, show_buttons=False,
            )
            _send_deep_message(card_content, msg_type, is_update=True)

        def _get_engine():
            rp = root_path or (project.root_path if project else "")
            if rp:
                return self.ctx.deep_engine_manager.get(chat_id, rp)
            # Best-effort fallback: if only one running engine, use it
            try:
                running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
                if len(running) == 1:
                    return running[0]
            except Exception:
                pass
            return None

        def _maybe_stream_update(force: bool = False) -> None:
            nonlocal last_stream_ts, last_stream_text_len

            engine = _get_engine()
            deep_project_id = engine.project.project_id if engine and engine.project else None
            progress = engine.progress if engine else None
            progress_bar = progress.progress_bar if progress else None

            now = time.monotonic()
            text_len = len(renderer.text_content or "")

            # Emit when forced OR enough time passed + enough new text accumulated
            min_interval = 2.5
            min_new_chars = 350
            if not force:
                if (now - last_stream_ts) < min_interval and (text_len - last_stream_text_len) < min_new_chars:
                    return

            plan_view = renderer.render_plan_view()
            recent = renderer.text_content or ""

            if not plan_view and not recent:
                return

            status = None
            try:
                status = engine.project.status if engine and engine.project else None
            except Exception:
                status = None

            if status == DeepProjectStatus.PLANNING:
                title = "🧠 分析/规划中"
            else:
                title = "🔄 执行中"

            title = append_duration_to_title(title, engine.project.duration() if engine and engine.project else None)

            parts = []
            if plan_view:
                parts.append(plan_view)
            if recent:
                parts.append(f"\n**📝 最近输出**\n{recent}")

            content = "\n\n".join(parts)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                title=title,
                content=content,
                progress_bar=progress_bar,
                deep_project_id=deep_project_id,
                is_executing=True,
                engine_name=engine_name,
            )
            _send_deep_message(card_content, msg_type, is_update=True)
            last_stream_ts = now
            last_stream_text_len = text_len

        def on_event(event: ACPEvent):
            """Process ACP events and update streaming display."""
            renderer.process_event(event)
            nonlocal last_plan_ts, last_plan_content

            # 1) Plan updates: send plan-only view (throttled)
            if event.event_type == ACPEventType.PLAN_UPDATE and event.plan:
                now = time.monotonic()
                plan_content = renderer.render_plan_view()
                if plan_content and (plan_content != last_plan_content or (now - last_plan_ts) > 1.5):
                    engine = _get_engine()
                    deep_project_id = engine.project.project_id if engine and engine.project else None
                    progress = engine.progress if engine else None
                    progress_bar = progress.progress_bar if progress else None
                    plan_title = append_duration_to_title("📋 执行计划", engine.project.duration() if engine and engine.project else None)
                    msg_type, card_content = CardBuilder.build_deep_card(
                        project=project,
                        title=plan_title,
                        content=plan_content,
                        progress_bar=progress_bar,
                        deep_project_id=deep_project_id,
                        is_executing=True,
                        engine_name=engine_name,
                    )
                    _send_deep_message(card_content, msg_type, is_update=True)
                    last_plan_ts = now
                    last_plan_content = plan_content

            # 2) Stream text/tool progress so users can see "分析→计划→执行" 过程
            if event.event_type in (
                ACPEventType.TEXT_CHUNK,
                ACPEventType.TOOL_CALL_START,
                ACPEventType.TOOL_CALL_UPDATE,
                ACPEventType.TOOL_CALL_DONE,
            ):
                _maybe_stream_update(force=(event.event_type == ACPEventType.TOOL_CALL_DONE))

        def on_project_done(deep_project: DeepProject):
            engine = _get_engine()
            progress = engine.progress if engine else None
            rendered_content = engine.get_rendered_content() if engine else ""

            summary_parts = []
            if progress:
                summary_parts.append(progress.format_summary())
            if rendered_content:
                summary_parts.append(f"\n**📝 执行输出**\n{rendered_content}")

            content = "\n\n".join(summary_parts) or "执行完成"
            status_emoji = "✅" if deep_project.status == DeepProjectStatus.COMPLETED else "⚠️"
            title = f"{status_emoji} Deep Agent 执行{'完成' if deep_project.status == DeepProjectStatus.COMPLETED else '结束'}"

            progress_bar = progress.progress_bar if progress else None
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar, deep_project_id=deep_project.project_id, engine_name=engine_name,
            )
            _send_deep_message(card_content, msg_type, is_update=True)
            self.add_reaction(message_id, EmojiReaction.on_multi_task_done())

            if project:
                self.context_manager.update_context(
                    project.project_id,
                    deep_result={"data": deep_project.to_dict()},
                )
                ctx = self.context_manager.store.get(project.project_id)
                if ctx:
                    ctx.create_version(
                        reason=f"deep_engine_done: {deep_project.name}",
                        source_mode=ContextSourceMode.DEEP_ENGINE,
                        summary=f"Deep Engine completed: tool_calls={len(progress.tool_calls) if progress else 0}",
                    )

        def on_error(error: str):
            content = reporter.format_error(error)
            title = reporter.get_error_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                engine_name=engine_name, show_buttons=False,
            )
            _send_deep_message(card_content, msg_type, is_update=True)
            self.add_reaction(message_id, EmojiReaction.on_error())

        return DeepEngineCallbacks(
            on_planning_done=on_planning_done,
            on_event=on_event,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    # ------------------------------------------------------------------
    # status / board
    # ------------------------------------------------------------------
    def show_deep_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)
        reporter = self.ctx.progress_reporter

        if not engine or not engine.project:
            running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
            if len(running) == 1 and running[0].project:
                engine = running[0]
            elif len(running) > 1:
                self.show_deep_board(message_id, chat_id)
                return
            else:
                engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project, title="📊 当前状态",
                    content="当前没有 Deep Agent 任务\n\n发送 `/deep 你的需求` 开始一个复杂任务\n发送 `/deep_status all` 查看所有项目任务",
                    engine_name=engine_name, show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        engine_name = engine.engine_name

        if project is None:
            try:
                project = self.project_manager.find_project_by_path(engine.root_path)
            except Exception:
                project = None

        status_content = reporter.format_status(engine.project)
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(
            engine.project,
            completed=engine.progress.completed_steps,
            total=engine.progress.total_steps,
        )
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=status_title, content=status_content,
            progress_bar=progress_info["progress_bar"],
            deep_project_id=progress_info["project_id"],
            is_executing=progress_info["is_executing"],
            is_paused=progress_info["is_paused"],
            engine_name=engine_name,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def show_deep_board(self, message_id: str, chat_id: str):
        engines = self.ctx.deep_engine_manager.list_engines(chat_id)
        candidates = []
        for e in engines:
            if not e.project:
                continue
            if e.project.status in (DeepProjectStatus.EXECUTING, DeepProjectStatus.PAUSED, DeepProjectStatus.IDLE):
                candidates.append(e)

        if not candidates:
            engine_name = self.get_engine_name(chat_id, project_id=None)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=None, title="📊 Deep Agent 看板",
                content="当前没有 Deep Agent 任务\n\n发送 `/deep <需求>` 开始一个复杂任务",
                engine_name=engine_name, show_buttons=False,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        def _sort_key(e):
            running_rank = 0 if e.is_running else 1
            paused_rank = 0 if (e.project and e.project.status == DeepProjectStatus.PAUSED) else 1
            return (running_rank, paused_rank, -(e.project.started_at or 0))

        candidates.sort(key=_sort_key)
        reporter = self.ctx.progress_reporter
        lines = ["**Deep Agent 任务（按项目）**", ""]
        for e in candidates[:10]:
            proj = None
            try:
                proj = self.project_manager.find_project_by_path(e.root_path)
            except Exception:
                proj = None
            proj_name = proj.project_name if proj else (e.project.name or "unknown")
            root = e.root_path
            info = reporter.get_progress_info(
                e.project,
                completed=e.progress.completed_steps,
                total=e.progress.total_steps,
            )
            status = e.project.status.value
            lines.append(f"- 🧠 **{proj_name}** · `{status}` · {info['progress_bar']} · `{root}`")

        msg_type, card_content = CardBuilder.build_smart_response_card(
            project=None, title="📊 Deep Agent 看板", content="\n".join(lines),
            working_dir=self.get_working_dir(chat_id), show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    # ------------------------------------------------------------------
    # pause / resume / stop
    # ------------------------------------------------------------------
    def pause_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)
        if not engine:
            engine = self.ctx.deep_engine_manager.get_active_engine(chat_id)
        if engine and engine.is_running:
            engine.pause()
            self.show_deep_status(message_id, chat_id, project=project)
            return
        self.reply_message(message_id, "当前没有正在执行的任务")

    def resume_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)

        if not engine:
            paused = [e for e in self.ctx.deep_engine_manager.list_engines(chat_id)
                      if e.project and e.project.status == DeepProjectStatus.PAUSED]
            if len(paused) == 1:
                engine = paused[0]
            elif len(paused) > 1:
                self.reply_message(message_id, "⚠️ 有多个项目存在可恢复的 Deep Agent 任务，请使用 `/deep_status all` 查看后切换项目再恢复")
                return

        if engine and engine.project and engine.project.status == DeepProjectStatus.PAUSED:
            if project is None:
                try:
                    project = self.project_manager.find_project_by_path(engine.root_path)
                except Exception:
                    project = None

            callbacks = self._create_deep_callbacks(
                message_id,
                chat_id,
                project,
                engine_name=engine.engine_name,
                root_path=engine.root_path,
            )

            def run_resume():
                engine.resume(callbacks)

            request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
            spec = TaskSpec(
                chat_id=chat_id,
                queue_key=f"{chat_id}:deep:{project.project_id if project else root_path}",
                name="deep_engine_resume", task_type="deep_engine",
                project_id=project.project_id if project else None,
                message_id=message_id, origin_message_id=message_id,
                request_id=request_id, priority=TaskPriority.HIGH,
            )
            handle = self.scheduler.submit(spec, lambda ctx: run_resume())
            try:
                self.ctx.message_linker.link_task(message_id, handle.run_id)
            except Exception as e:
                logger.debug("link_task失败(deep_engine_resume): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)
            self.show_deep_status(message_id, chat_id, project=project)
        else:
            self.reply_message(message_id, "当前没有可恢复的任务")

    def stop_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)

        if not engine:
            running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]
            elif len(running) > 1:
                self.reply_message(message_id, "⚠️ 有多个项目正在执行 Deep Agent 任务，请使用 `/stop_deep all` 或先 `/switch <项目>` 再停止")
                return

        if not engine or not engine.is_running:
            self.reply_message(message_id, "📊 当前没有正在执行的 Deep Agent 任务")
            return

        engine.stop()
        self.show_deep_status(message_id, chat_id, project=project)

    def stop_all_deep_engines(self, message_id: str, chat_id: str):
        engines = self.ctx.deep_engine_manager.get_active_engines(chat_id)
        if not engines:
            self.reply_message(message_id, "📊 当前没有正在执行的 Deep Agent 任务")
            return
        for e in engines:
            try:
                e.stop()
            except Exception as ex:
                logger.debug("停止deep engine失败: %s", ex)
        self.reply_message(message_id, f"🛑 已发送停止信号：{len(engines)} 个 Deep Agent 任务将在当前步骤完成后停止")

    # ------------------------------------------------------------------
    # update context
    # ------------------------------------------------------------------
    def update_deep_context(self, message_id: str, chat_id: str, update_message: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        engine = None
        if project:
            engine = self.ctx.deep_engine_manager.get(chat_id, project.root_path)

        if not engine:
            running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]

        if not engine and project is None:
            root_path = self.get_working_dir(chat_id)
            engine = self.ctx.deep_engine_manager.get(chat_id, root_path)

        if not engine or not engine.is_running:
            self.reply_message(message_id, "⚠️ 当前没有正在运行的 Deep Agent 任务\n\n请先使用 `/deep <需求>` 启动任务，或使用 `/deep_status all` 查看所有项目任务")
            return

        engine.inject_context(update_message)
        reporter = self.ctx.progress_reporter
        content = reporter.format_context_injected(update_message)
        title = reporter.get_context_injected_title()
        engine_name = engine.engine_name

        if project is None:
            try:
                project = self.project_manager.find_project_by_path(engine.root_path)
            except Exception:
                project = None

        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title, content=content,
            engine_name=engine_name, show_buttons=False,
        )
        self.send_message(chat_id, card_content, msg_type)
