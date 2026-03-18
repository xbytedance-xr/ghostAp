from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Optional

from src.project.context import ProjectContext
from src.utils.errors import GhostAPError

from ..shared import (
    build_responsive_layout,
)
from .core import CoreBuilder

if TYPE_CHECKING:
    from src.sandbox.executor import ExecutionResult


class SystemBuilder:
    """System-related card building utilities."""

    @staticmethod
    def build_error_card(
        exc: Exception | str,
        title: str = "操作失败",
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        from ..shared import build_quick_actions

        message = str(exc)
        quick_actions = []
        context = {}

        if isinstance(exc, GhostAPError):
            quick_actions = exc.quick_actions
            context = exc.context

        elements = [CoreBuilder._build_content_element(f"❌ **{title}**\n\n{message}")]

        if project:
            elements.insert(0, CoreBuilder._build_directory_element(project))
            elements.insert(1, {"tag": "hr"})

        if quick_actions:
            buttons = build_quick_actions(quick_actions, context)
            elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card("⚠️ 错误提示", "red", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_shell_result_card(
        cmd: str,
        result: "ExecutionResult",
        working_dir: Optional[str] = None,
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        """Build an interactive card for shell command execution results."""
        if result.success:
            header_title = "✅ 命令执行成功"
            header_template = "turquoise"
        else:
            header_title = "❌ 命令执行失败"
            header_template = "red"

        elements = [
            CoreBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
            {"tag": "markdown", "content": f"> 🖥️ `{cmd}`"},
        ]

        if result.error_message:
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"🚫 **{result.error_message}**",
                }
            )
        elif result.stdout or result.stderr:
            MAX_OUTPUT_LEN = 2000

            if result.stdout:
                stdout_content = result.stdout
                if len(stdout_content) > MAX_OUTPUT_LEN:
                    stdout_content = stdout_content[:MAX_OUTPUT_LEN] + "\n...(truncated)..."
                elements.append(
                    {
                        "tag": "markdown",
                        "content": f"```BASH\n{stdout_content}\n```",
                    }
                )
            if result.stderr:
                stderr_content = result.stderr
                if len(stderr_content) > MAX_OUTPUT_LEN:
                    stderr_content = stderr_content[:MAX_OUTPUT_LEN] + "\n...(truncated)..."
                elements.append(
                    {
                        "tag": "markdown",
                        "content": f"⚠️ **错误输出**:\n```BASH\n{stderr_content}\n```",
                    }
                )
        else:
            elements.append(
                {
                    "tag": "markdown",
                    "content": "✅ 命令执行成功（无输出）",
                }
            )

        elements.append(
            {
                "tag": "markdown",
                "content": f"返回码: `{result.return_code}`",
                "text_size": "notation",
            }
        )

        card = CoreBuilder._wrap_card(header_title, header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_tool_select_card(tools: list, project_id: Optional[str] = None) -> tuple[str, str]:
        elements = [{"tag": "markdown", "content": "请选择要使用的 TTADK 工具："}]

        buttons = []
        for tool in tools:
            btn_text = f"{tool.name}"
            if tool.description:
                btn_text += f" ({tool.description})"
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_text},
                    "type": "primary" if tool.is_default else "default",
                    "value": {"action": "select_ttadk_tool", "tool_name": tool.name, "project_id": project_id},
                }
            )

        elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card("🔧 TTADK 工具选择", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_model_select_card(
        models: list, tool_name: str, project_id: Optional[str] = None
    ) -> tuple[str, str]:
        elements = [
            {
                "tag": "markdown",
                "content": (
                    f"请为 **{tool_name}** 选择要使用的模型：\n"
                    "（若列表为空/不全，可点击下方『🔄 刷新模型列表』强制拉取）"
                ),
            }
        ]

        buttons = []
        for model in models:
            btn_text = f"{model.name}"
            if model.description:
                btn_text += f" ({model.description})"
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_text},
                    "type": "primary" if model.is_default else "default",
                    "value": {
                        "action": "select_ttadk_model",
                        "tool_name": tool_name,
                        "model_name": model.name,
                        "project_id": project_id,
                    },
                }
            )

        elements.extend(build_responsive_layout(buttons))

        # 辅助入口：强制刷新模型列表（常用于 Invalid model / 可用模型为空）
        elements.append({"tag": "hr"})
        elements.extend(
            build_responsive_layout(
                [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 刷新模型列表"},
                        "type": "primary",
                        "value": {
                            "action": "refresh_ttadk_models",
                            "tool_name": tool_name,
                            "project_id": project_id,
                        },
                    }
                ]
            )
        )

        card = CoreBuilder._wrap_card(f"🤖 {tool_name} 模型选择", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_command_menu_card(project: Optional[ProjectContext] = None) -> tuple[str, str]:
        """Build a mobile-friendly command menu card."""
        project_id = project.project_id if project else None

        buttons = [
            {
                "text": "➕ 新建项目",
                "type": "primary",
                "action": "new_project_prompt",
            },
            {
                "text": "🔄 切换项目",
                "type": "default",
                "action": "switch_project",
            },
            {
                "text": "🧠 Deep 任务",
                "type": "primary",
                "action": "enter_deep_prompt",
            },
            {
                "text": "📊 状态概览",
                "type": "default",
                "action": "show_status",
            },
            {
                "text": "🎮 TTADK",
                "type": "default",
                "action": "show_ttadk_menu",
            },
            {
                "text": "📖 帮助",
                "type": "default",
                "action": "show_help_menu",
            },
        ]

        # Convert to actual card buttons
        card_buttons = []
        for btn in buttons:
            card_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn["text"]},
                    "type": btn["type"],
                    "value": {"action": btn["action"], "project_id": project_id},
                }
            )

        elements = [
            CoreBuilder._build_directory_element(project),
            {"tag": "hr"},
            {"tag": "markdown", "content": "**📱 常用指令菜单**"},
        ]
        elements.extend(build_responsive_layout(card_buttons))

        card = CoreBuilder._wrap_card("📱 快捷菜单", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_help_card(
        project: Optional[ProjectContext] = None,
        category: str = "main",
        working_dir: Optional[str] = None,
        current_mode_str: str = "智能模式",
    ) -> tuple[str, str]:
        """Build a categorized help card."""

        # Extract primitives for caching
        project_name = project.project_name if project else None
        root_path = project.root_path if project else None
        project_id = project.project_id if project else None

        return SystemBuilder._build_help_card_cached(
            project_name=project_name,
            root_path=root_path,
            project_id=project_id,
            category=category,
            working_dir=working_dir,
            current_mode_str=current_mode_str,
        )

    @staticmethod
    @lru_cache(maxsize=64)
    def _build_help_card_cached(
        project_name: Optional[str],
        root_path: Optional[str],
        project_id: Optional[str],
        category: str,
        working_dir: Optional[str],
        current_mode_str: str,
    ) -> tuple[str, str]:
        """Internal cached builder for help cards using only primitive types."""

        project_info = f"**{project_name}** (`{root_path}`)" if project_name else "无"

        # Categories
        categories = [
            {"name": "编程模式", "id": "coding"},
            {"name": "Deep 任务", "id": "deep"},
            {"name": "项目管理", "id": "project"},
            {"name": "更多...", "id": "more"},
        ]

        category_buttons = []
        for cat in categories:
            is_active = cat["id"] == category or (category == "main" and cat["id"] == "coding")
            category_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": cat["name"]},
                    "type": "primary" if is_active else "default",
                    "value": {"action": "help_category", "category": cat["id"], "project_id": project_id},
                }
            )

        content = ""
        # Default to coding if main
        cat_key = "coding" if category == "main" else category

        if cat_key == "coding":
            content = (
                "**🔄 编程模式切换**\n"
                "`/coco` - 进入 Coco 编程模式（字节跳动 AI）\n"
                "`/claude` - 进入 Claude 编程模式（Anthropic AI）\n"
                "`/ttadk` - 进入 TTADK 多工具编程模式\n"
                "`/exit` - 退出当前编程模式\n"
                "`/coco_info` - 查看 Coco 会话信息\n"
                "`/claude_info` - 查看 Claude 会话信息\n"
                "`/ttadk_info` - 查看 TTADK 当前工具和模型"
            )
        elif cat_key == "deep":
            content = (
                "**🧠 Deep Engine（复杂任务）**\n"
                "`/deep <需求>` - 启动 Deep Engine\n"
                "`/deep_status` - 查看任务进度\n"
                "`/stop_deep` - 停止任务\n\n"
                "**🔄 Loop Engine（迭代闭环）**\n"
                "`/loop <需求>` - 启动 Loop 模式\n"
                "`/loop_status` - 查看迭代进度\n"
                "`/loop_guide <引导>` - 注入引导信息\n"
                "`/loop_pause` - 暂停迭代\n"
                "`/loop_resume` - 恢复迭代\n"
                "`/stop_loop` - 停止 Loop"
            )
        elif cat_key == "project":
            content = (
                "**📂 项目管理**\n"
                "`/projects` - 查看所有项目\n"
                "`/new <名称> [路径]` - 创建新项目\n"
                "`/switch <名称>` - 切换项目\n"
                "`/close <名称>` - 关闭项目\n"
                "`/status` - 查看所有引擎任务状态\n"
                "`/diff` - 查看最近两次版本变更"
            )
        elif cat_key == "more":
            content = (
                "**📋 Spec Engine（结构化开发闭环）**\n"
                "`/spec <需求>` - 启动\n"
                "`/spec_status` - 查看进度\n"
                "`/spec_guide <引导>` - 补充约束/偏好\n"
                "`/spec_history` - 查看历史\n"
                "`/spec_config` - 查看配置\n"
                "`/stop_spec` - 停止\n\n"
                "**🤖 TTADK 管理**\n"
                "`/ttadk_refresh` - 强制刷新 TTADK 模型列表\n"
                "`/ttadk_info` - 查看 TTADK 当前状态\n\n"
                "**💡 使用提示**\n"
                "1. 发送 `/coco` 或 `/claude` 进入编程模式\n"
                "2. 智能模式下直接输入 Shell 命令即可执行\n"
                "3. 发送 `/menu` 打开快捷菜单"
            )

        elements = [
            {
                "tag": "markdown",
                "text_size": "notation",
                "content": f"**当前状态**  •  {current_mode_str}  •  `{working_dir or '~'}`  •  项目: {project_info}",
            },
            {"tag": "hr"},
        ]

        elements.extend(build_responsive_layout(category_buttons))

        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "text_size": "normal", "content": content})

        card = CoreBuilder._wrap_card("📖 GhostAP 使用帮助", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
