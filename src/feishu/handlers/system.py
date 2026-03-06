"""System handler — help, exit mode, shell commands, directory switching, intercepted commands."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...coco_model import get_coco_model_manager
from ...ttadk import get_ttadk_manager
from ...tasking import TaskSpec, TaskPriority
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class SystemHandler(BaseHandler):
    """Help, exit, shell, directory, and intercepted-command handling."""

    # Reference to programming handlers set by ws_client after construction
    coco_handler = None
    claude_handler = None
    ttadk_handler = None
    project_handler = None
    deep_handler = None
    loop_handler = None
    spec_handler = None
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
    def is_spec_command(text: str) -> bool:
        text_lower = text.lower().strip()
        return text_lower.startswith("/spec") or text_lower.startswith("/stop_spec")

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
            "/coco_info", "/claude_info", "/ttadk_info",
            "/projects", "/status", "/project",
            "/switch",
            "/tasks",
            "/diff",
            "/trace",
            "/models", "/model",
            "/ttadk", "/ttadk_tool", "/ttadk_model",
        }
        if text_lower in exact_commands:
            return True
        prefix_commands = ("/switch ", "/new ", "/close ", "/tasks ", "/diff ", "/trace ", "/status ", "/model ", "/ttadk_tool ", "/ttadk_model ")
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
        elif text_lower == "/status" or text_lower.startswith("/status "):
            self.diagnostics_handler.show_unified_status(message_id, chat_id, text, project)
        elif text_lower == "/switch":
            self.project_handler.show_project_board(message_id, chat_id)
        elif text_lower == "/tasks" or text_lower.startswith("/tasks "):
            self.diagnostics_handler.show_task_board(message_id, chat_id, text, project)
        elif text_lower == "/diff" or text_lower.startswith("/diff "):
            self.diagnostics_handler.show_context_diff(message_id, chat_id, text, project)
        elif text_lower == "/trace" or text_lower.startswith("/trace "):
            self.diagnostics_handler.show_message_trace(message_id, chat_id, text, project)
        elif text_lower == "/models":
            self.show_models(message_id, chat_id)
        elif text_lower == "/model":
            self.show_current_model(message_id, chat_id)
        elif text_lower.startswith("/model "):
            model_name = text[7:].strip()
            self.switch_model(message_id, chat_id, model_name)
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
        elif text_lower == "/ttadk":
            self.handle_ttadk_command(message_id, chat_id, project)
        elif text_lower == "/ttadk_info":
            self.show_ttadk_info(message_id, chat_id)
        elif text_lower == "/ttadk_tool":
            self.show_ttadk_tools(message_id, chat_id)
        elif text_lower.startswith("/ttadk_tool "):
            tool_name = text[12:].strip()
            self.switch_ttadk_tool(message_id, chat_id, tool_name)
        elif text_lower == "/ttadk_model":
            self.show_ttadk_models(message_id, chat_id)
        elif text_lower.startswith("/ttadk_model "):
            model_name = text[13:].strip()
            self.switch_ttadk_model(message_id, chat_id, model_name)
        else:
            self.show_full_help(message_id, chat_id, project)

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
            self.reply_message(message_id, f"❌ 获取 TTADK 工具列表失败: {result.error}")
            return
        msg_type, card_content = CardBuilder.build_ttadk_tool_select_card(result.tools, project_id)
        self.reply_message(message_id, card_content, msg_type=msg_type)
    
    def show_ttadk_info(self, message_id: str, chat_id: str):
        manager = get_ttadk_manager()
        current_tool = manager.get_current_tool()
        current_model = manager.get_current_model()
        tools_result = manager.get_tools()
        models_result = manager.get_models(cwd=self._resolve_ttadk_cwd(chat_id))
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
        
        lines.append("\n使用 `/ttadk_tool <工具名>` 切换工具")
        lines.append("使用 `/ttadk_model <模型名>` 切换模型")
        
        self.reply_message(message_id, "\n".join(lines))
    
    def show_ttadk_tools(self, message_id: str, chat_id: str):
        manager = get_ttadk_manager()
        result = manager.get_tools()
        current = manager.get_current_tool()
        
        if result.error:
            self.reply_message(message_id, f"❌ 获取 TTADK 工具列表失败: {result.error}")
            return
        
        lines = ["**🔧 TTADK 可用工具列表**\n"]
        for tool in result.tools:
            marker = "✅" if current and tool.name == current else "•"
            lines.append(f"{marker} `{tool.name}` - {tool.description}")
        
        lines.append("\n使用 `/ttadk_tool <名称>` 切换工具")
        self.reply_message(message_id, "\n".join(lines))
    
    def switch_ttadk_tool(self, message_id: str, chat_id: str, tool_name: str):
        manager = get_ttadk_manager()
        success = manager.set_tool(tool_name)
        if success:
            self.add_reaction(message_id, EmojiReaction.on_done())
            self.reply_message(message_id, f"✅ 已切换到 TTADK 工具: `{tool_name}`")
        else:
            self.add_reaction(message_id, EmojiReaction.on_error())
            result = manager.get_tools()
            available = ", ".join([f"`{t.name}`" for t in result.tools]) if result.tools else "无可用工具"
            self.reply_message(message_id, f"❌ 未知 TTADK 工具: `{tool_name}`\n\n可用工具: {available}")
    
    def show_ttadk_models(self, message_id: str, chat_id: str):
        manager = get_ttadk_manager()
        result = manager.get_models(cwd=self._resolve_ttadk_cwd(chat_id))
        current = manager.get_current_model()
        
        if result.error:
            self.reply_message(message_id, f"❌ 获取 TTADK 模型列表失败: {result.error}")
            return
        
        lines = ["**🤖 TTADK 可用模型列表**\n"]
        for model in result.models:
            marker = "✅" if current and model.name == current else "•"
            lines.append(f"{marker} `{model.name}` - {model.description}")
        
        lines.append("\n使用 `/ttadk_model <名称>` 切换模型")
        self.reply_message(message_id, "\n".join(lines))
    
    def switch_ttadk_model(self, message_id: str, chat_id: str, model_name: str):
        manager = get_ttadk_manager()
        success = manager.set_model(model_name)
        if success:
            self.add_reaction(message_id, EmojiReaction.on_done())
            self.reply_message(message_id, f"✅ 已切换到 TTADK 模型: `{model_name}`")
        else:
            self.add_reaction(message_id, EmojiReaction.on_error())
            result = manager.get_models(cwd=self._resolve_ttadk_cwd(chat_id))
            available = ", ".join([f"`{m.name}`" for m in result.models]) if result.models else "无可用模型"
            self.reply_message(message_id, f"❌ 未知 TTADK 模型: `{model_name}`\n\n可用模型: {available}")

    def handle_select_ttadk_tool(self, message_id: str, chat_id: str, tool_name: str, project_id: Optional[str] = None):
        manager = get_ttadk_manager()
        success = manager.set_tool(tool_name)
        if not success:
            self.reply_message(message_id, f"❌ 设置 TTADK 工具失败: {tool_name}")
            return
        
        result = manager.get_models(cwd=self._resolve_ttadk_cwd(chat_id, project_id=project_id))
        if result.error:
            self.reply_message(message_id, f"❌ 获取 TTADK 模型列表失败: {result.error}")
            return
        msg_type, card_content = CardBuilder.build_ttadk_model_select_card(result.models, tool_name, project_id)
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def handle_select_ttadk_model(self, message_id: str, chat_id: str, tool_name: str, model_name: str, project: Optional["ProjectContext"] = None):
        manager = get_ttadk_manager()
        success = manager.set_model(model_name)
        if not success:
            self.reply_message(message_id, f"❌ 设置 TTADK 模型失败: {model_name}")
            return
        
        if self.ttadk_handler:
            self.ttadk_handler.current_tool = tool_name
            self.ttadk_handler.current_model = model_name
            self.ttadk_handler.enter_mode(message_id, chat_id, project=project)
        else:
            self.reply_message(message_id, "❌ TTADK 处理器未初始化")

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
        result = executor.execute(cmd, cwd=working_dir)
        msg_type, card_content = CardBuilder.build_shell_result_card(
            cmd, result, working_dir, project,
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
    # Model management
    # ------------------------------------------------------------------
    def show_models(self, message_id: str, chat_id: str):
        manager = get_coco_model_manager()
        result = manager.get_models()
        current = manager.get_current_model()

        lines = ["**🤖 可用模型列表**\n"]
        for m in result.models:
            marker = "✅" if m.name == current else "•"
            lines.append(f"{marker} `{m.name}` - {m.description}")

        lines.append("\n使用 `/model <名称>` 切换模型")
        self.reply_message(message_id, "\n".join(lines))

    def show_current_model(self, message_id: str, chat_id: str):
        manager = get_coco_model_manager()
        current = manager.get_current_model()
        if current:
            self.reply_message(message_id, f"🤖 当前模型: `{current}`")
        else:
            self.reply_message(message_id, "🤖 当前模型: 默认")

    def switch_model(self, message_id: str, chat_id: str, model_name: str):
        manager = get_coco_model_manager()
        success = manager.set_model(model_name)
        if success:
            self.add_reaction(message_id, EmojiReaction.on_done())
            self.reply_message(message_id, f"✅ 已切换到模型: `{model_name}`")
        else:
            self.add_reaction(message_id, EmojiReaction.on_error())
            result = manager.get_models()
            available = ", ".join([f"`{m.name}`" for m in result.models])
            self.reply_message(message_id, f"❌ 未知模型: `{model_name}`\n\n可用模型: {available}")

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
        from ...mode import InteractionMode

        current_mode = self.mode_manager.get_mode(chat_id)
        current_dir = self.get_working_dir(chat_id)

        mode_emoji = {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 Coco 编程模式",
            InteractionMode.CLAUDE: "🔮 Claude 编程模式",
            InteractionMode.TTADK: "🎮 TTADK 多工具模式",
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
                     "content": "**🔄 编程模式切换**\n`/coco` - 进入 Coco 编程模式（字节跳动 AI）\n`/claude` - 进入 Claude 编程模式（Anthropic AI）\n`/ttadk` - 进入 TTADK 多工具编程模式（支持 Coco/Claude/Cursor/Gemini 等）\n`/exit` - 退出当前编程模式\n`/coco_info` - 查看 Coco 会话信息\n`/claude_info` - 查看 Claude 会话信息\n`/ttadk_info` - 查看 TTADK 当前工具和模型"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**📂 项目管理**\n`/projects` - 查看所有项目\n`/new <名称> [路径]` - 创建新项目\n`/switch <名称>` - 切换项目\n`/close <名称>` - 关闭项目\n`/status` - 查看所有引擎任务状态\n`/status <task_id>` - 查看指定任务详情\n`/diff` - 查看最近两次版本变更"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**🧠 Deep Engine（复杂任务）**\n`/deep <需求>` - 启动 Deep Engine\n`/deep_status` - 查看任务进度\n`/stop_deep` - 停止任务"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**🔄 Loop Engine（迭代闭环）**\n`/loop <需求>` - 启动 Loop 模式\n`/loop_status` - 查看迭代进度\n`/loop_guide <引导>` - 注入引导信息\n`/loop_pause` - 暂停迭代\n`/loop_resume` - 恢复迭代\n`/stop_loop` - 停止 Loop"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**📋 Spec Engine（结构化开发闭环）**\n"
                                "适用：你希望按方法论持续迭代，输出可复盘的 Spec/Plan/Task/Build 产物\n"
                                "区别：Spec=结构化产物驱动闭环；Deep=一次性深度执行；Loop=验收标准驱动迭代\n\n"
                                "命令：\n"
                                "`/spec <需求>` - 启动\n"
                                "`/spec_status` - 查看进度\n"
                                "`/spec_guide <引导>` - 补充约束/偏好（下轮生效）\n"
                                "`/spec_history [N]` - 查看循环与 spec 文件历史（默认20，最多500）\n"
                                "`/spec_metrics [N]` - 查看目标达成度与指标变化（默认20，最多500）\n"
                                "`/spec_config` - 查看长程配置（阈值/保留策略）\n"
                                "`/spec_save` - 立即保存状态（用于断点续传）\n"
                                "`/spec_recover` - 恢复异常中断的任务（需指定 Task ID）\n"
                                "`/spec_pause` - 暂停  •  `/spec_resume` - 恢复  •  `/stop_spec` - 停止\n\n"
                                "最小示例：\n"
                                "- Web：`/spec 做一个登录页+登录接口`\n"
                                "- API：`/spec 新增 /v1/users 查询接口`\n"
                                "- 脚本：`/spec 写一个批量重命名脚本，支持dry-run`"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**🤖 模型管理**\n`/models` - 查看可用模型列表（Coco）\n`/model` - 查看当前使用的模型（Coco）\n`/model <名称>` - 切换到指定模型（Coco）\n`/ttadk_tool` - 查看 TTADK 可用工具\n`/ttadk_tool <工具>` - 切换 TTADK 使用的工具\n`/ttadk_model` - 查看 TTADK 可用模型\n`/ttadk_model <模型>` - 切换 TTADK 使用的模型"},
                    {"tag": "hr"},
                    {"tag": "markdown", "text_size": "normal",
                     "content": "**💡 使用提示**\n1. 发送 `/coco` 或 `/claude` 进入编程模式\n2. 在编程模式中直接对话，系统命令（如 `/help`）会自动拦截\n3. 智能模式下直接输入 Shell 命令即可执行\n4. 发送 `/help` 或 `/帮助` 随时查看本帮助"},
                ],
            },
        }

        card_content = json.dumps(help_card, ensure_ascii=False)
        self.reply_message(message_id, card_content, msg_type="interactive")
