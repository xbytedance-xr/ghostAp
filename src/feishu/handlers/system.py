"""System handler — help, exit mode, shell commands, directory switching, intercepted commands."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Optional

from ...acp.helper import fetch_acp_models, list_acp_tools
from ...acp.providers import tool_registry
from ...card import CardBuilder
from ...card.styles import UI_TEXT
from ...coco_model import get_coco_model_manager
from ...tasking import TaskPriority, TaskSpec
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from .base import BaseHandler
from .lock_commands import LockCommandsMixin
from .ttadk_commands import TTADKCommandsMixin

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class SystemHandler(LockCommandsMixin, TTADKCommandsMixin, BaseHandler):
    """Help, exit, shell, directory, and intercepted-command handling."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self._init_command_registry()
        self._ttadk_flow_start_times: dict[str, float] = {}
        self._ttadk_flow_last_duration_ms: OrderedDict[str, int] = OrderedDict()
        self._TTADK_FLOW_DURATION_MAX_SIZE = 200

    def _init_command_registry(self):
        """Initialize the command dispatch registry."""
        # Exact match handlers: command -> handler_func(message_id, chat_id, text, project)
        self._exact_handlers = {
            "/help": lambda m, c, t, p: self.show_full_help(m, c, p),
            "/帮助": lambda m, c, t, p: self.show_full_help(m, c, p),
            "/coco_status": lambda m, c, t, p: self.show_coco_status(m, c),
            "/coco_info": lambda m, c, t, p: self.get_handler("coco").show_info(m, c, p),
            "/claude_info": lambda m, c, t, p: self.get_handler("claude").show_info(m, c, p),
            "/aiden_info": lambda m, c, t, p: self.get_handler("aiden").show_info(m, c, p),
            "/codex_info": lambda m, c, t, p: self.get_handler("codex").show_info(m, c, p),
            "/gemini_info": lambda m, c, t, p: self.get_handler("gemini").show_info(m, c, p),
            "/projects": lambda m, c, t, p: self.get_handler("project").show_project_board(m, c),
            "/project": lambda m, c, t, p: self.get_handler("project").show_project_board(m, c),
            "/switch": lambda m, c, t, p: self.get_handler("project").show_project_board(m, c),
            "/ttadk": lambda m, c, t, p: self.handle_ttadk_command(m, c, p),
            "/acp": lambda m, c, t, p: self.handle_acp_command(m, c, p),
            "/wt": lambda m, c, t, p: self.get_handler("worktree").handle_worktree_command(m, c, p),
            "/worktree": lambda m, c, t, p: self.get_handler("worktree").handle_worktree_command(m, c, p),
            "/ttadk_info": lambda m, c, t, p: self.show_ttadk_info(m, c),
            "/ttadk_refresh": lambda m, c, t, p: self.refresh_ttadk_models(m, c, p),
            "/menu": lambda m, c, t, p: self.handle_menu_command(m, c, p),
            "/tools": lambda m, c, t, p: self.show_tools_list(m, c, p),
            "/tools_status": lambda m, c, t, p: self.show_tools_status(m, c, p),
            "/model": lambda m, c, t, p: self.handle_model_command(m, c, t, p),
            "/lock": lambda m, c, t, p: self._handle_lock_command(m, c, "lock"),
            "/unlock": lambda m, c, t, p: self._handle_lock_command(m, c, "unlock"),
        }

        # Prefix match handlers: prefix -> handler_func(message_id, chat_id, text, project)
        # Note: Order matters if prefixes overlap (not the case here yet)
        self._prefix_handlers = [
            ("/status", lambda m, c, t, p: self.get_handler("diagnostics").show_unified_status(m, c, t, p)),
            ("/tasks", lambda m, c, t, p: self.get_handler("diagnostics").show_task_board(m, c, t, p)),
            ("/diff", lambda m, c, t, p: self.get_handler("diagnostics").show_context_diff(m, c, t, p)),
            ("/trace", lambda m, c, t, p: self.get_handler("diagnostics").show_message_trace(m, c, t, p)),
            ("/worktree ", lambda m, c, t, p: self.get_handler("worktree").handle_worktree_prefix_command(m, c, t, p)),
            ("/wt ", lambda m, c, t, p: self.get_handler("worktree").handle_worktree_prefix_command(m, c, t, p)),
            ("/switch ", self._handle_switch_command),
            ("/new ", self._handle_new_project_command),
            ("/close ", self._handle_close_command),
            ("/model ", self.handle_model_command),
        ]

    def _handle_switch_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]):
        name = text[8:].strip()
        if name:
            self.get_handler("project").switch_project(
                message_id,
                chat_id,
                name,
                coco_handler=self.get_handler("coco"),
                claude_handler=self.get_handler("claude"),
            )
        else:
            self.get_handler("project").show_project_board(message_id, chat_id)

    def _handle_new_project_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]
    ):
        parts = text[5:].strip().split(None, 1)
        name = parts[0] if parts else ""
        path = parts[1] if len(parts) > 1 else self.get_working_dir(chat_id)
        if name:
            self.get_handler("project").create_project(message_id, chat_id, name, path)
        else:
            self.reply_error(
                message_id, UI_TEXT["system_new_project_usage"], title=UI_TEXT["system_arg_error"]
            )

    def _handle_close_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]):
        name = text[7:].strip()
        if name:
            self.get_handler("project").close_project(message_id, chat_id, name)

    # ------------------------------------------------------------------
    # Command predicates
    # ------------------------------------------------------------------
    @staticmethod
    def is_exit_command(text: str) -> bool:
        text_lower = text.lower().strip()
        exit_commands = {"/exit", "/quit", "/end_coco", "/exit_coco", "/end_claude", "/exit_claude"}
        exit_keywords = {"退出模式", "退出编程模式", "退出编程", "结束编程", "退出claude", "退出coco"}
        if text_lower in exit_commands:
            return True
        return any(kw in text_lower for kw in exit_keywords)

    @staticmethod
    def is_deep_command(text: str) -> bool:
        text_lower = text.lower().strip()
        return text_lower.startswith("/deep") or text_lower.startswith("/stop_deep")

    @staticmethod
    def is_loop_command(text: str) -> bool:
        text_lower = text.lower().strip()
        return text_lower.startswith("/loop") or text_lower.startswith("/stop_loop")

    @staticmethod
    def is_spec_command(text: str) -> bool:
        text_lower = text.lower().strip()
        spec_prefixes = (
            "/spec",
            "/stop_spec",
            "/spec_status",
            "/spec_history",
            "/spec_metrics",
            "/spec_config",
            "/spec_save",
            "/spec_pause",
            "/spec_resume",
            "/spec_recover",
            "/spec_guide",
            "/spec_export",
        )
        return any(text_lower == cmd or text_lower.startswith(f"{cmd} ") for cmd in spec_prefixes)

    @staticmethod
    def is_likely_shell_command(text: str) -> bool:
        """Heuristic check for common shell commands.

        Used for early routing in _handle_message to prevent shell commands
        from blocking behind long-running programming tasks on the project queue.
        """
        text_lower = text.strip()
        if not text_lower or text_lower.startswith("/"):
            return False
        first_word = text_lower.split()[0].lower()
        # Single-word commands that are almost certainly shell
        shell_exact = {
            "ls",
            "pwd",
            "whoami",
            "date",
            "uptime",
            "df",
            "du",
            "ps",
            "top",
            "htop",
            "free",
            "uname",
            "env",
            "id",
            "hostname",
            "which",
            "file",
            "wc",
            "tree",
        }
        if first_word in shell_exact:
            return True
        # Prefix patterns for parameterized shell commands
        shell_prefixes = {
            "ls",
            "cat",
            "head",
            "tail",
            "wc",
            "git",
            "find",
            "grep",
            "mkdir",
            "rm",
            "cp",
            "mv",
            "chmod",
            "chown",
            "touch",
            "echo",
            "curl",
            "wget",
            "pip",
            "npm",
            "yarn",
            "docker",
            "make",
            "tree",
        }
        return first_word in shell_prefixes

    @staticmethod
    def is_interceptable_command(text: str) -> bool:
        text_lower = text.lower().strip()
        exact_commands = {
            "/help",
            "/帮助",
            "/coco_info",
            "/claude_info",
            "/aiden_info",
            "/codex_info",
            "/gemini_info",
            "/ttadk_info",
            "/projects",
            "/status",
            "/project",
            "/switch",
            "/tasks",
            "/diff",
            "/trace",
            "/ttadk",
            "/acp",
            "/wt",
            "/worktree",
            "/ttadk_refresh",
            "/menu",
            "/model",
            "/lock",
            "/unlock",
        }
        if text_lower in exact_commands:
            return True
        prefix_commands = ("/worktree ", "/wt ", "/switch ", "/new ", "/close ", "/tasks ", "/diff ", "/trace ", "/status ", "/model ")
        return any(text_lower.startswith(p) for p in prefix_commands)

    # ------------------------------------------------------------------
    # Intercepted command router
    # ------------------------------------------------------------------
    def handle_intercepted_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        text_lower = text.lower().strip()

        # 1. Try exact match
        handler = self._exact_handlers.get(text_lower)
        if handler:
            handler(message_id, chat_id, text, project)
            return

        # 2. Try prefix match
        for prefix, handler in self._prefix_handlers:
            if text_lower.startswith(prefix):
                handler(message_id, chat_id, text, project)
                return

        # 3. Fallback to help
        self.show_full_help(message_id, chat_id, project)

    def handle_menu_command(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        msg_type, card_content = CardBuilder.build_command_menu_card(project)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_help_category(
        self,
        message_id: str,
        chat_id: str,
        category: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        current_mode = self.mode_manager.get_mode(chat_id)
        current_dir = self.get_working_dir(chat_id)

        # Determine admin status for conditional help content
        is_admin = False
        lock_enabled = True  # F-20: Always show lock section in /help for discoverability
        chat_lock_mgr = getattr(self.ctx, "chat_lock_manager", None)
        if chat_lock_mgr is not None:
            from ...thread import get_current_sender_id
            sender_id = get_current_sender_id() or ""
            if sender_id:
                is_admin = chat_lock_mgr.is_admin(sender_id)

        # FS-09: Inject guidance when ADMIN_USER_IDS is empty
        no_admin_configured = False
        try:
            from ...config import get_settings as _gs
            no_admin_configured = not _gs().admin_user_ids
        except Exception:
            logger.debug("failed to check admin config", exc_info=True)

        msg_type, card_content = CardBuilder.build_help_card(
            project, category, current_dir, current_mode,
            is_admin=is_admin, lock_enabled=lock_enabled, chat_id=chat_id,
            no_admin_configured=no_admin_configured,
        )

        if origin_message_id:
            if self.patch_message(origin_message_id, card_content):
                return

        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_deep_prompt(self, message_id: str, chat_id: str):
        self.reply_message(
            message_id,
            UI_TEXT["system_help_deep_prompt"],
        )

    # ------------------------------------------------------------------
    # ACP command handling
    # ------------------------------------------------------------------
    def _enter_mode_with_acp_model(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        target_project = project or self.project_manager.get_active_project(chat_id)
        if target_project:
            target_project.acp_tool_name = tool_name
            target_project.acp_model_name = model_name

        _TOOL_HANDLER_MAP = [
            ("coco",   "is_coco_mode"),
            ("claude", "is_claude_mode"),
            ("aiden",  "is_aiden_mode"),
            ("codex",  "is_codex_mode"),
            ("gemini", "is_gemini_mode"),
        ]
        for _tool, _mode_check in _TOOL_HANDLER_MAP:
            if tool_name != _tool:
                continue
            handler = self.get_handler(_tool)
            if not handler:
                break
            if hasattr(handler, "current_model"):
                handler.current_model = model_name
            # If already in this mode, switch model on the active session instead of
            # calling enter_mode() which would return early with an "already in mode" warning.
            mode_checker = getattr(self.mode_manager, _mode_check, None)
            if callable(mode_checker) and mode_checker(chat_id) and hasattr(handler, "switch_model"):
                handler.switch_model(message_id, chat_id, model_name, project=target_project)
            else:
                handler.enter_mode(message_id, chat_id, project=target_project)
            return

        self.reply_error(message_id, UI_TEXT["system_acp_unsupported_tool"].format(tool_name=tool_name))

    def handle_acp_command(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        project_id = project.project_id if project else None
        current_tool = project.acp_tool_name if project else None
        tools = list_acp_tools()
        if not tools:
            self.reply_error(message_id, UI_TEXT["system_acp_no_available_tools"])
            return
        msg_type, card_content = CardBuilder.build_acp_tool_select_card(tools, project_id, current_tool=current_tool)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def _fetch_acp_models(
        self,
        tool_name: str,
        *,
        cwd: Optional[str] = None,
        current_model: Optional[str] = None,
    ) -> list:
        """Thin wrapper around ``fetch_acp_models`` for easier testing.

        Unit tests can monkeypatch this method on ``SystemHandler`` instances
        without needing to stub the global helper import.
        """
        return fetch_acp_models(tool_name, cwd=cwd, current_model=current_model)

    def handle_select_acp_tool(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        tool = (tool_name or "").strip().lower()
        if not tool:
            self.reply_error(message_id, UI_TEXT["system_acp_select_tool_prompt"])
            return

        project = self.project_manager.get_project_for_chat(project_id, chat_id) if project_id else self.project_manager.get_active_project(chat_id)
        cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)

        current_model = None
        if project and getattr(project, "acp_tool_name", "") == tool:
            current_model = getattr(project, "acp_model_name", None)

        models = self._fetch_acp_models(tool, cwd=cwd, current_model=current_model)
        if not models:
            self.reply_error(message_id, UI_TEXT["system_acp_get_models_failed"].format(tool=tool))
            return

        msg_type, card_content = CardBuilder.build_acp_model_select_card(models, tool, project_id, current_model=current_model)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_refresh_acp_models(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        self.handle_select_acp_tool(message_id, chat_id, tool_name, project_id)

    def handle_select_acp_model(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
    ):
        tool = (tool_name or "").strip().lower()
        model = (model_name or "").strip()
        if not tool or not model:
            self.reply_error(message_id, UI_TEXT["system_acp_select_model_prompt"])
            return

        msg_type, card_content = CardBuilder.build_switching_status_card(tool, model)
        self.reply_message(message_id, card_content, msg_type=msg_type)
        self._enter_mode_with_acp_model(message_id, chat_id, tool, model, project)

    # ------------------------------------------------------------------
    # /model command — list/switch models for current ACP tool
    # ------------------------------------------------------------------
    def _resolve_current_acp_tool(self, chat_id: str, project: Optional["ProjectContext"] = None) -> str:
        """Resolve the ACP tool name relevant to the current context.

        Priority:
        1. project.acp_tool_name (explicit tool set on active project)
        2. Current interaction mode (coco/aiden/codex/gemini/claude)
        3. Default: "coco"
        """
        if project and getattr(project, "acp_tool_name", ""):
            return str(project.acp_tool_name).lower()

        mode_to_tool = {
            "coco": "coco",
            "aiden": "aiden",
            "codex": "codex",
            "gemini": "gemini",
            "claude": "claude",
        }
        for mode_check, tool in mode_to_tool.items():
            checker = getattr(self.mode_manager, f"is_{mode_check}_mode", None)
            if callable(checker) and checker(chat_id):
                return tool

        return "coco"

    def handle_model_command(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Handle /model [list|<name>|switch <name>] command.

        /model              — show model selection card for current ACP tool
        /model list         — same as above
        /model <name>       — switch directly to <name>
        /model switch <name>— same as /model <name>
        """
        text_stripped = (text or "").strip()
        parts = text_stripped.split(None, 2)
        # parts[0] == "/model" (case-insensitive)
        subcommand = parts[1].lower() if len(parts) >= 2 else ""

        # Resolve project if not provided
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        tool_name = self._resolve_current_acp_tool(chat_id, project)
        cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)
        project_id = project.project_id if project else None

        current_model: Optional[str] = None
        if project and getattr(project, "acp_tool_name", "") == tool_name:
            current_model = getattr(project, "acp_model_name", None)

        if subcommand in ("", "list", "ls"):
            # Show interactive model selection card
            self.reply_message(message_id, UI_TEXT["system_acp_querying_models"].format(tool_name=tool_name))
            models = self._fetch_acp_models(tool_name, cwd=cwd, current_model=current_model)
            if not models:
                self.reply_error(message_id, UI_TEXT["system_acp_get_models_failed"].format(tool_name=tool_name))
                return
            msg_type, card_content = CardBuilder.build_acp_model_select_card(models, tool_name, project_id, current_model=current_model)
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        # Direct switch: /model <name> or /model switch <name>
        if subcommand == "switch":
            model_name = parts[2].strip() if len(parts) >= 3 else ""
        else:
            model_name = parts[1].strip() if len(parts) >= 2 else ""

        if not model_name:
            self.reply_error(
                message_id,
                UI_TEXT["system_acp_specify_model_prompt"].format(
                    example=UI_TEXT["system_model_usage_example"]
                ),
            )
            return

        msg_type, card_content = CardBuilder.build_switching_status_card(tool_name, model_name)
        self.reply_message(message_id, card_content, msg_type=msg_type)
        self._enter_mode_with_acp_model(message_id, chat_id, tool_name, model_name, project)

    # ------------------------------------------------------------------
    # Exit current mode
    # ------------------------------------------------------------------
    def exit_current_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        from ...mode import InteractionMode
        from ...thread import get_current_thread_id, get_thread_manager

        _pid = project.project_id if project else None
        current_mode = self.mode_manager.get_mode(chat_id, project_id=_pid)

        thread_id = get_current_thread_id()
        if thread_id and current_mode == InteractionMode.SMART:
            thread_ctx = get_thread_manager().get(thread_id)
            if thread_ctx and thread_ctx.mode != "smart":
                try:
                    current_mode = InteractionMode(thread_ctx.mode)
                except ValueError:
                    logger.debug("invalid InteractionMode value: %s", thread_ctx.mode, exc_info=True)

        if current_mode == InteractionMode.COCO:
            self.get_handler("coco").exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.CLAUDE:
            self.get_handler("claude").exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.AIDEN:
            self.get_handler("aiden").exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.CODEX:
            self.get_handler("codex").exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.GEMINI:
            self.get_handler("gemini").exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.TTADK:
            self.get_handler("ttadk").exit_mode(message_id, chat_id, project)
        else:
            self.reply_message(message_id, UI_TEXT["system_already_in_mode"])

    # ------------------------------------------------------------------
    # Shell command submission
    # ------------------------------------------------------------------
    def execute_shell_and_reply(
        self,
        message_id: str,
        chat_id: str,
        cmd: str,
        working_dir: Optional[str],
        project: Optional["ProjectContext"] = None,
    ):
        """Execute a shell command via SandboxExecutor and reply with the result."""
        from ...sandbox import SandboxExecutor

        executor = SandboxExecutor()
        # Smart mode shell execution: disable interactive mode to avoid .bashrc noise and job control errors
        result = executor.execute(cmd, cwd=working_dir, interactive=False, chat_id=chat_id)
        msg_type, card_content = CardBuilder.build_shell_result_card(
            cmd,
            result,
            working_dir,
            project,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)
        if result.success:
            self.add_reaction(message_id, EmojiReaction.on_shell_executed())
        else:
            self.add_reaction(message_id, EmojiReaction.on_error())
        return result

    def submit_shell_command(
        self,
        message_id: str,
        chat_id: str,
        cmd: str,
        working_dir: Optional[str],
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ):
        project_id = project.project_id if project else None
        origin_message_id = origin_message_id or message_id
        queue_suffix = project_id or (working_dir or "cwd")

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:shell:{queue_suffix}",
            name="shell_command",
            task_type="shell",
            project_id=project_id,
            message_id=message_id,
            origin_message_id=origin_message_id,
            request_id=request_id,
            priority=TaskPriority.NORMAL,
        )

        def _run(_ctx):
            return self.execute_shell_and_reply(message_id, chat_id, cmd, working_dir, project)

        handle = self.scheduler.submit(spec, _run)
        try:
            self.ctx.message_linker.link_task(origin_message_id, handle.run_id)
        except Exception:
            logger.debug("failed to link task message", exc_info=True)
        return handle

    # ------------------------------------------------------------------
    # Directory change
    # ------------------------------------------------------------------
    def change_directory(self, message_id: str, chat_id: str, path: str, project: Optional["ProjectContext"] = None):
        current_dir = self.get_working_dir(chat_id)

        if not path:
            self.add_reaction(message_id, EmojiReaction.on_dir_changed())
            if project:
                content = ProjectBuilder.build_project_info_content(project, current_dir)
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    UI_TEXT["project_dir_info_title"],
                    content,
                    show_buttons=True,
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
            else:
                self.reply_message(message_id, fmt.format_current_dir(current_dir))
            return

        success, result = self.set_working_dir(chat_id, path)
        if success:
            self.add_reaction(message_id, EmojiReaction.on_dir_changed())
            card_res = CardBuilder.build_directory_change_card(project, result, success=True)
            if card_res:
                msg_type, card_content = card_res
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id and project:
                    self.register_message_project(response_id, project)
            else:
                self.reply_message(message_id, fmt.format_dir_change(result, True))
        else:
            self.add_reaction(message_id, EmojiReaction.on_error())
            card_res = CardBuilder.build_directory_change_card(project, result, success=False)
            if card_res:
                msg_type, card_content = card_res
                self.reply_message(message_id, card_content, msg_type=msg_type)
            else:
                self.reply_message(message_id, fmt.format_error(result))

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    def show_help(self, message_id: str, chat_id: str):
        project = self.project_manager.get_active_project(chat_id)
        self.show_full_help(message_id, chat_id, project)

    def show_full_help(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.handle_help_category(message_id, chat_id, "main", project)

    def show_coco_status(self, message_id: str, chat_id: str):
        manager = get_coco_model_manager()
        current_model = manager.get_current_model()
        models = manager.get_models().models

        content = CardBuilder.build_coco_status_content(current_model, models)
        self.reply_message(message_id, content)

    def show_tools_list(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show a list of all available ACP tools with quick access buttons."""
        # Define tool names
        names = ["coco", "claude", "aiden", "codex", "gemini"]
        emojis = {
            "coco": "🤖",
            "claude": "🔮",
            "aiden": "🎯",
            "codex": "💻",
            "gemini": "✨",
        }

        # Cached-first availability check: avoid blocking user-path on external probe.
        tools = []
        for name in names:
            is_available = tool_registry.get_availability(name, allow_sync_probe=False, trigger_async_probe=True)
            desc = UI_TEXT[f"system_acp_tool_desc_{name}"]
            tools.append(
                {
                    "name": name,
                    "emoji": emojis.get(name, "🤖"),
                    "description": desc,
                    "available": is_available,
                }
            )

        msg_type, card = CardBuilder.build_tools_list_card(tools, project)
        self.reply_interactive_card(message_id, card, msg_type=msg_type)

    def show_tools_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show detailed status of all tools with availability and session info."""
        # Define tool metadata
        tool_defs = [
            {"name": "coco", "emoji": "🤖", "manager": self.ctx.coco_manager},
            {"name": "claude", "emoji": "🔮", "manager": self.ctx.claude_manager},
            {"name": "aiden", "emoji": "🎯", "manager": self.ctx.aiden_manager},
            {"name": "codex", "emoji": "💻", "manager": self.ctx.codex_manager},
            {"name": "gemini", "emoji": "✨", "manager": self.ctx.gemini_manager},
        ]

        def _format_last_used(ts: float) -> str:
            """格式化最近使用时间，基于共享 TimeAgo 语义层。

            语义边界（秒 → bucket）交给 ``compute_time_ago_bucket`` 处理，
            本函数只负责结合现有 UI_TEXT 模板渲染具体文案，以保持系统
            状态卡片的既有风格。
            """

            try:
                raw_ts = float(ts or 0.0)
            except Exception:
                return UI_TEXT["system_unknown"]

            if raw_ts <= 0.0:
                return UI_TEXT["system_never_used"]

            try:
                idle_seconds = max(0, int(time.time() - raw_ts))
            except Exception:
                return UI_TEXT["system_unknown"]

            from src.utils.time_ago import compute_time_ago_bucket

            bucket = compute_time_ago_bucket(idle_seconds)
            kind = bucket["kind"]
            value = int(bucket["value"])

            # seconds 区间：保持原有「X 秒前」样式（使用实际 idle 秒数）
            if kind == "seconds":
                return UI_TEXT["time_secs_ago"].format(seconds=idle_seconds)

            # minutes 区间：使用 bucket 的分钟值 + 余下秒数，保留原有模板
            if kind == "minutes":
                m = value
                s = max(0, idle_seconds - m * 60)
                return UI_TEXT["time_mins_secs_ago"].format(minutes=m, seconds=s)

            # hours/days 统归为「X 小时 Y 分钟前」风格，避免新增文案 key
            total_minutes = idle_seconds // 60
            h, m = divmod(total_minutes, 60)
            return UI_TEXT["time_hours_mins_ago"].format(hours=h, minutes=m)

        # Gather availability + real session activity from ACP managers.
        tools = []
        active_sessions: dict[str, dict] = {}
        for meta in tool_defs:
            name = meta["name"]
            manager = meta["manager"]
            is_available = tool_registry.get_availability(name, allow_sync_probe=False, trigger_async_probe=True)

            sessions = []
            try:
                sessions = manager.list_active_sessions(chat_id=chat_id)
            except Exception:
                sessions = []

            last_active_ts = 0.0
            if sessions:
                try:
                    last_active_ts = max(float(s.get("last_active", 0.0) or 0.0) for s in sessions)
                except Exception:
                    last_active_ts = 0.0

            tools.append(
                {
                    "name": name,
                    "emoji": meta["emoji"],
                    "available": is_available,
                    "last_used": _format_last_used(last_active_ts),
                }
            )
            if sessions:
                # Card expects one active summary line; provide latest session in that tool.
                latest = None
                try:
                    latest = max(sessions, key=lambda s: float(s.get("last_active", 0.0) or 0.0))
                except Exception:
                    latest = sessions[0]
                if latest:
                    # chat_id 由 ACPSessionManager.list_active_sessions 统一解析并暴露，避免外部再做手工 split
                    session_chat_id = str(latest.get("chat_id") or "") or "N/A"
                    active_sessions[name] = {
                        "chat_id": session_chat_id,
                        "session_id": str(latest.get("session_id", "") or ""),
                        "message_count": int(latest.get("message_count", 0) or 0),
                    }

        msg_type, card = CardBuilder.build_tools_status_card(tools, active_sessions, project)
        self.reply_interactive_card(message_id, card, msg_type=msg_type)
