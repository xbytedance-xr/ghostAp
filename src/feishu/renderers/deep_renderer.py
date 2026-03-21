from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEvent, ACPEventRenderer, ACPEventType
from ...card import CardBuilder
from ...card.models import DeepCardState
from ...deep_engine import DeepEngineCallbacks
from ...deep_engine.models import DeepProject, DeepProjectStatus
from ...project import ContextSourceMode
from ...utils.text import append_duration_to_title
from ..emoji import EmojiReaction
from .base import BaseRenderer, SmartSender

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handlers.deep import DeepHandler

logger = logging.getLogger(__name__)


class DeepRenderer(BaseRenderer):
    """
    Handles UI rendering and state management for Deep Engine interactions.
    Separated from DeepHandler to improve maintainability.
    """

    def __init__(self, handler: "DeepHandler") -> None:
        super().__init__(handler)

    def get_default_ui_state(self) -> dict:
        return {
            "compact": self.settings.card_deep_compact_default,
            "expanded": False,
            "expand_ac": False,
            "view_mode": "status",
            "view_context": {},
        }

    def create_deep_callbacks(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        engine_name: str = "Coco",
        root_path: Optional[str] = None,
        initial_message_id: Optional[str] = None,
    ) -> DeepEngineCallbacks:
        request_id = self.handler.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        reporter = self.ctx.progress_reporter

        sender = SmartSender(
            handler=self.handler, message_id=message_id, chat_id=chat_id, initial_message_id=initial_message_id
        )

        renderer = ACPEventRenderer()

        def _send_deep_message(
            card_content: str, msg_type: str = "interactive", is_update: bool = False, throttle: bool = False
        ):
            """发送 deep 任务消息，委托给 SmartSender 处理（含重锚逻辑）。"""
            sender.send(card_content, msg_type, is_update, throttle, request_id)

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
                ),
            )
            # Critical state update: flush immediately
            _send_deep_message(card_content, msg_type, is_update=True, throttle=False)

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
            text_len = len(renderer.text_content or "")

            if not sender.check_throttle(text_len, force):
                return

            plan_view = renderer.render_plan_view()
            recent = renderer.text_content or ""

            if not plan_view and not recent:
                return

            # Update stream state regardless of whether we actually send (to avoid spam if rendering fails or is skipped)
            # But here we only update if we proceed.

            engine = _get_engine()
            deep_project_id = engine.project.project_id if engine and engine.project else None
            progress = engine.progress if engine else None
            progress_bar = progress.progress_bar if progress else None

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
            state = self.get_ui_state(deep_project_id) if deep_project_id else self.get_default_ui_state()

            # Apply collapsing to content (Thought/Plan chain)
            # Deep mode content is usually Markdown text, not a list of criteria.
            # We can use _render_collapsible_section which handles both.
            # Count items by newlines for text.
            content = self._render_collapsible_section(
                content, total_items=len(content.split("\n")), expanded=state.get("expand_ac", False)
            )

            warning_banner = None
            duration_raw = engine.project.duration() if engine and engine.project else 0
            timeout_raw = getattr(self.settings, "engine_timeout_warning_seconds", 0)
            duration_s = duration_raw if isinstance(duration_raw, (int, float)) else 0
            timeout_s = timeout_raw if isinstance(timeout_raw, (int, float)) else 0
            if status != DeepProjectStatus.PLANNING and duration_s and duration_s > timeout_s:
                warning_banner = "执行耗时较长，若无响应可尝试停止后重试"

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
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="deep",
                    warning_banner=warning_banner,
                ),
            )
            # Streaming updates: use throttling
            _send_deep_message(card_content, msg_type, is_update=True, throttle=True)
            sender.update_stream_state(text_len)

        def on_event(event: ACPEvent):
            """Process ACP events and update streaming display."""
            renderer.process_event(event)

            # 1) Plan updates: send plan-only view (throttled)
            if event.event_type == ACPEventType.PLAN_UPDATE and event.plan:
                plan_content = renderer.render_plan_view()
                if sender.check_plan_throttle(plan_content):
                    engine = _get_engine()
                    deep_project_id = engine.project.project_id if engine and engine.project else None
                    progress = engine.progress if engine else None
                    progress_bar = progress.progress_bar if progress else None
                    plan_title = append_duration_to_title(
                        "📋 执行计划", engine.project.duration() if engine and engine.project else None
                    )

                    state = self.get_ui_state(deep_project_id) if deep_project_id else self.get_default_ui_state()

                    # Apply collapsing to plan content
                    plan_content = self._render_collapsible_section(
                        plan_content, total_items=len(plan_content.split("\n")), expanded=state.get("expand_ac", False)
                    )

                    warning_banner = None
                    duration_raw = engine.project.duration() if engine and engine.project else 0
                    timeout_raw = getattr(self.settings, "engine_timeout_warning_seconds", 0)
                    duration_s = duration_raw if isinstance(duration_raw, (int, float)) else 0
                    timeout_s = timeout_raw if isinstance(timeout_raw, (int, float)) else 0
                    if duration_s and duration_s > timeout_s:
                        warning_banner = "执行耗时较长，若无响应可尝试停止后重试"

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
                            expand_ac=state.get("expand_ac", False),
                            action_prefix="deep",
                            warning_banner=warning_banner,
                        ),
                    )
                    # Plan updates: use throttling
                    _send_deep_message(card_content, msg_type, is_update=True, throttle=True)
                    sender.update_plan_state(plan_content)

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
                ),
            )
            # Final completion: immediate flush
            _send_deep_message(card_content, msg_type, is_update=True, throttle=False)
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
            
            engine = _get_engine()
            deep_project_id = engine.project.project_id if engine and engine.project else (project.project_id if project else None)
            
            extra_buttons = [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔁 重试"},
                    "type": "primary",
                    "value": {
                        "action": "deep_resume",
                        "project_id": project.project_id if project else deep_project_id,
                        "deep_project_id": deep_project_id,
                    },
                }
            ]
            
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    engine_name=engine_name,
                    show_buttons=True,
                    extra_buttons=extra_buttons,
                    action_prefix="deep",
                    deep_project_id=deep_project_id,
                ),
            )
            # Error state: immediate flush
            _send_deep_message(card_content, msg_type, is_update=True, throttle=False)
            self.handler.add_reaction(message_id, EmojiReaction.on_error())

        return DeepEngineCallbacks(
            on_planning_done=on_planning_done,
            on_event=on_event,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def render_deep_status(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
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
                engine_name = self.handler.get_engine_name(
                    chat_id, project_id=(project.project_id if project else None)
                )
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    state=DeepCardState(
                        title="📊 当前状态",
                        content="当前没有 Deep Agent 任务\n\n发送 `/deep 你的需求` 开始一个复杂任务\n发送 `/deep_status all` 查看所有项目任务",
                        engine_name=engine_name,
                        show_buttons=False,
                    ),
                )
                self.handler.reply_message(
                    message_id, card_content, msg_type=msg_type, origin_message_id=origin_message_id
                )
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
        state = self.get_ui_state(deep_project_id) if deep_project_id else self.get_default_ui_state()

        # Apply collapsing to status content
        status_content = self._render_collapsible_section(
            status_content, total_items=len(status_content.split("\n")), expanded=state.get("expand_ac", False)
        )

        warning_banner = None
        duration_raw = engine.project.duration()
        timeout_raw = getattr(self.settings, "engine_timeout_warning_seconds", 0)
        duration_s = duration_raw if isinstance(duration_raw, (int, float)) else 0
        timeout_s = timeout_raw if isinstance(timeout_raw, (int, float)) else 0
        if progress_info["is_executing"] and duration_s and duration_s > timeout_s:
            warning_banner = "执行耗时较长，若无响应可尝试停止后重试"

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
                expand_ac=state.get("expand_ac", False),
                action_prefix="deep",
                warning_banner=warning_banner,
            ),
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)
