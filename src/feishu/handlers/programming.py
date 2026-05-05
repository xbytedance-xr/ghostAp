"""Programming mode handlers — config-driven template for all programming modes.

The ``ProgrammingModeHandler`` captures the shared logic for Coco, Claude, Aiden,
Codex, Gemini, and TTADK modes.  Concrete subclasses declare configuration
attributes; the base class provides default implementations for all hooks.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEventRenderer
from ...acp.manager import ACPSessionManager
from ...agent_session import SyncSession
from ...card import CardBuilder
from ...card.ui_text import UI_TEXT
from ...project import ContextSourceMode
from ...utils.errors import get_error_detail, log_exception
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from ...mode import InteractionMode
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class ProgrammingModeHandler(BaseHandler):
    """Config-driven template base for all programming modes."""

    # ── Subclass MUST set these ──
    mode_name: str              # "Coco" / "Claude" / ...
    mode_emoji: str             # "🤖" / "🔮" / ...
    interaction_mode: InteractionMode
    mode_key: str               # "coco" / "claude" / ... — used for managers, project API
    context_source: ContextSourceMode

    # ── Optional overrides ──
    is_coco: bool = False
    thinking_text: str = ""

    _PROGRAMMING_MODE_KEYS = (
        (InteractionMode.COCO, "is_coco_mode", "coco"),
        (InteractionMode.CLAUDE, "is_claude_mode", "claude"),
        (InteractionMode.AIDEN, "is_aiden_mode", "aiden"),
        (InteractionMode.CODEX, "is_codex_mode", "codex"),
        (InteractionMode.GEMINI, "is_gemini_mode", "gemini"),
        (InteractionMode.TTADK, "is_ttadk_mode", "ttadk"),
    )

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_model: Optional[str] = None

    # ------------------------------------------------------------------
    # Config-driven default implementations (subclass may override)
    # ------------------------------------------------------------------
    def _get_session_manager(self) -> ACPSessionManager:
        return getattr(self.ctx, f"{self.mode_key}_manager")

    def _is_in_this_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.mode_manager.get_mode(chat_id, project_id) == self.interaction_mode

    def _is_in_opposite_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self._is_any_other_programming_mode(chat_id, project_id=project_id)

    def _exit_opposite_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, silent: bool = False):
        self._exit_other_programming_modes(message_id, chat_id, project=project, silent=silent)

    def _enter_mode_on_manager(self, chat_id: str, project_id: Optional[str] = None):
        self.mode_manager.enter_programming_mode(chat_id, self.interaction_mode, project_id=project_id)

    def _get_interaction_mode(self):
        return self.interaction_mode

    def _get_snapshot(self, project: "ProjectContext"):
        return getattr(project, f"{self.mode_key}_session_snapshot")

    def _set_mode_on_project(self, project: "ProjectContext", active: bool, session_id: str = "", count: int = 0):
        if active:
            project.set_programming_mode(self.mode_key, True, session_id, count)
        else:
            project.set_programming_mode(self.mode_key, False)

    def _update_snapshot_on_project(self, project: "ProjectContext", query: str, count: int, session_id: str = ""):
        project.update_programming_snapshot(self.mode_key, query, count, session_id)

    def _clear_snapshot_on_project(self, project: "ProjectContext"):
        setattr(project, f"{self.mode_key}_session_snapshot", None)

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        if project and getattr(project, "acp_tool_name", "") == self.mode_key:
            return getattr(project, "acp_model_name", None)
        return self._current_model

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value

    # ------------------------------------------------------------------
    # dynamic agent overrides (for TTADK, etc.)
    # ------------------------------------------------------------------
    def _get_agent_type_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _get_ttadk_tool_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _get_ttadk_model_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _uses_claude_cli(self) -> bool:
        return False

    def _deactivate_other_project_modes(self, project: Optional["ProjectContext"]) -> None:
        if not project:
            return
        current = self._get_interaction_mode()
        if current != InteractionMode.COCO:
            project.set_coco_mode(False)
        if current != InteractionMode.CLAUDE:
            project.set_claude_mode(False)
        if current != InteractionMode.AIDEN:
            project.set_aiden_mode(False)
        if current != InteractionMode.CODEX:
            project.set_codex_mode(False)
        if current != InteractionMode.GEMINI:
            project.set_gemini_mode(False)
        if current != InteractionMode.TTADK:
            project.set_ttadk_mode(False)

    def _iter_other_programming_mode_entries(self):
        current = self._get_interaction_mode()
        for mode, predicate_name, handler_key in self._PROGRAMMING_MODE_KEYS:
            if mode != current:
                yield mode, predicate_name, handler_key

    def _is_any_other_programming_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        for _mode, predicate_name, _handler_key in self._iter_other_programming_mode_entries():
            predicate = getattr(self.mode_manager, predicate_name, None)
            if callable(predicate) and predicate(chat_id, project_id=project_id):
                return True
        return False

    def _exit_other_programming_modes(self, message_id: str, chat_id: str, project: Optional["ProjectContext"], silent: bool = False):
        _pid = project.project_id if project else None
        for mode, predicate_name, handler_key in self._iter_other_programming_mode_entries():
            predicate = getattr(self.mode_manager, predicate_name, None)
            if not callable(predicate) or not predicate(chat_id, project_id=_pid):
                continue
            handler = self.get_handler(handler_key)
            if handler and handler is not self and hasattr(handler, "exit_mode"):
                handler.exit_mode(message_id, chat_id, project=project, silent=silent)

    # ------------------------------------------------------------------
    # enter_mode
    # ------------------------------------------------------------------
    def enter_mode(
        self, message_id: str, chat_id: str, silent: bool = False, project: Optional["ProjectContext"] = None,
        thread_id: Optional[str] = None,
    ):
        from ...thread import get_current_thread_id

        project_id = project.project_id if project else None

        if thread_id is None:
            thread_id = get_current_thread_id()
        _thread_enabled = self.settings.thread_programming_enabled and not thread_id

        if not thread_id and self._is_in_this_mode(chat_id, project_id=project_id):
            if not silent:
                if _thread_enabled:
                    self.reply_text(
                        message_id,
                        fmt.format_warning(
                            UI_TEXT["mode_already_in_thread_msg"].format(name=self.mode_name)
                        ),
                    )
                else:
                    info = self._get_session_manager().get_session_info(chat_id, project_id=project_id)
                    self.reply_text(
                        message_id,
                        fmt.format_warning(
                            UI_TEXT["mode_already_in_msg"].format(name=self.mode_name, info=info)
                        ),
                    )
            return

        previous_mode = self.mode_manager.get_mode(chat_id, project_id=project_id)

        if not thread_id and self._is_in_opposite_mode(chat_id, project_id=project_id):
            self._exit_opposite_mode(message_id, chat_id, project=project, silent=True)

        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("自动创建项目: %s @ %s", project.project_name, project.root_path)
                project_id = project.project_id
            except Exception as e:
                log_exception(logger, "自动创建项目失败", e)

        working_dir = self.get_working_dir(chat_id)
        cwd = project.root_path if project else working_dir

        if project:
            valid, path_msg = self.project_manager.validate_project_path(project.project_id)
            if not valid:
                if not silent:
                    self.reply_text(message_id, UI_TEXT["mode_invalid_project_path"].format(msg=path_msg))
                return

        if _thread_enabled:
            self._enter_mode_on_manager(chat_id, project_id=project_id)
            self.add_reaction(message_id, EmojiReaction.on_coco_enter())
            if project:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True)
            if not silent:
                content = UI_TEXT["mode_enter_thread_msg"].format(emoji=self.mode_emoji, name=self.mode_name)
                if self.mode_name == "TTADK":
                    content += UI_TEXT["ttadk_extra_hint"]
                if project:
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project,
                        UI_TEXT["mode_card_enter_title"].format(emoji=self.mode_emoji, name=self.mode_name),
                        content,
                        show_buttons=True,
                        footer=UI_TEXT["mode_project_dir_label"].format(path=project.root_path),
                    )
                    self.reply_card(message_id, card_content)
                else:
                    self.reply_text(message_id, content)
            if project:
                self.record_mode_transition(
                    project.project_id,
                    previous_mode,
                    self._get_interaction_mode(),
                    reason=f"enter_{self.mode_name.lower()}_mode(thread_pending)",
                    chat_id=chat_id,
                )
            return

        target_session_id = None
        snapshot = self._get_snapshot(project) if project else None
        if snapshot and snapshot.is_resumable and not thread_id:
            target_session_id = snapshot.session_id

        startup_timeout = getattr(self.settings, "acp_startup_timeout", 20)
        try:
            agent_type_override = self._get_agent_type_override(project)
            model_name = self._get_model_name_override(project)
            session = self._get_session_manager().ensure_session(
                chat_id,
                cwd=cwd,
                session_id=target_session_id,
                startup_timeout=startup_timeout,
                project_id=project_id,
                agent_type_override=agent_type_override,
                model_name=model_name,
                thread_id=thread_id,
            )
        except TimeoutError as e:
            if not silent:
                if self.mode_name == "TTADK":
                    project_id = project.project_id if project else None
                    msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(
                        UI_TEXT["mode_ttadk_startup_timeout"],
                        project_id=project_id,
                    )
                    self.reply_card(message_id, card_content)
                else:
                    self.send_error_card(
                        chat_id,
                        e,
                        title=UI_TEXT["mode_startup_timeout_title"].format(name=self.mode_name),
                        origin_message_id=message_id,
                    )
            return
        except Exception as e:
            if not silent:
                if self.mode_name == "TTADK":
                    project_id = project.project_id if project else None
                    msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(
                        UI_TEXT["mode_ttadk_unavailable"],
                        project_id=project_id,
                    )
                    self.reply_card(message_id, card_content)
                else:
                    self.send_error_card(
                        chat_id,
                        e,
                        title=UI_TEXT["mode_startup_fail_title"].format(name=self.mode_name),
                        origin_message_id=message_id,
                    )
            return

        try:
            if (
                agent_type_override
                and str(agent_type_override).lower().startswith("ttadk_")
                and getattr(session, "_degraded_to", "")
            ):
                degraded_to = getattr(session, "_degraded_to", "")
                reason = getattr(session, "_degraded_reason", "")
                if not silent:
                    self.reply_text(
                        message_id,
                        fmt.format_warning(
                            UI_TEXT["mode_ttadk_degraded_msg"].format(tool=degraded_to, reason=reason or "(empty)")
                        ),
                    )
        except Exception:
            logger.debug("best-effort TTADK degrade notification failed", exc_info=True)

        if not thread_id:
            self._enter_mode_on_manager(chat_id, project_id=project_id)
        self.add_reaction(message_id, EmojiReaction.on_coco_enter())

        if project and snapshot and snapshot.is_resumable:
            if not thread_id:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True, snapshot.session_id, snapshot.query_count)
            if not silent:
                mode_hint = UI_TEXT["mode_resume_hint_default"]
                if self.mode_name == "TTADK":
                    mode_hint = UI_TEXT["mode_resume_hint_ttadk"]
                content = UI_TEXT["mode_resume_msg"].format(name=self.mode_name, session_id=session.session_id, query_count=snapshot.query_count, hint=mode_hint)
                
                banner = CardBuilder._build_banner_element(UI_TEXT["mode_resume_banner"].format(name=self.mode_name), type="success")
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    UI_TEXT["mode_card_programming_title"].format(emoji=self.mode_emoji, name=self.mode_name),
                    content,
                    show_buttons=True,
                    footer=UI_TEXT["mode_project_dir_label"].format(path=project.root_path),
                    banner=banner,
                )
                response_id = self.reply_card(message_id, card_content)
                if response_id:
                    self.register_message_project(response_id, project)
        elif project:
            if not thread_id:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True, session.session_id)
            if not silent:
                content = UI_TEXT["mode_enter_msg"].format(emoji=self.mode_emoji, name=self.mode_name)
                if self.mode_name == "TTADK":
                    content += UI_TEXT["ttadk_extra_hint"]
                
                banner = CardBuilder._build_banner_element(UI_TEXT["mode_enter_banner"].format(name=self.mode_name), type="success")
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    UI_TEXT["mode_card_programming_title"].format(emoji=self.mode_emoji, name=self.mode_name),
                    content,
                    show_buttons=True,
                    footer=UI_TEXT["mode_project_dir_label"].format(path=project.root_path),
                    banner=banner,
                )
                response_id = self.reply_card(message_id, card_content)
                if response_id:
                    self.register_message_project(response_id, project)
        else:
            if not silent:
                if self.is_coco:
                    self.reply_text(message_id, fmt.format_coco_enter())
                else:
                    self.reply_text(
                        message_id,
                        UI_TEXT["mode_enter_no_project_msg"].format(emoji=self.mode_emoji, name=self.mode_name),
                    )

        if project:
            self.record_mode_transition(
                project.project_id,
                previous_mode,
                self._get_interaction_mode(),
                reason=f"enter_{self.mode_name.lower()}_mode",
                chat_id=chat_id,
            )

    # ------------------------------------------------------------------
    # switch_model — live model switch for an active session
    # ------------------------------------------------------------------
    def switch_model(
        self,
        message_id: str,
        chat_id: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Switch the model for the active programming session.

        Strategy:
        1. Try ACP protocol `session/setModel` — no restart, context preserved.
        2. Fall back to session restart: end existing session, then call
           ensure_session() with the new model_name (bypasses enter_mode's
           "already in mode" early-return guard).
        """
        project_id = project.project_id if project else None
        cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)
        mgr = self._get_session_manager()

        session = mgr.get_session(chat_id, project_id=project_id)
        if session:
            # Attempt protocol-level model switch (preserves conversation context).
            set_model_fn = getattr(session, "set_model", None)
            if callable(set_model_fn):
                try:
                    if set_model_fn(model_name):
                        logger.info("[%s] Model switched via ACP protocol: %s", self.mode_name, model_name)
                        
                        banner = CardBuilder._build_banner_element(UI_TEXT["mode_model_switched_banner"].format(name=self.mode_name, model=model_name), type="success")
                        msg_type, card_content = CardBuilder.build_project_response_card(
                            project,
                            UI_TEXT["mode_model_switched_title"].format(name=self.mode_name),
                            UI_TEXT["mode_model_switch_context_kept"],
                            banner=banner,
                        )
                        self.reply_card(message_id, card_content)
                        return
                except Exception as e:
                    logger.warning("[%s] ACP set_model failed, will restart session: %s", self.mode_name, get_error_detail(e))

            # Fall back: end session so ensure_session restarts with new model arg.
            mgr.end_session(chat_id, project_id=project_id)

        # Start new session with the requested model (mode stays active).
        startup_timeout = float(getattr(self.settings, "acp_startup_timeout", 20) or 20)
        try:
            agent_type_override = self._get_agent_type_override(project)
            mgr.ensure_session(
                chat_id,
                cwd=cwd,
                startup_timeout=startup_timeout,
                project_id=project_id,
                agent_type_override=agent_type_override,
                model_name=model_name,
            )
            
            banner = CardBuilder._build_banner_element(UI_TEXT["mode_model_switched_banner"].format(name=self.mode_name, model=model_name), type="success")
            msg_type, card_content = CardBuilder.build_project_response_card(
                project,
                UI_TEXT["mode_model_switched_title"].format(name=self.mode_name),
                UI_TEXT["mode_model_switch_restarted"],
                banner=banner,
            )
            self.reply_card(message_id, card_content)
        except Exception as e:
            from ...utils.errors import log_exception
            log_exception(logger, f"切换 {self.mode_name} 模型失败", e)
            self.reply_error(message_id, UI_TEXT["mode_model_switch_error"].format(name=self.mode_name, error=get_error_detail(e)))

    # ------------------------------------------------------------------
    # Thread context registration
    # ------------------------------------------------------------------
    def _register_thread_context(
        self,
        thread_root_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        session: SyncSession,
        alias_keys: Optional[list[str]] = None,
    ) -> None:
        try:
            from ...thread import get_thread_manager

            mode_name = self._get_interaction_mode().value
            tool_name = None
            model_name = None
            if project:
                project_id = project.project_id
            else:
                active = self.project_manager.get_active_project(chat_id)
                project_id = active.project_id if active else (session.session_id or "unknown")
                if active:
                    project = active
            if project and project.ttadk_tool_name:
                tool_name = project.ttadk_tool_name
            if project and project.ttadk_model_name:
                model_name = project.ttadk_model_name

            get_thread_manager().register(
                thread_root_id=thread_root_id,
                chat_id=chat_id,
                project_id=project_id,
                mode=mode_name,
                tool_name=tool_name,
                model_name=model_name,
                alias_keys=alias_keys,
            )
        except Exception as e:
            logger.warning("[Thread] Failed to register context: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # exit_mode
    # ------------------------------------------------------------------
    def exit_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, silent: bool = False):
        from ...thread import get_current_thread_id, get_thread_manager

        project_id = project.project_id if project else None
        thread_id = get_current_thread_id()
        session = self._get_session_manager().get_session(chat_id, project_id=project_id, thread_id=thread_id)

        # Capture before exit_to_smart resets mode state
        was_in_this_mode = self._is_in_this_mode(chat_id, project_id=project_id)

        is_pending_slot = (
            not thread_id
            and not session
            and self.settings.thread_programming_enabled
            and was_in_this_mode
        )
        # User is in this mode (mode_manager) but has no active session (e.g. entered mode without sending any message)
        is_mode_only_exit = (
            not thread_id
            and not session
            and not self.settings.thread_programming_enabled
            and was_in_this_mode
        )

        if project:
            if session:
                self._update_snapshot_on_project(
                    project,
                    query=session.last_query,
                    count=session.message_count,
                    session_id=session.session_id,
                )
                self.context_manager.update_context(
                    project.project_id,
                    session_snapshot={
                        "data": session.to_snapshot(),
                        "source_mode": self.context_source.value,
                    },
                    chat_id=chat_id,
                )
            if not thread_id:
                self._set_mode_on_project(project, False)

        if not thread_id:
            self.mode_manager.exit_to_smart(chat_id, project_id=project_id)

        try:
            has_session = self._get_session_manager().end_session(chat_id, project_id=project_id, thread_id=thread_id)
            if silent:
                # Silent mode: skip all user-facing messages (used for automatic mode switching)
                if has_session or is_pending_slot or is_mode_only_exit:
                    self.add_reaction(message_id, EmojiReaction.on_coco_exit())
                return
            if has_session or is_pending_slot or is_mode_only_exit:
                self.add_reaction(message_id, EmojiReaction.on_coco_exit())

                if project:
                    content = UI_TEXT["mode_exit_msg"].format(name=self.mode_name)
                    if is_pending_slot or is_mode_only_exit:
                        content = UI_TEXT["mode_exit_pending_msg"].format(name=self.mode_name)
                    
                    banner = CardBuilder._build_banner_element(UI_TEXT["mode_exit_banner"].format(name=self.mode_name), type="info")
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project,
                        UI_TEXT["mode_exit_card_title"],
                        content,
                        show_buttons=True,
                        banner=banner,
                    )
                    response_id = self.reply_card(
                        message_id, card_content,
                        reply_in_thread=True if thread_id else None,
                    )
                    if response_id:
                        self.register_message_project(response_id, project)
                else:
                    self.reply_text(
                        message_id,
                        UI_TEXT["mode_exit_pending_msg"].format(name=self.mode_name),
                        reply_in_thread=True if thread_id else None,
                    )
            else:
                self.reply_text(
                    message_id,
                    fmt.format_warning(UI_TEXT["mode_not_in_msg"].format(name=self.mode_name)),
                    reply_in_thread=True if thread_id else None,
                )
        finally:
            if thread_id is not None:
                get_thread_manager().remove(thread_id)

    # ------------------------------------------------------------------
    # handle_message
    # ------------------------------------------------------------------
    def handle_message(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        from ...thread import get_current_thread_id

        project_id = project.project_id if project else None
        thread_id = get_current_thread_id()
        session = self._get_session_manager().get_session(chat_id, project_id=project_id, thread_id=thread_id)

        if not session:
            self.enter_mode(message_id, chat_id, project=project, thread_id=thread_id)
            if not project:
                working_dir = self.get_working_dir(chat_id)
                try:
                    project, _ = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                    project_id = project.project_id
                except Exception:
                    active_project = self.project_manager.get_active_project(chat_id)
                    if active_project:
                        project = active_project
                        project_id = active_project.project_id
            session = self._get_session_manager().get_session(chat_id, project_id=project_id, thread_id=thread_id)
            if not session and thread_id:
                active_project = self.project_manager.get_active_project(chat_id)
                if active_project and active_project is not project:
                    project = active_project
                    project_id = active_project.project_id
                    session = self._get_session_manager().get_session(
                        chat_id,
                        project_id=project_id,
                        thread_id=thread_id,
                    )
            if not session:
                self.reply_text(
                    message_id,
                    fmt.format_warning(
                        UI_TEXT["mode_session_fail_msg"].format(name=self.mode_name, cmd=self.mode_name.lower())
                    ),
                    reply_in_thread=True if thread_id else None,
                )
                return

        text = self.inject_bridge_context(text, project, chat_id=chat_id)
        global_working_dir = self.get_working_dir(chat_id)
        cwd = project.root_path if project else global_working_dir

        # Repo lock: acquire before prompt execution, release after streaming.
        # The lock is held across the entire streaming phase; periodic touch()
        # in the on_event callback keeps last_active_time fresh to prevent
        # idle-timeout release.
        root_path = getattr(project, "root_path", None) if project else None

        from ...repo_lock import LockConflictError

        repo_lock_mgr = None
        needs_release = False
        try:
            _, repo_lock_mgr, needs_release = self._acquire_repo_lock(root_path, chat_id)
        except LockConflictError as err:
            self.send_lock_conflict_card(err, message_id, text)
            return

        try:
            self.handle_response(message_id, chat_id, text, session, project, cwd, global_working_dir,
                                 _repo_lock_mgr=repo_lock_mgr, _root_path=root_path)
        finally:
            if needs_release:
                self._release_repo_lock(root_path, chat_id, repo_lock_mgr)

    # ------------------------------------------------------------------
    # handle_response (streaming / non-streaming)
    # ------------------------------------------------------------------
    def handle_response(
        self, message_id: str, chat_id: str, text: str, session: SyncSession, project, cwd: str, global_working_dir: str,
        *, _repo_lock_mgr=None, _root_path: str | None = None,
    ):
        from ...acp.models import ACPEvent
        from ...card.delivery.factory import create_card_delivery
        from ...card.delivery.feishu_client import FeishuCardAPIClient
        from ...card.programming_adapter import ProgrammingCardSession, build_programming_metadata
        from ...card.session import CardSession

        project_name = project.project_name if project else None
        project_path = project.root_path if project else global_working_dir
        project_id = project.project_id if project else None

        with self.ctx.pending_image_lock:
            image_keys = self.ctx.pending_image_keys.get(message_id)

        logger.info("开始 %s 输出: project=%s, path=%s", self.mode_name, project_name, project_path)

        from ...thread import get_current_thread_id as _get_tid
        _thread_id = _get_tid()

        # Build metadata for new card system
        tool_name = None
        model_name = self._get_model_name_override(project)
        if self.mode_name == "TTADK":
            tool_name = self._get_ttadk_tool_display(project)
            if not model_name:
                model_name = self._get_ttadk_model_display(project)

        metadata = build_programming_metadata(
            self.mode_name,
            tool_name=tool_name,
            model_name=model_name,
            project_name=project_name,
        )

        # Create card delivery + session
        api_client = FeishuCardAPIClient(self.ctx.api_client_factory())
        delivery = create_card_delivery(api_client)
        from src.card.session.config import SessionConfig
        config = SessionConfig(metadata=metadata, reply_to=message_id)
        card_session = CardSession(
            chat_id=chat_id,
            config=config,
            delivery=delivery,
        )
        prog_session = ProgrammingCardSession(card_session)

        # Start card (creates in Feishu)
        try:
            prog_session.start()
        except Exception as e:
            logger.warning("创建流式卡片失败: %s", str(e))
            # Fallback to non-streaming text mode
            self._handle_response_non_streaming(
                message_id, chat_id, text, session, project, global_working_dir,
                _repo_lock_mgr=_repo_lock_mgr, _root_path=_root_path,
            )
            return

        # Message linking
        card_message_id = prog_session.get_message_id()
        if card_message_id:
            try:
                rid = self.ensure_request_id(message_id, chat_id=chat_id, project_id=project_id)
                self.ctx.message_linker.register_origin(
                    message_id, request_id=rid, chat_id=chat_id, project_id=project_id
                )
                self.ctx.message_linker.link_reply(message_id, card_message_id)
            except Exception as e:
                logger.debug("link消息失败(programming): %s", e)

        # Streaming execution
        timeout = self.settings.coco_execution_timeout if self.is_coco else self.settings.claude_execution_timeout
        update_count = [0]
        _last_touch = [time.monotonic()]
        _TOUCH_INTERVAL = 30

        # Heartbeat for repo lock
        _streaming_hb_stop = threading.Event()
        try:
            _streaming_max_beats = max(1, int(self.settings.repo_lock_hard_timeout // _TOUCH_INTERVAL))
        except Exception:
            _streaming_max_beats = 120

        if _repo_lock_mgr and _root_path:
            from ...utils.heartbeat import RepoLockHeartbeat
            _streaming_hb = RepoLockHeartbeat(
                _streaming_hb_stop,
                lambda: _repo_lock_mgr.touch(_root_path, chat_id),
                interval=_TOUCH_INTERVAL,
                max_beats=_streaming_max_beats,
                name=f"prog-stream-{_root_path}",
            )
            _streaming_hb.start()
        else:
            _streaming_hb = None

        def on_event(event: ACPEvent):
            update_count[0] += 1
            # Heartbeat touch
            if _repo_lock_mgr and _root_path:
                now = time.monotonic()
                if now - _last_touch[0] >= _TOUCH_INTERVAL:
                    _repo_lock_mgr.touch(_root_path, chat_id)
                    _last_touch[0] = now
            # Dispatch to new card session
            try:
                prog_session.on_event(event)
            except Exception as e:
                logger.debug("card session event处理失败: %s", str(e))

        final_response = ""
        try:
            result = session.send_prompt(text, on_event=on_event, timeout=timeout)
            prog_session.finish()
            final_response = prog_session.get_final_text()
            # Fallback if no text captured
            if not final_response and result and result.text:
                final_response = result.text
            if not final_response:
                final_response = UI_TEXT["mode_exec_complete"]
        except TimeoutError as e:
            final_response = UI_TEXT["mode_exec_timeout_msg"].format(error=get_error_detail(e))
            log_exception(logger, f"{self.mode_name} ACP执行超时", e, level=logging.WARNING)
            prog_session.fail(final_response)
        except Exception as e:
            final_response = UI_TEXT["mode_exec_exception_msg"].format(error=get_error_detail(e))
            log_exception(logger, f"{self.mode_name} ACP执行异常", e)
            prog_session.fail(final_response)
            from ...utils.errors import GhostAPError
            if isinstance(e, GhostAPError) and e.quick_actions:
                self.send_error_card(chat_id, e, title=UI_TEXT["mode_exec_exception_title"], origin_message_id=message_id)
        finally:
            _streaming_hb_stop.set()
            if _streaming_hb is not None:
                _streaming_hb.join(timeout=2)

        logger.info("%s ACP输出完成: 事件数=%d, 最终长度=%d", self.mode_name, update_count[0], len(final_response))

        # Post-processing
        if project:
            self._update_snapshot_on_project(project, text, session.message_count, session.session_id)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", final_response)
            source = self.mode_name.lower()
            self.context_manager.update_context(
                project.project_id,
                conversation={"role": "user", "content": text, "source_mode": source, "message_id": message_id},
                chat_id=chat_id,
            )
            self.context_manager.update_context(
                project.project_id,
                conversation={"role": "assistant", "content": final_response, "source_mode": source},
                chat_id=chat_id,
            )

        self.add_reaction(message_id, EmojiReaction.on_coco_response())

        if card_message_id and project:
            self.register_message_project(card_message_id, project)

    def _handle_response_non_streaming(
        self, message_id: str, chat_id: str, text: str, session: SyncSession, project, global_working_dir: str,
        *, _repo_lock_mgr=None, _root_path: str | None = None,
    ):
        """Fallback: handle response in non-streaming text mode."""
        timeout = self.settings.coco_execution_timeout if self.is_coco else self.settings.claude_execution_timeout

        # Heartbeat
        _TOUCH_INTERVAL = 30
        _hb_stop = threading.Event()
        try:
            _max_beats = max(1, int(self.settings.repo_lock_hard_timeout // _TOUCH_INTERVAL))
        except Exception:
            _max_beats = 120

        if _repo_lock_mgr and _root_path:
            from ...utils.heartbeat import RepoLockHeartbeat
            _hb = RepoLockHeartbeat(
                _hb_stop,
                lambda: _repo_lock_mgr.touch(_root_path, chat_id),
                interval=_TOUCH_INTERVAL,
                max_beats=_max_beats,
                name=f"prog-nonstream-{_root_path}",
            )
            _hb.start()
        else:
            _hb = None

        try:
            renderer = ACPEventRenderer()
            result = session.send_prompt(text, on_event=None, timeout=timeout)
            final_response = (
                (getattr(result, "text", None) or "").strip()
                or renderer.get_final_content()
                or UI_TEXT["mode_exec_complete"]
            )
            response_with_dir = f"{final_response}\n\n---\n{UI_TEXT['mode_working_dir_label'].format(path=global_working_dir)}"
            self.reply_text(message_id, response_with_dir)
        except TimeoutError as e:
            log_exception(logger, f"{self.mode_name} ACP执行超时", e, level=logging.WARNING)
            msg_type, content = CardBuilder.build_error_card(e, title=UI_TEXT["mode_exec_timeout_title"], project=project)
            self.reply_card(message_id, content)
        except Exception as e:
            msg_type, content = CardBuilder.build_error_card(e, title=UI_TEXT["mode_exec_exception_title"], project=project)
            self.reply_card(message_id, content)
        finally:
            _hb_stop.set()
            if _hb is not None:
                _hb.join(timeout=2)

    # ------------------------------------------------------------------
    # show_info
    # ------------------------------------------------------------------
    def show_info(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        from ...thread import get_current_thread_id

        project_id = project.project_id if project else None
        thread_id = get_current_thread_id()
        info = self._get_session_manager().get_session_info(chat_id, project_id=project_id, thread_id=thread_id)
        if info:
            if project:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    UI_TEXT["mode_session_info_title"].format(name=self.mode_name),
                    info,
                    show_buttons=True,
                )
                response_id = self.reply_card(message_id, card_content)
                if response_id:
                    self.register_message_project(response_id, project)
            else:
                self.reply_text(message_id, info)
        else:
            self.reply_text(message_id, fmt.format_warning(UI_TEXT["mode_not_in_msg"].format(name=self.mode_name)))

    # ------------------------------------------------------------------
    # Card actions
    # ------------------------------------------------------------------
    def handle_card_enter(self, message_id: str, chat_id: str, project_id: str, value: Optional[dict] = None):
        if project_id:
            project = self.project_manager.get_project_for_chat(project_id, chat_id)
            if project:
                self.project_manager.set_active_project(chat_id, project_id)

                snapshot = self._get_snapshot(project)
                if snapshot and snapshot.is_resumable:
                    if self.is_coco:
                        msg_type, card_content = CardBuilder.build_coco_resume_card(project)
                    elif self.mode_name == "Claude":
                        msg_type, card_content = CardBuilder.build_claude_resume_card(project)
                    else:
                        msg_type, card_content = CardBuilder.build_ttadk_resume_card(project)
                    response_id = self.reply_card(message_id, card_content)
                    if response_id:
                        self.register_message_project(response_id, project)
                    return

                self.enter_mode(message_id, chat_id, project=project)
                return

        self.enter_mode(message_id, chat_id)

    def handle_card_exit(self, message_id: str, chat_id: str, project_id: str, value: Optional[dict] = None):
        from ...thread import get_current_thread_id
        if project_id:
            project = self.project_manager.get_project_for_chat(project_id, chat_id)
            if project and not get_current_thread_id():
                self._set_mode_on_project(project, False)
            self.exit_mode(message_id, chat_id, project=project)
            return
        self.exit_mode(message_id, chat_id)

    def handle_card_resume(self, message_id: str, chat_id: str, project_id: str, session_id: str):
        from ...thread import get_current_thread_id

        thread_id = get_current_thread_id()
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else None
        pid = project.project_id if project else None
        if project:
            self.project_manager.set_active_project(chat_id, project_id)

        self.add_reaction(message_id, EmojiReaction.on_coco_enter())

        previous_mode = self.mode_manager.get_mode(chat_id)

        cwd = project.root_path if project else self.get_working_dir(chat_id)
        if self._uses_claude_cli():
            # Claude resume: start_session with session_id, set resumed
            try:
                agent_type_override = self._get_agent_type_override(project)
                model_name = self._get_model_name_override(project)
                session = self.ctx.claude_manager.start_session(
                    chat_id,
                    cwd=cwd,
                    session_id=session_id,
                    project_id=pid,
                    agent_type_override=agent_type_override,
                    model_name=model_name,
                    thread_id=thread_id,
                )
            except Exception as e:
                self.send_error_card(
                    chat_id,
                    e,
                    title=UI_TEXT["mode_resume_fail_title"].format(name="Claude"),
                    origin_message_id=message_id,
                )
                return
            session.is_resumed = True

            if thread_id and project:
                self._register_thread_context(thread_id, chat_id, project, session)
            if not thread_id:
                self._enter_mode_on_manager(chat_id, project_id=pid)
        else:
            try:
                agent_type_override = self._get_agent_type_override(project)
                model_name = self._get_model_name_override(project)
                session = self._get_session_manager().start_session(
                    chat_id,
                    cwd=cwd,
                    session_id=session_id,
                    project_id=pid,
                    agent_type_override=agent_type_override,
                    model_name=model_name,
                    thread_id=thread_id,
                )
            except Exception as e:
                self.send_error_card(
                    chat_id,
                    e,
                    title=UI_TEXT["mode_resume_fail_title"].format(name=self.mode_name),
                    origin_message_id=message_id,
                )
                return
            if thread_id and project:
                self._register_thread_context(thread_id, chat_id, project, session)
            if not thread_id:
                self._enter_mode_on_manager(chat_id, project_id=pid)

        if project:
            if not thread_id:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True, session_id)
            self.record_mode_transition(
                project.project_id,
                previous_mode,
                self._get_interaction_mode(),
                reason=f"resume_{self.mode_name.lower()}_session",
                chat_id=chat_id,
            )
            content = UI_TEXT["mode_resume_card_content"].format(name=self.mode_name, session_id=session_id)
            msg_type, card_content = CardBuilder.build_project_response_card(
                project,
                UI_TEXT["mode_resume_card_title"].format(name=self.mode_name),
                content,
                show_buttons=True,
            )
            response_id = self.reply_card(message_id, card_content)
            if response_id:
                self.register_message_project(response_id, project)
        else:
            self.reply_text(message_id, UI_TEXT["mode_resume_no_project_msg"].format(name=self.mode_name, session_id=session_id))

    def handle_card_new(self, message_id: str, chat_id: str, project_id: str, value: Optional[dict] = None):
        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else None
        if project:
            self.project_manager.set_active_project(chat_id, project_id)
            self._clear_snapshot_on_project(project)
            self.enter_mode(message_id, chat_id, project=project)
            return
        self.enter_mode(message_id, chat_id)


# ======================================================================
# Concrete subclasses
# ======================================================================


class CocoModeHandler(ProgrammingModeHandler):
    mode_name = "Coco"
    mode_emoji = "🤖"
    is_coco = True
    interaction_mode = InteractionMode.COCO
    mode_key = "coco"
    context_source = ContextSourceMode.COCO
    thinking_text = UI_TEXT["mode_thinking_msg"].format(emoji="🤔", name="Coco")


class ClaudeModeHandler(ProgrammingModeHandler):
    mode_name = "Claude"
    mode_emoji = "🔮"
    interaction_mode = InteractionMode.CLAUDE
    mode_key = "claude"
    context_source = ContextSourceMode.CLAUDE
    thinking_text = UI_TEXT["mode_thinking_msg"].format(emoji="🔮", name="Claude")

    def _uses_claude_cli(self) -> bool:
        return True


class AidenModeHandler(ProgrammingModeHandler):
    mode_name = "Aiden"
    mode_emoji = "🎯"
    interaction_mode = InteractionMode.AIDEN
    mode_key = "aiden"
    context_source = ContextSourceMode.AIDEN
    thinking_text = UI_TEXT["mode_thinking_msg"].format(emoji="🎯", name="Aiden")


class CodexModeHandler(ProgrammingModeHandler):
    mode_name = "Codex"
    mode_emoji = "⚡"
    interaction_mode = InteractionMode.CODEX
    mode_key = "codex"
    context_source = ContextSourceMode.CODEX
    thinking_text = UI_TEXT["mode_thinking_msg"].format(emoji="⚡", name="Codex")


class GeminiModeHandler(ProgrammingModeHandler):
    mode_name = "Gemini"
    mode_emoji = "✨"
    interaction_mode = InteractionMode.GEMINI
    mode_key = "gemini"
    context_source = ContextSourceMode.GEMINI
    thinking_text = UI_TEXT["mode_thinking_msg"].format(emoji="✨", name="Gemini")


class TTADKModeHandler(ProgrammingModeHandler):
    mode_name = "TTADK"
    mode_emoji = "🎮"
    interaction_mode = InteractionMode.TTADK
    mode_key = "ttadk"
    context_source = ContextSourceMode.TTADK
    thinking_text = UI_TEXT["mode_thinking_msg"].format(emoji="🎮", name="TTADK")

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_tool: Optional[str] = None

    def _get_agent_type_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        tool = (project.ttadk_tool_name if project else None) or self._current_tool
        if not tool:
            from ...ttadk import get_ttadk_manager

            tool = get_ttadk_manager().get_current_tool() or "coco"
        return f"ttadk_{tool}"

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        model = (project.ttadk_model_name if project else None) or self._current_model
        if not model:
            from ...ttadk import get_ttadk_manager

            model = get_ttadk_manager().get_current_model()
        return model

    def _get_ttadk_tool_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        agent_type = self._get_agent_type_override(project)
        return agent_type.replace("ttadk_", "", 1) if agent_type else None

    def _get_ttadk_model_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return self._get_model_name_override(project)

    @property
    def current_tool(self) -> Optional[str]:
        return self._current_tool

    @current_tool.setter
    def current_tool(self, value: Optional[str]):
        self._current_tool = value
