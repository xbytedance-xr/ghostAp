"""System handler — help, exit mode, shell commands, directory switching, intercepted commands."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from acp.stdio import spawn_agent_process

from ...acp.helper import fetch_acp_models, list_acp_tools, probe_acp_models
from ...acp.providers import tool_registry
from ...card import CardBuilder
from ...card.builders.system import SystemBuilder
from ...card.styles import UI_TEXT
from ...coco_model import get_coco_model_manager
from ...tasking import TaskPriority, TaskSpec
from ...ttadk import get_ttadk_manager
from ...ttadk.manager import auto_update_ttadk
from ...utils.path import normalize_ttadk_cwd
from ...utils.errors import get_error_detail
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class SystemHandler(BaseHandler):
    """Help, exit, shell, directory, and intercepted-command handling."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self._init_command_registry()
        self._ttadk_flow_start_times: dict[str, float] = {}
        self._ttadk_flow_last_duration_ms: dict[str, int] = {}

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
            "/wt": lambda m, c, t, p: self.handle_worktree_command(m, c, p),
            "/worktree": lambda m, c, t, p: self.handle_worktree_command(m, c, p),
            "/ttadk_info": lambda m, c, t, p: self.show_ttadk_info(m, c),
            "/ttadk_refresh": lambda m, c, t, p: self.refresh_ttadk_models(m, c, p),
            "/menu": lambda m, c, t, p: self.handle_menu_command(m, c, p),
            "/tools": lambda m, c, t, p: self.show_tools_list(m, c, p),
            "/tools_status": lambda m, c, t, p: self.show_tools_status(m, c, p),
            "/model": lambda m, c, t, p: self.handle_model_command(m, c, t, p),
        }

        # Prefix match handlers: prefix -> handler_func(message_id, chat_id, text, project)
        # Note: Order matters if prefixes overlap (not the case here yet)
        self._prefix_handlers = [
            ("/status", lambda m, c, t, p: self.get_handler("diagnostics").show_unified_status(m, c, t, p)),
            ("/tasks", lambda m, c, t, p: self.get_handler("diagnostics").show_task_board(m, c, t, p)),
            ("/diff", lambda m, c, t, p: self.get_handler("diagnostics").show_context_diff(m, c, t, p)),
            ("/trace", lambda m, c, t, p: self.get_handler("diagnostics").show_message_trace(m, c, t, p)),
            ("/worktree ", self._handle_worktree_prefix_command),
            ("/wt ", self._handle_worktree_prefix_command),
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

    def _handle_worktree_prefix_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]
    ):
        """Parse '/wt <goal>' or '/worktree <goal>' and delegate to handle_worktree_command."""
        text_lower = text.lower().strip()
        if text_lower.startswith("/worktree"):
            goal = text[len("/worktree"):].strip()
        else:
            goal = text[len("/wt"):].strip()
        self.handle_worktree_command(message_id, chat_id, project, goal=goal)

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

        msg_type, card_content = CardBuilder.build_help_card(project, category, current_dir, current_mode)

        if origin_message_id:
            if self.patch_message(origin_message_id, card_content):
                return

        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_deep_prompt(self, message_id: str, chat_id: str):
        self.reply_message(
            message_id,
            UI_TEXT["system_help_deep_prompt"],
        )

    def refresh_ttadk_models(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """强制刷新 TTADK 当前工具的真实模型列表（优先 probe），并返回诊断摘要。"""
        manager = get_ttadk_manager()
        cwd = None
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project=project)
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(where="SystemHandler.refresh_ttadk_models", raw_cwd=raw_cwd, normalized_cwd=cwd)
        except Exception:
            cwd = None

        tool = manager.get_current_tool() or ""
        try:
            result = manager.refresh_models(tool_name=tool or None, cwd=cwd)
        except Exception as e:
            self.reply_error(
                message_id, get_error_detail(e), title=UI_TEXT["system_ttadk_refresh_error"]
            )
            return

        msg_type, card_content = CardBuilder.build_ttadk_refresh_result_card(tool, result)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def _maybe_log_ttadk_cwd(self, *, where: str, raw_cwd: Optional[str], normalized_cwd: Optional[str]) -> None:
        """TTADK cwd 归一化的可观测日志（debug + 配置开关）。"""
        try:
            from ...config import get_settings

            if not bool(getattr(get_settings(), "ttadk_cwd_debug_enabled", False)):
                return
        except Exception:
            return
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            is_abs = bool(normalized_cwd) and Path(str(normalized_cwd)).is_absolute()
        except Exception:
            is_abs = False
        logger.debug(
            "[TTADK:CWD] where=%s raw_cwd=%r normalized_cwd=%r is_abs=%s",
            str(where or ""),
            raw_cwd,
            normalized_cwd,
            bool(is_abs),
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
        tools = list_acp_tools()
        if not tools:
            self.reply_error(message_id, UI_TEXT["system_acp_no_available_tools"])
            return
        msg_type, card_content = CardBuilder.build_acp_tool_select_card(tools, project_id)
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

        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)

        current_model = None
        if project and getattr(project, "acp_tool_name", "") == tool:
            current_model = getattr(project, "acp_model_name", None)

        models = self._fetch_acp_models(tool, cwd=cwd, current_model=current_model)
        if not models:
            self.reply_error(message_id, UI_TEXT["system_acp_get_models_failed"].format(tool=tool))
            return

        msg_type, card_content = CardBuilder.build_acp_model_select_card(models, tool, project_id)
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
            msg_type, card_content = CardBuilder.build_acp_model_select_card(models, tool_name, project_id)
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
    # TTADK command handling
    # ------------------------------------------------------------------
    def _resolve_ttadk_cwd(
        self,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        project_id: Optional[str] = None,
    ) -> Optional[str]:
        if project:
            return project.root_path
        if project_id:
            ctx = self.project_manager.get_project(project_id)
            if ctx:
                return ctx.root_path
        active = self.project_manager.get_active_project(chat_id)
        if active:
            return active.root_path
        return None

    def _resolve_ttadk_yolo_enabled(
        self,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        project_id: Optional[str] = None,
    ) -> bool:
        if project is not None:
            return bool(getattr(project, "ttadk_yolo_enabled", False))
        if project_id:
            ctx = self.project_manager.get_project(project_id)
            if ctx is not None:
                return bool(getattr(ctx, "ttadk_yolo_enabled", False))
        active = self.project_manager.get_active_project(chat_id)
        if active is not None:
            return bool(getattr(active, "ttadk_yolo_enabled", False))
        return bool(getattr(self.settings, "ttadk_yolo_default_enabled", False))

    def _apply_ttadk_yolo_enabled(
        self,
        chat_id: str,
        enabled: bool,
        project: Optional["ProjectContext"] = None,
        project_id: Optional[str] = None,
    ) -> Optional["ProjectContext"]:
        target = project
        if target is None and project_id:
            target = self.project_manager.get_project(project_id)
        if target is None:
            target = self.project_manager.get_active_project(chat_id)
        if target is not None:
            target.ttadk_yolo_enabled = bool(enabled)
        return target

    def _pick_ttadk_auto_model(
        self,
        models: list,
        *,
        project: Optional["ProjectContext"] = None,
        current_model: Optional[str] = None,
    ) -> Optional[str]:
        if not models:
            return None
        normalized = [m for m in models if getattr(m, "name", None)]
        if not normalized:
            return None
        model_names = {m.name: m for m in normalized}

        if project:
            project_model = str(getattr(project, "ttadk_model_name", "") or "").strip()
            if project_model and project_model in model_names:
                return project_model

        for model in normalized:
            if bool(getattr(model, "is_default", False)):
                return model.name

        settings_model = str(getattr(self.settings, "ttadk_default_model", "") or "").strip()
        if settings_model and settings_model in model_names:
            return settings_model

        if current_model and current_model in model_names:
            return current_model

        if len(normalized) == 1:
            return normalized[0].name
        return None

    def _pick_ttadk_auto_tool(
        self,
        tools: list,
        *,
        project: Optional["ProjectContext"] = None,
        current_tool: Optional[str] = None,
    ) -> Optional[str]:
        if not tools:
            return None
        normalized = [t for t in tools if getattr(t, "name", None)]
        if not normalized:
            return None
        tool_names = {t.name: t for t in normalized}

        if project:
            project_tool = str(getattr(project, "ttadk_tool_name", "") or "").strip().lower()
            if project_tool and project_tool in tool_names:
                return project_tool

        settings_tool = str(getattr(self.settings, "ttadk_default_tool", "") or "").strip().lower()
        if settings_tool and settings_tool in tool_names:
            return settings_tool

        if current_tool and current_tool in tool_names:
            return current_tool

        if len(normalized) == 1:
            return normalized[0].name
        return None

    def _mark_ttadk_flow_start(self, chat_id: str) -> None:
        self._ttadk_flow_start_times[chat_id] = time.perf_counter()

    def _report_ttadk_flow_duration(self, chat_id: str, project_id: Optional[str], where: str) -> None:
        start = self._ttadk_flow_start_times.pop(chat_id, None)
        if start is None:
            return
        duration_ms = int(round((time.perf_counter() - start) * 1000))
        self._ttadk_flow_last_duration_ms[chat_id] = duration_ms
        logger.info(
            "ttadk_flow_duration_ms=%s chat_id=%s project_id=%s where=%s",
            duration_ms,
            chat_id,
            project_id,
            where,
        )

    # ------------------------------------------------------------------
    # Worktree commands
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
        # 若用户在命令级直接提供了 goal，则将其记录到旅程高层状态机中，
        # 以便后续卡片/执行路径统一从 WorktreeJourneyState 读取目标与阶段。
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
            tools, selected_dicts, project_id, goal=goal,
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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
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

        # Resolve goal: card value > state.selection.pending_goal
        mgr = self._worktree_manager()
        state = mgr.get_state(project)
        goal = str(value.get("goal") or value.get("_input_value") or state.selection.pending_goal or "").strip()
        if goal:
            state.selection.pending_goal = goal

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

        # Logic to skip model selection card if tool has only one model or is in skip-list
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
                goal=goal,
            )
        else:
            # Auto-add pending item, picking a model if available
            model_name = None
            model_display = None
            if models:
                # Use default model if marked, else first one
                target = next((m for m in models if m.get("is_default")), models[0])
                model_name = target["name"]
                model_display = target.get("display_name")

            state, _, msg = mgr.add_pending_item(
                project, model_name=model_name, model_display_name=model_display
            )
            mgr.back_to_tool_selection(project)

            # Fast path: goal exists -> skip tool list, auto-execute
            if goal:
                # Provide immediate feedback on the current card
                selected_dicts = [item.to_dict() for item in mgr.get_state(project).selection.selected_items]
                self.patch_message(
                    message_id,
                    CardBuilder.build_worktree_tool_select_card(
                        self._get_available_worktree_tools(),
                        selected_dicts,
                        pid,
                        message=UI_TEXT["worktree_auto_executing_banner"],
                        goal=goal,
                    )[1]
                )
                self.handle_finish_worktree_selection(message_id, chat_id, project_id=pid, value=value)
                return

            state = mgr.get_state(project)
            tools = self._get_available_worktree_tools()
            selected_dicts = [item.to_dict() for item in state.selection.selected_items]
            msg_type, card = CardBuilder.build_worktree_tool_select_card(
                tools,
                selected_dicts,
                pid,
                message=msg,
                goal=goal,
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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        model_name = value.get("_option") or value.get("model_name") or None
        model_display = value.get("model_display_name") or model_name

        mgr = self._worktree_manager()

        state = mgr.get_state(project)
        goal = str(value.get("goal") or state.selection.pending_goal or "").strip()
        if goal:
            state.selection.pending_goal = goal

        # Capture pending tool info BEFORE add_pending_item clears it
        pending_tool = state.selection.pending_item

        state, added, msg = mgr.add_pending_item(project, model_name=model_name, model_display_name=model_display)
        # Back to tool selection for next tool
        mgr.back_to_tool_selection(project)

        # Fast path: goal exists -> skip tool list, auto-execute
        if goal:
            # Provide immediate feedback on the current card
            if pending_tool:
                cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)
                current_model = None
                if project and getattr(project, "acp_tool_name", "") == pending_tool.tool_name:
                    current_model = getattr(project, "acp_model_name", None)

                self.patch_message(
                    message_id,
                    CardBuilder.build_worktree_model_select_card(
                        self._get_models_for_tool(
                            pending_tool.tool_name,
                            provider=pending_tool.provider,
                            cwd=cwd,
                            current_model=current_model,
                        ),
                        pending_tool.display_name,
                        [item.to_dict() for item in state.selection.selected_items],
                        project.project_id,
                        message=UI_TEXT["worktree_auto_executing_banner"],
                        goal=goal,
                    )[1]
                )
            self.handle_finish_worktree_selection(message_id, chat_id, project_id=project_id, value=value)
            return

        state = mgr.get_state(project)
        selected_dicts = [item.to_dict() for item in state.selection.selected_items]
        tools = self._get_available_worktree_tools()
        pid = project.project_id
        msg_type, card = CardBuilder.build_worktree_tool_select_card(
            tools, selected_dicts, pid, message=msg, goal=goal,
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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()

        # Resolve goal: card button value > card input > state.selection.pending_goal
        state = mgr.get_state(project)
        goal = str(
            value.get("goal")
            or value.get("worktree_goal")
            or value.get("_input_value")
            or state.selection.pending_goal
            or ""
        ).strip()

        state = mgr.finalize_selection(project)
        pid = project.project_id

        if not state.enabled:
            self.reply_error(message_id, UI_TEXT["system_worktree_no_selection_error"])
            return

        # Fast path: goal exists → skip confirm card, auto-execute
        if goal:
            # Provide immediate feedback on the current card
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

        # Fallback: no goal → show confirm card (backward compatible)
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
        """Fast path: ensure_worktrees → set units ready → execute with silent_mode."""
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state = mgr.ensure_worktrees(project)

        if state.last_error:
            self.reply_error(message_id, UI_TEXT["system_worktree_create_failed"].format(error=state.last_error))
            return

        # 静默自动执行路径：标记旅程已进入 AUTO_EXECUTING 阶段，并记录目标与 silent_mode。
        mgr.apply_journey_event(state, event="auto_execute_started", goal=goal, silent_mode=True)

        for unit in state.units:
            unit.status = "ready"

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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state = mgr.ensure_worktrees(project)

        if state.last_error:
            self.reply_error(message_id, UI_TEXT["system_worktree_create_failed"].format(error=state.last_error))
            return

        # Mark all units as "ready" so the message-routing interception can
        # detect that worktree mode is awaiting a user goal.
        for unit in state.units:
            unit.status = "ready"

        # Check if user provided a goal in the input box
        goal = str(value.get("worktree_goal") or value.get("_input_value") or "").strip()

        if goal:
            # Provide immediate feedback on the current card
            # 非静默路径：用户在确认卡中输入目标后直接开始执行，视作自动执行入口，silent_mode=False。
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
            # If goal is present, skip the "waiting" step and go straight to execution
            self.handle_worktree_execute(message_id, chat_id, goal, project=project)
            return

        # Show progress card with "ready" status
        units_dicts = [u.to_dict() for u in state.units]
        pid = project.project_id
        msg_type, card = CardBuilder.build_worktree_progress_card(
            units_dicts, pid, message=UI_TEXT["system_worktree_created_prompt"],
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
        """Route user message as a worktree goal — trigger parallel execution.

        When *silent_mode* is True (auto-execute fast path), throttle interval
        is raised to 30s and a 10-min timeout safety-valve notification is sent.
        """
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

        # Send an initial "executing" progress card
        state = mgr.get_state(project)
        units_dicts = [u.to_dict() for u in state.units]
        if silent_mode:
            init_msg = UI_TEXT["worktree_start_silent"].format(
                goal=goal[:80]
            )
        else:
            init_msg = UI_TEXT["worktree_executing"].format(goal=goal[:80])
        msg_type, card = CardBuilder.build_worktree_progress_card(
            units_dicts, pid, message=init_msg,
        )
        progress_mid = self.send_message(chat_id, card, msg_type=msg_type)

        # Build a throttled callback for live progress updates
        _update_lock = threading.Lock()
        _last_update: list[float] = [0.0]
        _THROTTLE_INTERVAL = 30.0 if silent_mode else 0.5  # seconds
        _exec_start = time.time()
        _TIMEOUT_NOTIFY = 600.0  # 10 minutes
        _timeout_notified: list[bool] = [False]

        def _on_unit_update(unit):
            now = time.time()
            elapsed = now - _exec_start
            with _update_lock:
                # Silent mode 10-min timeout safety-valve
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
                            cur_dicts, pid,
                            message=msg,
                        )
                        if progress_mid:
                            self.patch_message(progress_mid, cd, msg_type=mt)
                    except Exception:
                        pass
                    return

                if now - _last_update[0] < _THROTTLE_INTERVAL:
                    return
                _last_update[0] = now
            try:
                cur_state = mgr.get_state(project)
                cur_dicts = [u.to_dict() for u in cur_state.units]
                msg = UI_TEXT["worktree_executing_live"].format(goal=goal[:60])
                mt, cd = CardBuilder.build_worktree_progress_card(
                    cur_dicts, pid, message=msg,
                )
                if progress_mid:
                    self.patch_message(progress_mid, cd, msg_type=mt)
            except Exception:
                pass

        # Execute in the current thread (already inside a task-scheduler slot)
        state = mgr.execute_goal(project, goal, on_unit_update=_on_unit_update)

        # Show final result — cleanup card with merge/cleanup entry
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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        state, merge_results = mgr.merge_to_base(project)

        if state.last_error:
            self.reply_error(message_id, state.last_error)
            return

        pid = project.project_id
        msg_type, card = CardBuilder.build_worktree_cleanup_card(
            state.merge_notes, pid, state.base_branch or "main", merge_results=merge_results,
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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        mgr.cleanup_worktrees(project)
        self.reply_message(message_id, UI_TEXT["system_worktree_cleanup_success"])

    def handle_worktree_execute_action(
        self,
        message_id: str,
        chat_id: str,
        project_id: Optional[str] = None,
        value: dict | None = None,
    ):
        """Card action: execute worktree goal from progress card input."""
        value = value or {}
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        goal = str(value.get("worktree_goal") or value.get("_input_value") or "").strip()
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
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_error(message_id, UI_TEXT["system_worktree_project_not_found"])
            return

        mgr = self._worktree_manager()
        pid = project.project_id
        state = mgr.get_state(project)

        # Guard: reject if any unit is still running
        if any(u.status == "running" for u in state.units):
            self.reply_message(message_id, UI_TEXT["system_worktree_unit_running_error"])
            return

        # Send an initial progress card indicating retry is starting
        units_dicts = [u.to_dict() for u in state.units]
        msg_type, card = CardBuilder.build_worktree_progress_card(
            units_dicts, pid, message=UI_TEXT["system_worktree_retry_starting"],
        )
        progress_mid = self.send_message(chat_id, card, msg_type=msg_type)

        # Throttled live progress callback (same pattern as handle_worktree_execute)
        _update_lock = threading.Lock()
        _last_update: list[float] = [0.0]
        _THROTTLE_INTERVAL = 0.5

        def _on_unit_update(unit):
            now = time.time()
            with _update_lock:
                if now - _last_update[0] < _THROTTLE_INTERVAL:
                    return
                _last_update[0] = now
            try:
                cur_state = mgr.get_state(project)
                cur_dicts = [u.to_dict() for u in cur_state.units]
                mt, cd = CardBuilder.build_worktree_progress_card(
                    cur_dicts, pid, message=UI_TEXT["worktree_executing_live"].format(goal=UI_TEXT["system_worktree_retry_goal"]),
                )
                if progress_mid:
                    self.patch_message(progress_mid, cd, msg_type=mt)
            except Exception:
                pass

        # Execute retry
        state = mgr.retry_failed_units(project, on_unit_update=_on_unit_update)

        # Show final result
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

    def _reply_ttadk_load_hint(self, message_id: str, text: str, project_id: Optional[str] = None) -> None:
        msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(text, project_id=project_id)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_ttadk_command(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        force_select: bool = False,
    ):
        project = project or self.project_manager.get_active_project(chat_id)
        project_id = project.project_id if project else None
        manager = get_ttadk_manager()

        auto_update_ttadk()

        self._mark_ttadk_flow_start(chat_id)

        result = manager.get_tools()
        if result.error:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_list_load_error"].format(error=result.error), project_id=project_id
            )
            return

        # Fetch models for each tool to build combined card
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project=project, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
        except Exception:
            cwd = None

        models_by_tool: dict[str, list] = {}
        for tool in (result.tools or []):
            try:
                prev_tool = manager.get_current_tool()
                if prev_tool != tool.name:
                    manager.set_tool(tool.name)
                models_result = manager.get_models(cwd=cwd)
                models_by_tool[tool.name] = models_result.models or []
                # Restore previous tool
                if prev_tool and prev_tool != tool.name:
                    manager.set_tool(prev_tool)
            except Exception:
                models_by_tool[tool.name] = []

        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=project, project_id=project_id)
        msg_type, card_content = CardBuilder.build_ttadk_combined_select_card(
            result.tools, models_by_tool, project_id, yolo_enabled=yolo_enabled
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def show_ttadk_info(self, message_id: str, chat_id: str):
        manager = get_ttadk_manager()
        current_tool = manager.get_current_tool()
        current_model = manager.get_current_model()
        tools_result = manager.get_tools()
        raw_cwd = self._resolve_ttadk_cwd(chat_id)
        norm_cwd = normalize_ttadk_cwd(raw_cwd)
        self._maybe_log_ttadk_cwd(where="SystemHandler.show_ttadk_info", raw_cwd=raw_cwd, normalized_cwd=norm_cwd)
        models_result = manager.get_models(cwd=norm_cwd)
        tool_desc = {t.name: t.description for t in (tools_result.tools or [])}
        model_desc = {m.name: m.description for m in (models_result.models or [])}

        content = SystemBuilder.build_ttadk_info_content(
            current_tool, current_model, tool_desc, model_desc
        )
        self.reply_message(message_id, content)

    def handle_select_ttadk_tool(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        manager = get_ttadk_manager()
        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(
                where="SystemHandler.handle_select_ttadk_tool", raw_cwd=raw_cwd, normalized_cwd=cwd
            )
        except Exception:
            cwd = None
        logger.info(
            "[TTADK] 选择工具: chat_id=%s project_id=%s tool=%s cwd=%s",
            chat_id,
            project_id,
            tool_name,
            cwd,
        )
        success = manager.set_tool(tool_name)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_tool_error"].format(tool=tool_name), project_id=project_id
            )
            return
        if project:
            project.ttadk_tool_name = tool_name
            current_model = manager.get_current_model()
            if current_model:
                project.ttadk_model_name = current_model

        result = manager.get_models(cwd=cwd)
        if result.error:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_model_load_error"].format(error=result.error), project_id=project_id
            )
            return

        # 只有在模型列表为空且有警告时才发送单独的警告消息
        # 其他情况（如 official_cli_disabled）不影响使用，不单独发送
        warnings = getattr(result, "warnings", None) or []
        has_models = bool(result.models)
        critical_warnings = [w for w in warnings if w in ("models_untrusted", "missing_tool")]

        if (not has_models and warnings) or critical_warnings:
            # 模型列表为空且有警告，或者有严重警告（如 models_untrusted），发送警告消息
            w_str = "; ".join(critical_warnings if critical_warnings else warnings)
            msg = UI_TEXT["system_ttadk_model_warning"].format(
                warnings=w_str
            )
            self.reply_message(message_id, msg)

        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=project, project_id=project_id)
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
            result.models, tool_name, project_id, yolo_enabled=yolo_enabled
        )
        patched = self.patch_message(message_id, card_content)
        if not patched:
            self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_select_ttadk_model(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
        silent: bool = False,
    ):
        project_id = project.project_id if project else None
        if not silent:
            # 立即给予用户反馈，避免"没反应"
            msg_type, card_content = CardBuilder.build_switching_status_card(tool_name, model_name)
            self.reply_message(message_id, card_content, msg_type=msg_type)

        manager = get_ttadk_manager()
        logger.info(
            "[TTADK] 选择模型: chat_id=%s project_id=%s tool=%s model=%s",
            chat_id,
            getattr(project, "project_id", None),
            tool_name,
            model_name,
        )
        success = manager.set_model(model_name)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_model_error"].format(model=model_name), project_id=project_id
            )
            return

        target_project = project or self.project_manager.get_active_project(chat_id)
        if target_project:
            target_project.ttadk_tool_name = tool_name or manager.get_current_tool()
            target_project.ttadk_model_name = model_name

        ttadk_handler = self.get_handler("ttadk")
        if ttadk_handler:
            ttadk_handler.current_tool = tool_name
            ttadk_handler.current_model = model_name
            ttadk_handler.enter_mode(message_id, chat_id, project=target_project)
            project_id = target_project.project_id if target_project else None
            self._report_ttadk_flow_duration(chat_id, project_id, "enter_mode")
        else:
            self.reply_error(message_id, UI_TEXT["system_ttadk_handler_uninitialized"])

    def handle_select_ttadk_combined(
        self,
        message_id: str,
        chat_id: str,
        tool_name: str,
        model_name: str,
        project: Optional["ProjectContext"] = None,
    ):
        """Handle the combined tool+model selection from the single-step card."""
        manager = get_ttadk_manager()
        project = project or self.project_manager.get_active_project(chat_id)
        project_id = project.project_id if project else None

        # Set tool first
        tool = (tool_name or "").strip().lower()
        model = (model_name or "").strip()
        if not tool or not model:
            self.reply_error(message_id, UI_TEXT["system_ttadk_no_tool"])
            return

        success = manager.set_tool(tool)
        if not success:
            self._reply_ttadk_load_hint(
                message_id, UI_TEXT["system_ttadk_switch_tool_error"].format(tool=tool), project_id=project_id
            )
            return

        if project:
            project.ttadk_tool_name = tool

        # Then delegate to the existing model selection handler
        self.handle_select_ttadk_model(
            message_id, chat_id, tool, model, project=project, silent=False
        )

    def handle_refresh_ttadk_models(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        manager = get_ttadk_manager()
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(
                where="SystemHandler.handle_refresh_ttadk_models", raw_cwd=raw_cwd, normalized_cwd=cwd
            )
        except Exception:
            cwd = None

        tool = (tool_name or manager.get_current_tool() or "").strip().lower()
        if not tool:
            self.reply_message(message_id, UI_TEXT["system_ttadk_no_tool"])
            return

        try:
            result = manager.refresh_models(tool_name=tool, cwd=cwd)
        except Exception as e:
            self.reply_error(message_id, get_error_detail(e), title=UI_TEXT["system_ttadk_refresh_error"])
            return

        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project_id=project_id)
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
            result.models or [], tool, project_id, yolo_enabled=yolo_enabled
        )
        patched = self.patch_message(message_id, card_content)
        if not patched:
            self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_toggle_ttadk_yolo(
        self,
        message_id: str,
        chat_id: str,
        enabled: bool,
        view: str = "tool_select",
        tool_name: str = "",
        project_id: Optional[str] = None,
    ):
        manager = get_ttadk_manager()
        target_project = self._apply_ttadk_yolo_enabled(chat_id, enabled, project_id=project_id)
        yolo_enabled = self._resolve_ttadk_yolo_enabled(chat_id, project=target_project, project_id=project_id)

        if view == "model_select":
            tool = (tool_name or manager.get_current_tool() or "").strip().lower()
            if not tool:
                self.reply_message(message_id, UI_TEXT["system_ttadk_no_tool"])
                return
            try:
                raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=project_id)
                cwd = normalize_ttadk_cwd(raw_cwd)
                self._maybe_log_ttadk_cwd(
                    where="SystemHandler.handle_toggle_ttadk_yolo", raw_cwd=raw_cwd, normalized_cwd=cwd
                )
            except Exception:
                cwd = None

            if manager.get_current_tool() != tool:
                manager.set_tool(tool)
            result = manager.get_models(cwd=cwd)
            if result.error:
                self.reply_error(message_id, result.error, title=UI_TEXT["system_ttadk_get_tools_error"])
                return
            msg_type, card_content = CardBuilder.build_ttadk_model_select_card(
                result.models or [], tool, project_id, yolo_enabled=yolo_enabled
            )
            patched = self.patch_message(message_id, card_content)
            if not patched:
                self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        tools_result = manager.get_tools()
        if tools_result.error:
            self.reply_error(message_id, tools_result.error, title=UI_TEXT["system_ttadk_get_tools_error"])
            return
        msg_type, card_content = CardBuilder.build_ttadk_tool_select_card(
            tools_result.tools, project_id, yolo_enabled=yolo_enabled
        )
        patched = self.patch_message(message_id, card_content)
        if not patched:
            self.reply_message(message_id, card_content, msg_type=msg_type)

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
                    pass

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
        result = executor.execute(cmd, cwd=working_dir, interactive=False)
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
            pass
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
                sessions = manager.list_active_sessions()
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
                    chat_id = str(latest.get("chat_id") or "") or "N/A"
                    active_sessions[name] = {
                        "chat_id": chat_id,
                        "session_id": str(latest.get("session_id", "") or ""),
                        "message_count": int(latest.get("message_count", 0) or 0),
                    }

        msg_type, card = CardBuilder.build_tools_status_card(tools, active_sessions, project)
        self.reply_interactive_card(message_id, card, msg_type=msg_type)
