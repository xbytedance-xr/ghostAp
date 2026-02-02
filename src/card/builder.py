import json
import time
from typing import Optional
from ..project.context import ProjectContext, ProjectStatus
from .themes import get_theme
from .shared import (
    apply_compact_style,
    build_mode_buttons,
    build_responsive_layout,
    BUTTON_SIZE,
)


class CardBuilder:
    BUTTON_SIZE = BUTTON_SIZE

    @staticmethod
    def _apply_compact_button_style(button: dict) -> dict:
        return apply_compact_style(button)

    @staticmethod
    def _build_button_grid(buttons: list[dict], columns: int = 2) -> list[dict]:
        from .shared import _build_button_grid
        return _build_button_grid(buttons, columns)

    @staticmethod
    def _build_button_row_action(buttons: list[dict]) -> list[dict]:
        from .shared import _build_button_row_action
        return _build_button_row_action(buttons)

    @staticmethod
    def _build_buttons_responsive(buttons: list[dict]) -> list[dict]:
        return build_responsive_layout(buttons)

    @staticmethod
    def _build_content_element(content: str, with_title: Optional[str] = None) -> dict:
        full_content = f"**{with_title}**\n\n{content}" if with_title else content
        return {
            "tag": "markdown",
            "content": full_content
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
            "tag": "markdown",
            "content": f"📁 `{path}`"
        }

    @staticmethod
    def _build_footer_buttons(project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False) -> list[dict]:
        project_id = project.project_id if project else None
        return build_mode_buttons(is_coco_mode, project_id, is_claude_mode)

    @staticmethod
    def _build_footer_note(project: Optional[ProjectContext], working_dir: Optional[str] = None) -> Optional[dict]:
        if project:
            return {
                "tag": "markdown",
                "content": f"📂 项目目录: `{project.root_path}`",
                "text_size": "notation",
            }
        return None

    @staticmethod
    def _wrap_card(header_title: str, header_template: str, elements: list[dict]) -> dict:
        """Build a schema 2.0 card JSON structure."""
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": header_template,
            },
            "body": {
                "elements": elements,
            },
        }

    # ---- Response card (unified for coco/smart/project) ----

    @staticmethod
    def _build_response_card_inner(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
        is_coco_mode: bool = False,
        is_claude_mode: bool = False,
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color if project else ("blue" if is_coco_mode else "blue"))

        # Determine actual mode from project if available
        actual_coco = is_coco_mode or (project and project.coco_mode)
        actual_claude = is_claude_mode or (project and project.claude_mode)

        header_title = CardBuilder._build_header_title(project, is_coco_mode=actual_coco, is_claude_mode=actual_claude)

        elements = [
            CardBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
        ]

        if image_keys:
            elements.extend(CardBuilder._build_image_elements(image_keys))
            elements.append({"tag": "hr"})

        elements.append(CardBuilder._build_content_element(content, title))

        if footer:
            elements.append({
                "tag": "markdown",
                "content": footer,
                "text_size": "notation",
            })
        else:
            footer_note = CardBuilder._build_footer_note(project, working_dir)
            if footer_note:
                elements.append(footer_note)

        if show_buttons:
            buttons = CardBuilder._build_footer_buttons(project, is_coco_mode=actual_coco, is_claude_mode=actual_claude)
            if extra_buttons:
                buttons.extend([apply_compact_style(b) for b in extra_buttons])
            elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_coco_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        return CardBuilder._build_response_card_inner(
            project, title, content, working_dir, show_buttons,
            is_coco_mode=True,
        )

    @staticmethod
    def build_smart_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        return CardBuilder._build_response_card_inner(
            project, title, content, working_dir, show_buttons,
            is_coco_mode=False,
        )

    @staticmethod
    def _build_image_elements(image_keys: list[str]) -> list[dict]:
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
        return CardBuilder._build_response_card_inner(
            project, title, content,
            show_buttons=show_buttons,
            is_coco_mode=project.coco_mode,
            is_claude_mode=project.claude_mode,
            extra_buttons=extra_buttons,
            footer=footer,
            image_keys=image_keys,
        )

    @staticmethod
    def build_status_board_card(
        projects: list[ProjectContext],
        current_project_id: Optional[str] = None,
    ) -> tuple[str, str]:
        if not projects:
            empty_elements = [
                {
                    "tag": "markdown",
                    "content": "暂无项目\n\n发送 `/new 项目名 路径` 创建新项目"
                },
                *build_responsive_layout([
                    apply_compact_style({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "➕ 新建项目"},
                        "type": "primary",
                        "value": {"action": "new_project_prompt"}
                    })
                ])
            ]
            card = CardBuilder._wrap_card("📋 项目看板", "blue", empty_elements)
            return "interactive", json.dumps(card, ensure_ascii=False)

        elements = [
            {
                "tag": "markdown",
                "content": f"共 **{len(projects)}** 个项目"
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
                "tag": "markdown",
                "content": project_content
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

            elements.extend(build_responsive_layout(buttons))
            elements.append({"tag": "hr"})

        elements.pop()

        elements.extend(build_responsive_layout([
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

        card = CardBuilder._wrap_card("📋 项目看板", "blue", elements)
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
                "tag": "markdown",
                "content": suggestion_text
            })

        if buttons:
            elements.extend(build_responsive_layout(buttons[:4]))
        else:
            elements.extend(build_responsive_layout(
                CardBuilder._build_footer_buttons(
                    project,
                    is_coco_mode=project.coco_mode,
                    is_claude_mode=project.claude_mode,
                )
            ))

        card = CardBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    # ---- Resume cards (unified) ----

    @staticmethod
    def _build_resume_card(
        project: ProjectContext,
        mode: str,
    ) -> tuple[str, str]:
        """Internal unified resume card builder for coco/claude."""
        theme = get_theme(project.theme_color)
        is_coco = mode == "coco"
        is_claude = mode == "claude"
        mode_name = "Coco" if is_coco else "Claude"

        snapshot = project.coco_session_snapshot if is_coco else project.claude_session_snapshot
        if not snapshot:
            return CardBuilder.build_project_response_card(
                project, f"{mode_name} 模式", "没有可恢复的会话", show_buttons=True
            )

        last_query_display = snapshot.last_query[:50] + "..." if len(snapshot.last_query) > 50 else snapshot.last_query
        content = (
            f"🔄 检测到未完成的 {mode_name} 会话\n\n"
            f"• 会话 ID: `{snapshot.session_id}`\n"
            f"• 对话数: {snapshot.query_count} 条\n"
            f"• 最后对话: {last_query_display}"
        )

        resume_action = f"resume_{mode}"
        new_action = f"new_{mode}"

        buttons = [
            apply_compact_style({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 恢复会话"},
                "type": "primary",
                "value": {
                    "action": resume_action,
                    "project_id": project.project_id,
                    "session_id": snapshot.session_id,
                },
            }),
            apply_compact_style({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🆕 开始新会话"},
                "type": "default",
                "value": {
                    "action": new_action,
                    "project_id": project.project_id,
                },
            }),
        ]

        header_title = CardBuilder._build_header_title(project, is_coco_mode=is_coco, is_claude_mode=is_claude)

        elements = [
            CardBuilder._build_directory_element(project),
            {"tag": "hr"},
            CardBuilder._build_content_element(content),
        ]
        elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_coco_resume_card(project: ProjectContext) -> tuple[str, str]:
        return CardBuilder._build_resume_card(project, "coco")

    @staticmethod
    def build_claude_resume_card(project: ProjectContext) -> tuple[str, str]:
        return CardBuilder._build_resume_card(project, "claude")

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
            apply_compact_style({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🤖 开始 Coco"},
                "type": "primary",
                "value": {
                    "action": "enter_coco",
                    "project_id": project.project_id
                }
            }),
            apply_compact_style({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔮 开始 Claude"},
                "type": "default",
                "value": {
                    "action": "enter_claude",
                    "project_id": project.project_id
                }
            }),
            apply_compact_style({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📋 项目看板"},
                "type": "default",
                "value": {"action": "show_board"}
            }),
        ]

        elements = [
            CardBuilder._build_content_element(content),
        ]
        elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card("🎉 新项目已创建", theme.header_template, elements)
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

        card = CardBuilder._wrap_card("⚠️ 操作失败", "red", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    # ---- Deep Engine cards ----

    @staticmethod
    def _build_deep_header_title(
        project: Optional[ProjectContext],
        engine_name: str = "Coco",
    ) -> str:
        if project:
            return f"🧠 {project.project_name} · Deep Agent ({engine_name})"
        return f"🧠 Deep Agent ({engine_name})"

    @staticmethod
    def _pick_engine_template(engine_name: str) -> str:
        name = (engine_name or "").strip().lower()
        if name.startswith("claude"):
            return "purple"
        if name.startswith("coco"):
            return "blue"
        return "turquoise"

    @staticmethod
    def _build_deep_buttons(
        project_id: Optional[str] = None,
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
                "behaviors": [{
                    "type": "callback",
                    "value": {"action": "deep_pause", "project_id": project_id, "deep_project_id": deep_project_id}
                }]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "behaviors": [{
                    "type": "callback",
                    "value": {"action": "deep_stop", "project_id": project_id, "deep_project_id": deep_project_id}
                }]
            })
        elif is_paused:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "▶️ 继续"},
                "type": "primary",
                "behaviors": [{
                    "type": "callback",
                    "value": {"action": "deep_resume", "project_id": project_id, "deep_project_id": deep_project_id}
                }]
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "behaviors": [{
                    "type": "callback",
                    "value": {"action": "deep_stop", "project_id": project_id, "deep_project_id": deep_project_id}
                }]
            })
        return [apply_compact_style(b) for b in buttons]

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
        header_template = CardBuilder._pick_engine_template(engine_name)
        theme = get_theme(header_template)

        header_title = CardBuilder._build_deep_header_title(project, engine_name)

        elements = [
            CardBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
        ]

        if progress_bar and (not content or progress_bar not in content):
            elements.append({"tag": "markdown", "content": f"📊 {progress_bar}"})

        elements.append(CardBuilder._build_content_element(content, title))

        if show_buttons:
            if is_executing or is_paused:
                buttons = CardBuilder._build_deep_buttons(
                    project.project_id if project else None,
                    deep_project_id,
                    is_executing,
                    is_paused,
                )
            else:
                buttons = CardBuilder._build_footer_buttons(project, is_coco_mode=False, is_claude_mode=False)

            if buttons:
                elements.append({"tag": "hr"})
                elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card(header_title, theme.header_template, elements)
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
