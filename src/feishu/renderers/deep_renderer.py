
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEvent, ACPEventType, ACPEventRenderer
from ...card import CardBuilder
from ...card.models import DeepCardState
from ...deep_engine import DeepEngineCallbacks
from ...deep_engine.models import DeepProject, DeepProjectStatus
from ...project import ContextSourceMode
from ...utils.text import append_duration_to_title
from ..emoji import EmojiReaction

if TYPE_CHECKING:
    from ..handlers.deep import DeepHandler
    from ...project import ProjectContext

logger = logging.getLogger(__name__)

class DeepRenderer:
    """
    Handles UI rendering and state management for Deep Engine interactions.
    Separated from DeepHandler to improve maintainability.
    """

    def __init__(self, handler: "DeepHandler") -> None:
        self.handler = handler
        self.ctx = handler.ctx
        self.settings = handler.settings
        # deep_project_id -> {"compact": bool, "expanded": bool}
        self.ui_states: dict[str, dict] = {}

    def get_ui_state(self, deep_project_id: str) -> dict:
        if not deep_project_id:
            return {"compact": self.settings.card_deep_compact_default, "expanded": False}
        if deep_project_id not in self.ui_states:
            self.ui_states[deep_project_id] = {
                "compact": self.settings.card_deep_compact_default,
                "expanded": False
            }
        return self.ui_states[deep_project_id]

    def update_ui_state(self, deep_project_id: str, **kwargs):
        state = self.get_ui_state(deep_project_id)
        state.update(kwargs)

    def create_deep_callbacks(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        engine_name: str = "Coco",
        root_path: Optional[str] = None,
        initial_message_id: Optional[str] = None,
    ) -> DeepEngineCallbacks:
        request_id = self.handler.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
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
                if self.handler.patch_message(current_status_message_id[0], card_content, max_retries=1):
                    return

                # Patch failed, stop here if it was an update to an existing card
                logger.error("Failed to patch deep message. Skipping update.")
                return

            # 2. Create new message (Only if not updating or no existing message)
            use_thread = self.settings.default_reply_mode == "thread"
            result_id = None
            if use_thread:
                reply_to = thread_root_message_id[0] or message_id
                result_id = self.handler.reply_message(
                    reply_to, card_content, msg_type=msg_type,
                    origin_message_id=message_id, request_id=request_id,
                    reply_in_thread=True,
                )
                if thread_root_message_id[0] is None and result_id:
                    thread_root_message_id[0] = result_id
            else:
                result_id = self.handler.send_message(chat_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id)

            if result_id:
                current_status_message_id[0] = result_id

        def on_planning_done(deep_project: DeepProject):
            content = f"🚀 ACP Deep 执行开始\n\n📂 **{deep_project.name}**\n🔗 路径: `{deep_project.root_path}`"
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title="🚀 开始执行",
                    content=content,
                    deep_project_id=deep_project.project_id,
                    engine_name=engine_name,
                    show_buttons=False,
                )
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
            min_interval = self.settings.deep_stream_interval
            min_new_chars = self.settings.deep_stream_min_chars
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
            
            # Read UI state
            state = self.get_ui_state(deep_project_id) if deep_project_id else {"compact": self.settings.card_deep_compact_default, "expanded": False}
            
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    deep_project_id=deep_project_id,
                    is_executing=True,
                    engine_name=engine_name,
                    compact=state["compact"],
                    expanded=state["expanded"],
                )
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
                    
                    state = self.get_ui_state(deep_project_id) if deep_project_id else {"compact": self.settings.card_deep_compact_default, "expanded": False}
                    
                    msg_type, card_content = CardBuilder.build_deep_card(
                        project=project,
                        state=DeepCardState(
                            title=plan_title,
                            content=plan_content,
                            progress_bar=progress_bar,
                            deep_project_id=deep_project_id,
                            is_executing=True,
                            engine_name=engine_name,
                            compact=state["compact"],
                            expanded=state["expanded"],
                        )
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
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    deep_project_id=deep_project.project_id,
                    engine_name=engine_name,
                )
            )
            _send_deep_message(card_content, msg_type, is_update=True)
            self.handler.add_reaction(message_id, EmojiReaction.on_multi_task_done())

            if project:
                self.handler.context_manager.update_context(
                    project.project_id,
                    deep_result={"data": deep_project.to_dict()},
                )
                ctx = self.handler.context_manager.store.get(project.project_id)
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
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    engine_name=engine_name,
                    show_buttons=False,
                )
            )
            _send_deep_message(card_content, msg_type, is_update=True)
            self.handler.add_reaction(message_id, EmojiReaction.on_error())

        return DeepEngineCallbacks(
            on_planning_done=on_planning_done,
            on_event=on_event,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def render_deep_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, origin_message_id: Optional[str] = None):
        if project is None:
            project = self.handler.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.handler.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)
        reporter = self.ctx.progress_reporter

        if not engine or not engine.project:
            running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
            if len(running) == 1 and running[0].project:
                engine = running[0]
            elif len(running) > 1:
                self.handler.show_deep_board(message_id, chat_id)
                return
            else:
                engine_name = self.handler.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    state=DeepCardState(
                        title="📊 当前状态",
                        content="当前没有 Deep Agent 任务\n\n发送 `/deep 你的需求` 开始一个复杂任务\n发送 `/deep_status all` 查看所有项目任务",
                        engine_name=engine_name,
                        show_buttons=False,
                    )
                )
                self.handler.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=origin_message_id)
                return

        engine_name = engine.engine_name

        if project is None:
            try:
                project = self.handler.project_manager.find_project_by_path(engine.root_path)
            except Exception:
                project = None

        status_content = reporter.format_status(engine.project)
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(
            engine.project,
            completed=engine.progress.completed_steps,
            total=engine.progress.total_steps,
        )
        
        deep_project_id = progress_info["project_id"]
        state = self.get_ui_state(deep_project_id) if deep_project_id else {"compact": False, "expanded": False}
        
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            state=DeepCardState(
                title=status_title,
                content=status_content,
                progress_bar=progress_info["progress_bar"],
                deep_project_id=deep_project_id,
                is_executing=progress_info["is_executing"],
                is_paused=progress_info["is_paused"],
                engine_name=engine_name,
                compact=state["compact"],
                expanded=state["expanded"],
            )
        )

        # 尝试使用 Patch 更新原消息 (仅当 origin_message_id 存在时)
        patched = False
        if origin_message_id:
            patched = self.handler.patch_message(origin_message_id, card_content, max_retries=1)

        if not patched:
            self.handler.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=origin_message_id)
