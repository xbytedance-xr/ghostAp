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
from ...card.events.worktree import (
    worktree_cleanup,
    worktree_completed_no_change,
    worktree_confirm,
    worktree_merge,
    worktree_progress,
    worktree_tool_select,
)
from ...card.ui_text import UI_TEXT
from ...model_selection import DEFAULT_MODEL_OPTION_VALUE, is_default_model_option
from ...worktree_engine.models import WorktreeUnitStatus, truncate_goal
from ...repo_lock import LockConflictError
from ...utils.errors import get_error_detail
from ..slash_command_parser import CommandMatch, SlashCommandParser
from .base import BaseHandler

if TYPE_CHECKING:
    from ...card.protocols import RendererProtocol
    from ...card.session import CardSession
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


def _worktree_iteration_count(state) -> int:
    value = getattr(state, "iteration_count", 0)
    return value if isinstance(value, int) else 0


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
    def _normalize_ttadk_tool_option(tool: dict) -> dict:
        """Normalize TTADK subtool choices to agent + tool + model semantics."""
        item = dict(tool or {})
        item["provider"] = "ttadk"
        item["agent_name"] = item.get("agent_name") or "ttadk"
        display_name = str(item.get("display_name") or item.get("tool_name") or item.get("name") or "").strip()
        prefix = "TTADK · "
        if display_name.startswith(prefix):
            display_name = display_name[len(prefix):].strip()
        if display_name:
            item["display_name"] = display_name
        return item

    @staticmethod
    def _resolve_worktree_goal(value: dict, state=None) -> str:
        """Unify goal resolution from card value + state fallback."""
        return str(
            value.get("worktree_goal")
            or value.get("_input_value")
            or (state.selection.pending_goal if state else "")
            or ""
        ).strip()

    def _ensure_worktree_topic_context(
        self,
        *,
        message_id: str,
        chat_id: str,
        project: "ProjectContext",
    ) -> str:
        from ...thread import get_current_thread_id, get_thread_manager, set_current_thread_id

        thread_root_id = get_current_thread_id() or message_id
        mgr = get_thread_manager()
        ctx = mgr.get(thread_root_id)
        if not ctx:
            mgr.bind_engine(
                thread_root_id=thread_root_id,
                chat_id=chat_id,
                project_id=project.project_id,
                mode="worktree",
            )
        elif ctx.mode != "worktree":
            mgr.bind_engine(
                thread_root_id=ctx.thread_root_id,
                chat_id=ctx.chat_id,
                project_id=ctx.project_id,
                mode="worktree",
                tool_name=ctx.tool_name,
                model_name=ctx.model_name,
            )
            thread_root_id = ctx.thread_root_id
        set_current_thread_id(thread_root_id)
        return thread_root_id

    def _worktree_thread_root_id(self, project: "ProjectContext") -> str:
        try:
            return self._worktree_manager().get_session_key(project).thread_root_id
        except Exception:
            from ...thread import get_current_thread_id

            return get_current_thread_id() or ""

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
                            session.dispatch(worktree_progress(
                                cur_dicts, project_id=pid, message=msg,
                                iteration=_worktree_iteration_count(cur_state),
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
                    session.dispatch(worktree_progress(
                        cur_dicts, project_id=pid, message=msg,
                        iteration=_worktree_iteration_count(cur_state),
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
        """Backward-compatible entry for '/wt <goal>' and '/worktree <goal>'.

        Do not parse via ``startswith()+切片``: defer to the shared
        :class:`~src.feishu.slash_command_parser.SlashCommandParser`.
        """
        m = SlashCommandParser.parse(text)
        if not m or m.command not in {"/worktree", "/wt"}:
            # Not a worktree slash command; fall back to the legacy behavior (no goal).
            self.handle_worktree_command(message_id, chat_id, project, goal="")
            return
        self.handle_worktree_command_match(message_id, chat_id, m, project=project)

    def handle_worktree_command_match(
        self,
        message_id: str,
        chat_id: str,
        command_match: CommandMatch,
        *,
        project: Optional["ProjectContext"] = None,
        from_card: bool = False,
    ) -> None:
        """Primary entry for worktree slash commands.

        全链路只消费 CommandMatch（单一事实源），避免 handler 内二次 parse。
        """
        goal = (
            (command_match.args or "").strip()
            if command_match.command in {"/worktree", "/wt"}
            else ""
        )
        self.handle_worktree_command(message_id, chat_id, project, from_card=from_card, goal=goal)

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
        thread_root_id = self._ensure_worktree_topic_context(
            message_id=message_id,
            chat_id=chat_id,
            project=project,
        )

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
        session.dispatch(worktree_tool_select(
            tools=tools, selected=selected_dicts, project_id=project_id, thread_root_id=thread_root_id,
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
        thread_root_id = self._worktree_thread_root_id(project)

        if provider == "ttadk" and tool_name == "ttadk":
            selected_dicts = [item.to_dict() for item in state.selection.selected_items]
            ttadk_tools = [self._normalize_ttadk_tool_option(t) for t in self._get_ttadk_worktree_tools()]
            # Show TTADK tool selection via session dispatch
            session = self._get_or_create_session(chat_id, pid)
            session.dispatch(worktree_tool_select(
                tools=ttadk_tools, selected=selected_dicts, project_id=pid,
                message="请选择 TTADK 工具",
                thread_root_id=thread_root_id,
            ))
            return

        from ...worktree_engine.selection import WorktreeToolOption

        option = WorktreeToolOption(
            provider=provider,
            tool_name=tool_name,
            display_name=value.get("display_name") or tool_name,
            agent_name=value.get("agent_name") or "",
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
            # Worktree 强调"工具 × 模型"组合，必须让用户显式选模型；只有完全拿不到
            # 模型列表时才回退为"默认模型"直接添加。len==1 也仍展示模型卡，方便
            # 后续运行环境扩充模型时无需改 UI 路径。
            if not models or option.skip_model_selection:
                should_skip_model = True

        if not should_skip_model:
            # Show model selection as tool_select with model list
            session = self._get_or_create_session(chat_id, pid)
            model_tools = []
            model_tools.append({
                "id": DEFAULT_MODEL_OPTION_VALUE,
                "name": UI_TEXT["system_acp_default_model_option"],
                "description": UI_TEXT["system_acp_default_model_desc"],
                "use_default_model": True,
            })
            for m in models:
                model_id = str(m.get("name") or "").strip()
                if not model_id:
                    continue
                display = str(m.get("display_name") or model_id).strip() or model_id
                # Description carries ACP-side metadata (quota, load, ...).
                # Truncate so it stays a single tidy line under the model name
                # instead of dwarfing the row.
                blurb = str(m.get("description") or "").strip()
                if blurb and blurb != display:
                    if len(blurb) > 60:
                        blurb = blurb[:60].rstrip() + "…"
                else:
                    blurb = ""
                model_tools.append({
                    "id": model_id,
                    "name": display,
                    "description": blurb,
                })
            session.dispatch(worktree_tool_select(
                tools=model_tools, selected=selected_dicts, project_id=pid,
                message=UI_TEXT["system_worktree_select_model_prompt"].format(tool=option.display_name),
                select_action="worktree_select_model",
                pending_tool=option.display_name,
                thread_root_id=thread_root_id,
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
            session.dispatch(worktree_tool_select(
                tools=tools, selected=selected_dicts, project_id=pid,
                message=msg,
                thread_root_id=thread_root_id,
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

        raw_model_name = (
            value.get("_option")
            or value.get("model_name")
            or value.get("id")
            or value.get("name")
            or value.get("tool_name")
            or None
        )
        use_default_model = bool(value.get("use_default_model")) or is_default_model_option(raw_model_name)
        model_name = None if use_default_model else raw_model_name
        model_display = (
            None
            if use_default_model
            else (
                value.get("model_display_name")
                or value.get("display_name")
                or value.get("name")
                or model_name
            )
        )

        mgr = self._worktree_manager()

        state = mgr.get_state(project)

        state, added, msg = mgr.add_pending_item(project, model_name=model_name, model_display_name=model_display)
        mgr.back_to_tool_selection(project)

        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        tools = self._get_available_worktree_tools()
        pid = project.project_id
        thread_root_id = self._worktree_thread_root_id(project)
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(worktree_tool_select(
            tools=tools, selected=selected_dicts, project_id=pid, message=msg,
            thread_root_id=thread_root_id,
        ))

    def handle_worktree_remove_item(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: remove a single already-selected item from the list."""
        value = value or {}
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        selection_key = str(value.get("selection_key") or value.get("_option") or "").strip()
        mgr = self._worktree_manager()
        _, _, msg = mgr.remove_selected_item(project, selection_key)

        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        pid = project.project_id
        thread_root_id = self._worktree_thread_root_id(project)
        tools = self._get_available_worktree_tools()
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(worktree_tool_select(
            tools=tools, selected=selected_dicts, project_id=pid, message=msg,
            thread_root_id=thread_root_id,
        ))

    def handle_worktree_clear_items(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: clear all selected items and return to tool selection."""
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        _, _, msg = mgr.clear_selected_items(project)

        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        pid = project.project_id
        thread_root_id = self._worktree_thread_root_id(project)
        tools = self._get_available_worktree_tools()
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(worktree_tool_select(
            tools=tools, selected=selected_dicts, project_id=pid, message=msg,
            thread_root_id=thread_root_id,
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
        thread_root_id = self._worktree_thread_root_id(project)

        if not state.enabled:
            self.reply_error(message_id, UI_TEXT["system_worktree_no_selection_error"])
            return

        session = self._get_or_create_session(chat_id, pid)

        if goal:
            session.dispatch(worktree_confirm(
                selected_items=[item.to_dict() for item in state.selection.selected_items],
                project_id=pid,
                message=UI_TEXT["worktree_auto_executing_banner"],
                goal=goal,
                thread_root_id=thread_root_id,
            ))
            self._auto_execute_worktree(message_id, chat_id, goal, project=project)
            return

        mgr.apply_journey_event(state, event="awaiting_goal")
        ready_msg = UI_TEXT["system_worktree_created_prompt"] + "\n" + UI_TEXT["worktree_ready_intercept_hint"]
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        session.dispatch(worktree_confirm(
            selected_items=selected_dicts, project_id=pid, message=ready_msg,
            thread_root_id=thread_root_id,
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
        thread_root_id = self._worktree_thread_root_id(project)

        if state.last_error:
            error_msg = UI_TEXT["system_worktree_create_failed"].format(error=state.last_error)
            pid = project.project_id
            units_dicts = [u.to_dict() for u in state.units] if state.units else []
            if units_dicts:
                session = self._get_or_create_session(chat_id, pid)
                session.dispatch(worktree_progress(
                    units_dicts, pid, message=error_msg, thread_root_id=thread_root_id,
                ))
            self.reply_error(message_id, error_msg)
            return

        mgr.apply_journey_event(state, event="auto_execute_started", goal=goal, silent_mode=True)

        mgr.mark_units_ready(project)

        self.handle_worktree_execute(message_id, chat_id, goal, project=project, silent_mode=True)

    @staticmethod
    def _merge_results_allow_cleanup(merge_results: list[dict]) -> bool:
        return bool(merge_results) and all(bool(result.get("success")) for result in merge_results)

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
        thread_root_id = self._worktree_thread_root_id(project)

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
            session.dispatch(worktree_confirm(
                selected_items=[item.to_dict() for item in mgr.get_state(project).selection.selected_items],
                project_id=pid,
                message=UI_TEXT["worktree_auto_executing_banner"],
                goal=goal,
                thread_root_id=thread_root_id,
            ))
            self.handle_worktree_execute(message_id, chat_id, goal, project=project)
            return

        units_dicts = [u.to_dict() for u in state.units]
        ready_msg = UI_TEXT["system_worktree_created_prompt"] + "\n" + UI_TEXT["worktree_ready_intercept_hint"]
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(worktree_progress(
            units_dicts, project_id=pid, message=ready_msg,
            thread_root_id=thread_root_id,
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
        thread_root_id = self._worktree_thread_root_id(project)

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
        session.dispatch(worktree_progress(
            units_dicts, project_id=pid, message=init_msg,
            silent=silent_mode,
            iteration=_worktree_iteration_count(state) + 1,
            thread_root_id=thread_root_id,
        ))

        _on_unit_update = self._make_throttled_progress_callback(
            mgr, project, "", goal, silent_mode=silent_mode, session=session,
        )

        root_path = getattr(project, "root_path", None)

        def _locked_body():
            state = mgr.execute_goal(project, goal, on_unit_update=_on_unit_update)
            final_units = [u.to_dict() for u in state.units]
            merge_notes = list(state.merge_notes)
            base_branch = state.base_branch or "main"
            merge_results: list[dict] = []
            cleanup_warnings = []
            cleaned = False

            if not state.last_error and state.merge_entry_ready:
                state, merge_results = mgr.merge_to_base(project)
                if self._merge_results_allow_cleanup(merge_results):
                    state, cleanup_warnings = mgr.cleanup_worktrees(project, force=True)
                    cleaned = not cleanup_warnings

            return state, final_units, merge_notes, base_branch, merge_results, cleanup_warnings, cleaned

        try:
            state, final_dicts, merge_notes, base_branch, merge_results, cleanup_warnings, cleaned = self._with_repo_lock(root_path, chat_id, _locked_body)
        except LockConflictError as e:
            # Rich lock-conflict display with context info
            error_detail = get_error_detail(e)
            lock_msg = UI_TEXT["system_worktree_lock_conflict"].format(error_detail=error_detail)
            session.dispatch(CardEvent.failed(error=lock_msg))
            return

        if state.last_error:
            session.dispatch(CardEvent.failed(error=state.last_error))
            return

        if merge_results:
            cleanup_phase = "completed" if cleaned else "actions"
            session.dispatch(worktree_cleanup(
                merge_notes, base_branch=base_branch,
                merge_results=merge_results,
                project_id=pid, units=final_dicts,
                cleanup_phase=cleanup_phase,
                thread_root_id=thread_root_id,
            ))
            if cleanup_warnings:
                details = "\n".join(
                    f"- {'未提交变更' if w.has_uncommitted else ''}"
                    f"{'、' if w.has_uncommitted and w.has_unmerged else ''}"
                    f"{'未合并分支 ' + w.unmerged_branch if w.has_unmerged else ''}"
                    for w in cleanup_warnings
                )
                self.reply_text(
                    message_id,
                    UI_TEXT["system_worktree_cleanup_warnings"].format(details=details),
                )
            elif cleaned:
                self._close_session(pid)
        else:
            session.dispatch(worktree_completed_no_change(
                final_dicts, project_id=pid,
                message=UI_TEXT["worktree_completed_no_change"],
                iteration=_worktree_iteration_count(state),
                thread_root_id=thread_root_id,
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
        thread_root_id = self._worktree_thread_root_id(project)
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

        merge_notes = list(state.merge_notes)
        base_branch = state.base_branch or "main"
        cleanup_warnings = []
        cleaned = False
        if self._merge_results_allow_cleanup(merge_results):
            def _locked_cleanup():
                return mgr.cleanup_worktrees(project, force=True)
            try:
                state, cleanup_warnings = self._with_repo_lock(root_path, chat_id, _locked_cleanup)
                cleaned = not cleanup_warnings
            except LockConflictError as e:
                self.send_lock_conflict_card(e, message_id, "")
                return

        # Use CardSession for cleanup card
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(worktree_cleanup(
            merge_notes, base_branch=base_branch,
            merge_results=[r if isinstance(r, dict) else {"branch": str(r), "success": True} for r in (merge_results or [])],
            project_id=pid,
            cleanup_phase="completed" if cleaned else "actions",
            thread_root_id=thread_root_id,
        ))
        if cleanup_warnings:
            details = "\n".join(
                f"- {'未提交变更' if w.has_uncommitted else ''}"
                f"{'、' if w.has_uncommitted and w.has_unmerged else ''}"
                f"{'未合并分支 ' + w.unmerged_branch if w.has_unmerged else ''}"
                for w in cleanup_warnings
            )
            self.reply_text(
                message_id,
                UI_TEXT["system_worktree_cleanup_warnings"].format(details=details),
            )
        elif cleaned:
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
        thread_root_id = self._worktree_thread_root_id(project)

        if not state.merge_notes:
            self.reply_error(message_id, UI_TEXT["system_worktree_no_merge_content"])
            return

        pid = project.project_id
        session = self._get_or_create_session(chat_id, pid)
        session.dispatch(worktree_merge(
            merge_notes=state.merge_notes,
            base_branch=state.base_branch or "main",
            project_id=pid,
            thread_root_id=thread_root_id,
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
        thread_root_id = self._worktree_thread_root_id(project)

        if any(u.status == WorktreeUnitStatus.RUNNING for u in state.units):
            self.reply_text(message_id, UI_TEXT["system_worktree_unit_running_error"])
            return

        units_dicts = [u.to_dict() for u in state.units]

        # Use CardSession for retry progress
        session = self._get_or_create_session(chat_id, pid, reply_to=message_id)
        session.dispatch(CardEvent.started())
        session.dispatch(worktree_progress(
            units_dicts, project_id=pid, message=UI_TEXT["system_worktree_retry_starting"],
            iteration=_worktree_iteration_count(state) + 1,
            thread_root_id=thread_root_id,
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
            session.dispatch(worktree_cleanup(
                state.merge_notes, base_branch=state.base_branch or "main",
                project_id=pid, units=final_dicts,
                thread_root_id=thread_root_id,
            ))
        else:
            session.dispatch(worktree_progress(
                final_dicts, project_id=pid,
                message=UI_TEXT["system_worktree_retry_completed"],
                iteration=_worktree_iteration_count(state),
                thread_root_id=thread_root_id,
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
