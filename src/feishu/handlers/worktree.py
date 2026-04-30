"""Worktree handler — parallel multi-tool worktree execution flow.

Extracted from ``SystemHandler`` to reduce god-class complexity.
All worktree card actions and command handlers live here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...card.styles import UI_TEXT
from ...worktree_engine.models import WorktreeUnitStatus, truncate_goal
from ...repo_lock import LockConflictError
from ...utils.errors import get_error_detail
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class WorktreeHandler(BaseHandler):
    """Parallel multi-tool worktree execution flow."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _worktree_manager(self):
        """Lazy-init WorktreeManager instance."""
        mgr = getattr(self, "_wt_manager", None)
        if mgr is None:
            from ...worktree_engine.manager import WorktreeManager

            mgr = WorktreeManager(self.project_manager)
            self._wt_manager = mgr
        return mgr

    def _get_available_worktree_tools(self) -> list[dict]:
        """Helper to fetch available worktree tools for card builders.

        Wrapped as an instance method so tests can easily monkeypatch
        the tool list without touching the underlying WorktreeManager
        implementation.
        """
        mgr = self._worktree_manager()
        return mgr.get_available_tools()

    def _get_ttadk_worktree_tools(self) -> list[dict]:
        mgr = self._worktree_manager()
        return mgr.get_ttadk_tools()

    def _get_models_for_tool(
        self,
        tool_name: str,
        provider: str = "ttadk",
        cwd: Optional[str] = None,
        current_model: Optional[str] = None,
    ) -> list[dict]:
        """Helper to fetch models for a given worktree tool.

        This thin wrapper around ``WorktreeManager.get_models_for_tool``
        exists primarily to make unit tests easier to stub without
        depending on the manager's internal behaviour.
        """
        mgr = self._worktree_manager()
        return mgr.get_models_for_tool(
            tool_name, provider=provider, cwd=cwd, current_model=current_model
        )

    @staticmethod
    def _resolve_worktree_goal(value: dict, state=None) -> str:
        """Unify goal resolution from card value + state fallback.

        Priority: value["worktree_goal"] > value["_input_value"] > state.selection.pending_goal
        """
        return str(
            value.get("worktree_goal")
            or value.get("_input_value")
            or (state.selection.pending_goal if state else "")
            or ""
        ).strip()

    def _make_throttled_progress_callback(
        self,
        mgr,
        project,
        progress_mid: str,
        goal: str,
        *,
        silent_mode: bool = False,
        clock=None,
    ):
        """Build a throttled on_unit_update closure for live progress updates.

        Args:
            clock: Injectable time source (default: time.time). Used for testability.
        """
        if clock is None:
            clock = time.time
        _update_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        _last_update: list[float] = [0.0]
        _THROTTLE_INTERVAL = 30.0 if silent_mode else 0.5
        _exec_start = clock()
        _TIMEOUT_NOTIFY = 600.0
        _timeout_notified: list[bool] = [False]
        pid = project.project_id

        def _on_unit_update(unit):
            now = clock()
            elapsed = now - _exec_start
            with _update_lock:
                if silent_mode and elapsed >= _TIMEOUT_NOTIFY and not _timeout_notified[0]:
                    _timeout_notified[0] = True
                    _last_update[0] = now
                    try:
                        cur_state = mgr.get_state(project)
                        cur_dicts = [u.to_dict() for u in cur_state.units]
                        msg = UI_TEXT["worktree_still_running"].format(
                            elapsed=int(elapsed // 60)
                        )
                        mt, cd = CardBuilder.build_worktree_progress_card(
                            cur_dicts, pid, message=msg,
                        )
                        if progress_mid:
                            self.patch_message(progress_mid, cd, msg_type=mt)
                    except Exception:
                        logger.debug("worktree progress update failed", exc_info=True)
                    return

                if now - _last_update[0] < _THROTTLE_INTERVAL:
                    return
                _last_update[0] = now
            try:
                cur_state = mgr.get_state(project)
                cur_dicts = [u.to_dict() for u in cur_state.units]
                msg = UI_TEXT["worktree_executing_live"].format(goal=truncate_goal(goal))
                mt, cd = CardBuilder.build_worktree_progress_card(
                    cur_dicts, pid, message=msg,
                )
                if progress_mid:
                    self.patch_message(progress_mid, cd, msg_type=mt)
            except Exception:
                logger.debug("worktree progress update failed", exc_info=True)

        return _on_unit_update

    # ------------------------------------------------------------------
    # Public command / card-action handlers
    # ------------------------------------------------------------------

    def handle_worktree_prefix_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Parse '/wt <goal>' or '/worktree <goal>' and delegate to handle_worktree_command."""
        text_lower = text.lower().strip()
        if text_lower.startswith("/worktree"):
            goal = text[len("/worktree"):].strip()
        else:
            goal = text[len("/wt"):].strip()
        self.handle_worktree_command(message_id, chat_id, project, goal=goal)

    def handle_worktree_command(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        from_card: bool = False,
        goal: str = "",
    ):
        """Handle /wt or /worktree command — start tool selection flow."""
        project = project or self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_no_active_project"])
            return

        goal = str(goal or "").strip()[:500]  # cap at 500 chars

        mgr = self._worktree_manager()
        state = mgr.start_selection(project, goal=goal)
        if goal:
            mgr.apply_journey_event(state, event="goal_created", goal=goal)
        tools = self._get_available_worktree_tools()
        if not tools:
            self.reply_error(message_id, UI_TEXT["system_worktree_no_available_tools"])
            return

        project_id = project.project_id if project else None
        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        msg_type, card = CardBuilder.build_worktree_tool_select_card(
            tools, selected_dicts, project_id,
        )
        if from_card:
            self.patch_message(message_id, card, msg_type=msg_type)
        else:
            self.reply_message(message_id, card, msg_type=msg_type)

    def handle_worktree_select_tool(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: user selected a tool from the worktree tool list."""
        value = value or {}
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        tool_name = value.get("_option") or value.get("tool_name", "")
        provider = value.get("provider", "")
        supports_model = value.get("supports_model", False)
        skip_model_selection = value.get("skip_model_selection", False)

        if not tool_name:
            self.reply_error(message_id, UI_TEXT["system_worktree_select_tool_error"])
            return

        mgr = self._worktree_manager()
        state = mgr.get_state(project)
        if provider == "ttadk" and tool_name == "ttadk":
            pid = project.project_id
            selected_dicts = [item.to_dict() for item in state.selection.selected_items]
            msg_type, card = CardBuilder.build_worktree_ttadk_tool_select_card(
                self._get_ttadk_worktree_tools(),
                selected_dicts,
                pid,
            )
            self.patch_message(message_id, card, msg_type=msg_type)
            return

        from ...worktree_engine.selection import WorktreeToolOption

        option = WorktreeToolOption(
            provider=provider,
            tool_name=tool_name,
            display_name=value.get("display_name") or tool_name,
            supports_model=bool(supports_model),
            model_optional=True,
            skip_model_selection=bool(skip_model_selection),
        )

        mgr.select_tool(project, option)
        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        pid = project.project_id

        should_skip_model = not option.supports_model
        models = []

        if option.supports_model:
            cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)
            current_model = None
            if project and getattr(project, "acp_tool_name", "") == tool_name:
                current_model = getattr(project, "acp_model_name", None)

            models = self._get_models_for_tool(
                tool_name, provider=provider, cwd=cwd, current_model=current_model
            )
            if len(models) <= 1 or option.skip_model_selection:
                should_skip_model = True

        if not should_skip_model:
            msg_type, card = CardBuilder.build_worktree_model_select_card(
                models,
                option.display_name,
                selected_dicts,
                pid,
                message=UI_TEXT["system_worktree_selection_finished_banner"].format(tool=option.display_name),
            )
            self.patch_message(message_id, card, msg_type=msg_type)
        else:
            model_name = None
            model_display = None
            if models:
                target = next((m for m in models if m.get("is_default")), models[0])
                model_name = target["name"]
                model_display = target.get("display_name")

            state, _, msg = mgr.add_pending_item(
                project, model_name=model_name, model_display_name=model_display
            )
            mgr.back_to_tool_selection(project)

            state = mgr.get_state(project)
            tools = self._get_available_worktree_tools()
            selected_dicts = [item.to_dict() for item in state.selection.selected_items]
            msg_type, card = CardBuilder.build_worktree_tool_select_card(
                tools,
                selected_dicts,
                pid,
                message=msg,
            )
            self.patch_message(message_id, card, msg_type=msg_type)

    def handle_worktree_select_model(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: user selected a model for the pending tool."""
        value = value or {}
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        model_name = value.get("_option") or value.get("model_name") or None
        model_display = value.get("model_display_name") or model_name

        mgr = self._worktree_manager()

        state = mgr.get_state(project)
        pending_tool = state.selection.pending_item

        state, added, msg = mgr.add_pending_item(project, model_name=model_name, model_display_name=model_display)
        mgr.back_to_tool_selection(project)

        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        tools = self._get_available_worktree_tools()
        pid = project.project_id
        msg_type, card = CardBuilder.build_worktree_tool_select_card(
            tools, selected_dicts, pid, message=msg,
        )
        self.patch_message(message_id, card, msg_type=msg_type)

    def handle_finish_worktree_selection(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: user finished selecting tools — show confirm card or auto-execute."""
        value = value or {}
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()

        state = mgr.get_state(project)
        goal = self._resolve_worktree_goal(value, state)

        state = mgr.finalize_selection(project)
        pid = project.project_id

        if not state.enabled:
            self.reply_error(message_id, UI_TEXT["system_worktree_no_selection_error"])
            return

        if goal:
            self.patch_message(
                message_id,
                CardBuilder.build_worktree_confirm_card(
                    [item.to_dict() for item in state.selection.selected_items],
                    pid,
                    message=UI_TEXT["worktree_auto_executing_banner"],
                    goal=goal,
                )[1]
            )
            self._auto_execute_worktree(message_id, chat_id, goal, project=project)
            return

        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        msg_type, card = CardBuilder.build_worktree_confirm_card(selected_dicts, pid)
        self.patch_message(message_id, card, msg_type=msg_type)

    def _auto_execute_worktree(
        self,
        message_id: str,
        chat_id: str,
        goal: str,
        project: Optional["ProjectContext"] = None,
    ):
        """Fast path: ensure_worktrees -> set units ready -> execute with silent_mode."""
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state = mgr.ensure_worktrees(project)

        if state.last_error:
            error_msg = UI_TEXT["system_worktree_create_failed"].format(error=state.last_error)
            pid = project.project_id
            units_dicts = [u.to_dict() for u in state.units] if state.units else []
            if units_dicts:
                _, error_card = CardBuilder.build_worktree_progress_card(
                    units_dicts, pid, message=error_msg,
                )
                self.patch_message(message_id, error_card)
            self.reply_error(message_id, error_msg)
            return

        mgr.apply_journey_event(state, event="auto_execute_started", goal=goal, silent_mode=True)

        mgr.mark_units_ready(project)

        self.handle_worktree_execute(message_id, chat_id, goal, project=project, silent_mode=True)

    def handle_worktree_confirm_start(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: user confirmed selections — create worktrees and await goal."""
        value = value or {}
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state = mgr.ensure_worktrees(project)

        if state.last_error:
            self.reply_error(message_id, UI_TEXT["system_worktree_create_failed"].format(error=state.last_error))
            return

        mgr.mark_units_ready(project)

        state = mgr.get_state(project)
        goal = self._resolve_worktree_goal(value, state)

        if goal:
            mgr.apply_journey_event(state, event="auto_execute_started", goal=goal, silent_mode=False)
            self.patch_message(
                message_id,
                CardBuilder.build_worktree_confirm_card(
                    [item.to_dict() for item in mgr.get_state(project).selection.selected_items],
                    project.project_id,
                    message=UI_TEXT["worktree_auto_executing_banner"],
                    goal=goal,
                )[1]
            )
            self.handle_worktree_execute(message_id, chat_id, goal, project=project)
            return

        units_dicts = [u.to_dict() for u in state.units]
        pid = project.project_id
        ready_msg = UI_TEXT["system_worktree_created_prompt"] + "\n" + UI_TEXT["worktree_ready_intercept_hint"]
        msg_type, card = CardBuilder.build_worktree_progress_card(
            units_dicts, pid, message=ready_msg,
        )
        self.patch_message(message_id, card, msg_type=msg_type)

    def handle_worktree_execute(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional["ProjectContext"] = None,
        silent_mode: bool = False,
    ):
        """Route user message as a worktree goal — trigger parallel execution."""
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        goal = str(text or "").strip()
        if not goal:
            self.reply_message(message_id, UI_TEXT["system_worktree_goal_required"])
            return

        mgr = self._worktree_manager()
        pid = project.project_id

        state = mgr.get_state(project)
        units_dicts = [u.to_dict() for u in state.units]
        if silent_mode:
            init_msg = UI_TEXT["worktree_start_silent"].format(
                goal=truncate_goal(goal)
            )
        else:
            init_msg = UI_TEXT["worktree_executing"].format(goal=truncate_goal(goal))
        msg_type, card = CardBuilder.build_worktree_progress_card(
            units_dicts, pid, message=init_msg,
        )
        progress_mid = self.send_message(chat_id, card, msg_type=msg_type)

        _on_unit_update = self._make_throttled_progress_callback(
            mgr, project, progress_mid, goal, silent_mode=silent_mode,
        )

        root_path = getattr(project, "root_path", None)

        def _locked_body():
            return mgr.execute_goal(project, goal, on_unit_update=_on_unit_update)

        try:
            state = self._with_repo_lock(root_path, chat_id, _locked_body)
        except LockConflictError as e:
            self.send_lock_conflict_card(e, message_id, goal)
            return

        if state.last_error:
            self.reply_error(message_id, state.last_error)
            return

        final_dicts = [u.to_dict() for u in state.units]
        if state.merge_entry_ready:
            msg_type, card = CardBuilder.build_worktree_cleanup_card(
                state.merge_notes, pid, state.base_branch or "main",
                units=final_dicts,
            )
        else:
            msg_type, card = CardBuilder.build_worktree_progress_card(
                final_dicts, pid, message=UI_TEXT["worktree_completed_no_change"],
            )
        if progress_mid:
            self.patch_message(progress_mid, card, msg_type=msg_type)
        else:
            self.reply_message(message_id, card, msg_type=msg_type)

    def handle_worktree_merge(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: merge all worktree branches back to base."""
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        root_path = getattr(project, "root_path", None)

        def _locked_merge():
            return mgr.merge_to_base(project)
        try:
            state, merge_results = self._with_repo_lock(root_path, chat_id, _locked_merge)
        except LockConflictError as e:
            self.send_lock_conflict_card(e, message_id, "")
            return

        if state.last_error:
            self.reply_error(message_id, state.last_error)
            return

        pid = project.project_id
        msg_type, card = CardBuilder.build_worktree_cleanup_card(
            state.merge_notes, pid, state.base_branch or "main", merge_results=merge_results,
        )
        self.patch_message(message_id, card, msg_type=msg_type)

    def handle_show_worktree_merge_entry(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: show the merge entry card with pending integration items."""
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state = mgr.get_state(project)

        if not state.merge_notes:
            self.reply_error(message_id, "当前无待合并内容")
            return

        pid = project.project_id
        msg_type, card = CardBuilder.build_worktree_merge_entry_card(
            state.merge_notes, pid, state.base_branch or "main",
        )
        self.patch_message(message_id, card, msg_type=msg_type)

    def handle_worktree_cleanup(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: remove all worktrees and branches."""
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        root_path = getattr(project, "root_path", None)
        force = bool((value or {}).get("force", False))

        def _locked_cleanup():
            return mgr.cleanup_worktrees(project, force=force)
        try:
            state, warnings = self._with_repo_lock(root_path, chat_id, _locked_cleanup)
        except LockConflictError as e:
            self.send_lock_conflict_card(e, message_id, "")
            return
        if warnings and not force:
            details = "\n".join(
                f"- {'未提交变更' if w.has_uncommitted else ''}"
                f"{'、' if w.has_uncommitted and w.has_unmerged else ''}"
                f"{'未合并分支 ' + w.unmerged_branch if w.has_unmerged else ''}"
                for w in warnings
            )
            self.reply_message(
                message_id,
                UI_TEXT["system_worktree_cleanup_warnings"].format(details=details),
            )
        else:
            self.reply_message(message_id, UI_TEXT["system_worktree_cleanup_success"])

    def handle_worktree_execute_action(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: execute goal from progress card input."""
        value = value or {}
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        goal = self._resolve_worktree_goal(value)
        if not goal:
            self.reply_message(message_id, UI_TEXT["system_worktree_goal_required"])
            return

        self.handle_worktree_execute(message_id, chat_id, goal, project=project)

    def handle_worktree_retry_failed(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: retry only the failed worktree units."""
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        pid = project.project_id
        state = mgr.get_state(project)

        if any(u.status == WorktreeUnitStatus.RUNNING for u in state.units):
            self.reply_message(message_id, UI_TEXT["system_worktree_unit_running_error"])
            return

        units_dicts = [u.to_dict() for u in state.units]
        msg_type, card = CardBuilder.build_worktree_progress_card(
            units_dicts, pid, message=UI_TEXT["system_worktree_retry_starting"],
        )
        progress_mid = self.send_message(chat_id, card, msg_type=msg_type)

        retry_goal = state.journey.goal or UI_TEXT["system_worktree_retry_goal"]
        _on_unit_update = self._make_throttled_progress_callback(
            mgr, project, progress_mid, retry_goal,
        )

        state = mgr.retry_failed_units(project, on_unit_update=_on_unit_update)

        if state.last_error:
            self.reply_error(message_id, state.last_error)
            return

        final_dicts = [u.to_dict() for u in state.units]
        if state.merge_entry_ready:
            msg_type, card = CardBuilder.build_worktree_cleanup_card(
                state.merge_notes, pid, state.base_branch or "main",
                units=final_dicts,
            )
        else:
            msg_type, card = CardBuilder.build_worktree_progress_card(
                final_dicts, pid, message=UI_TEXT["system_worktree_retry_completed"],
            )
        if progress_mid:
            self.patch_message(progress_mid, card, msg_type=msg_type)
        else:
            self.reply_message(message_id, card, msg_type=msg_type)
