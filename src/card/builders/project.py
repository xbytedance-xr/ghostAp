import json
from typing import Optional

from src.project.context import ProjectContext, ProjectStatus

from ..shared import (
    apply_compact_style,
    build_responsive_layout,
    get_theme,
)
from .core import CoreBuilder


class ProjectBuilder:
    """Project-related card building utilities."""

    @staticmethod
    def build_project_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        show_buttons: bool = True,
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        banner: Optional[dict] = None,
    ) -> tuple[str, str]:
        return ProjectBuilder._build_response_card_inner(
            project,
            title,
            content,
            show_buttons=show_buttons,
            is_coco_mode=bool(project and getattr(project, "coco_mode", False)),
            is_claude_mode=bool(project and getattr(project, "claude_mode", False)),
            is_ttadk_mode=bool(project and getattr(project, "ttadk_mode", False)),
            is_gemini_mode=bool(project and getattr(project, "gemini_mode", False)),
            extra_buttons=extra_buttons,
            footer=footer,
            image_keys=image_keys,
            banner=banner,
        )

    @staticmethod
    def build_coco_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        return ProjectBuilder._build_response_card_inner(
            project,
            title,
            content,
            working_dir,
            show_buttons,
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
        return ProjectBuilder._build_response_card_inner(
            project,
            title,
            content,
            working_dir,
            show_buttons,
            is_coco_mode=False,
        )

    @staticmethod
    def _build_response_card_inner(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
        is_coco_mode: bool = False,
        is_claude_mode: bool = False,
        is_ttadk_mode: bool = False,
        is_gemini_mode: bool = False,
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        banner: Optional[dict] = None,
    ) -> tuple[str, str]:
        theme_color = getattr(project, "theme_color", None) if project else None
        if not theme_color:
            theme_color = "orange" if is_ttadk_mode else "turquoise" if is_gemini_mode else "blue"
        theme = get_theme(theme_color)

        # Determine actual mode from project if available
        actual_coco = is_coco_mode or (project and getattr(project, "coco_mode", False))
        actual_claude = is_claude_mode or (project and getattr(project, "claude_mode", False))
        actual_ttadk = is_ttadk_mode or (project and getattr(project, "ttadk_mode", False))
        actual_gemini = is_gemini_mode or (project and getattr(project, "gemini_mode", False))

        header_title = CoreBuilder._build_header_title(
            project,
            is_coco_mode=actual_coco,
            is_claude_mode=actual_claude,
            is_ttadk_mode=actual_ttadk,
            is_gemini_mode=actual_gemini,
        )

        elements = [
            CoreBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
        ]

        if banner:
            elements.append(banner)
            elements.append({"tag": "hr"})

        if actual_ttadk:
            ttadk_status = CoreBuilder._build_ttadk_status_element(project)
            if ttadk_status:
                elements.append(ttadk_status)
                elements.append({"tag": "hr"})

        if image_keys:
            elements.extend(CoreBuilder._build_image_elements(image_keys))
            elements.append({"tag": "hr"})

        elements.append(CoreBuilder._build_content_element(content, title))

        if footer:
            elements.append(
                {
                    "tag": "markdown",
                    "content": footer,
                    "text_size": "notation",
                }
            )
        else:
            footer_note = CoreBuilder._build_footer_note(project, working_dir)
            if footer_note:
                elements.append(footer_note)

        if show_buttons:
            buttons = CoreBuilder._build_footer_buttons(
                project,
                is_coco_mode=actual_coco,
                is_claude_mode=actual_claude,
                is_ttadk_mode=actual_ttadk,
                is_gemini_mode=actual_gemini,
            )
            if extra_buttons:
                buttons.extend([apply_compact_style(b) for b in extra_buttons])
            elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_status_board_card(
        projects: list[ProjectContext],
        current_project_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[str, str]:
        if not projects:
            empty_elements = [
                {"tag": "markdown", "content": "暂无项目\n\n发送 `/new 项目名 路径` 创建新项目"},
                *build_responsive_layout(
                    [
                        apply_compact_style(
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "➕ 新建项目"},
                                "type": "primary",
                                "value": {"action": "new_project_prompt"},
                            }
                        )
                    ]
                ),
            ]
            card = CoreBuilder._wrap_card("📋 项目看板", "blue", empty_elements)
            return "interactive", json.dumps(card, ensure_ascii=False)

        # Sort: Current project first, then by last_active descending
        def _sort_key(p):
            is_cur = p.project_id == current_project_id
            return (not is_cur, -(p.last_active or 0))

        sorted_projects = sorted(projects, key=_sort_key)
        total_projects = len(sorted_projects)
        total_pages = (total_projects + page_size - 1) // page_size

        # Clamp page
        page = max(1, min(page, total_pages)) if total_pages > 0 else 1

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_projects = sorted_projects[start_idx:end_idx]

        elements = [{"tag": "markdown", "content": f"共 **{total_projects}** 个项目"}, {"tag": "hr"}]

        for project in page_projects:
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

            last_active = CoreBuilder._format_time_ago(project.last_active)

            project_content = (
                f"{status_emoji} **{project.project_name}**{current_marker}\n"
                f"└─ 📁 `{project.root_path}`\n"
                f"└─ ⏱️ {last_active}{mode_info}"
            )

            elements.append({"tag": "markdown", "content": project_content})

            buttons = []
            if not is_current:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "切换到此项目"},
                        "type": "primary",
                        "value": {"action": "switch_to", "project_id": project.project_id},
                    }
                )
            else:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "继续开发"},
                        "type": "primary",
                        "value": {"action": "continue_dev", "project_id": project.project_id},
                    }
                )

            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看详情"},
                    "type": "default",
                    "value": {"action": "show_detail", "project_id": project.project_id},
                }
            )

            elements.extend(build_responsive_layout(buttons))
            elements.append({"tag": "hr"})

        # Remove last hr if it's not needed (but we add pagination so maybe keep it)
        # Actually standard practice is elements.pop() then add footer buttons
        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

        # Pagination & Global Actions
        pagination_buttons = []
        if page > 1:
            pagination_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "⬅️ 上一页"},
                    "type": "default",
                    "value": {"action": "switch_board_page", "page": page - 1},
                }
            )

        if page < total_pages:
            pagination_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "下一页 ➡️"},
                    "type": "default",
                    "value": {"action": "switch_board_page", "page": page + 1},
                }
            )

        if pagination_buttons:
            elements.append({"tag": "hr"})
            elements.extend(build_responsive_layout(pagination_buttons))
            if total_pages > 1:
                elements.append(
                    {
                        "tag": "markdown",
                        "content": f"第 {page}/{total_pages} 页",
                        "text_align": "center",
                        "text_size": "notation",
                    }
                )

        elements.append({"tag": "hr"})
        elements.extend(
            build_responsive_layout(
                [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "➕ 新建项目"},
                        "type": "default",
                        "value": {"action": "new_project_prompt"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 刷新"},
                        "type": "default",
                        "value": {"action": "refresh_board"},
                    },
                ]
            )
        )

        card = CoreBuilder._wrap_card("📋 项目看板", "blue", elements)
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
            CoreBuilder._build_directory_element(project),
            {"tag": "hr"},
            CoreBuilder._build_content_element(content),
        ]

        if suggestions:
            suggestion_text = "💡 **建议下一步:**\n" + "\n".join(f"• {s}" for s in suggestions)
            elements.append({"tag": "markdown", "content": suggestion_text})

        if buttons:
            elements.extend(build_responsive_layout(buttons[:4]))
        else:
            elements.extend(
                build_responsive_layout(
                    CoreBuilder._build_footer_buttons(
                        project,
                        is_coco_mode=project.coco_mode,
                        is_claude_mode=project.claude_mode,
                    )
                )
            )

        card = CoreBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _build_resume_card(
        project: ProjectContext,
        mode: str,
    ) -> tuple[str, str]:
        theme = get_theme(project.theme_color)
        is_coco = mode == "coco"
        is_claude = mode == "claude"
        is_ttadk = mode == "ttadk"
        if is_coco:
            mode_name = "Coco"
            snapshot = project.coco_session_snapshot
        elif is_claude:
            mode_name = "Claude"
            snapshot = project.claude_session_snapshot
        else:
            mode_name = "TTADK"
            snapshot = project.ttadk_session_snapshot

        if not snapshot:
            return ProjectBuilder.build_project_response_card(
                project, f"{mode_name} 模式", "没有可恢复的会话", show_buttons=True
            )

        content = (
            f"🔄 检测到未完成的 {mode_name} 会话\n\n"
            f"• 会话 ID: `{snapshot.session_id}`\n"
            f"• 对话数: {snapshot.query_count} 条\n"
            f"• 最后对话: {snapshot.last_query}"
        )

        resume_action = f"resume_{mode}"
        new_action = f"new_{mode}"

        buttons = [
            apply_compact_style(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 恢复会话"},
                    "type": "primary",
                    "value": {
                        "action": resume_action,
                        "project_id": project.project_id,
                        "session_id": snapshot.session_id,
                    },
                }
            ),
            apply_compact_style(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🆕 开始新会话"},
                    "type": "default",
                    "value": {
                        "action": new_action,
                        "project_id": project.project_id,
                    },
                }
            ),
        ]

        header_title = CoreBuilder._build_header_title(project, is_coco_mode=is_coco, is_claude_mode=is_claude, is_ttadk_mode=is_ttadk)

        elements = [
            CoreBuilder._build_directory_element(project),
            {"tag": "hr"},
        ]
        if is_ttadk:
            ttadk_status = CoreBuilder._build_ttadk_status_element(project)
            if ttadk_status:
                elements.append(ttadk_status)
                elements.append({"tag": "hr"})
        elements.append(CoreBuilder._build_content_element(content))
        elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_coco_resume_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder._build_resume_card(project, "coco")

    @staticmethod
    def build_claude_resume_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder._build_resume_card(project, "claude")

    @staticmethod
    def build_ttadk_resume_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder._build_resume_card(project, "ttadk")

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
            apply_compact_style(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🤖 开始 Coco"},
                    "type": "primary",
                    "value": {"action": "enter_coco", "project_id": project.project_id},
                }
            ),
            apply_compact_style(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔮 开始 Claude"},
                    "type": "default",
                    "value": {"action": "enter_claude", "project_id": project.project_id},
                }
            ),
            apply_compact_style(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📋 项目看板"},
                    "type": "default",
                    "value": {"action": "show_board"},
                }
            ),
        ]

        elements = [
            CoreBuilder._build_content_element(content),
        ]
        elements.extend(build_responsive_layout(buttons))

        card = CoreBuilder._wrap_card("🎉 新项目已创建", theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
