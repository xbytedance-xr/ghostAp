from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder, EngineCardState
from ...acp import ACPEventRenderer, ACPEventType
from ...card.styles import UI_TEXT
from ...spec_engine import SpecEngineCallbacks
from ...spec_engine.models import (
    ReviewResult,
    SpecCycle,
    SpecPhase,
    SpecProject,
)
from ...spec_engine.retry_status import RetryEvent, RetryStatus
from ...utils.text import append_duration_to_title
from ..emoji import EmojiReaction
from .base import BaseRenderer, _StreamThrottle, _create_direct_session

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
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"],
        engine_name: str = "Coco", model_name: str = "",
    ) -> SpecEngineCallbacks:
        request_id = self.handler.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        reporter = self.ctx.spec_reporter

        # Mutable container: session can be replaced on cycle boundaries
        _session = [_create_direct_session(self.handler, chat_id, message_id)]
        _throttle = _StreamThrottle(self.settings)

        spec_project_id = project.project_id if project else self.handler.get_working_dir(chat_id)

        # Build subtitle: "🔧 Coco · gpt-4" or "🔧 Coco"
        _subtitle = f"🔧 {engine_name} · {model_name}" if model_name else f"🔧 {engine_name}"

        _max_cycles = 0

        # ACP event renderer for real-time tool call display
        acp_renderer = ACPEventRenderer()
        _footer_status: list[Optional[str]] = [None]
        _last_phase_content: list[str] = [""]

        def _send_spec_message(card_content: str, msg_type: str = "interactive", new_card: bool = False):
            """Send spec message. If new_card=True, close current session and create a new one."""
            if new_card:
                _session[0].close()
                _session[0] = _create_direct_session(self.handler, chat_id, message_id)
            card_content = self._check_and_truncate_payload(card_content)
            _session[0].send(card_content)

        def on_analyzing_done(spec_project: SpecProject):
            self.update_ui_state(spec_project_id, view_mode="status", view_context={})

            content = reporter.format_analyzing_done(spec_project)
            title = reporter.get_analyzing_done_title()
            msg_type, card_content = CardBuilder.build_engine_card(
                project=project,
                state=EngineCardState(
                    title=title,
                    subtitle=_subtitle,
                    content=content,
                    engine_name=f"Spec({engine_name})",
                    show_buttons=False,
                ),
            )
            # Immediate flush
            _send_spec_message(card_content, msg_type)

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
                    subtitle=_subtitle,
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
            _send_spec_message(card_content, msg_type)

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
                        subtitle=_subtitle,
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
                _send_spec_message(card_content, msg_type, new_card=True)

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
                    subtitle=_subtitle,
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
            _send_spec_message(card_content, msg_type)

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
                    subtitle=_subtitle,
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
            _send_spec_message(card_content, msg_type, new_card=True)
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
            _send_spec_message(card_content, msg_type)
            self.handler.add_reaction(message_id, EmojiReaction.on_error())

        def _get_engine_and_state():
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            spec_project = engine.project if engine else None
            state = self.get_ui_state(spec_project_id)
            max_c = _max_cycles or (spec_project.cycle_count_total if spec_project else 10)
            return engine, spec_project, state, max_c

        def _build_phase_card(
            title: str, content: str, spec_project, state: dict, *, show_buttons: bool = True,
            footer_status: Optional[str] = None, extra_buttons: Optional[list] = None,
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
                    subtitle=_subtitle,
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
                    extra_buttons=extra_buttons,
                ),
            )
            _send_spec_message(card_content, msg_type)

        def on_phase_start(cycle_num: int, phase: SpecPhase):
            # Reset renderer for new phase
            acp_renderer.reset()
            _footer_status[0] = "tool_running"

            _, spec_project, state, max_c = _get_engine_and_state()
            content = reporter.format_phase_start_content(cycle_num, phase, max_c)
            _last_phase_content[0] = content
            title = reporter.get_cycle_start_title(cycle_num, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(title, content, spec_project, state, footer_status="tool_running")

        def on_phase_event(cycle_num: int, phase: SpecPhase, event):
            """Real-time ACP event processing — renders tool call details into phase card."""
            acp_renderer.process_event(event)

            # Track footer_status
            if event.event_type == ACPEventType.THOUGHT_CHUNK:
                _footer_status[0] = "thinking"
            elif event.event_type in (ACPEventType.TOOL_CALL_START, ACPEventType.TOOL_CALL_UPDATE):
                _footer_status[0] = "tool_running"
            elif event.event_type == ACPEventType.TEXT_CHUNK:
                _footer_status[0] = None

            # Only trigger card update on meaningful events (throttled)
            if event.event_type not in (
                ACPEventType.TOOL_CALL_DONE,
                ACPEventType.PLAN_UPDATE,
            ):
                return

            tool_summary = acp_renderer.render_summary()
            if not _throttle.check_throttle(len(tool_summary), force=False, min_interval=2.0, min_new_chars=10):
                return

            _, spec_project, state, max_c = _get_engine_and_state()
            base_content = reporter.format_phase_start_content(cycle_num, phase, max_c)

            # Append tool call summary
            if tool_summary:
                base_content += f"\n---\n{tool_summary}"

            _last_phase_content[0] = base_content
            title = reporter.get_cycle_start_title(cycle_num, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(
                title, base_content, spec_project, state,
                footer_status=_footer_status[0],
            )
            _throttle.update_stream_state(len(tool_summary))

        def on_phase_done(cycle_num: int, phase: SpecPhase, output: str):
            _, spec_project, state, max_c = _get_engine_and_state()
            content = reporter.format_phase_done_content(cycle_num, phase, max_c, output)

            # Append tool call summary from this phase
            tool_summary = acp_renderer.render_summary()
            if tool_summary:
                content += f"\n---\n{tool_summary}"
            _footer_status[0] = None
            _last_phase_content[0] = content

            title = reporter.get_cycle_start_title(cycle_num, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(title, content, spec_project, state)

        # RetryStatus → UI_TEXT key mapping for review retry
        _RETRY_STATUS_TEXT: dict[RetryStatus, str] = {
            RetryStatus.WAITING: "retry_waiting",
            RetryStatus.EXECUTING: "retry_executing",
            RetryStatus.EXHAUSTED: "retry_exhausted",
            RetryStatus.NO_RETRY: "retry_no_retry",
        }

        def on_phase_retry(attempt: int, max_attempts: int, detail: str):
            """Push phase-level retry status (ACP call retry) to card."""
            _, spec_project, state, max_c = _get_engine_and_state()
            retry_text = UI_TEXT["phase_retry_progress"].format(attempt=attempt, max_attempts=max_attempts)
            if detail:
                retry_text += f" — {detail[:80]}"
            _footer_status[0] = retry_text
            # Use cycle_start_title (current cycle) as the card title during phase retry
            cycle_num = state.get("current_cycle", 1) if state else 1
            title = reporter.get_cycle_start_title(cycle_num, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(
                title, _last_phase_content[0], spec_project, state,
                footer_status=retry_text,
            )

        def _make_retry_button(text: str, action: str) -> dict:
            """Create a small inline button for retry card actions."""
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": text},
                "type": "default",
                "size": "small",
                "behaviors": [{"type": "callback", "value": {"action": action}}],
            }

        def on_review_retry(cycle: int, event: RetryEvent):
            """Push review-level retry status to card footer."""
            # SUCCEEDED: don't push card — let subsequent phase flow naturally override
            if event.status == RetryStatus.SUCCEEDED:
                return
            _, spec_project, state, max_c = _get_engine_and_state()
            # Map RetryStatus → formatted UI text
            text_key = _RETRY_STATUS_TEXT.get(event.status, "retry_executing")
            if event.status == RetryStatus.WAITING:
                detail_msg = UI_TEXT[text_key].format(sec=int(event.delay_sec), i=event.attempt, n=event.max_attempts)
            elif event.status == RetryStatus.EXECUTING:
                detail_msg = UI_TEXT[text_key].format(i=event.attempt, n=event.max_attempts)
            elif event.status == RetryStatus.EXHAUSTED:
                detail_msg = UI_TEXT[text_key].format(n=event.max_attempts)
            elif event.status == RetryStatus.NO_RETRY:
                # Distinguish: config disabled (max_attempts==0) vs budget exhausted
                if event.max_attempts == 0:
                    detail_msg = UI_TEXT["retry_no_retry_disabled"]
                else:
                    detail_msg = UI_TEXT["retry_no_retry_budget"]
            else:
                detail_msg = UI_TEXT[text_key]
            _footer_status[0] = detail_msg

            # Build inline buttons based on retry state
            buttons = None
            if event.status in (RetryStatus.WAITING, RetryStatus.EXECUTING):
                buttons = [
                    _make_retry_button(UI_TEXT["btn_stop_review"], "spec_stop"),
                    _make_retry_button(UI_TEXT["btn_skip_retry"], "spec_skip_retry"),
                ]
            elif event.status in (RetryStatus.EXHAUSTED, RetryStatus.NO_RETRY):
                buttons = [_make_retry_button(UI_TEXT["btn_continue"], "spec_resume")]

            title = reporter.get_cycle_start_title(cycle, max_c)
            title = append_duration_to_title(title, spec_project.duration() if spec_project else None)
            _build_phase_card(
                title, _last_phase_content[0], spec_project, state,
                footer_status=detail_msg,
                extra_buttons=buttons,
            )

        return SpecEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_cycle_start=on_cycle_start,
            on_phase_start=on_phase_start,
            on_phase_event=on_phase_event,
            on_phase_done=on_phase_done,
            on_cycle_done=on_cycle_done,
            on_review_done=on_review_done,
            on_project_done=on_project_done,
            on_error=on_error,
            on_phase_retry=on_phase_retry,
            on_review_retry=on_review_retry,
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
                self.handler.reply_card(message_id, card_content)
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
