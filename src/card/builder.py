import json
import time
from typing import Optional
from ..project.context import ProjectContext, ProjectStatus
from .themes import get_theme


class CardBuilder:
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
    def build_project_response_card(
        project: ProjectContext,
        title: str,
        content: str,
        show_buttons: bool = True,
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)

        header_title = f"{project.get_status_emoji()} {project.project_name}"
        if project.coco_mode:
            header_title += " 🤖"

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"📂 `{project.root_path}`"
                }
            },
            {
                "tag": "hr"
            },
            CardBuilder._build_content_element(content, title)
        ]

        if footer:
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": footer}
                ]
            })

        if show_buttons:
            buttons = []

            if project.coco_mode:
                buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🚪 退出Coco"},
                    "type": "default",
                    "value": {
                        "action": "exit_coco",
                        "project_id": project.project_id
                    }
                })
            else:
                buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🤖 Coco模式"},
                    "type": "primary",
                    "value": {
                        "action": "enter_coco",
                        "project_id": project.project_id
                    }
                })

            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📊 状态"},
                "type": "default",
                "value": {
                    "action": "show_status",
                    "project_id": project.project_id
                }
            })

            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 切换项目"},
                "type": "default",
                "value": {
                    "action": "switch_project"
                }
            })

            if extra_buttons:
                buttons.extend(extra_buttons)

            elements.append({
                "tag": "action",
                "actions": buttons[:4]
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
            if project.coco_mode:
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

            elements.append({
                "tag": "action",
                "actions": buttons
            })

            elements.append({"tag": "hr"})

        elements.pop()

        elements.append({
            "tag": "action",
            "actions": [
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
            ]
        })

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
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"📂 **{project.project_name}** | `{project.root_path}`"
                }
            },
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
            elements.append({
                "tag": "action",
                "actions": buttons[:4]
            })
        else:
            default_buttons = [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "继续开发"},
                    "type": "primary",
                    "value": {
                        "action": "continue_dev",
                        "project_id": project.project_id
                    }
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🤖 Coco帮我"},
                    "type": "default",
                    "value": {
                        "action": "enter_coco",
                        "project_id": project.project_id
                    }
                }
            ]
            elements.append({
                "tag": "action",
                "actions": default_buttons
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

        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"📂 `{project.root_path}`"
                }
            },
            {"tag": "hr"},
            CardBuilder._build_content_element(content),
            {
                "tag": "action",
                "actions": buttons
            }
        ]

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{project.get_status_emoji()} {project.project_name}"},
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
                "text": {"tag": "plain_text", "content": "📁 查看文件"},
                "type": "default",
                "value": {
                    "action": "list_files",
                    "project_id": project.project_id
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📊 项目看板"},
                "type": "default",
                "value": {"action": "show_board"}
            }
        ]

        elements = [
            CardBuilder._build_content_element(content),
            {
                "tag": "action",
                "actions": buttons
            }
        ]

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
            elements.insert(0, {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"📂 **{project.project_name}** | `{project.root_path}`"
                }
            })
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
