from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder, EngineCardState
from ...spec_engine import SpecEngineCallbacks
from ...spec_engine.models import (
    ReviewResult,
    SpecCycle,
    SpecPhase,
    SpecProject,
)
from ...utils.text import append_duration_to_title
from ..emoji import EmojiReaction
from .base import BaseRenderer, SmartSender

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handlers.spec import SpecHandler

logger = logging.getLogger(__name__)


class SpecRenderer(BaseRenderer):
    """
    Handles UI rendering and state management for Spec Engine interactions.
    """

    def __init__(self, handler: "SpecHandler") -> None:
        super().__init__(handler)

    def get_default_ui_state(self) -> dict:
        return super().get_default_ui_state()

    def create_spec_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str = "Coco"
    ) -> SpecEngineCallbacks:
        request_id = self.handler.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        reporter = self.ctx.spec_reporter

        sender = SmartSender(handler=self.handler, message_id=message_id, chat_id=chat_id, initial_message_id=None)

        spec_project_id = project.project_id if project else self.handler.get_working_dir(chat_id)

        _max_cycles = 0

        def _send_spec_message(
            card_content: str, msg_type: str = "interactive", is_update: bool = False, throttle: bool = False
        ):
            sender.send(card_content, msg_type, is_update, throttle, request_id)

        def on_analyzing_done(spec_project: SpecProject):
            self.update_ui_state(spec_project_id, view_mode="status", view_context={})

            content = reporter.format_analyzing_done(spec_project)
            title = reporter.get_analyzing_done_title()
            msg_type, card_content = CardBuilder.build_engine_card(
                project=project,
                state=EngineCardState(
                    title=title,
                    content=content,
                    engine_name=f"Spec({engine_name})",
                    show_buttons=False,
                ),
            )
            # Immediate flush
            _send_spec_message(card_content, msg_type, is_update=False, throttle=False)

        def on_cycle_start(current: int, max_cycles: int):
            nonlocal _max_cycles
            _max_cycles = max_cycles
            self.update_ui_state(spec_project_id, view_mode="status", view_context={})

            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            spec_project = engine.project if engine else None
            state = self.get_ui_state(spec_project_id)

            criteria_status = ""
            progress_bar = None
            status_line = None
            duration_line = None
            criteria_section = None

            if spec_project:
                criteria_status = reporter.format_criteria_brief(spec_project)
                progress_bar = self._generate_progress_bar(spec_project.satisfied_count, spec_project.total_criteria)
                status_line = reporter.format_status_line(spec_project)
                duration_line = reporter.format_duration_line(spec_project)
                criteria_section = reporter.format_criteria_section(spec_project)

                criteria_section = self._render_collapsible_section(
                    criteria_section,
                    total_items=spec_project.total_criteria,
                    expanded=state.get("expand_ac", False),
                    completed_count=spec_project.satisfied_count,
                )

            content = reporter.format_cycle_start(current, max_cycles, criteria_status=criteria_status)
            title = reporter.get_cycle_start_title(current, max_cycles)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)

            warning_banner = self._check_warning_banner(
                spec_project.duration() if spec_project else 0,
            )

            msg_type, card_content = CardBuilder.build_engine_card(
                project=project,
                state=EngineCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    is_executing=True,
                    engine_name=f"Spec({engine_name})",
                    status_line=status_line,
                    duration_line=duration_line,
                    criteria_section=criteria_section,
                    project_id=spec_project.project_id if spec_project else (project.project_id if project else None),
                    engine_project_id=spec_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="spec",
                    warning_banner=warning_banner,
                    footer_status="tool_running",
                ),
            )
            # Cycle start is significant, immediate flush
            _send_spec_message(card_content, msg_type, is_update=True, throttle=False)

        def on_cycle_done(cycle_num: int, cycle: SpecCycle):
            self.update_ui_state(spec_project_id, view_mode="cycle_done", view_context={"cycle_num": cycle_num})

            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            state = self.get_ui_state(spec_project_id)

            if engine and engine.project:
                sp = engine.project
                content = reporter.format_cycle_done(cycle_num, cycle)
                title = reporter.get_cycle_done_title(cycle_num, cycle.status == "completed")
                title = append_duration_to_title(title, sp.duration())
                progress_bar = self._generate_progress_bar(sp.satisfied_count, sp.total_criteria)
                status_line = reporter.format_status_line(sp)
                duration_line = reporter.format_duration_line(sp)
                criteria_section = reporter.format_criteria_section(sp)

                criteria_section = self._render_collapsible_section(
                    criteria_section,
                    total_items=sp.total_criteria,
                    expanded=state.get("expand_ac", False),
                    completed_count=sp.satisfied_count,
                )

                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    state=EngineCardState(
                        title=title,
                        content=content,
                        progress_bar=progress_bar,
                        engine_name=f"Spec({engine_name})",
                        status_line=status_line,
                        duration_line=duration_line,
                        criteria_section=criteria_section,
                        project_id=sp.project_id,
                        engine_project_id=spec_project_id,
                        compact=state["compact"],
                        expanded=state["expanded"],
                        expand_ac=state.get("expand_ac", False),
                        action_prefix="spec",
                        show_buttons=False,
                    ),
                )
                _send_spec_message(card_content, msg_type, is_update=False, throttle=False)
                sender.current_message_id = None

        def on_review_done(cycle_num: int, review: ReviewResult):
            self.update_ui_state(spec_project_id, view_mode="review_done", view_context={"cycle_num": cycle_num})

            content = reporter.format_review_result(review, cycle_num)
            title = reporter.get_review_title(cycle_num, review.all_passed)

            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            sp = engine.project if (engine and engine.project) else None
            progress_bar = None
            status_line = None
            duration_line = None
            criteria_section = None
            if sp:
                progress_bar = self._generate_progress_bar(sp.satisfied_count, sp.total_criteria)
                title = append_duration_to_title(title, sp.duration())
                status_line = reporter.format_status_line(sp)
                duration_line = reporter.format_duration_line(sp)
                criteria_section = reporter.format_criteria_section(sp)

            state = self.get_ui_state(spec_project_id)
            if sp and criteria_section:
                criteria_section = self._render_collapsible_section(
                    criteria_section,
                    total_items=sp.total_criteria,
                    expanded=state.get("expand_ac", False),
                    completed_count=sp.satisfied_count,
                )
            msg_type, card_content = CardBuilder.build_engine_card(
                project=project,
                state=EngineCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    is_executing=True,
                    engine_name=f"Spec({engine_name})",
                    status_line=status_line,
                    duration_line=duration_line,
                    criteria_section=criteria_section,
                    project_id=sp.project_id if sp else (project.project_id if project else None),
                    engine_project_id=spec_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="spec",
                ),
            )
            # Review done is significant, immediate flush
            _send_spec_message(card_content, msg_type, is_update=True, throttle=False)

        def on_project_done(spec_project: SpecProject):
            self.update_ui_state(spec_project_id, view_mode="status", view_context={})

            content = reporter.format_project_done(spec_project)
            title = reporter.get_project_done_title(spec_project)
            progress_bar = self._generate_progress_bar(spec_project.satisfied_count, spec_project.total_criteria)
            duration_line = reporter.format_duration_line(spec_project)

            terminal_state = "completed" if spec_project.status.value == "completed" else "failed"

            state = self.get_ui_state(spec_project_id)
            msg_type, card_content = CardBuilder.build_engine_card(
                project=project,
                state=EngineCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    project_id=spec_project.project_id,
                    engine_name=f"Spec({engine_name})",
                    duration_line=duration_line,
                    engine_project_id=spec_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="spec",
                    terminal_state=terminal_state,
                ),
            )
            # Project done: independent message
            _send_spec_message(card_content, msg_type, is_update=False, throttle=False)
            self.handler.add_reaction(message_id, EmojiReaction.on_multi_task_done())

        def on_error(error: str):
            self.update_ui_state(spec_project_id, view_mode="error", view_context={"error": error})

            state = self.get_ui_state(spec_project_id)
            active_engine = None if project else self.ctx.spec_engine_manager.get_active_engine(chat_id)
            resolved_project_id = project.project_id if project else (
                active_engine.project.project_id if active_engine and active_engine.project else None
            )
            msg_type, card_content = self.build_error_card(
                project=project,
                engine_name=engine_name,
                error_msg=error,
                state=state,
                project_id=resolved_project_id,
                engine_project_id=spec_project_id,
                terminal_state="failed",
            )
            _send_spec_message(card_content, msg_type, is_update=True)
            self.handler.add_reaction(message_id, EmojiReaction.on_error())

        def _get_engine_and_state():
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            spec_project = engine.project if engine else None
            state = self.get_ui_state(spec_project_id)
            max_c = _max_cycles or (spec_project.cycle_count_total if spec_project else 10)
            return engine, spec_project, state, max_c

        def _build_phase_card(
            title: str, content: str, spec_project, state: dict, *, show_buttons: bool = False, throttle: bool = True,
            footer_status: Optional[str] = None,
        ):
            progress_bar = None
            status_line = None
            duration_line = None
            if spec_project:
                progress_bar = self._generate_progress_bar(spec_project.satisfied_count, spec_project.total_criteria)
                status_line = reporter.format_status_line(spec_project)
                duration_line = reporter.format_duration_line(spec_project)

            msg_type, card_content = CardBuilder.build_engine_card(
                project=project,
                state=EngineCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    is_executing=True,
                    engine_name=f"Spec({engine_name})",
                    status_line=status_line,
                    duration_line=duration_line,
                    project_id=spec_project.project_id if spec_project else (project.project_id if project else None),
                    engine_project_id=spec_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="spec",
                    show_buttons=show_buttons,
                    footer_status=footer_status,
                ),
            )
            _send_spec_message(card_content, msg_type, is_update=True, throttle=throttle)

        def on_phase_start(cycle_num: int, phase: SpecPhase):
            _, spec_project, state, max_c = _get_engine_and_state()
            content = reporter.format_phase_start_content(cycle_num, phase, max_c)
            title = reporter.get_cycle_start_title(cycle_num, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(title, content, spec_project, state, show_buttons=False, throttle=True, footer_status="tool_running")

        def on_phase_done(cycle_num: int, phase: SpecPhase, output: str):
            _, spec_project, state, max_c = _get_engine_and_state()
            content = reporter.format_phase_done_content(cycle_num, phase, max_c, output)
            title = reporter.get_cycle_start_title(cycle_num, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(title, content, spec_project, state, show_buttons=False, throttle=True)

        return SpecEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_cycle_start=on_cycle_start,
            on_phase_start=on_phase_start,
            on_phase_done=on_phase_done,
            on_cycle_done=on_cycle_done,
            on_review_done=on_review_done,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def render_current_view(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        if project is None:
            project = self.handler.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.handler.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)

        spec_project_id = project.project_id if project else root_path
        state = self.get_ui_state(spec_project_id)

        view_mode = state.get("view_mode", "status")
        view_context = state.get("view_context", {})

        if not engine or not engine.project:
            running = self.ctx.spec_engine_manager.get_active_engines(chat_id)
            if len(running) == 1 and running[0].project:
                engine = running[0]
            else:
                engine_name = self.handler.get_engine_name(
                    chat_id, project_id=(project.project_id if project else None)
                )
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    state=EngineCardState(
                        title="📊 Spec 状态",
                        content="当前没有 Spec 任务\n\n发送 `/spec 你的需求` 开始结构化开发闭环",
                        engine_name=f"Spec({engine_name})",
                        show_buttons=False,
                    ),
                )
                self.handler.reply_message(message_id, card_content, msg_type=msg_type)
                return

        if view_mode == "status":
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)
        elif view_mode == "cycle_done":
            cycle_num = view_context.get("cycle_num")
            self._render_cycle_view(message_id, chat_id, project, engine, state, cycle_num, origin_message_id)
        elif view_mode == "review_done":
            cycle_num = view_context.get("cycle_num")
            self._render_review_view(message_id, chat_id, project, engine, state, cycle_num, origin_message_id)
        elif view_mode == "error":
            error_msg = view_context.get("error", "未知错误")
            self._render_error_view(message_id, chat_id, project, engine, state, error_msg, origin_message_id)
        else:
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)

    def _render_status_view(self, message_id: str, chat_id: str, project, engine, state, origin_message_id):
        reporter = self.ctx.spec_reporter
        engine_name = engine.engine_name

        status_content = reporter.format_status(engine.project)
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(engine.project)

        progress_bar = self._generate_progress_bar(progress_info["satisfied_count"], progress_info["total_criteria"])

        warning_banner = self._check_warning_banner(
            engine.project.duration(),
            is_executing=progress_info["is_running"],
        )

        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title=status_title,
                content=status_content,
                progress_bar=progress_bar,
                project_id=project.project_id if project else engine.project.project_id,
                is_executing=progress_info["is_running"],
                is_paused=progress_info["is_paused"],
                engine_name=f"Spec({engine_name})",
                engine_project_id=project.project_id if project else engine.project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                expand_ac=state.get("expand_ac", False),
                action_prefix="spec",
                warning_banner=warning_banner,
            ),
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def _render_cycle_view(self, message_id: str, chat_id: str, project, engine, state, cycle_num, origin_message_id):
        reporter = self.ctx.spec_reporter
        engine_name = engine.engine_name
        spec_project = engine.project

        cycle = next((c for c in spec_project.cycles if c.cycle_number == cycle_num), None)
        if not cycle:
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)
            return

        content = reporter.format_cycle_done(cycle_num, cycle)
        title = reporter.get_cycle_done_title(cycle_num, cycle.status == "completed")
        title = append_duration_to_title(title, spec_project.duration())
        progress_bar = self._generate_progress_bar(spec_project.satisfied_count, spec_project.total_criteria)
        status_line = reporter.format_status_line(spec_project)
        duration_line = reporter.format_duration_line(spec_project)
        criteria_section = reporter.format_criteria_section(spec_project)

        criteria_section = self._render_collapsible_section(
            criteria_section,
            total_items=spec_project.total_criteria,
            expanded=state.get("expand_ac", False),
            completed_count=spec_project.satisfied_count,
        )

        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title=title,
                content=content,
                progress_bar=progress_bar,
                is_executing=True,
                engine_name=f"Spec({engine_name})",
                status_line=status_line,
                duration_line=duration_line,
                criteria_section=criteria_section,
                project_id=project.project_id if project else spec_project.project_id,
                engine_project_id=project.project_id if project else spec_project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                action_prefix="spec",
            ),
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def _render_review_view(self, message_id: str, chat_id: str, project, engine, state, cycle_num, origin_message_id):
        reporter = self.ctx.spec_reporter
        engine_name = engine.engine_name
        spec_project = engine.project

        cycle = next((c for c in spec_project.cycles if c.cycle_number == cycle_num), None)
        if not cycle or not cycle.review_result:
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)
            return

        content = reporter.format_review_result(cycle.review_result, cycle_num)
        title = reporter.get_review_title(cycle_num, cycle.review_result.all_passed)
        title = append_duration_to_title(title, spec_project.duration())
        progress_bar = self._generate_progress_bar(spec_project.satisfied_count, spec_project.total_criteria)
        status_line = reporter.format_status_line(spec_project)
        duration_line = reporter.format_duration_line(spec_project)
        criteria_section = reporter.format_criteria_section(spec_project)

        criteria_section = self._render_collapsible_section(
            criteria_section,
            total_items=spec_project.total_criteria,
            expanded=state.get("expand_ac", False),
            completed_count=spec_project.satisfied_count,
        )

        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title=title,
                content=content,
                progress_bar=progress_bar,
                is_executing=True,
                engine_name=f"Spec({engine_name})",
                status_line=status_line,
                duration_line=duration_line,
                criteria_section=criteria_section,
                project_id=project.project_id if project else spec_project.project_id,
                engine_project_id=project.project_id if project else spec_project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                expand_ac=state.get("expand_ac", False),
                action_prefix="spec",
            ),
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def build_error_card(
        self,
        *,
        project,
        engine_name: str,
        error_msg: str,
        state: Optional[dict] = None,
        project_id: Optional[str] = None,
        engine_project_id: Optional[str] = None,
        footer_note: Optional[str] = None,
        terminal_state: Optional[str] = None,
    ) -> tuple[str, str]:
        reporter = self.ctx.spec_reporter
        ui_state = state or self.get_default_ui_state()
        resolved_project_id = project.project_id if project else project_id
        resolved_engine_project_id = engine_project_id or resolved_project_id

        if not isinstance(resolved_project_id, (str, int)):
            resolved_project_id = None
        if not isinstance(resolved_engine_project_id, (str, int)):
            resolved_engine_project_id = resolved_project_id

        content = reporter.format_error(error_msg)
        title = reporter.get_error_title()

        saved_task_id = None
        try:
            m = re.search(r"task_id=([a-zA-Z0-9_\-]+)", str(error_msg or ""))
            saved_task_id = m.group(1) if m else None
        except Exception:
            saved_task_id = None

        extra_buttons = None
        if saved_task_id:
            extra_buttons = [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔁 重试"},
                    "type": "primary",
                    "value": {
                        "action": "spec_retry",
                        "task_id": saved_task_id,
                        "project_id": resolved_project_id or resolved_engine_project_id,
                        "deep_project_id": resolved_engine_project_id,
                    },
                }
            ]

        return CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title=title,
                content=content,
                project_id=resolved_project_id,
                engine_name=f"Spec({engine_name})",
                show_buttons=True,
                engine_project_id=resolved_engine_project_id,
                compact=ui_state["compact"],
                expanded=ui_state["expanded"],
                action_prefix="spec",
                extra_buttons=extra_buttons,
                footer_note=footer_note,
                terminal_state=terminal_state,
            ),
        )

    def _render_error_view(self, message_id: str, chat_id: str, project, engine, state, error_msg, origin_message_id):
        msg_type, card_content = self.build_error_card(
            project=project,
            engine_name=engine.engine_name,
            error_msg=error_msg,
            state=state,
            project_id=project.project_id if project else engine.project.project_id,
            engine_project_id=project.project_id if project else engine.project.root_path,
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)
