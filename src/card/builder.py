import json
import time
from typing import Optional
from ..project.context import ProjectContext, ProjectStatus
from .themes import get_theme
from ..config import get_settings


class CardBuilder:
    # 统一的移动端紧凑按钮样式（飞书卡片支持 small/medium/large，small 更适合手机端）
    BUTTON_SIZE = "small"

    @staticmethod
    def _apply_compact_button_style(button: dict) -> dict:
        # 只做“加字段”式的温和兼容：不改变既有 key，避免影响回调结构
        if isinstance(button, dict) and button.get("tag") == "button":
            button.setdefault("size", CardBuilder.BUTTON_SIZE)
        return button

    @staticmethod
    def _build_button_grid(buttons: list[dict], columns: int = 2) -> list[dict]:
        """把按钮按两列网格排布，尽量在手机端一行显示两个按钮。

        飞书卡片不支持 CSS Flexbox/Grid，这里使用 column_set 模拟两列布局。
        """
        if not buttons:
            return []

        if columns <= 0:
            columns = 2

        styled = [CardBuilder._apply_compact_button_style(b) for b in buttons]
        rows: list[dict] = []

        for i in range(0, len(styled), columns):
            chunk = styled[i : i + columns]

            # 目前项目里都用两列，因此这里固定生成两列，确保手机端“两个按钮一行”。
            col_1 = chunk[0] if len(chunk) > 0 else None
            col_2 = chunk[1] if len(chunk) > 1 else None

            rows.append({
                "tag": "column_set",
                "flex_mode": "stretch",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [col_1] if col_1 else []
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [col_2] if col_2 else []
                    }
                ]
            })

        return rows

    @staticmethod
    def _build_button_row_action(buttons: list[dict]) -> list[dict]:
        if not buttons:
            return []
        styled = [CardBuilder._apply_compact_button_style(b) for b in buttons]
        return [{"tag": "action", "actions": styled}]

    @staticmethod
    def _build_buttons_responsive(buttons: list[dict]) -> list[dict]:
        """响应式按钮布局。

        由于飞书卡片不支持 CSS media query，这里用“策略”来兼顾：
        - 桌面端：尽量保持原有 action 布局
        - 手机端：在按钮数量较多时使用两列 column_set，避免自动换行导致堆叠

        可通过 settings.card_button_layout 覆盖：desktop/mobile/responsive
        """
        if not buttons:
            return []

        layout = (get_settings().card_button_layout or "responsive").strip().lower()
        if layout == "desktop":
            return CardBuilder._build_button_row_action(buttons)
        if layout == "mobile":
            return CardBuilder._build_button_grid(buttons)

        # responsive（默认）：两按钮以内用 action，更贴近桌面端观感；更多按钮用两列 grid
        if len(buttons) <= 2:
            return CardBuilder._build_button_row_action(buttons)
        return CardBuilder._build_button_grid(buttons)
    @staticmethod
    def _has_code_block(content: str) -> bool:
        return "```" in content
    
    @staticmethod
    def _build_content_element(content: str, with_title: Optional[str] = None) -> dict:
        full_content = f"**{with_title}**\n\n{content}" if with_title else content
        
        if CardBuilder._has_code_block(content):
            return {
                "tag": "markdown",
                "content": full_content
            }
        else:
            return {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": full_content
                }
            }

    @staticmethod
    def _build_header_title(project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False) -> str:
        if not project:
            if is_claude_mode:
                return "🔮 Claude 编程模式"
            mode_icon = "🤖" if is_coco_mode else "🧠"
            mode_name = "编程模式" if is_coco_mode else "智能模式"
            return f"{mode_icon} {mode_name}"

        if is_claude_mode or project.claude_mode:
            return f"🔮 {project.project_name} · Claude"
        elif is_coco_mode or project.coco_mode:
            return f"🤖 {project.project_name} · Coco"
        else:
            return f"🧠 {project.project_name}"

    @staticmethod
    def _build_directory_element(project: Optional[ProjectContext], working_dir: Optional[str] = None) -> dict:
        if project:
            path = project.root_path
        elif working_dir:
            path = working_dir
        else:
            path = "~"
        
        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"📁 `{path}`"
            }
        }

    @staticmethod
    def _build_footer_buttons(project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False) -> list[dict]:
        buttons = []
        project_id = project.project_id if project else None

        if is_claude_mode or (project and project.claude_mode):
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🚪 退出Claude"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {"action": "exit_claude", "project_id": project_id}}]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 切换项目"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}]
            })
        elif is_coco_mode or (project and project.coco_mode):
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🚪 退出Coco"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {"action": "exit_coco", "project_id": project_id}}]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 切换项目"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}]
            })
        else:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🤖 Coco模式"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {"action": "enter_coco", "project_id": project_id}}]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔮 Claude模式"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {"action": "enter_claude", "project_id": project_id}}]
            })

        return [CardBuilder._apply_compact_button_style(b) for b in buttons]

    @staticmethod
    def _build_footer_note(project: Optional[ProjectContext], working_dir: Optional[str] = None) -> Optional[dict]:
        if project:
            return {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"📂 项目目录: {project.root_path}"}
                ]
            }
        return None

    @staticmethod
    def build_coco_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color if project else "blue")
        
        header_title = CardBuilder._build_header_title(project, is_coco_mode=True)
        
        elements = [
            CardBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
            CardBuilder._build_content_element(content, title)
        ]
        
        footer_note = CardBuilder._build_footer_note(project, working_dir)
        if footer_note:
            elements.append(footer_note)
        
        if show_buttons:
            elements.extend(CardBuilder._build_buttons_responsive(
                CardBuilder._build_footer_buttons(project, is_coco_mode=True)
            ))
        
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }
        
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_smart_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color if project else "blue")
        
        header_title = CardBuilder._build_header_title(project, is_coco_mode=False)
        
        elements = [
            CardBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
            CardBuilder._build_content_element(content, title)
        ]
        
        footer_note = CardBuilder._build_footer_note(project, working_dir)
        if footer_note:
            elements.append(footer_note)
        
        if show_buttons:
            elements.extend(CardBuilder._build_buttons_responsive(
                CardBuilder._build_footer_buttons(project, is_coco_mode=False)
            ))
        
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }
        
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _build_image_elements(image_keys: list[str]) -> list[dict]:
        """构建飞书卡片图片元素列表"""
        elements = []
        for i, key in enumerate(image_keys):
            elements.append({
                "tag": "img",
                "img_key": key,
                "alt": {
                    "tag": "plain_text",
                    "content": f"图片 {i + 1}"
                }
            })
        return elements

    @staticmethod
    def build_project_response_card(
        project: ProjectContext,
        title: str,
        content: str,
        show_buttons: bool = True,
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)

        header_title = CardBuilder._build_header_title(project, is_coco_mode=project.coco_mode, is_claude_mode=project.claude_mode)

        elements = [
            CardBuilder._build_directory_element(project),
            {"tag": "hr"},
        ]

        if image_keys:
            elements.extend(CardBuilder._build_image_elements(image_keys))
            elements.append({"tag": "hr"})

        elements.append(CardBuilder._build_content_element(content, title))

        if footer:
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": footer}
                ]
            })

        if show_buttons:
            buttons = CardBuilder._build_footer_buttons(project, is_coco_mode=project.coco_mode, is_claude_mode=project.claude_mode)
            if extra_buttons:
                # extra_buttons 也统一做紧凑样式
                buttons.extend([CardBuilder._apply_compact_button_style(b) for b in extra_buttons])
            # 手机上纵向空间紧张，这个 hr 会显得很“高”，去掉更紧凑
            elements.extend(CardBuilder._build_buttons_responsive(buttons))

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_status_board_card(
        projects: list[ProjectContext],
        current_project_id: Optional[str] = None,
    ) -> tuple[str, str]:
        if not projects:
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "📋 项目看板"},
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "暂无项目\n\n发送 `/new 项目名 路径` 创建新项目"
                        }
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "➕ 新建项目"},
                                "type": "primary",
                                "value": {"action": "new_project_prompt"}
                            }
                        ]
                    }
                ]
            }
            card["elements"][1]["actions"] = [
                CardBuilder._apply_compact_button_style(b) for b in card["elements"][1]["actions"]
            ]
            return "interactive", json.dumps(card, ensure_ascii=False)

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"共 **{len(projects)}** 个项目"
                }
            },
            {"tag": "hr"}
        ]

        for project in projects:
            is_current = project.project_id == current_project_id
            status_emoji = project.get_status_emoji()
            current_marker = " (当前)" if is_current else ""

            mode_info = ""
            if project.claude_mode:
                query_count = 0
                if project.claude_session_snapshot:
                    query_count = project.claude_session_snapshot.query_count
                mode_info = f" | 🔮 Claude 模式中 (消息数: {query_count})"
            elif project.coco_mode:
                query_count = 0
                if project.coco_session_snapshot:
                    query_count = project.coco_session_snapshot.query_count
                mode_info = f" | 🤖 Coco 模式中 (消息数: {query_count})"
            elif project.status == ProjectStatus.BUSY and project.current_task:
                mode_info = f" | ⏳ {project.current_task.task_type}"

            last_active = CardBuilder._format_time_ago(project.last_active)

            project_content = (
                f"{status_emoji} **{project.project_name}**{current_marker}\n"
                f"└─ 📁 `{project.root_path}`\n"
                f"└─ ⏱️ {last_active}{mode_info}"
            )

            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": project_content}
            })

            buttons = []
            if not is_current:
                buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "切换到此项目"},
                    "type": "primary",
                    "value": {
                        "action": "switch_to",
                        "project_id": project.project_id
                    }
                })
            else:
                buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "继续开发"},
                    "type": "primary",
                    "value": {
                        "action": "continue_dev",
                        "project_id": project.project_id
                    }
                })

            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看详情"},
                "type": "default",
                "value": {
                    "action": "show_detail",
                    "project_id": project.project_id
                }
            })

            # 用 column_set 强制两列布局，手机端更稳定（避免 action 自动换行堆叠）
            elements.extend(CardBuilder._build_buttons_responsive(buttons))

            elements.append({"tag": "hr"})

        elements.pop()

        elements.extend(CardBuilder._build_buttons_responsive([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "➕ 新建项目"},
                "type": "default",
                "value": {"action": "new_project_prompt"}
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 刷新"},
                "type": "default",
                "value": {"action": "refresh_board"}
            }
        ]))

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📋 项目看板"},
                "template": "blue"
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_notification_card(
        project: ProjectContext,
        notification_type: str,
        title: str,
        content: str,
        suggestions: Optional[list[str]] = None,
        buttons: Optional[list[dict]] = None,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)

        type_emoji = {
            "success": "✅",
            "error": "❌",
            "warning": "⚠️",
            "info": "ℹ️",
            "task_complete": "🎉",
        }.get(notification_type, "📢")

        header_title = f"{type_emoji} {title}"

        elements = [
            CardBuilder._build_directory_element(project),
            {"tag": "hr"},
            CardBuilder._build_content_element(content)
        ]

        if suggestions:
            suggestion_text = "💡 **建议下一步:**\n" + "\n".join(f"• {s}" for s in suggestions)
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": suggestion_text}
            })

        if buttons:
            elements.extend(CardBuilder._build_buttons_responsive(buttons[:4]))
        else:
            elements.extend(CardBuilder._build_buttons_responsive(
                CardBuilder._build_footer_buttons(
                    project,
                    is_coco_mode=project.coco_mode,
                    is_claude_mode=project.claude_mode,
                )
            ))

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_coco_resume_card(
        project: ProjectContext,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)

        if not project.coco_session_snapshot:
            return CardBuilder.build_project_response_card(
                project, "Coco 模式", "没有可恢复的会话", show_buttons=True
            )

        snapshot = project.coco_session_snapshot
        content = (
            f"🔄 检测到未完成的 Coco 会话\n\n"
            f"• 会话 ID: `{snapshot.session_id}`\n"
            f"• 对话数: {snapshot.query_count} 条\n"
            f"• 最后对话: {snapshot.last_query[:50]}..." if len(snapshot.last_query) > 50 else f"• 最后对话: {snapshot.last_query}"
        )

        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 恢复会话"},
                "type": "primary",
                "value": {
                    "action": "resume_coco",
                    "project_id": project.project_id,
                    "session_id": snapshot.session_id
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🆕 开始新会话"},
                "type": "default",
                "value": {
                    "action": "new_coco",
                    "project_id": project.project_id
                }
            }
        ]
        buttons = [CardBuilder._apply_compact_button_style(b) for b in buttons]

        header_title = CardBuilder._build_header_title(project, is_coco_mode=True)

        elements = [
            CardBuilder._build_directory_element(project),
            {"tag": "hr"},
            CardBuilder._build_content_element(content),
        ]

        elements.extend(CardBuilder._build_buttons_responsive(buttons))

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_claude_resume_card(
        project: ProjectContext,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)

        if not project.claude_session_snapshot:
            return CardBuilder.build_project_response_card(
                project, "Claude 模式", "没有可恢复的会话", show_buttons=True
            )

        snapshot = project.claude_session_snapshot
        last_query_display = snapshot.last_query[:50] + "..." if len(snapshot.last_query) > 50 else snapshot.last_query
        content = (
            f"🔄 检测到未完成的 Claude 会话\n\n"
            f"• 会话 ID: `{snapshot.session_id}`\n"
            f"• 对话数: {snapshot.query_count} 条\n"
            f"• 最后对话: {last_query_display}"
        )

        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 恢复会话"},
                "type": "primary",
                "value": {
                    "action": "resume_claude",
                    "project_id": project.project_id,
                    "session_id": snapshot.session_id
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🆕 开始新会话"},
                "type": "default",
                "value": {
                    "action": "new_claude",
                    "project_id": project.project_id
                }
            }
        ]
        buttons = [CardBuilder._apply_compact_button_style(b) for b in buttons]

        header_title = CardBuilder._build_header_title(project, is_claude_mode=True)

        elements = [
            CardBuilder._build_directory_element(project),
            {"tag": "hr"},
            CardBuilder._build_content_element(content),
        ]

        elements.extend(CardBuilder._build_buttons_responsive(buttons))

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_project_created_card(
        project: ProjectContext,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)

        content = (
            f"✅ 项目 **{project.project_name}** 创建成功\n\n"
            f"• 项目 ID: `{project.project_id}`\n"
            f"• 路径: `{project.root_path}`\n"
            f"• 主题色: {project.emoji_prefix} {project.theme_color}"
        )

        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🤖 开始 Coco"},
                "type": "primary",
                "value": {
                    "action": "enter_coco",
                    "project_id": project.project_id
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔮 开始 Claude"},
                "type": "default",
                "value": {
                    "action": "enter_claude",
                    "project_id": project.project_id
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📋 项目看板"},
                "type": "default",
                "value": {"action": "show_board"}
            }
        ]
        buttons = [CardBuilder._apply_compact_button_style(b) for b in buttons]

        elements = [
            CardBuilder._build_content_element(content),
        ]

        elements.extend(CardBuilder._build_buttons_responsive(buttons))

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🎉 新项目已创建"},
                "template": theme.header_template
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_error_card(
        error_message: str,
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        elements = [
            CardBuilder._build_content_element(f"❌ **错误**\n\n{error_message}")
        ]

        if project:
            elements.insert(0, CardBuilder._build_directory_element(project))
            elements.insert(1, {"tag": "hr"})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ 操作失败"},
                "template": "red"
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _build_deep_header_title(
        project: Optional[ProjectContext],
        engine_name: str = "Coco",
    ) -> str:
        if project:
            return f"🧠 {project.project_name} · Deep ({engine_name})"
        return f"🧠 Deep Engine ({engine_name})"

    @staticmethod
    def _build_deep_buttons(
        deep_project_id: Optional[str] = None,
        is_executing: bool = False,
        is_paused: bool = False,
    ) -> list[dict]:
        buttons = []
        if is_executing:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⏸️ 暂停"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {"action": "deep_pause", "project_id": deep_project_id}}]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "behaviors": [{"type": "callback", "value": {"action": "deep_stop", "project_id": deep_project_id}}]
            })
        elif is_paused:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "▶️ 继续"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {"action": "deep_resume", "project_id": deep_project_id}}]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "behaviors": [{"type": "callback", "value": {"action": "deep_stop", "project_id": deep_project_id}}]
            })
        return buttons

    @staticmethod
    def build_deep_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        progress_bar: Optional[str] = None,
        deep_project_id: Optional[str] = None,
        is_executing: bool = False,
        is_paused: bool = False,
        engine_name: str = "Coco",
        show_buttons: bool = True,
        working_dir: Optional[str] = None,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color if project else "turquoise")

        header_title = CardBuilder._build_deep_header_title(project, engine_name)

        elements = [
            CardBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
        ]

        if progress_bar:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"📊 {progress_bar}"}
            })

        elements.append(CardBuilder._build_content_element(content, title))

        if show_buttons:
            if is_executing or is_paused:
                buttons = CardBuilder._build_deep_buttons(deep_project_id, is_executing, is_paused)
            else:
                buttons = CardBuilder._build_footer_buttons(project, is_coco_mode=False, is_claude_mode=False)

            if buttons:
                elements.append({"tag": "hr"})
                elements.append({
                    "tag": "column_set",
                    "flex_mode": "stretch",
                    "background_style": "default",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [buttons[0]] if buttons else []
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [buttons[1]] if len(buttons) > 1 else []
                        }
                    ]
                })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": theme.header_template
            },
            "elements": elements
        }

        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _format_time_ago(timestamp: float) -> str:
        diff = time.time() - timestamp
        if diff < 60:
            return "刚刚"
        elif diff < 3600:
            minutes = int(diff / 60)
            return f"{minutes} 分钟前"
        elif diff < 86400:
            hours = int(diff / 3600)
            return f"{hours} 小时前"
        else:
            days = int(diff / 86400)
            return f"{days} 天前"
