"""Worktree handler — parallel multi-tool worktree execution flow.

Extracted from ``SystemHandler`` to reduce god-class complexity.
All worktree card actions and command handlers live here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from ...card.events import CardEvent, CardEventType
from ...card.ui_text import UI_TEXT
from ...worktree_engine.models import WorktreeUnitStatus, truncate_goal
from ...repo_lock import LockConflictError
from ...utils.errors import get_error_detail
from .base import BaseHandler

if TYPE_CHECKING:
    from ...card.protocols import RendererProtocol
    from ...card.session import CardSession
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class WorktreeHandler(BaseHandler):
    """Parallel multi-tool worktree execution flow."""

    def __init__(self, ctx: "HandlerContext", renderer: "RendererProtocol | None" = None) -> None:
        super().__init__(ctx)
        if renderer is None:
            from ..renderers import get_renderer
            renderer = get_renderer("worktree", self)
        self._renderer = renderer

    # ------------------------------------------------------------------
    # CardSession management (delegated to WorktreeRenderer)
    # ------------------------------------------------------------------

    def _get_or_create_session(
        self, chat_id: str, project_id: str, *, reply_to: str | None = None
    ) -> "CardSession":
        """Get or create a CardSession for a worktree project."""
        return self._renderer.get_or_create_session(chat_id, project_id, reply_to=reply_to)

    def _close_session(self, project_id: str) -> None:
        """Close and remove a worktree session."""
        self._renderer.close_session(project_id)

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
        """Helper to fetch available worktree tools for card builders."""
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
        """Helper to fetch models for a given worktree tool."""
        mgr = self._worktree_manager()
        return mgr.get_models_for_tool(
            tool_name, provider=provider, cwd=cwd, current_model=current_model
        )

    @staticmethod
    def _resolve_worktree_goal(value: dict, state=None) -> str:
        """Unify goal resolution from card value + state fallback."""
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
        session: "CardSession | None" = None,
    ):
        """Build a throttled on_unit_update closure for live progress updates."""
        if clock is None:
            clock = time.time
        _update_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        _last_update: list[float] = [0.0]
        _THROTTLE_INTERVAL = 30.0 if silent_mode else 0.5
        _exec_start = clock()
        _TIMEOUT_NOTIFY = 120.0
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
                        if session:
                            session.dispatch(CardEvent.worktree_progress(
                                cur_dicts, project_id=pid, message=msg,
                            ))
                    except Exception:
                        logger.warning("worktree progress update failed", exc_info=True)
                    return

                if now - _last_update[0] < _THROTTLE_INTERVAL:
                    return
                _last_update[0] = now
            try:
                cur_state = mgr.get_state(project)
                cur_dicts = [u.to_dict() for u in cur_state.units]
                msg = UI_TEXT["worktree_executing_live"].format(goal=truncate_goal(goal))
                if session:
                    session.dispatch(CardEvent.worktree_progress(
                        cur_dicts, project_id=pid, message=msg,
                    ))
            except Exception:
                logger.warning("worktree progress update failed", exc_info=True)

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

        # Dispatch tool selection through CardSession
        session = self._get_or_create_session(chat_id, project_id, reply_to=message_id if not from_card else None)
        session.dispatch(CardEvent.worktree_tool_select(
            tools=tools, selected=selected_dicts, project_id=project_id,
        ))

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
        pid = project.project_id

        if provider == "ttadk" and tool_name == "ttadk":
            selected_dicts = [item.to_dict() for item in state.selection.selected_items]
            ttadk_tools = self._get_ttadk_worktree_tools()
            # Show TTADK tool selection via session dispatch
            session = self._get_or_create_session(chat_id, pid)
            session.dispatch(CardEvent.worktree_tool_select(
                tools=ttadk_tools, selected=selected_dicts, project_id=pid,
                message="请选择 TTADK 工具",
            ))
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
            # Show model selection as tool_select with model list
            session = self._get_or_create_session(chat_id, pid)
            model_tools = [
                {"id": m["name"], "name": m.get("display_name", m["name"]),
                 "description": f"模型: {m.get('display_name', m['name'])}"}
                for m in models
            ]
            session.dispatch(CardEvent.worktree_tool_select(
                tools=model_tools, selected=selected_dicts, project_id=pid,
                message=UI_TEXT["system_worktree_selection_finished_banner"].format(tool=option.display_name),
            ))
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
            session = self._get_or_create_session(chat_id, pid)
            session.dispatch(CardEvent.worktree_tool_select(
                tools=tools, selected=selected_dicts, project_id=pid,
                message=msg,
            ))

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

        state, added, msg = mgr.add_pending_item(project, model_name=model_name, model_display_name=model_display)
        mgr.back_to_tool_selection(project)

        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        tools = self._get_available_worktree_tools()
        pid = project.project_id
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(CardEvent.worktree_tool_select(
            tools=tools, selected=selected_dicts, project_id=pid, message=msg,
        ))

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

        session = self._get_or_create_session(chat_id, pid)

        if goal:
            session.dispatch(CardEvent.worktree_confirm(
                selected_items=[item.to_dict() for item in state.selection.selected_items],
                project_id=pid,
                message=UI_TEXT["worktree_auto_executing_banner"],
                goal=goal,
            ))
            self._auto_execute_worktree(message_id, chat_id, goal, project=project)
            return

        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        session.dispatch(CardEvent.worktree_confirm(
            selected_items=selected_dicts, project_id=pid,
        ))

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
                session = self._get_or_create_session(chat_id, pid)
                session.dispatch(CardEvent.worktree_progress(
                    units_dicts, pid, message=error_msg,
                ))
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
        pid = project.project_id

        if goal:
            mgr.apply_journey_event(state, event="auto_execute_started", goal=goal, silent_mode=False)
            session = self._get_or_create_session(chat_id, pid)
            session.dispatch(CardEvent.worktree_confirm(
                selected_items=[item.to_dict() for item in mgr.get_state(project).selection.selected_items],
                project_id=pid,
                message=UI_TEXT["worktree_auto_executing_banner"],
                goal=goal,
            ))
            self.handle_worktree_execute(message_id, chat_id, goal, project=project)
            return

        units_dicts = [u.to_dict() for u in state.units]
        ready_msg = UI_TEXT["system_worktree_created_prompt"] + "\n" + UI_TEXT["worktree_ready_intercept_hint"]
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(CardEvent.worktree_progress(
            units_dicts, project_id=pid, message=ready_msg,
        ))

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
            self.reply_text(message_id, UI_TEXT["system_worktree_goal_required"])
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

        # Create a CardSession for progress tracking
        session = self._get_or_create_session(chat_id, pid, reply_to=message_id)
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.worktree_progress(
            units_dicts, project_id=pid, message=init_msg,
            silent=silent_mode,
        ))

        _on_unit_update = self._make_throttled_progress_callback(
            mgr, project, "", goal, silent_mode=silent_mode, session=session,
        )

        root_path = getattr(project, "root_path", None)

        def _locked_body():
            return mgr.execute_goal(project, goal, on_unit_update=_on_unit_update)

        try:
            state = self._with_repo_lock(root_path, chat_id, _locked_body)
        except LockConflictError as e:
            # Rich lock-conflict display with context info
            error_detail = get_error_detail(e)
            lock_msg = UI_TEXT["system_worktree_lock_conflict"].format(error_detail=error_detail)
            session.dispatch(CardEvent.failed(error=lock_msg))
            return

        if state.last_error:
            session.dispatch(CardEvent.failed(error=state.last_error))
            return

        final_dicts = [u.to_dict() for u in state.units]
        if state.merge_entry_ready:
            session.dispatch(CardEvent.worktree_cleanup(
                state.merge_notes, base_branch=state.base_branch or "main",
                project_id=pid, units=final_dicts,
            ))
        else:
            session.dispatch(CardEvent.worktree_completed_no_change(
                final_dicts, project_id=pid,
                message=UI_TEXT["worktree_completed_no_change"],
            ))
            # Keep session open with retry button — don't close immediately

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
        pid = project.project_id
        root_path = getattr(project, "root_path", None)

        # Provide immediate feedback: show loading state
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(CardEvent.progress_updated(current=0, total=1, label=UI_TEXT["system_worktree_merging"]))

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

        # Use CardSession for cleanup card
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(CardEvent.worktree_cleanup(
            state.merge_notes, base_branch=state.base_branch or "main",
            merge_results=[r if isinstance(r, dict) else {"branch": str(r), "success": True} for r in (merge_results or [])],
            project_id=pid,
            cleanup_phase="actions",
        ))
        # Merge succeeded — close session
        self._close_session(pid)

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
            self.reply_error(message_id, UI_TEXT["system_worktree_no_merge_content"])
            return

        pid = project.project_id
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(CardEvent.worktree_merge(
            merge_notes=state.merge_notes,
            base_branch=state.base_branch or "main",
            project_id=pid,
        ))

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
            self.reply_text(
                message_id,
                UI_TEXT["system_worktree_cleanup_warnings"].format(details=details),
            )
        else:
            self.reply_text(message_id, UI_TEXT["system_worktree_cleanup_success"])
            # Cleanup succeeded — close session
            self._close_session(project.project_id)

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
            self.reply_text(message_id, UI_TEXT["system_worktree_goal_required"])
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
            self.reply_text(message_id, UI_TEXT["system_worktree_unit_running_error"])
            return

        units_dicts = [u.to_dict() for u in state.units]

        # Use CardSession for retry progress
        session = self._get_or_create_session(chat_id, pid, reply_to=message_id)
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.worktree_progress(
            units_dicts, project_id=pid, message=UI_TEXT["system_worktree_retry_starting"],
        ))

        retry_goal = state.journey.goal or UI_TEXT["system_worktree_retry_goal"]
        _on_unit_update = self._make_throttled_progress_callback(
            mgr, project, "", retry_goal, session=session,
        )

        state = mgr.retry_failed_units(project, on_unit_update=_on_unit_update)

        if state.last_error:
            session.dispatch(CardEvent.failed(error=state.last_error))
            return

        final_dicts = [u.to_dict() for u in state.units]
        if state.merge_entry_ready:
            session.dispatch(CardEvent.worktree_cleanup(
                state.merge_notes, base_branch=state.base_branch or "main",
                project_id=pid, units=final_dicts,
            ))
        else:
            session.dispatch(CardEvent.worktree_progress(
                final_dicts, project_id=pid,
                message=UI_TEXT["system_worktree_retry_completed"],
            ))
            session.dispatch(CardEvent.completed())
            self._close_session(pid)

    def handle_worktree_retry_all(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: re-execute all worktree units from scratch."""
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state = mgr.get_state(project)

        if any(u.status == WorktreeUnitStatus.RUNNING for u in state.units):
            self.reply_text(message_id, UI_TEXT["system_worktree_unit_running_error"])
            return

        # Reset all units to pending so they will be re-executed
        for unit in state.units:
            unit.status = WorktreeUnitStatus.PENDING
            unit.result = None
            unit.error = None

        goal = state.journey.goal or ""
        self.handle_worktree_execute(message_id, chat_id, goal, project=project)