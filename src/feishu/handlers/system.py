"""System handler — help, exit mode, shell commands, directory switching, intercepted commands."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...tasking import TaskSpec, TaskPriority
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class SystemHandler(BaseHandler):
    """Help, exit, shell, directory, and intercepted-command handling."""

    # Reference to programming handlers set by ws_client after construction
    coco_handler = None
    claude_handler = None
    project_handler = None
    deep_handler = None
    loop_handler = None
    diagnostics_handler = None

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
            "ls", "pwd", "whoami", "date", "uptime", "df", "du",
            "ps", "top", "htop", "free", "uname", "env", "id",
            "hostname", "which", "file", "wc", "tree",
        }
        if first_word in shell_exact:
            return True
        # Prefix patterns for parameterized shell commands
        shell_prefixes = {
            "ls", "cat", "head", "tail", "wc",
            "git", "find", "grep", "mkdir", "rm", "cp", "mv",
            "chmod", "chown", "touch", "echo", "curl", "wget",
            "pip", "npm", "yarn", "docker", "make", "tree",
        }
        return first_word in shell_prefixes

    @staticmethod
    def is_interceptable_command(text: str) -> bool:
        text_lower = text.lower().strip()
        exact_commands = {
            "/help", "/帮助",
            "/coco_info", "/claude_info",
            "/projects", "/status", "/project",
            "/switch",
            "/tasks",
            "/diff",
            "/trace",
        }
        if text_lower in exact_commands:
            return True
        prefix_commands = ("/switch ", "/new ", "/close ", "/tasks ", "/diff ", "/trace ")
        return any(text_lower.startswith(p) for p in prefix_commands)

    # ------------------------------------------------------------------
    # Intercepted command router
    # ------------------------------------------------------------------
    def handle_intercepted_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        text_lower = text.lower().strip()

        if text_lower in ("/help", "/帮助"):
            self.show_full_help(message_id, chat_id, project)
        elif text_lower == "/coco_info":
            self.coco_handler.show_info(message_id, chat_id, project)
        elif text_lower == "/claude_info":
            self.claude_handler.show_info(message_id, chat_id, project)
        elif text_lower in ("/projects", "/project"):
            self.project_handler.show_project_board(message_id, chat_id)
        elif text_lower == "/status":
            self.project_handler.show_project_status(message_id, chat_id, project)
        elif text_lower == "/switch":
            self.project_handler.show_project_board(message_id, chat_id)
        elif text_lower == "/tasks" or text_lower.startswith("/tasks "):
            self.diagnostics_handler.show_task_board(message_id, chat_id, text, project)
        elif text_lower == "/diff" or text_lower.startswith("/diff "):
            self.diagnostics_handler.show_context_diff(message_id, chat_id, text, project)
        elif text_lower == "/trace" or text_lower.startswith("/trace "):
            self.diagnostics_handler.show_message_trace(message_id, chat_id, text, project)
        elif text_lower.startswith("/switch "):
            name = text[8:].strip()
            if name:
                self.project_handler.switch_project(
                    message_id, chat_id, name,
                    coco_handler=self.coco_handler,
                    claude_handler=self.claude_handler,
                )
            else:
                self.project_handler.show_project_board(message_id, chat_id)
        elif text_lower.startswith("/new "):
            parts = text[5:].strip().split(None, 1)
            name = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else self.get_working_dir(chat_id)
            if name:
                self.project_handler.create_project(message_id, chat_id, name, path)
            else:
                self.reply_message(message_id, "用法: `/new 项目名 [路径]`")
        elif text_lower.startswith("/close "):
            name = text[7:].strip()
            if name:
                self.project_handler.close_project(message_id, chat_id, name)
        else:
            self.show_full_help(message_id, chat_id, project)

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
        result = executor.execute(cmd, cwd=working_dir)
        self.reply_message(message_id, result.to_message())
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
                content = (
                    f"📂 **项目目录**: `{project.root_path}`\n"
                    f"📁 **工作目录**: `{current_dir}`"
                )
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "目录信息", content, show_buttons=True,
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
                    project, "目录已切换", content, show_buttons=True,
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
            "• `/status` - 查看当前项目状态\n"
            "• `/diff` - 查看最近两次版本变更（Diff 报告）"
        )

        if project:
            self.reply_message(message_id, f"当前项目: **{project.project_name}**\n\n{help_md}{project_help}")
        else:
            self.reply_message(message_id, f"{help_md}{project_help}")

    def show_full_help(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        from ...mode import InteractionMode

        current_mode = self.mode_manager.get_mode(chat_id)
        current_dir = self.get_working_dir(chat_id)

        mode_emoji = {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 Coco 编程模式",
            InteractionMode.CLAUDE: "🔮 Claude 编程模式",
        }
        current_mode_str = mode_emoji.get(current_mode, "🧠 智能模式")
        project_info = f"**{project.project_name}** (`{project.root_path}`)" if project else "无"

        help_card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📖 GhostAP 使用帮助"},
                "template": "blue",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "text_size": "notation",
                     "content": f"**当前状态**  •  {current_mode_str}  •  `{current_dir}`  •  项目: {project_info}"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**🔄 编程模式切换**\n`/coco` - 进入 Coco 编程模式（字节跳动 AI）\n`/claude` - 进入 Claude 编程模式（Anthropic AI）\n`/exit` - 退出当前编程模式\n`/coco_info` - 查看 Coco 会话信息\n`/claude_info` - 查看 Claude 会话信息"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**📂 项目管理**\n`/projects` - 查看所有项目\n`/new <名称> [路径]` - 创建新项目\n`/switch <名称>` - 切换项目\n`/close <名称>` - 关闭项目\n`/status` - 查看当前项目状态\n`/diff` - 查看最近两次版本变更（Diff 报告）"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**🧠 Deep Engine（复杂任务）**\n`/deep <需求>` - 启动 Deep Engine\n`/deep_status` - 查看任务进度\n`/stop_deep` - 停止任务"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**🔄 Loop Engine（迭代闭环）**\n`/loop <需求>` - 启动 Loop 模式\n`/loop_status` - 查看迭代进度\n`/loop_guide <引导>` - 注入引导信息\n`/loop_pause` - 暂停迭代\n`/loop_resume` - 恢复迭代\n`/stop_loop` - 停止 Loop"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**💡 使用提示**\n1. 发送 `/coco` 或 `/claude` 进入编程模式\n2. 在编程模式中直接对话，系统命令（如 `/help`）会自动拦截\n3. 智能模式下直接输入 Shell 命令即可执行\n4. 发送 `/help` 或 `/帮助` 随时查看本帮助"},
                ],
            },
        }

        card_content = json.dumps(help_card, ensure_ascii=False)
        self.reply_message(message_id, card_content, msg_type="interactive")
