from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Optional

from src.project.context import ProjectContext
from src.utils.errors import GhostAPError, get_error_detail

from ..models import ModelOptionView, ToolOptionView
from ..shared import build_responsive_layout
from ..styles import THRESHOLDS, UI_TEXT
from .core import CoreBuilder

if TYPE_CHECKING:
    from src.sandbox.executor import ExecutionResult


class SystemBuilder:
    """System-related card building utilities."""

    @staticmethod
    def build_directory_change_card(
        project: Optional[ProjectContext],
        path: str,
        success: bool = True,
    ) -> Optional[tuple[str, str]]:
        """Build a card for directory change result."""
        from .core import CoreBuilder
        from .project import ProjectBuilder

        if success:
            banner_msg = UI_TEXT["project_dir_switched_banner"].format(path=path)
            banner = CoreBuilder._build_banner_element(banner_msg, type="success")
            title = UI_TEXT["system_dir_changed_title"]
            detail = UI_TEXT["project_dir_switched_detail"].format(path=path)
        else:
            banner_msg = UI_TEXT["project_dir_switch_failed_banner"].format(path=path)
            banner = CoreBuilder._build_banner_element(banner_msg, type="error")
            title = UI_TEXT["system_error_title"]
            detail = UI_TEXT["project_dir_switch_failed_detail"].format(path=path)

        if project:
            return ProjectBuilder.build_project_response_card(
                project,
                title,
                detail,
                show_buttons=True,
                banner=banner,
            )
        return None

    @staticmethod
    def build_ttadk_refresh_result_card(
        tool: str,
        result: any,
    ) -> tuple[str, str]:
        """Build a summary message for TTADK refresh result."""
        lines = [UI_TEXT["system_ttadk_refresh_success"]]
        if tool:
            lines.append(UI_TEXT["system_ttadk_refresh_label_tool"].format(tool=tool))

        source = getattr(result, "source", "")
        if source:
            lines.append(UI_TEXT["system_ttadk_refresh_label_source"].format(source=source))

        warnings = getattr(result, "warnings", None)
        if warnings:
            lines.append(
                UI_TEXT["system_ttadk_refresh_label_warning"].format(
                    warnings="; ".join(warnings)
                )
            )

        diagnostics = getattr(result, "diagnostics", None)
        if diagnostics:
            try:
                attempts = (diagnostics or {}).get("attempts")
                if attempts:
                    lines.append(
                        UI_TEXT["system_ttadk_refresh_label_diag"].format(
                            attempts=attempts
                        )
                    )
            except Exception:
                pass

        footer = UI_TEXT["system_ttadk_refresh_footer"]
        if footer:
            lines.append(footer)

        return "text", "\n".join(lines)

    @staticmethod
    def build_switching_status_card(
        tool: str,
        model: str,
    ) -> tuple[str, str]:
        """Build a simple status message for switching tools/models."""
        msg = UI_TEXT["system_switching_to"].format(
            tool=tool, model=model
        )
        return "text", msg

    @staticmethod
    def build_ttadk_info_content(
        current_tool: Optional[str],
        current_model: Optional[str],
        tool_desc: dict[str, str],
        model_desc: dict[str, str],
    ) -> str:
        """Build the Markdown content for TTADK status info."""
        lines = [UI_TEXT["system_ttadk_info_header"]]

        if current_tool:
            tool_label = tool_desc.get(current_tool, UI_TEXT["system_ttadk_ai_tool_label"])
            lines.append(f"{UI_TEXT['system_label_current_tool']}: `{current_tool}` - {tool_label}")
        else:
            lines.append(f"{UI_TEXT['system_label_current_tool']}: " + UI_TEXT["system_not_set"])

        if current_model:
            model_label = model_desc.get(current_model, current_model)
            lines.append(f"{UI_TEXT['system_label_current_model']}: `{current_model}` - {model_label}")
        else:
            lines.append(f"{UI_TEXT['system_label_current_model']}: " + UI_TEXT["system_not_set"])

        footer = UI_TEXT["system_ttadk_info_footer"]
        if footer:
            lines.append(footer)

        return "\n".join(lines)

    @staticmethod
    def build_coco_status_content(
        current_model: Optional[str],
        models: list,
    ) -> str:
        """Build the Markdown content for Coco status info."""
        status_lines = [UI_TEXT["system_coco_status_title"]]
        status_lines.append(UI_TEXT["system_coco_current_model"].format(model=current_model or UI_TEXT["system_not_set"]))

        status_lines.append(UI_TEXT["system_coco_available_models"])
        for m in models:
            mark = "✅ " if m.name == current_model else "   "
            status_lines.append(f"{mark}`{m.name}` - {m.description}")

        return "\n".join(status_lines)

    @staticmethod
    def build_error_card(
        exc: Exception | str,
        title: str = "",
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        from ..shared import build_quick_actions

        if not title:
            title = UI_TEXT["system_error_title"]

        message = get_error_detail(exc) if isinstance(exc, Exception) else (str(exc) or UI_TEXT["system_unknown_error"])
        quick_actions = []
        context = {}

        if isinstance(exc, GhostAPError):
            quick_actions = exc.quick_actions
            context = exc.context

        elements = []
        if project:
            elements.append(CoreBuilder._build_directory_element(project))
            elements.append({"tag": "hr"})

        elements.append(CoreBuilder._build_content_element(f"❌ **{title}**\n\n{message}"))

        # project info is handled by project_response_card if needed, but build_error_card
        # is often used for generic errors. Original code had optional project.
        # We'll stick to a simpler interactive card here or wrap it.
        
        if quick_actions:
            buttons = build_quick_actions(quick_actions, context)
            elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card(UI_TEXT["system_error_prompt_title"], "red", elements)
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
            header_title = UI_TEXT["system_shell_success_title"]
            header_template = "turquoise"
        else:
            header_title = UI_TEXT["system_shell_failed_title"]
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
            from ..truncation import truncate_bash_output

            _shell_notice = UI_TEXT["shell_truncated"]

            if result.stdout:
                stdout_content = truncate_bash_output(
                    result.stdout,
                    max_chars=THRESHOLDS["SHELL_STDOUT_MAX"],
                    max_lines=999999,  # no line limit for shell result cards
                    notice=_shell_notice,
                )
                elements.append(
                    {
                        "tag": "markdown",
                        "content": f"```BASH\n{stdout_content}\n```",
                    }
                )
            if result.stderr:
                stderr_content = truncate_bash_output(
                    result.stderr,
                    max_chars=THRESHOLDS["SHELL_STDERR_MAX"],
                    max_lines=999999,
                    notice=_shell_notice,
                )
                elements.append(
                    {
                        "tag": "markdown",
                        "content": f"{UI_TEXT['system_shell_stderr_label']}\n```BASH\n{stderr_content}\n```",
                    }
                )
        else:
            elements.append(
                {
                    "tag": "markdown",
                    "content": UI_TEXT["system_shell_no_output"],
                }
            )

        elements.append(
            {
                "tag": "markdown",
                "content": UI_TEXT["system_shell_return_code"].format(code=result.return_code),
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
        label = UI_TEXT["system_ttadk_yolo_on"] if enabled else UI_TEXT["system_ttadk_yolo_off"]
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
        tools: list, project_id: Optional[str] = None, yolo_enabled: bool = False, current_tool: Optional[str] = None
    ) -> tuple[str, str]:
        elements = [{"tag": "markdown", "content": UI_TEXT["system_ttadk_select_tool_prompt"]}]

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
                "placeholder": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_select_tool_placeholder"]},
                "initial_option": current_tool,
                "value": {
                    "action": "select_ttadk_tool",
                    "project_id": project_id,
                },
                "options": options
            }
        )

        card = CoreBuilder._wrap_card(UI_TEXT["system_ttadk_tool_select_title"], "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_model_select_card(
        models: list, tool_name: str, project_id: Optional[str] = None, yolo_enabled: bool = False, current_model: Optional[str] = None
    ) -> tuple[str, str]:
        elements = [
            {
                "tag": "markdown",
                "content": (
                    UI_TEXT["system_ttadk_select_model_prompt"].format(tool=tool_name) + "\n" +
                    UI_TEXT["system_ttadk_select_model_hint"]
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
                "placeholder": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_select_model_placeholder"]},
                "initial_option": current_model,
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
                        "text": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_refresh_btn"]},
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

        card = CoreBuilder._wrap_card(UI_TEXT["system_ttadk_model_select_title"].format(tool=tool_name), "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_combined_select_card(
        tools: list,
        models_by_tool: dict,
        project_id: Optional[str] = None,
        yolo_enabled: bool = False,
        current_tool: Optional[str] = None,
        current_model: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build a combined TTADK tool + model selection card (single step)."""
        elements = [{"tag": "markdown", "content": UI_TEXT["system_ttadk_combined_select_prompt"]}]

        elements.extend(
            build_responsive_layout(
                [SystemBuilder._build_ttadk_yolo_toggle_button(yolo_enabled, project_id, "combined_select")]
            )
        )
        elements.append({"tag": "hr"})

        # Tool selection
        tool_options = []
        for tool in tools:
            btn_text = f"{tool.name}"
            if tool.description:
                btn_text += f" ({tool.description})"
            tool_options.append(
                {
                    "text": {"tag": "plain_text", "content": btn_text},
                    "value": tool.name,
                }
            )

        elements.append({"tag": "markdown", "content": UI_TEXT["system_ttadk_label_tool"]})
        elements.append(
            {
                "tag": "select_static",
                "placeholder": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_select_tool_placeholder"]},
                "initial_option": current_tool,
                "value": {
                    "action": "select_ttadk_combined_tool",
                    "project_id": project_id,
                },
                "options": tool_options,
            }
        )

        # Model selection per tool (show current_tool's models or first tool's models by default)
        if tools and models_by_tool:
            selected_tool = current_tool
            if not selected_tool or selected_tool not in models_by_tool:
                selected_tool = tools[0].name
            models = models_by_tool.get(selected_tool, [])
            if models:
                elements.append({"tag": "hr"})
                elements.append({"tag": "markdown", "content": UI_TEXT["system_ttadk_label_model"].format(tool=selected_tool)})

                model_options = []
                for model in models:
                    btn_text = f"{model.name}"
                    if model.description:
                        btn_text += f" ({model.description})"
                    model_options.append(
                        {
                            "text": {"tag": "plain_text", "content": btn_text},
                            "value": model.name,
                        }
                    )

                elements.append(
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_select_model_placeholder"]},
                        "initial_option": current_model,
                        "value": {
                            "action": "select_ttadk_combined",
                            "tool_name": selected_tool,
                            "project_id": project_id,
                        },
                        "options": model_options,
                    }
                )

        # Refresh button
        elements.append({"tag": "hr"})
        elements.extend(
            build_responsive_layout(
                [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_refresh_btn"]},
                        "type": "primary",
                        "value": {
                            "action": "refresh_ttadk_models",
                            "project_id": project_id,
                        },
                    }
                ]
            )
        )

        card = CoreBuilder._wrap_card(UI_TEXT["system_ttadk_combined_title"], "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_soft_failure_card(
        message: str,
        project_id: Optional[str] = None,
        *,
        action: str = "show_ttadk_menu",
        button_text: str = "",
    ) -> tuple[str, str]:
        # Clean the message for the banner (CoreBuilder adds its own emoji)
        banner_msg = message.replace("⚠️ ", "").strip()
        elements = [
            CoreBuilder._build_banner_element(banner_msg, type="warning")
        ]

        effective_text = button_text or UI_TEXT["system_ttadk_btn_reenter"]
        button = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": effective_text},
            "type": "primary",
            "value": {"action": action, "project_id": project_id},
        }
        elements.extend(build_responsive_layout([button]))

        card = CoreBuilder._wrap_card(UI_TEXT["system_ttadk_unavailable_title"], "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _format_ttadk_soft_failure_message(reason: str) -> str:
        cleaned = str(reason or "").strip()
        if not cleaned:
            cleaned = UI_TEXT["system_ttadk_unavailable"]
        return UI_TEXT["system_ttadk_soft_failure_msg"].format(reason=cleaned)

    @staticmethod
    def build_ttadk_soft_failure_card_for(
        reason: str,
        project_id: Optional[str] = None,
        *,
        action: str = "show_ttadk_menu",
        button_text: str = "",
    ) -> tuple[str, str]:
        message = SystemBuilder._format_ttadk_soft_failure_message(reason)
        return SystemBuilder.build_ttadk_soft_failure_card(
            message,
            project_id,
            action=action,
            button_text=button_text or UI_TEXT["system_ttadk_btn_continue"],
        )

    @staticmethod
    def build_acp_tool_select_card(
        tools: list,
        project_id: Optional[str] = None,
        current_tool: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build an interactive card for ACP tool selection.

        ``tools`` 可以是：
        - ``ToolOptionView`` 列表（首选，卡片层通用视图模型）；
        - 旧版的字符串列表或带 ``name`` 属性的对象列表（向后兼容）。
        """

        elements = [{"tag": "markdown", "content": UI_TEXT["system_acp_select_tool_prompt"]}]

        tool_options: list[ToolOptionView] = []
        for item in tools or []:
            if isinstance(item, ToolOptionView):
                tool_options.append(item)
                continue

            name = getattr(item, "name", None) or str(item)
            name = str(name or "").strip()
            if not name:
                continue

            # 优先使用 UI_TEXT 中的描述，保持既有文案；否则回退到对象上的 description 字段
            desc_key = f"system_acp_tool_desc_{name}"
            desc = UI_TEXT[desc_key] if desc_key in UI_TEXT else getattr(item, "description", "")
            emoji = getattr(item, "emoji", "🤖")
            is_default = bool(getattr(item, "is_default", False))
            disabled = bool(getattr(item, "disabled", False))

            tool_options.append(
                ToolOptionView(
                    name=name,
                    description=str(desc or ""),
                    is_default=is_default,
                    emoji=str(emoji or "🤖"),
                    disabled=disabled,
                )
            )

        buttons = []
        for t in tool_options:
            btn_text = f"{t.emoji} {t.name}"
            if t.description:
                btn_text += f" ({t.description})"

            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_text},
                    "type": "primary" if t.name == current_tool else "default",
                    "disabled": bool(t.disabled),
                    "value": {
                        "action": "select_acp_tool",
                        "tool_name": t.name,
                        "project_id": project_id,
                    },
                }
            )

        elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card(UI_TEXT["system_acp_tool_select_title"], "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_acp_model_select_card(
        models: list,
        tool_name: str,
        project_id: Optional[str] = None,
        current_model: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build an interactive card for ACP model selection.

        ``models`` 可以是 ModelOptionView 或任意带 ``name``/``description`` 属性的对象。"""

        elements = [
            {
                "tag": "markdown",
                "content": UI_TEXT["system_acp_select_model_prompt"].format(tool=tool_name),
            }
        ]

        items: list[ModelOptionView] = []
        for model in models or []:
            if isinstance(model, ModelOptionView):
                items.append(model)
                continue

            m_name = getattr(model, "name", None) or str(model)
            m_name = str(m_name or "").strip()
            if not m_name:
                continue
            m_desc = getattr(model, "description", "")
            display = getattr(model, "display_name", None) or getattr(model, "friendly_name", None)

            items.append(
                ModelOptionView(
                    name=m_name,
                    description=str(m_desc or ""),
                    is_default=bool(getattr(model, "is_default", False)),
                    display_name=str(display) if display is not None else None,
                )
            )

        buttons = []
        for m in items:
            label = m.display_name or m.name
            btn_text = f"{label}"
            if m.description and m.description != label:
                btn_text += f" ({m.description})"

            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn_text},
                    "type": "primary" if m.name == current_model else "default",
                    "value": {
                        "action": "select_acp_model",
                        "tool_name": tool_name,
                        "model_name": m.name,
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
                        "text": {"tag": "plain_text", "content": UI_TEXT["system_ttadk_refresh_btn"]},
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

        card = CoreBuilder._wrap_card(UI_TEXT["system_acp_model_select_title"].format(tool=tool_name), "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_command_menu_card(project: Optional[ProjectContext] = None) -> tuple[str, str]:
        """Build a mobile-friendly command menu card."""
        project_id = project.project_id if project else None

        buttons = [
            {
                "text": UI_TEXT["system_menu_btn_new_project"],
                "type": "primary",
                "action": "new_project_prompt",
            },
            {
                "text": UI_TEXT["system_menu_btn_switch_project"],
                "type": "default",
                "action": "switch_project",
            },
            {
                "text": UI_TEXT["system_menu_btn_deep_task"],
                "type": "primary",
                "action": "enter_deep_prompt",
            },
            {
                "text": UI_TEXT["system_menu_btn_status"],
                "type": "default",
                "action": "show_status",
            },
            {
                "text": UI_TEXT["system_menu_btn_ttadk"],
                "type": "default",
                "action": "show_ttadk_menu",
            },
            {
                "text": UI_TEXT["system_menu_btn_acp"],
                "type": "default",
                "action": "show_acp_menu",
            },
            {
                "text": UI_TEXT["system_menu_btn_help"],
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
            {"tag": "markdown", "content": UI_TEXT["system_menu_header"]},
        ]
        elements.extend(build_responsive_layout(card_buttons))

        card = CoreBuilder._wrap_card(UI_TEXT["system_menu_title"], "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_help_card(
        project: Optional[ProjectContext] = None,
        category: str = "main",
        working_dir: Optional[str] = None,
        current_mode: any = None,
    ) -> tuple[str, str]:
        """Build a categorized help card."""
        from ...mode import InteractionMode
        
        mode_emoji = {
            InteractionMode.SMART: UI_TEXT["system_mode_smart"],
            InteractionMode.COCO: UI_TEXT["system_mode_coco"],
            InteractionMode.CLAUDE: UI_TEXT["system_mode_claude"],
            InteractionMode.AIDEN: UI_TEXT["system_mode_aiden"],
            InteractionMode.CODEX: UI_TEXT["system_mode_codex"],
            InteractionMode.GEMINI: UI_TEXT["system_mode_gemini"],
            InteractionMode.TTADK: UI_TEXT["system_mode_ttadk"],
        }
        
        current_mode_str = mode_emoji.get(current_mode, UI_TEXT["system_mode_smart"])

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
        """Build the help card with all commands expanded and mobile-friendly quick actions.

        The ``category`` parameter is accepted for backward compatibility but
        no longer drives tab switching — the card always renders every section
        so users see all commands at once.
        """
        del category  # kept for call-site compatibility; unused

        project_info = f"**{project_name}** (`{root_path}`)" if project_name else UI_TEXT["system_no_project"]

        # Quick-action buttons — tap targets that map to existing callbacks.
        # Ordered by expected mobile usage frequency.
        quick_actions = [
            (UI_TEXT["system_menu_btn_deep_task"], "primary", "enter_deep_prompt"),
            (UI_TEXT["system_menu_btn_ttadk"], "default", "show_ttadk_menu"),
            (UI_TEXT["system_menu_btn_acp"], "default", "show_acp_menu"),
            (UI_TEXT["system_menu_btn_status"], "default", "show_status"),
            (UI_TEXT["system_menu_btn_switch_project"], "default", "switch_project"),
            (UI_TEXT["system_menu_btn_new_project"], "default", "new_project_prompt"),
        ]
        quick_buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": btn_type,
                "value": {"action": action, "project_id": project_id},
            }
            for label, btn_type, action in quick_actions
        ]

        # All command sections — rendered inline so nothing is hidden behind a tab.
        sections = [
            (
                UI_TEXT["system_help_section_modes"],
                UI_TEXT["system_help_section_modes_body"]
            ),
            (
                UI_TEXT["system_help_section_deep"],
                UI_TEXT["system_help_section_deep_body"]
            ),
            (
                UI_TEXT["system_help_section_loop"],
                UI_TEXT["system_help_section_loop_body"]
            ),
            (
                UI_TEXT["system_help_section_spec"],
                UI_TEXT["system_help_section_spec_body"]
            ),
            (
                UI_TEXT["system_help_section_project"],
                UI_TEXT["system_help_section_project_body"]
            ),
            (
                UI_TEXT["system_help_section_ttadk"],
                UI_TEXT["system_help_section_ttadk_body"]
            ),
            (
                UI_TEXT["system_help_section_worktree"],
                UI_TEXT["system_help_section_worktree_body"]
            ),
        ]

        tips = UI_TEXT["system_help_tips"]

        elements = [
            {
                "tag": "markdown",
                "text_size": "notation",
                "content": UI_TEXT["system_help_status_header"].format(
                    mode=current_mode_str,
                    cwd=working_dir or '~',
                    project=project_info
                ),
            },
            {"tag": "hr"},
            {"tag": "markdown", "content": UI_TEXT["system_help_quick_entry"]},
        ]
        elements.extend(build_responsive_layout(quick_buttons))
        elements.append({"tag": "hr"})

        for idx, (title, body) in enumerate(sections):
            elements.append({"tag": "markdown", "content": f"**{title}**\n{body}"})
            if idx < len(sections) - 1:
                elements.append({"tag": "hr"})

        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "text_size": "notation", "content": tips})

        card = CoreBuilder._wrap_card(UI_TEXT["system_help_title"], "blue", elements)
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

        elements.append({"tag": "markdown", "content": UI_TEXT["system_tools_list_header"]})

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
                "content": UI_TEXT["system_tools_list_footer"].format(
                    available=available_count,
                    total=len(tools)
                ),
            }
        )

        card = CoreBuilder._wrap_card(UI_TEXT["system_tools_list_title"], "blue", elements)
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

        elements.append({"tag": "markdown", "content": UI_TEXT["system_tools_status_header"]})

        for tool in tools:
            tool_name = tool["name"]
            emoji = tool.get("emoji", "🤖")
            is_available = tool.get("available", False)
            last_used = tool.get("last_used", UI_TEXT["system_never_used"])

            status_text = UI_TEXT["system_status_available"] if is_available else UI_TEXT["system_status_unavailable"]
            active_info = ""
            if tool_name in active_sessions:
                session_info = active_sessions[tool_name]
                active_info = UI_TEXT["system_tools_status_active_session"].format(
                    chat_id=session_info.get('chat_id', 'N/A')
                )

            elements.append(
                {
                    "tag": "markdown",
                    "content": UI_TEXT["system_tools_status_item"].format(
                        emoji=emoji,
                        name=tool_name.capitalize(),
                        status=status_text,
                        last_used=last_used,
                        active_info=active_info
                    ),
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
                        "text": {"tag": "plain_text", "content": UI_TEXT["system_tools_status_btn_enter"].format(name=tool['name'].capitalize())},
                        "type": "default",
                        "value": {"action": f"enter_{tool['name']}", "project_id": project.project_id if project else None},
                    }
                )

        if action_buttons:
            elements.extend(build_responsive_layout(action_buttons))

        card = CoreBuilder._wrap_card(UI_TEXT["system_tools_status_title"], "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
