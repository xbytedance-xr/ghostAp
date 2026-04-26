"""Diagnostics handler — task board, context diff report, message trace, unified status."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...card.styles import UI_TEXT
from ...project import ContextEntryType, context_helper
from ...tasking import TaskPriority, TaskSpec
from ...utils.text import format_duration
from ...utils.errors import get_error_detail
from .base import BaseHandler
from .diagnostics_helper import DiagnosticsHelper

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class DiagnosticsHandler(BaseHandler):
    """Task board, context diff reports, and message tracing."""

    # ------------------------------------------------------------------
    # Task board
    # ------------------------------------------------------------------
    def show_task_board(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip().lower()
        except Exception:
            arg = ""

        try:
            mode_display = self.mode_manager.get_mode_display_name(chat_id)
        except Exception:
            mode_display = ""

        if arg in ("all", "-a", "--all"):
            tasks = self.scheduler.list_tasks(chat_id=chat_id, include_done=False, limit=50)
            groups: dict[str, list] = {}
            for st in tasks:
                pid = st.spec.project_id or ""
                groups.setdefault(pid, []).append(st)

            content = CardBuilder.build_task_board_content(
                tasks, mode_display, groups=groups, project_manager=self.project_manager
            )

            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=None,
                title=UI_TEXT["diag_task_board_title"],
                content=content,
                working_dir=self.get_working_dir(chat_id),
                show_buttons=True,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        # Default: current project
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        if not project:
            self.reply_message(
                message_id,
                UI_TEXT["diag_no_active_project_tasks"]
            )
            return

        tasks = self.scheduler.list_tasks(chat_id=chat_id, project_id=project.project_id, include_done=False, limit=30)
        content = CardBuilder.build_task_board_content(tasks, mode_display)

        msg_type, card_content = CardBuilder.build_project_response_card(
            project,
            UI_TEXT["diag_task_board_title"],
            content,
            show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    # ------------------------------------------------------------------
    # Unified status — /status [task_id|all]
    # ------------------------------------------------------------------

    def _build_lock_status_lines(self, chat_id: str, project: Optional["ProjectContext"] = None, is_admin: bool = False) -> str:
        """Build a Markdown snippet summarizing lock state as an independent section.

        Always shows subsystem enablement status.  Active lock details are
        appended when present.
        """
        from datetime import datetime
        from ...card.builders.lock import format_elapsed_ago, format_lock_duration
        from ...card.styles import UI_TEXT

        parts: list[str] = []

        chat_lock_mgr = getattr(self.ctx, "chat_lock_manager", None)
        repo_lock_mgr_obj = getattr(self.ctx, "repo_lock_manager", None)

        # --- Chat lock detail (only when active) ---
        if chat_lock_mgr is not None:
            lock_entry = chat_lock_mgr.get_lock_info(chat_id)
            if lock_entry:
                display_name = lock_entry.locked_by_name or (
                    lock_entry.locked_by[:8] + "..." if len(lock_entry.locked_by) > 8 else lock_entry.locked_by
                )
                try:
                    lock_dt = datetime.fromtimestamp(lock_entry.locked_at_wall)
                    if lock_dt.date() == datetime.now().date():
                        lock_time_str = lock_dt.strftime("%H:%M")
                    else:
                        lock_time_str = lock_dt.strftime("%m-%d %H:%M")
                except Exception:
                    lock_time_str = UI_TEXT["lock_status_time_unknown"]
                _chat_lock_line = UI_TEXT["lock_status_chat_locked"].format(name=display_name, time=lock_time_str)
                # Append duration on a new indented sub-line for visual clarity
                _chat_lock_line += f"\n  {format_lock_duration(lock_entry.locked_at)}"
                # Append auto-unlock countdown on its own sub-line (hours/minutes mixed format)
                try:
                    _max_dur = getattr(chat_lock_mgr, "_max_duration", 86400)
                    _remaining = max(0, _max_dur - (time.monotonic() - lock_entry.locked_at))
                    if _remaining > 3600:
                        _rh = int(_remaining // 3600)
                        _rm = int((_remaining % 3600) // 60)
                        _countdown_str = f"约 {_rh} 小时 {_rm} 分钟" if _rm else f"约 {_rh} 小时"
                    elif _remaining > 60:
                        _countdown_str = f"约 {int(_remaining // 60)} 分钟"
                    else:
                        _countdown_str = None
                    if _countdown_str:
                        _chat_lock_line += f"\n  ⏰ 预计 {_countdown_str}后自动释放"
                    else:
                        _chat_lock_line += f"\n  {UI_TEXT['lock_status_release_imminent'].strip()}"
                except Exception:
                    pass
                if is_admin:
                    _chat_lock_line += UI_TEXT["lock_status_admin_unlock_hint"]
                parts.append(_chat_lock_line)

        # --- Repo lock detail (only when active) ---
        root_path = getattr(project, "root_path", None) if project else None
        if root_path and repo_lock_mgr_obj is not None:
            try:
                info = repo_lock_mgr_obj.get_lock_info(root_path)
                if info:
                    from pathlib import Path
                    repo_name = Path(root_path).name or root_path
                    holder_display = UI_TEXT["lock_status_holder_self"] if info.chat_id == chat_id else UI_TEXT["lock_status_holder_other"]
                    duration = format_elapsed_ago(time.monotonic() - info.acquired_at)
                    # Calculate remaining auto-release time
                    try:
                        from ...config import get_settings
                        idle_timeout = get_settings().repo_lock_idle_timeout
                    except Exception:
                        idle_timeout = 300
                    remaining_secs = max(0, idle_timeout - info.idle_seconds)
                    remaining_min = int(remaining_secs // 60)
                    if remaining_min > 0:
                        release_hint = UI_TEXT["lock_status_release_countdown"].format(minutes=remaining_min)
                    else:
                        release_hint = UI_TEXT["lock_status_release_imminent"]
                    _repo_lock_line = UI_TEXT["lock_status_repo_locked"].format(
                            repo_name=repo_name, holder=holder_display,
                            duration=duration, release_hint=release_hint,
                        )
                    if is_admin and info.chat_id != chat_id:
                        _repo_lock_line += UI_TEXT["lock_status_admin_force_release_hint"]
                    elif not is_admin:
                        _repo_lock_line += UI_TEXT["lock_status_nonadmin_repo_hint"]
                    parts.append(_repo_lock_line)
            except Exception:
                pass

        # When lock subsystem is enabled but no active locks, show "unlocked" status.
        _lock_enabled = chat_lock_mgr is not None or repo_lock_mgr_obj is not None
        if not parts:
            if _lock_enabled:
                return UI_TEXT["lock_status_section_header"] + UI_TEXT["lock_status_no_active_lock"]
            return ""
        return UI_TEXT["lock_status_section_header"] + "\n\n".join(parts)

    def show_unified_status(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        """Show unified status across all engine types (Deep/Loop/Spec).

        - /status          → list running/paused engine tasks for current chat
        - /status all      → include completed tasks
        - /status <task_id> → detailed status for a specific task
        """
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        # /status <task_id> — specific task lookup
        if arg and arg.lower() not in ("all", "-a", "--all"):
            self._show_task_detail(message_id, chat_id, arg, project)
            return

        include_done = arg.lower() in ("all", "-a", "--all") if arg else False

        # Collect engines across all three types
        entries = DiagnosticsHelper.get_all_engine_statuses(self.ctx, chat_id, include_done=include_done)

        project_name = project.project_name if project else ""
        content = CardBuilder.build_unified_status_content(entries, include_done, project_name)

        # Determine admin status for lock hints
        _is_admin = False
        _chat_lock_mgr = getattr(self.ctx, "chat_lock_manager", None)
        if _chat_lock_mgr is not None:
            from ...thread import get_current_sender_id
            _sender = get_current_sender_id() or ""
            if _sender:
                _is_admin = _chat_lock_mgr.is_admin(_sender)

        # Append lock status section
        lock_lines = self._build_lock_status_lines(chat_id, project, is_admin=_is_admin)
        if lock_lines:
            content += "\n\n" + lock_lines

        msg_type, card_content = CardBuilder.build_smart_response_card(
            project=project,
            title=UI_TEXT["diag_unified_status_title"],
            content=content,
            working_dir=self.get_working_dir(chat_id),
            show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def _show_task_detail(
        self, message_id: str, chat_id: str, task_id: str, project: Optional["ProjectContext"] = None
    ):
        """Show detailed status for a specific task by task_id."""
        # Search across all engine managers
        for engine in self.ctx.deep_engine_manager.list_engines(chat_id):
            if (
                engine.project
                and engine.project.task_id
                and (engine.project.task_id == task_id or task_id in engine.project.task_id)
            ):
                content = self.ctx.progress_reporter.format_status(engine.project)
                title = UI_TEXT["diag_task_detail_deep_title"]
                engine_name = engine.engine_name
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title=title,
                    content=content,
                    engine_name=engine_name,
                    show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        for engine in self.ctx.loop_engine_manager.list_engines(chat_id):
            if (
                engine.project
                and engine.project.task_id
                and (engine.project.task_id == task_id or task_id in engine.project.task_id)
            ):
                content = self.ctx.loop_reporter.format_status(engine.project)
                title = UI_TEXT["diag_task_detail_loop_title"]
                engine_name = engine.engine_name
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title=title,
                    content=content,
                    engine_name=f"Loop({engine_name})",
                    show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        for engine in self.ctx.spec_engine_manager.list_engines(chat_id):
            if (
                engine.project
                and engine.project.task_id
                and (engine.project.task_id == task_id or task_id in engine.project.task_id)
            ):
                content = self.ctx.spec_reporter.format_status(engine.project)
                title = UI_TEXT["diag_task_detail_spec_title"]
                engine_name = engine.engine_name
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title=title,
                    content=content,
                    engine_name=f"Spec({engine_name})",
                    show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        # Also check scheduler by task_id
        state = self.scheduler.get_state_by_task_id(task_id, chat_id=chat_id)
        if state:
            content = CardBuilder.build_task_detail_content(state)
            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=project,
                title=UI_TEXT["diag_task_detail_title"],
                content=content,
                working_dir=self.get_working_dir(chat_id),
                show_buttons=False,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        self.reply_message(
            message_id,
            UI_TEXT["diag_task_not_found"].format(
                id=task_id
            ),
        )

    # ------------------------------------------------------------------
    # Context diff
    # ------------------------------------------------------------------
    def show_context_diff(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        self._submit_diff_report(message_id, chat_id, text, project)

    def _build_context_diff_report(
        self, chat_id: str, text: str, project: "ProjectContext"
    ) -> tuple[bool, str, Optional[str]]:
        ctx = self.context_manager.store.get(project.project_id)
        if not ctx:
            return (
                False,
                "",
                UI_TEXT["diag_diff_no_record"],
            )

        versions = list(ctx.versions)
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        ok, from_vnum, to_vnum, show_current, error_key = context_helper.resolve_diff_range(arg, versions)
        if not ok:
            return False, "", UI_TEXT[error_key]

        from_v, to_v, entries = context_helper.filter_context_entries(ctx, from_vnum, to_vnum, show_current)

        if not from_v:
            return (
                False,
                "",
                UI_TEXT["diag_diff_version_not_found"].format(
                    vnum=from_vnum, total=len(versions)
                ),
            )
        if to_vnum is not None and not to_v:
            return (
                False,
                "",
                UI_TEXT["diag_diff_version_not_found"].format(
                    vnum=to_vnum, total=len(versions)
                ),
            )

        content = CardBuilder.build_diff_report_content(project, from_v, to_v, entries, show_current)
        return True, content, None

    def _submit_diff_report(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_message(
                message_id,
                UI_TEXT["diag_diff_no_active_project"]
            )
            return

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=project.project_id)
        streaming_manager = self.get_streaming_manager()
        ref_note = self.format_ref_note(message_id, request_id)
        
        banner_tpl = UI_TEXT["diag_diff_generating_banner"]
        initial = f"{banner_tpl}\n\n{ref_note}" if ref_note else banner_tpl

        card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project.project_name,
            project_path=project.root_path,
            project_id=project.project_id,
            initial_content=initial,
            is_coco_mode=False,
            is_claude_mode=False,
            reply_to_message_id=message_id,
        )
        card_message_id = streaming_manager.send_streaming_card(card) if card else None
        if card_message_id:
            try:
                self.register_message_project(card_message_id, project)
            except Exception:
                pass
            try:
                self.ctx.message_linker.register_origin(
                    message_id, request_id=request_id, chat_id=chat_id, project_id=project.project_id
                )
                self.ctx.message_linker.link_reply(message_id, card_message_id)
            except Exception:
                pass

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:diff:{project.project_id}",
            name="diff_report",
            task_type="diff_report",
            project_id=project.project_id,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            priority=TaskPriority.NORMAL,
        )

        def _run(task_ctx):
            try:
                try:
                    full_ref = self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)
                    if card and card_message_id and full_ref:
                        streaming_manager.update_content(card, f"{banner_tpl}\n\n{full_ref}")
                except Exception:
                    pass

                task_ctx.progress(UI_TEXT["diag_step_parsing"], 5)
                if card and card_message_id:
                    try:
                        progress_tpl = UI_TEXT["diag_diff_generating_progress"]
                        streaming_manager.update_content(
                            card,
                            f"{progress_tpl.format(step=UI_TEXT['diag_step_parsing'], pct=5)}\n\n{self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)}",
                        )
                    except Exception:
                        pass

                ok, content, err = self._build_context_diff_report(chat_id, text, project)
                if not ok:
                    msg = err or UI_TEXT["diag_diff_failed"]
                    final_ref = self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)
                    final = f"❌ {msg}\n\n{final_ref}" if final_ref else f"❌ {msg}"
                    if card and card_message_id:
                        streaming_manager.close_streaming(card, final_content=final)
                    else:
                        self.reply_message(message_id, msg, origin_message_id=message_id, request_id=request_id)
                    return

                task_ctx.progress(UI_TEXT["diag_step_generating"], 80)
                if card and card_message_id:
                    try:
                        progress_tpl = UI_TEXT["diag_diff_generating_progress"]
                        streaming_manager.update_content(
                            card,
                            f"{progress_tpl.format(step=UI_TEXT['diag_step_generating'], pct=80)}\n\n{self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)}",
                        )
                    except Exception:
                        pass

                final_ref = self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)
                final = f"{content}\n\n{final_ref}" if final_ref and final_ref not in content else content
                if card and card_message_id:
                    streaming_manager.close_streaming(card, final_content=final)
                else:
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project,
                        UI_TEXT["diag_diff_report_title"],
                        final,
                        show_buttons=False,
                        footer=UI_TEXT["diag_diff_usage_footer"],
                    )
                    rid = self.reply_message_with_id(
                        message_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id
                    )
                    if rid:
                        self.register_message_project(rid, project)
                task_ctx.progress(UI_TEXT["diag_step_completed"], 100)
            except Exception as e:
                msg = UI_TEXT["diag_diff_exception"].format(error=get_error_detail(e))
                final_ref = self.format_ref_note(message_id, request_id, run_id=getattr(task_ctx, "run_id", None))
                final = f"❌ {msg}\n\n{final_ref}" if final_ref else f"❌ {msg}"
                try:
                    if card and card_message_id:
                        streaming_manager.close_streaming(card, final_content=final)
                except Exception:
                    pass
                self.reply_message(message_id, msg, origin_message_id=message_id, request_id=request_id)

        handle = self.scheduler.submit(spec, _run)
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception:
            pass

        if card and card_message_id:
            try:
                full_ref = self.format_ref_note(message_id, request_id, run_id=handle.run_id)
                msg = UI_TEXT["diag_diff_started_banner"]
                streaming_manager.update_content(card, f"{msg}\n\n{full_ref}")
            except Exception:
                pass
        return handle

    # ------------------------------------------------------------------
    # Message trace
    # ------------------------------------------------------------------
    def show_message_trace(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        key = arg or message_id
        data = None
        try:
            data = self.ctx.message_linker.query(key)
        except Exception:
            data = None

        if not data:
            self.reply_message(
                message_id,
                UI_TEXT["diag_trace_not_found"].format(key=key)
            )
            return

        # Chat-level isolation: reject trace data belonging to a different chat
        trace_chat_id = data.get("chat_id")
        if trace_chat_id and trace_chat_id != chat_id:
            self.reply_message(
                message_id,
                UI_TEXT["diag_trace_not_found"].format(key=key)
            )
            return

        proj_id = data.get("project_id")
        if project is None and proj_id:
            try:
                project = self.project_manager.get_project_for_chat(proj_id, chat_id)
            except Exception:
                project = None

        content = CardBuilder.build_message_trace_content(data)
        title = UI_TEXT["diag_trace_title"]
        footer = UI_TEXT["diag_trace_usage_footer"]

        if project:
            msg_type, card_content = CardBuilder.build_project_response_card(
                project,
                title,
                content,
                show_buttons=False,
                footer=footer,
            )
        else:
            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=None,
                title=title,
                content=content,
                working_dir=self.get_working_dir(chat_id),
                show_buttons=False,
            )
        self.reply_message(message_id, card_content, msg_type=msg_type)
