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
    def _build_ttadk_yolo_toggle_button(
        yolo_enabled: bool, project_id: Optional[str], view: str, tool_name: str = ""
    ) -> dict:
        enabled = bool(yolo_enabled)
        label = "⚡ YOLO：已开启（点击关闭）" if enabled else "⚡ YOLO：已关闭（点击开启）"
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "primary" if enabled else "default",
            "value": {
                "action": "toggle_ttadk_yolo",
                "enabled": not enabled,
                "project_id": project_id,
                "view": view,
                "tool_name": tool_name,
            },
        }

    @staticmethod
    def build_ttadk_tool_select_card(
        tools: list, project_id: Optional[str] = None, yolo_enabled: bool = False
    ) -> tuple[str, str]:
        elements = [{"tag": "markdown", "content": "请选择要使用的 TTADK 工具："}]

        elements.extend(
            build_responsive_layout(
                [SystemBuilder._build_ttadk_yolo_toggle_button(yolo_enabled, project_id, "tool_select")]
            )
        )
        elements.append({"tag": "hr"})

        options = []
        for tool in tools:
            btn_text = f"{tool.name}"
            if tool.description:
                btn_text += f" ({tool.description})"
            options.append(
                {
                    "text": {"tag": "plain_text", "content": btn_text},
                    "value": tool.name
                }
            )

        elements.append(
            {
                "tag": "select_static",
                "placeholder": {"tag": "plain_text", "content": "请选择工具..."},
                "value": {
                    "action": "select_ttadk_tool",
                    "project_id": project_id,
                },
                "options": options
            }
        )

        card = CoreBuilder._wrap_card("🔧 TTADK 工具选择", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_model_select_card(
        models: list, tool_name: str, project_id: Optional[str] = None, yolo_enabled: bool = False
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

        elements.extend(
            build_responsive_layout(
                [SystemBuilder._build_ttadk_yolo_toggle_button(yolo_enabled, project_id, "model_select", tool_name)]
            )
        )
        elements.append({"tag": "hr"})

        options = []
        for model in models:
            btn_text = f"{model.name}"
            if model.description:
                btn_text += f" ({model.description})"
            options.append(
                {
                    "text": {"tag": "plain_text", "content": btn_text},
                    "value": model.name
                }
            )
            
        elements.append(
            {
                "tag": "select_static",
                "placeholder": {"tag": "plain_text", "content": "请选择模型..."},
                "value": {
                    "action": "select_ttadk_model",
                    "tool_name": tool_name,
                    "project_id": project_id,
                },
                "options": options
            }
        )

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
    def build_ttadk_soft_failure_card(
        message: str,
        project_id: Optional[str] = None,
        *,
        action: str = "show_ttadk_menu",
        button_text: str = "🔄 重新进入TTADK",
    ) -> tuple[str, str]:
        elements = [
            {
                "tag": "markdown",
                "content": message.strip(),
            }
        ]

        button = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": button_text},
            "type": "primary",
            "value": {"action": action, "project_id": project_id},
        }
        elements.extend(build_responsive_layout([button]))

        card = CoreBuilder._wrap_card("⚠️ TTADK 暂不可用", "orange", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _format_ttadk_soft_failure_message(reason: str) -> str:
        cleaned = str(reason or "").strip()
        if not cleaned:
            cleaned = "TTADK 暂不可用"
        return f"⚠️ {cleaned}\n\n已为你保留选择，可点击继续或稍后重试。"

    @staticmethod
    def build_ttadk_soft_failure_card_for(
        reason: str,
        project_id: Optional[str] = None,
        *,
        action: str = "show_ttadk_menu",
        button_text: str = "继续进入TTADK",
    ) -> tuple[str, str]:
        message = SystemBuilder._format_ttadk_soft_failure_message(reason)
        return SystemBuilder.build_ttadk_soft_failure_card(
            message,
            project_id,
            action=action,
            button_text=button_text,
        )

    @staticmethod
    def build_acp_tool_select_card(tools: list, project_id: Optional[str] = None) -> tuple[str, str]:
        elements = [{"tag": "markdown", "content": "请选择要使用的 ACP 工具："}]

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
                    "value": {"action": "select_acp_tool", "tool_name": tool.name, "project_id": project_id},
                }
            )

        elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card("🧩 ACP 工具选择", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_acp_model_select_card(models: list, tool_name: str, project_id: Optional[str] = None) -> tuple[str, str]:
        elements = [
            {
                "tag": "markdown",
                "content": f"请为 **{tool_name}** 选择要使用的模型：",
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
                        "action": "select_acp_model",
                        "tool_name": tool_name,
                        "model_name": model.name,
                        "project_id": project_id,
                    },
                }
            )

        elements.extend(build_responsive_layout(buttons))
        elements.append({"tag": "hr"})
        elements.extend(
            build_responsive_layout(
                [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 刷新模型列表"},
                        "type": "primary",
                        "value": {
                            "action": "refresh_acp_models",
                            "tool_name": tool_name,
                            "project_id": project_id,
                        },
                    }
                ]
            )
        )

        card = CoreBuilder._wrap_card(f"🧠 {tool_name} 模型选择", "blue", elements)
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
                "text": "🧩 ACP",
                "type": "default",
                "action": "show_acp_menu",
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
                "`/aiden` - 进入 Aiden 编程模式\n"
                "`/codex` - 进入 Codex 编程模式\n"
                "`/ttadk` - 进入 TTADK 多工具编程模式\n"
                "`/exit` - 退出当前编程模式\n"
                "`/coco_info` - 查看 Coco 会话信息\n"
                "`/claude_info` - 查看 Claude 会话信息\n"
                "`/aiden_info` - 查看 Aiden 会话信息\n"
                "`/codex_info` - 查看 Codex 会话信息\n"
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
                "`/spec_pause` - 暂停当前任务\n"
                "`/spec_resume` - 继续当前任务\n"
                "`/spec_guide <引导>` - 补充约束/偏好\n"
                "`/spec_history` - 查看历史\n"
                "`/spec_metrics` - 查看目标达成度\n"
                "`/spec_config` - 查看配置\n"
                "`/spec_save` - 立即保存状态\n"
                "`/spec_export` - 导出当前报告\n"
                "`/spec_recover [任务ID]` - 恢复失败任务\n"
                "`/stop_spec` - 停止\n\n"
                "**🤖 TTADK 管理**\n"
                "`/ttadk_refresh` - 强制刷新 TTADK 模型列表\n"
                "`/ttadk_info` - 查看 TTADK 当前状态\n\n"
                "**💡 使用提示**\n"
                "1. 发送 `/coco`、`/claude`、`/aiden` 或 `/codex` 进入编程模式\n"
                "2. 发送 `/tools` 查看所有可用工具\n"
                "3. 智能模式下直接输入 Shell 命令即可执行\n"
                "4. 发送 `/menu` 打开快捷菜单"
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

    @staticmethod
    def build_tools_list_card(
        tools: list[dict],
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        """Build a card showing all available ACP tools."""
        elements = []

        if project:
            elements.append(CoreBuilder._build_directory_element(project))
            elements.append({"tag": "hr"})

        elements.append({"tag": "markdown", "content": "**🔧 可用工具列表**"})

        # Add tool buttons
        buttons = []
        for tool in tools:
            tool_name = tool["name"]
            emoji = tool.get("emoji", "🤖")
            description = tool.get("description", "")
            is_available = tool.get("available", False)

            btn_text = f"{emoji} {tool_name.capitalize()}"
            if description:
                btn_text += f" ({description})"
            if not is_available:
                btn_text += " ⚠️"

            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_text},
                    "type": "primary" if is_available else "default",
                    "disabled": not is_available,
                    "value": {"action": f"enter_{tool_name}", "project_id": project.project_id if project else None},
                }
            )

        elements.extend(build_responsive_layout(buttons))

        # Add status indicator
        available_count = sum(1 for t in tools if t.get("available", False))
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "markdown",
                "text_size": "notation",
                "content": f"可用工具: {available_count}/{len(tools)} • 点击按钮进入对应模式 • 灰色按钮表示工具不可用",
            }
        )

        card = CoreBuilder._wrap_card("🛠️ 工具选择", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_tools_status_card(
        tools: list[dict],
        active_sessions: dict[str, dict] = None,
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        """Build a card showing detailed status of all tools."""
        active_sessions = active_sessions or {}
        elements = []

        if project:
            elements.append(CoreBuilder._build_directory_element(project))
            elements.append({"tag": "hr"})

        elements.append({"tag": "markdown", "content": "**📊 工具状态详情**"})

        for tool in tools:
            tool_name = tool["name"]
            emoji = tool.get("emoji", "🤖")
            is_available = tool.get("available", False)
            last_used = tool.get("last_used", "从未使用")

            status_text = "✅ 可用" if is_available else "❌ 不可用"
            active_info = ""
            if tool_name in active_sessions:
                session_info = active_sessions[tool_name]
                active_info = f"\n   🔴 活跃会话: {session_info.get('chat_id', 'N/A')}"

            elements.append(
                {
                    "tag": "markdown",
                    "content": f"{emoji} **{tool_name.capitalize()}**\n"
                    f"   状态: {status_text}\n"
                    f"   最后使用: {last_used}"
                    f"{active_info}",
                }
            )

        elements.append({"tag": "hr"})

        # Quick actions
        action_buttons = []
        for tool in tools:
            if tool.get("available", False):
                action_buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"进入 {tool['name'].capitalize()}"},
                        "type": "default",
                        "value": {"action": f"enter_{tool['name']}", "project_id": project.project_id if project else None},
                    }
                )

        if action_buttons:
            elements.extend(build_responsive_layout(action_buttons))

        card = CoreBuilder._wrap_card("📋 工具状态", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
