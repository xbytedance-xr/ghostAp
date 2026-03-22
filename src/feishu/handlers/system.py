"""System handler — help, exit mode, shell commands, directory switching, intercepted commands."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from acp.stdio import spawn_agent_process

from ...card import CardBuilder
from ...card.builders.system import SystemBuilder
from ...coco_model import get_coco_model_manager
from ...tasking import TaskPriority, TaskSpec
from ...ttadk import get_ttadk_manager
from ...utils.path import normalize_ttadk_cwd
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


@dataclass
class _ACPToolOption:
    name: str
    description: str = ""
    is_default: bool = False


@dataclass
class _ACPModelOption:
    name: str
    description: str = ""
    is_default: bool = False


class SystemHandler(BaseHandler):
    """Help, exit, shell, directory, and intercepted-command handling."""

    # Reference to programming handlers set by ws_client after construction
    coco_handler = None
    claude_handler = None
    aiden_handler = None
    codex_handler = None
    gemini_handler = None
    ttadk_handler = None
    project_handler = None
    deep_handler = None
    loop_handler = None
    spec_handler = None
    diagnostics_handler = None

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self._init_command_registry()

    def _init_command_registry(self):
        """Initialize the command dispatch registry."""
        # Exact match handlers: command -> handler_func(message_id, chat_id, text, project)
        self._exact_handlers = {
            "/help": lambda m, c, t, p: self.show_full_help(m, c, p),
            "/帮助": lambda m, c, t, p: self.show_full_help(m, c, p),
            "/coco_status": lambda m, c, t, p: self.show_coco_status(m, c),
            "/coco_info": lambda m, c, t, p: self.coco_handler.show_info(m, c, p),
            "/claude_info": lambda m, c, t, p: self.claude_handler.show_info(m, c, p),
            "/aiden_info": lambda m, c, t, p: self.aiden_handler.show_info(m, c, p),
            "/codex_info": lambda m, c, t, p: self.codex_handler.show_info(m, c, p),
            "/gemini_info": lambda m, c, t, p: self.gemini_handler.show_info(m, c, p),
            "/projects": lambda m, c, t, p: self.project_handler.show_project_board(m, c),
            "/project": lambda m, c, t, p: self.project_handler.show_project_board(m, c),
            "/switch": lambda m, c, t, p: self.project_handler.show_project_board(m, c),
            "/ttadk": lambda m, c, t, p: self.handle_ttadk_command(m, c, p),
            "/acp": lambda m, c, t, p: self.handle_acp_command(m, c, p),
            "/ttadk_info": lambda m, c, t, p: self.show_ttadk_info(m, c),
            "/ttadk_refresh": lambda m, c, t, p: self.refresh_ttadk_models(m, c, p),
            "/menu": lambda m, c, t, p: self.handle_menu_command(m, c, p),
            "/tools": lambda m, c, t, p: self.show_tools_list(m, c, p),
            "/tools_status": lambda m, c, t, p: self.show_tools_status(m, c, p),
        }

        # Prefix match handlers: prefix -> handler_func(message_id, chat_id, text, project)
        # Note: Order matters if prefixes overlap (not the case here yet)
        self._prefix_handlers = [
            ("/status", lambda m, c, t, p: self.diagnostics_handler.show_unified_status(m, c, t, p)),
            ("/tasks", lambda m, c, t, p: self.diagnostics_handler.show_task_board(m, c, t, p)),
            ("/diff", lambda m, c, t, p: self.diagnostics_handler.show_context_diff(m, c, t, p)),
            ("/trace", lambda m, c, t, p: self.diagnostics_handler.show_message_trace(m, c, t, p)),
            ("/switch ", self._handle_switch_command),
            ("/new ", self._handle_new_project_command),
            ("/close ", self._handle_close_command),
        ]

    def _handle_switch_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]):
        name = text[8:].strip()
        if name:
            self.project_handler.switch_project(
                message_id,
                chat_id,
                name,
                coco_handler=self.coco_handler,
                claude_handler=self.claude_handler,
            )
        else:
            self.project_handler.show_project_board(message_id, chat_id)

    def _handle_new_project_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]
    ):
        from ...card.styles import UI_TEXT

        parts = text[5:].strip().split(None, 1)
        name = parts[0] if parts else ""
        path = parts[1] if len(parts) > 1 else self.get_working_dir(chat_id)
        if name:
            self.project_handler.create_project(message_id, chat_id, name, path)
        else:
            self.reply_error(
                message_id, UI_TEXT.get("system_new_project_usage", "用法: `/new 项目名 [路径]`"), title="参数错误"
            )

    def _handle_close_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"]):
        name = text[7:].strip()
        if name:
            self.project_handler.close_project(message_id, chat_id, name)

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
            "/ttadk_refresh",
            "/menu",
        }
        if text_lower in exact_commands:
            return True
        prefix_commands = ("/switch ", "/new ", "/close ", "/tasks ", "/diff ", "/trace ", "/status ")
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
        from ...card.styles import UI_TEXT
        from ...mode import InteractionMode

        current_mode = self.mode_manager.get_mode(chat_id)
        current_dir = self.get_working_dir(chat_id)

        mode_emoji = {
            InteractionMode.SMART: UI_TEXT.get("system_mode_smart", "🧠 智能模式"),
            InteractionMode.COCO: UI_TEXT.get("system_mode_coco", "🤖 Coco 编程模式"),
            InteractionMode.CLAUDE: UI_TEXT.get("system_mode_claude", "🔮 Claude 编程模式"),
            InteractionMode.AIDEN: UI_TEXT.get("system_mode_aiden", "🎯 Aiden 编程模式"),
            InteractionMode.CODEX: UI_TEXT.get("system_mode_codex", "💻 Codex 编程模式"),
            InteractionMode.GEMINI: UI_TEXT.get("system_mode_gemini", "✨ Gemini 编程模式"),
            InteractionMode.TTADK: UI_TEXT.get("system_mode_ttadk", "🎮 TTADK 多工具模式"),
        }
        current_mode_str = mode_emoji.get(current_mode, UI_TEXT.get("system_mode_smart", "🧠 智能模式"))

        msg_type, card_content = CardBuilder.build_help_card(project, category, current_dir, current_mode_str)

        if origin_message_id:
            if self.patch_message(origin_message_id, card_content):
                return

        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_deep_prompt(self, message_id: str, chat_id: str):
        from ...card.styles import UI_TEXT

        self.reply_message(
            message_id,
            UI_TEXT.get(
                "system_help_deep_prompt",
                "🧠 启动 Deep Engine\n\n请发送: `/deep <你的需求>`\n\n例如: `/deep 帮我重构 src/feishu 模块`",
            ),
        )

    def refresh_ttadk_models(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """强制刷新 TTADK 当前工具的真实模型列表（优先 probe），并返回诊断摘要。"""
        from ...card.styles import UI_TEXT

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
                message_id, str(e), title=UI_TEXT.get("system_ttadk_refresh_error", "刷新 TTADK 模型列表失败")
            )
            return

        lines = [UI_TEXT.get("system_ttadk_refresh_success", "✅ 已触发 TTADK 模型列表强制刷新")]
        if tool:
            lines.append(f"工具: `{tool}`")
        if getattr(result, "source", ""):
            lines.append(f"来源: `{result.source}`")
        if getattr(result, "warnings", None):
            lines.append(f"⚠️ 警告: {'; '.join(result.warnings)}")
        if getattr(result, "diagnostics", None):
            try:
                attempts = (result.diagnostics or {}).get("attempts")
                if attempts:
                    lines.append(f"诊断: attempts={attempts}")
            except Exception:
                pass
        lines.append("\n最短修复路径：若仍不可用，请确认在项目目录执行过 `ttadk init`，或切换 tool 后重试。")
        self.reply_message(message_id, "\n".join(lines))

    def handle_refresh_ttadk_models(
        self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None
    ):
        """卡片按钮入口：强制刷新指定 tool 的模型列表，并重新渲染模型选择卡片。"""
        from ...card.styles import UI_TEXT

        manager = get_ttadk_manager()
        tool = (tool_name or manager.get_current_tool() or "").strip()
        try:
            raw_cwd = self._resolve_ttadk_cwd(chat_id, project_id=(project_id or None))
            cwd = normalize_ttadk_cwd(raw_cwd)
            self._maybe_log_ttadk_cwd(
                where="SystemHandler.handle_refresh_ttadk_models", raw_cwd=raw_cwd, normalized_cwd=cwd
            )
        except Exception:
            cwd = None

        if not tool:
            self.reply_message(
                message_id, UI_TEXT.get("system_ttadk_no_tool", "⚠️ 未指定 TTADK 工具，建议先发送 `/ttadk` 选择工具")
            )
            return

        try:
            result = manager.refresh_models(tool_name=tool, cwd=cwd)
        except Exception as e:
            self.reply_error(
                message_id, str(e), title=UI_TEXT.get("system_ttadk_refresh_error", "刷新 TTADK 模型列表失败")
            )
            return

        # 直接复用刷新结果渲染模型选择卡片（force_refresh=True 已经回填缓存）
        models = list(getattr(result, "models", None) or [])
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(models, tool, project_id)
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
    def _list_acp_tools(self) -> list[_ACPToolOption]:
        from ...acp.providers import tool_registry

        names = ["coco", "claude", "aiden", "codex", "gemini"]
        desc = {
            "coco": "字节跳动 AI",
            "claude": "Anthropic AI",
            "aiden": "Aiden CLI",
            "codex": "OpenAI Codex",
            "gemini": "Google Gemini CLI",
        }
        out: list[_ACPToolOption] = []
        for name in names:
            provider = tool_registry.get_provider(name)
            if not provider:
                continue
            try:
                available = bool(provider.check_availability())
            except Exception:
                available = False
            if available:
                out.append(_ACPToolOption(name=name, description=desc.get(name, ""), is_default=(name == "coco")))
        return out

    def _fetch_acp_models(self, tool_name: str, cwd: Optional[str], current_model: Optional[str] = None) -> list[_ACPModelOption]:
        from ...acp.client import GhostAPClient
        from ...acp.providers import tool_registry

        provider = tool_registry.get_provider(tool_name)
        if not provider:
            return []

        cmd, args = provider.get_serve_command(None)

        async def _probe() -> list[_ACPModelOption]:
            import os

            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            client = GhostAPClient(on_event=lambda _ev: None, auto_approve=True)

            async with spawn_agent_process(client, cmd, *args, env=env, cwd=(cwd or str(Path.cwd()))) as (conn, _proc):
                await conn.initialize(protocol_version=1)
                resp = await conn.new_session(cwd=(cwd or str(Path.cwd())))
                models_state = getattr(resp, "models", None)
                available = list(getattr(models_state, "available_models", []) or [])
                current_id = str(
                    getattr(models_state, "current_model_id", "") or getattr(models_state, "currentModelId", "")
                )
                target_default = str((current_model or current_id or "")).strip()

                items: list[_ACPModelOption] = []
                seen: set[str] = set()
                for item in available:
                    model_id = str(
                        getattr(item, "model_id", "") or getattr(item, "modelId", "") or getattr(item, "name", "")
                    ).strip()
                    if not model_id or model_id in seen:
                        continue
                    seen.add(model_id)
                    description = str(getattr(item, "description", "") or getattr(item, "name", "") or model_id).strip()
                    items.append(
                        _ACPModelOption(
                            name=model_id,
                            description=description,
                            is_default=(model_id == target_default),
                        )
                    )
                return items

        try:
            models = asyncio.run(_probe())
        except Exception as e:
            logger.info("[ACP] fetch models failed: tool=%s err=%s", tool_name, e)
            models = []

        if models:
            return models

        if tool_name == "coco":
            try:
                fallback = get_coco_model_manager().get_models().models
                return [
                    _ACPModelOption(name=m.name, description=m.description, is_default=bool(getattr(m, "is_default", False)))
                    for m in (fallback or [])
                    if getattr(m, "name", "")
                ]
            except Exception:
                pass

        if current_model:
            return [_ACPModelOption(name=str(current_model), description=str(current_model), is_default=True)]

        return []

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

        if tool_name == "coco" and self.coco_handler:
            self.coco_handler.current_model = model_name
            self.coco_handler.enter_mode(message_id, chat_id, project=target_project)
            return
        if tool_name == "claude" and self.claude_handler:
            self.claude_handler.current_model = model_name
            self.claude_handler.enter_mode(message_id, chat_id, project=target_project)
            return
        if tool_name == "aiden" and self.aiden_handler:
            self.aiden_handler.current_model = model_name
            self.aiden_handler.enter_mode(message_id, chat_id, project=target_project)
            return
        if tool_name == "codex" and self.codex_handler:
            self.codex_handler.current_model = model_name
            self.codex_handler.enter_mode(message_id, chat_id, project=target_project)
            return
        if tool_name == "gemini" and self.gemini_handler:
            self.gemini_handler.current_model = model_name
            self.gemini_handler.enter_mode(message_id, chat_id, project=target_project)
            return

        self.reply_error(message_id, f"不支持的 ACP 工具: {tool_name}")

    def handle_acp_command(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        project_id = project.project_id if project else None
        tools = self._list_acp_tools()
        if not tools:
            self.reply_error(message_id, "未检测到可用 ACP 工具")
            return
        msg_type, card_content = CardBuilder.build_acp_tool_select_card(tools, project_id)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_select_acp_tool(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        tool = (tool_name or "").strip().lower()
        if not tool:
            self.reply_error(message_id, "请选择 ACP 工具")
            return

        project = self.project_manager.get_project(project_id) if project_id else self.project_manager.get_active_project(chat_id)
        cwd = (project.root_path if project else None) or self.get_working_dir(chat_id)

        current_model = None
        if project and getattr(project, "acp_tool_name", "") == tool:
            current_model = getattr(project, "acp_model_name", None)

        models = self._fetch_acp_models(tool, cwd=cwd, current_model=current_model)
        if not models:
            self.reply_error(message_id, f"获取 {tool} 模型列表失败，请稍后重试")
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
            self.reply_error(message_id, "请选择 ACP 模型")
            return

        self.reply_message(message_id, f"🔄 正在切换到 {tool} / {model}...")
        self._enter_mode_with_acp_model(message_id, chat_id, tool, model, project)

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

    def handle_ttadk_command(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        project_id = project.project_id if project else None
        manager = get_ttadk_manager()
        result = manager.get_tools()
        if result.error:
            self.reply_error(message_id, f"获取 TTADK 工具列表失败: {result.error}")
            return
        msg_type, card_content = CardBuilder.build_ttadk_tool_select_card(result.tools, project_id)
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

        lines = ["**🎮 TTADK 当前状态**\n"]

        if current_tool:
            lines.append(f"🔧 **当前工具**: `{current_tool}` - {tool_desc.get(current_tool, 'AI Tool')}")
        else:
            lines.append("🔧 **当前工具**: 未设置")

        if current_model:
            lines.append(f"🤖 **当前模型**: `{current_model}` - {model_desc.get(current_model, current_model)}")
        else:
            lines.append("🤖 **当前模型**: 未设置")

        lines.append("\n使用 `/ttadk` 切换工具或模型")

        self.reply_message(message_id, "\n".join(lines))

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
            self.reply_error(message_id, f"设置 TTADK 工具失败: {tool_name}")
            return
        if project:
            project.ttadk_tool_name = tool_name
            current_model = manager.get_current_model()
            if current_model:
                project.ttadk_model_name = current_model

        result = manager.get_models(cwd=cwd)
        if result.error:
            self.reply_error(message_id, f"获取 TTADK 模型列表失败: {result.error}")
            return

        # 只有在模型列表为空且有警告时才发送单独的警告消息
        # 其他情况（如 official_cli_disabled）不影响使用，不单独发送
        warnings = getattr(result, "warnings", None) or []
        has_models = bool(result.models)
        critical_warnings = [w for w in warnings if w in ("models_untrusted", "missing_tool")]

        if not has_models and warnings:
            # 模型列表为空且有警告，发送警告消息
            self.reply_message(message_id, f"⚠️ TTADK 模型列表可能不完整/不可信: {'; '.join(warnings)}")
        elif critical_warnings:
            # 有严重警告（如 models_untrusted），发送警告消息
            self.reply_message(message_id, f"⚠️ TTADK 模型列表可能不完整/不可信: {'; '.join(critical_warnings)}")

        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(result.models, tool_name, project_id)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_select_ttadk_model(
        self, message_id: str, chat_id: str, tool_name: str, model_name: str, project: Optional["ProjectContext"] = None
    ):
        # 立即给予用户反馈，避免"没反应"
        self.reply_message(message_id, f"🔄 正在切换到模型: {model_name}...")

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
            self.reply_error(message_id, f"设置 TTADK 模型失败: {model_name}")
            return

        target_project = project or self.project_manager.get_active_project(chat_id)
        if target_project:
            target_project.ttadk_tool_name = tool_name or manager.get_current_tool()
            target_project.ttadk_model_name = model_name

        if self.ttadk_handler:
            self.ttadk_handler.current_tool = tool_name
            self.ttadk_handler.current_model = model_name
            self.ttadk_handler.enter_mode(message_id, chat_id, project=target_project)
        else:
            self.reply_error(message_id, "TTADK 处理器未初始化")

    # ------------------------------------------------------------------
    # Exit current mode
    # ------------------------------------------------------------------
    def exit_current_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        from ...mode import InteractionMode

        _pid = project.project_id if project else None
        current_mode = self.mode_manager.get_mode(chat_id, project_id=_pid)
        if current_mode == InteractionMode.COCO:
            self.coco_handler.exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.CLAUDE:
            self.claude_handler.exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.AIDEN:
            self.aiden_handler.exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.CODEX:
            self.codex_handler.exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.GEMINI:
            self.gemini_handler.exit_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.TTADK:
            self.ttadk_handler.exit_mode(message_id, chat_id, project)
        else:
            self.reply_message(message_id, "🧠 当前已经在智能模式中")

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
                content = f"📂 **项目目录**: `{project.root_path}`\n📁 **工作目录**: `{current_dir}`"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    "目录信息",
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
            if project:
                content = f"✅ 已切换到: `{result}`"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    "目录已切换",
                    content,
                    show_buttons=True,
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
            else:
                self.reply_message(message_id, fmt.format_dir_change(result, True))
        else:
            self.add_reaction(message_id, EmojiReaction.on_error())
            self.reply_message(message_id, fmt.format_error(result))

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    def show_help(self, message_id: str, chat_id: str):
        is_coco_mode = self.mode_manager.is_coco_mode(chat_id)
        current_dir = self.get_working_dir(chat_id)
        project = self.project_manager.get_active_project(chat_id)

        help_result = fmt.format_help(current_dir, is_coco_mode)

        if isinstance(help_result, tuple) and len(help_result) == 2:
            try:
                post_data = json.loads(help_result[1])
                lang_data = next(iter(post_data.values()))
                md_parts = []
                for row in lang_data.get("content", []):
                    for elem in row:
                        if elem.get("tag") == "md":
                            md_parts.append(elem.get("text", ""))
                help_md = "\n".join(md_parts)
            except Exception:
                help_md = str(help_result[1])
        else:
            help_md = str(help_result)

        project_help = (
            "\n\n📋 **项目管理命令**\n"
            "• `/projects` - 查看项目看板\n"
            "• `/new 名称 路径` - 创建新项目\n"
            "• `/switch 名称` - 切换项目\n"
            "• `/status` - 查看所有引擎任务状态（Deep/Loop/Spec）\n"
            "• `/status <task_id>` - 查看指定任务详情\n"
            "• `/diff` - 查看最近两次版本变更（Diff 报告）"
        )

        if project:
            self.reply_message(message_id, f"当前项目: **{project.project_name}**\n\n{help_md}{project_help}")
        else:
            self.reply_message(message_id, f"{help_md}{project_help}")

    def show_full_help(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.handle_help_category(message_id, chat_id, "main", project)

    def show_coco_status(self, message_id: str, chat_id: str):
        manager = get_coco_model_manager()
        current_model = manager.get_current_model()
        models = manager.get_models().models

        status_lines = ["**🤖 Coco 状态**\n"]
        status_lines.append(f"当前模型: `{current_model or '未设置 (默认)'}`")

        status_lines.append("\n**可用模型:**")
        for m in models:
            mark = "✅ " if m.name == current_model else "   "
            status_lines.append(f"{mark}`{m.name}` - {m.description}")

        self.reply_message(message_id, "\n".join(status_lines))

    def show_tools_list(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show a list of all available ACP tools with quick access buttons."""
        from ...acp.providers import tool_registry

        # Define tool metadata
        tool_metadata = [
            {"name": "coco", "emoji": "🤖", "description": "字节跳动 AI"},
            {"name": "claude", "emoji": "🔮", "description": "Anthropic AI"},
            {"name": "aiden", "emoji": "🎯", "description": ""},
            {"name": "codex", "emoji": "💻", "description": ""},
            {"name": "gemini", "emoji": "✨", "description": "Google Gemini CLI"},
        ]

        # Check availability for each tool
        tools = []
        for meta in tool_metadata:
            provider = tool_registry.get_provider(meta["name"])
            is_available = provider.check_availability() if provider else False
            tools.append(
                {
                    "name": meta["name"],
                    "emoji": meta["emoji"],
                    "description": meta["description"],
                    "available": is_available,
                }
            )

        msg_type, card = SystemBuilder.build_tools_list_card(tools, project)
        self.reply_interactive_card(message_id, card, msg_type=msg_type)

    def show_tools_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show detailed status of all tools with availability and session info."""
        from ...acp.providers import tool_registry
        from ...mode import get_mode_manager

        mode_manager = get_mode_manager()

        # Define tool metadata
        tool_metadata = [
            {"name": "coco", "emoji": "🤖"},
            {"name": "claude", "emoji": "🔮"},
            {"name": "aiden", "emoji": "🎯"},
            {"name": "codex", "emoji": "💻"},
            {"name": "gemini", "emoji": "✨"},
        ]

        # Check availability and last used time for each tool
        tools = []
        for meta in tool_metadata:
            provider = tool_registry.get_provider(meta["name"])
            is_available = provider.check_availability() if provider else False
            tools.append(
                {
                    "name": meta["name"],
                    "emoji": meta["emoji"],
                    "available": is_available,
                    "last_used": "从未使用",  # TODO: Track actual last used time
                }
            )

        # Get active sessions (from mode manager)
        active_sessions = {}
        # TODO: Get actual active session info from session managers

        msg_type, card = SystemBuilder.build_tools_status_card(tools, active_sessions, project)
        self.reply_interactive_card(message_id, card, msg_type=msg_type)
