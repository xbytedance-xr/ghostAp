import json
import time
from functools import lru_cache
from typing import Optional
from ..project.context import ProjectContext, ProjectStatus
from .models import DeepCardState
from .shared import (
    get_theme,
    apply_compact_style,
    build_mode_buttons,
    build_responsive_layout,
    get_button_size,
)


class CardBuilder:

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
    def _truncate_markdown(content: str, max_chars: int) -> str:
        """Truncate markdown content safely, closing code blocks and bold tags."""
        if len(content) <= max_chars:
            return content

        # Reserve space for the warning message (approx 100 chars)
        # We'll use a slightly different warning message strategy:
        # keep the END of the log, and prepend a warning.
        warning_msg = "\n> ⚠️ **日志内容过长，已被截断**\n> 🔍 完整日志请查看服务器本地文件\n> (仅显示末尾内容)...\n"
        keep_chars = max_chars - len(warning_msg) - 20 # buffer

        truncated_content = content[-keep_chars:]
        
        # 1. Check code blocks (```)
        # If the truncated content has an odd number of ```, it means we started inside a code block
        # (assuming the original content was valid). 
        # Actually, since we are taking the TAIL, we need to know if the tail *starts* inside a block.
        # But we don't have the full context easily if we just look at tail.
        # 
        # Better approach: Look at the full content to determine state at cut point.
        # Cut point is: len(content) - keep_chars
        cut_index = len(content) - keep_chars
        pre_cut_content = content[:cut_index]
        
        # Count ``` in pre-cut content to see if we are inside a code block
        # We assume ``` always toggle.
        code_block_markers = pre_cut_content.count("```")
        is_inside_code_block = (code_block_markers % 2 != 0)
        
        # 2. Check bold tags (**)
        # Similar logic for **
        bold_markers = pre_cut_content.count("**")
        is_inside_bold = (bold_markers % 2 != 0)
        
        parts = [warning_msg]
        
        # If we are inside a code block at the start of our tail, we need to prefix with ```
        # to "re-open" the block so the tail renders as code.
        # However, we usually want the previous block to be CLOSED before our warning.
        # But here we are discarding the head.
        # So the state is:
        # [Head (discarded)] <--- cut ---> [Tail]
        # If Head ended with open ```, then Tail starts "inside" code.
        # To make Tail render correctly as code, we should prepend ``` to it.
        # AND to make the warning render correctly (not as code), we should ensure warning is outside.
        # 
        # Actually, the warning is prepended to the tail.
        # So the structure is: [Warning] + [Tail]
        # If Tail expects to be inside code, we must start [Tail] with ```.
        
        # Correct logic:
        # 1. We insert warning. Warning is markdown, not code.
        # 2. If we were inside code block at cut point, we must open a code block after warning
        #    so the rest of the content (Tail) is treated as code.
        if is_inside_code_block:
            parts.append("```\n")
        
        # If we were inside bold, we must open bold
        if is_inside_bold:
            parts.append("**")
            
        parts.append(truncated_content)
        
        # Now check if we need to close tags at the very end of Tail
        # We count markers in the (potentially modified) tail?
        # No, simpler: check the total markers in (Pre-cut + Tail).
        # Since original content was assumed valid (closed), 
        # if Pre-cut has odd markers, then Tail MUST have odd markers to close it.
        # 
        # Wait, if we added ``` at start of Tail, we added 1 marker.
        # Original Tail (content[-keep_chars:]) has N markers.
        # Total in our new string: (1 if added else 0) + N
        # We want the final result to be closed (even number).
        # 
        # Example: 
        # Original: ```abc...xyz``` (2 markers)
        # Cut: inside. Pre-cut has 1 (odd). Tail has 1 (odd).
        # We add ``` prefix to Tail.
        # New Tail: ``` + xyz```. Markers: 1 + 1 = 2 (Even). Closed.
        # 
        # Example 2: 
        # Original: ```abc... (unclosed error in original?) -> Assume original is valid.
        # 
        # So if we prepend ```, the tail is self-contained.
        # What if original tail has unclosed blocks?
        # e.g. Original: ... ```code ... (cut) ... code``` ...
        # If cut is outside, Pre-cut even. Tail even.
        # We add nothing. Tail is ```code``` (2 markers). Even. Closed.
        #
        # What if cut is inside?
        # Original: ```start ... (cut) ... end```
        # Pre-cut: 1. Tail: 1.
        # We add ```. Tail: ```...end```. Markers: 2. Closed.
        #
        # What if inside bold?
        # Original: **bold**
        # Cut inside. Pre: 1. Tail: 1.
        # Add **. Tail: **bold**. Markers: 2. Closed.
        #
        # So it seems we just need to re-open whatever was open at cut point.
        # AND we need to ensure the final string is closed.
        # If the original string was valid, then re-opening matches the "missing" opening from head,
        # so the existing closing tags in tail should balance it.
        # 
        # BUT, what if we cut *inside* a `**` marker? e.g. `*` | `*`
        # `count("**")` might be tricky.
        # For robustness, we can just check the *final* string state.
        
        result = "".join(parts)
        
        # Final safety check: ensure closed
        if result.count("```") % 2 != 0:
            result += "\n```"
        if result.count("**") % 2 != 0:
            result += "**"
            
        return result

    @staticmethod
    def _build_content_element(content: str, with_title: Optional[str] = None, max_chars: int = 4000) -> dict:
        full_content = f"**{with_title}**\n\n{content}" if with_title else content
        
        # Smart truncation to prevent API errors and render issues
        if len(full_content) > max_chars:
            full_content = CardBuilder._truncate_markdown(full_content, max_chars)
            
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

        content = (
            f"🔄 检测到未完成的 {mode_name} 会话\n\n"
            f"• 会话 ID: `{snapshot.session_id}`\n"
            f"• 对话数: {snapshot.query_count} 条\n"
            f"• 最后对话: {snapshot.last_query}"
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
        exc: Exception | str,
        title: str = "操作失败",
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        from ..utils.errors import GhostAPError
        from .shared import build_quick_actions

        message = str(exc)
        quick_actions = []
        context = {}

        if isinstance(exc, GhostAPError):
            quick_actions = exc.quick_actions
            context = exc.context

        elements = [
            CardBuilder._build_content_element(f"❌ **{title}**\n\n{message}")
        ]

        if project:
            elements.insert(0, CardBuilder._build_directory_element(project))
            elements.insert(1, {"tag": "hr"})

        if quick_actions:
            buttons = build_quick_actions(quick_actions, context)
            elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card("⚠️ 错误提示", "red", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    # ---- Shell result card ----

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
            CardBuilder._build_directory_element(project, working_dir),
            {"tag": "hr"},
            {"tag": "markdown", "content": f"> 🖥️ `{cmd}`"},
        ]

        if result.error_message:
            elements.append({
                "tag": "markdown",
                "content": f"🚫 **{result.error_message}**",
            })
        elif result.stdout or result.stderr:
            if result.stdout:
                elements.append({
                    "tag": "markdown",
                    "content": f"```BASH\n{result.stdout}\n```",
                })
            if result.stderr:
                elements.append({
                    "tag": "markdown",
                    "content": f"⚠️ **错误输出**:\n```BASH\n{result.stderr}\n```",
                })
        else:
            elements.append({
                "tag": "markdown",
                "content": "✅ 命令执行成功（无输出）",
            })

        elements.append({
            "tag": "markdown",
            "content": f"返回码: `{result.return_code}`",
            "text_size": "notation",
        })

        card = CardBuilder._wrap_card(header_title, header_template, elements)
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
    def _pick_deep_template(engine_name: str, status: str = "running") -> str:
        """Pick header template color based on engine and status."""
        status = status.lower()
        if status == "error":
            return "red"
        if status == "completed":
            return "green"
        if status == "paused":
            return "orange"
        if status == "planning":
            return "blue"
        
        # Default/Executing
        name = (engine_name or "").strip().lower()
        if name.startswith("loop"):
            return "purple"  # Use purple for Loop to distinguish from Deep (turquoise)
        if name.startswith("claude"):
            return "violet"  # Use violet for Claude to distinguish from Loop
        if name.startswith("spec"):
            return "green"
        return "turquoise"

    @staticmethod
    def _build_deep_buttons(state: DeepCardState) -> list[dict]:
        buttons = []
        # Status Control Buttons
        if state.is_executing:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⏸️ 暂停"},
                "type": "default",
                "value": {"action": f"{state.action_prefix}_pause", "project_id": state.deep_project_id, "deep_project_id": state.deep_project_id}
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "value": {"action": f"{state.action_prefix}_stop", "project_id": state.deep_project_id, "deep_project_id": state.deep_project_id}
            })
        elif state.is_paused:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "▶️ 继续"},
                "type": "primary",
                "value": {"action": f"{state.action_prefix}_resume", "project_id": state.deep_project_id, "deep_project_id": state.deep_project_id}
            })
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "value": {"action": f"{state.action_prefix}_stop", "project_id": state.deep_project_id, "deep_project_id": state.deep_project_id}
            })
            
        # Log Expand/Collapse Button
        lines = (state.content or "").split('\n')
        threshold = 5 if state.compact else 10
        
        if len(lines) > threshold:
            expand_text = "🔼 收起日志" if state.expanded else "🔽 展开日志"
            expand_action = f"{state.action_prefix}_collapse" if state.expanded else f"{state.action_prefix}_expand"
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": expand_text},
                "type": "default",
                "value": {
                    "action": expand_action, 
                    "project_id": state.deep_project_id, 
                    "deep_project_id": state.deep_project_id
                }
            })

        # Mode Switch Button
        mode_text = "当前: 精简" if state.compact else "当前: 完整"
        mode_action = f"{state.action_prefix}_mode_full" if state.compact else f"{state.action_prefix}_mode_compact"
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"👁️ {mode_text}"},
            "type": "default",
            "value": {
                "action": mode_action,
                "project_id": state.deep_project_id,
                "deep_project_id": state.deep_project_id
            }
        })
        
        # History Button (Only for Loop Engine in Status View)
        if state.action_prefix == "loop":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📜 历史"},
                "type": "default",
                "value": {
                    "action": "loop_history",
                    "project_id": state.deep_project_id,
                    "deep_project_id": state.deep_project_id
                }
            })
            
        return [apply_compact_style(b) for b in buttons]

    @staticmethod
    def build_deep_card(
        project: Optional[ProjectContext],
        state: DeepCardState,
    ) -> tuple[str, str]:
        # Determine status for color mapping
        status_key = "running"
        title_lower = state.title.lower()
        if "error" in title_lower or "失败" in state.title:
            status_key = "error"
        elif "完成" in state.title or "结束" in state.title or "completed" in title_lower or "finished" in title_lower or "success" in title_lower:
            status_key = "completed"
        elif state.is_paused:
            status_key = "paused"
        elif "规划" in state.title or "分析" in state.title or "planning" in title_lower or "analyzing" in title_lower:
            status_key = "planning"
            
        header_template = CardBuilder._pick_deep_template(state.engine_name, status_key)
        theme = get_theme(header_template)
        
        # Optimize Title with Icons based on status if not already present
        # (This is a simple heuristic, assuming title passed in might already have icons)
        if not state.title:
            header_title = CardBuilder._build_deep_header_title(project, state.engine_name)
        else:
            header_title = state.title

        elements = [
            CardBuilder._build_directory_element(project, state.working_dir),
            {"tag": "hr"},
        ]

        # Progress bar
        if state.progress_bar and (not state.content or state.progress_bar not in state.content):
            elements.append({"tag": "markdown", "content": f"📊 {state.progress_bar}"})

        # Status + duration line (compact, notation-size)
        meta_parts = [p for p in (state.status_line, state.duration_line) if p]
        if meta_parts:
            # Loop engine: separate lines for better readability on mobile
            is_loop = "loop" in state.engine_name.lower()
            separator = "\n" if is_loop else " · "
            
            elements.append({
                "tag": "markdown",
                "content": separator.join(meta_parts),
                "text_size": "notation",
            })

        # Separator before main content (only if we have meta above)
        if meta_parts:
            elements.append({"tag": "hr"})

        # Main content processing
        display_content = state.content
        
        if state.expanded:
            # If expanded, show full content regardless of mode
            pass
        elif state.compact:
            # Error check - show more context for errors
            is_error = status_key == "error"
            
            if is_error:
                if not display_content:
                     display_content = "发生错误 (无详细信息)"
                else:
                    lines = display_content.split('\n')
                    # Show first 5 lines for errors instead of hard char limit
                    if len(lines) > 5:
                        display_content = "\n".join(lines[:5]) + "\n...(更多错误详情请展开)..."
            else:
                # Compact mode: show last 5 lines for running/paused to avoid scroll trap
                if not display_content:
                    display_content = "正在执行..."
                else:
                    lines = display_content.split('\n')
                    if len(lines) > 5:
                        display_content = "...\n" + "\n".join(lines[-5:]) + "\n(点击展开查看更多)"
                    elif len(display_content) > 500:
                        # Fallback for very long lines
                        display_content = "..." + display_content[-500:]
        else:
            # Full mode: Line-based truncation if not expanded
            if display_content:
                lines = display_content.split('\n')
                MAX_LINES = 10
                if len(lines) > MAX_LINES:
                    display_content = "...(已折叠 {} 行)...\n".format(len(lines) - MAX_LINES) + "\n".join(lines[-MAX_LINES:])

        elements.append(CardBuilder._build_content_element(display_content))

        # Criteria section (independent element) - Skip in compact mode unless very short
        if state.criteria_section and not state.compact:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": state.criteria_section})

        # Footer note
        if state.footer_note:
            elements.append({
                "tag": "markdown",
                "content": state.footer_note,
                "text_size": "notation",
            })

        if state.show_buttons:
            buttons = []
            if state.is_executing or state.is_paused:
                buttons = CardBuilder._build_deep_buttons(state)
            else:
                # Finished or not started, still show mode switch
                base_buttons = CardBuilder._build_footer_buttons(project, is_coco_mode=False, is_claude_mode=False)
                # Add mode switch button
                mode_text = "当前: 精简" if state.compact else "当前: 完整"
                mode_action = f"{state.action_prefix}_mode_full" if state.compact else f"{state.action_prefix}_mode_compact"
                mode_btn = apply_compact_style({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"👁️ {mode_text}"},
                    "type": "default",
                    "value": {
                        "action": mode_action,
                        "project_id": project.project_id if project else None,
                        "deep_project_id": state.deep_project_id
                    }
                })
                # Also add expand/collapse if there is enough content
                lines = state.content.split('\n')
                threshold = 5 if state.compact else 10
                if len(lines) > threshold:
                    expand_text = "🔼 收起日志" if state.expanded else "🔽 展开日志"
                    expand_action = f"{state.action_prefix}_collapse" if state.expanded else f"{state.action_prefix}_expand"
                    expand_btn = apply_compact_style({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": expand_text},
                        "type": "default",
                        "value": {
                            "action": expand_action, 
                            "project_id": project.project_id if project else None, 
                            "deep_project_id": state.deep_project_id
                        }
                    })
                    buttons.append(expand_btn)
                
                buttons.append(mode_btn)
                buttons.extend(base_buttons)

            if buttons:
                elements.append({"tag": "hr"})
                elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card(header_title, theme.header_template, elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_history_list_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        history_buttons: list[dict],
        page: int,
        has_next: bool,
        deep_project_id: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build a history list card with pagination."""
        theme = get_theme("blue")
        header_title = f"📜 {project.project_name if project else 'Loop'} · 历史记录"
        
        elements = [
            {"tag": "markdown", "content": f"**{title}**\n\n{content}"},
            {"tag": "hr"},
        ]
        
        # History Items (as buttons grid)
        elements.extend(build_responsive_layout(history_buttons))
        
        # Pagination Controls
        nav_buttons = []
        if page > 1:
            nav_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⬅️ 上一页"},
                "type": "default",
                "value": {
                    "action": "loop_history_page", 
                    "page": page - 1,
                    "project_id": project.project_id if project else None,
                    "deep_project_id": deep_project_id
                }
            })
            
        if has_next:
            nav_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "➡️ 下一页"},
                "type": "default",
                "value": {
                    "action": "loop_history_page",
                    "page": page + 1,
                    "project_id": project.project_id if project else None,
                    "deep_project_id": deep_project_id
                }
            })
            
        # Back to Status
        nav_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 返回状态"},
            "type": "primary",
            "value": {
                "action": "loop_back_to_list", # Reusing generic back action name or specific
                "project_id": project.project_id if project else None,
                "deep_project_id": deep_project_id
            }
        })
        
        if nav_buttons:
            elements.append({"tag": "hr"})
            elements.extend(build_responsive_layout([apply_compact_style(b) for b in nav_buttons]))

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

    @staticmethod
    def build_ttadk_tool_select_card(tools: list, project_id: Optional[str] = None) -> tuple[str, str]:
        elements = [
            {
                "tag": "markdown",
                "content": "请选择要使用的 TTADK 工具："
            }
        ]

        buttons = []
        for tool in tools:
            btn_text = f"{tool.name}"
            if tool.description:
                btn_text += f" ({tool.description})"
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn_text},
                "type": "primary" if tool.is_default else "default",
                "value": {
                    "action": "select_ttadk_tool",
                    "tool_name": tool.name,
                    "project_id": project_id
                }
            })

        elements.extend(build_responsive_layout(buttons))

        card = CardBuilder._wrap_card("🔧 TTADK 工具选择", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_ttadk_model_select_card(
        models: list,
        tool_name: str,
        project_id: Optional[str] = None
    ) -> tuple[str, str]:
        elements = [
            {
                "tag": "markdown",
                "content": (
                    f"请为 **{tool_name}** 选择要使用的模型：\n"
                    "（若列表为空/不全，可点击下方『🔄 刷新模型列表』强制拉取）"
                )
            }
        ]

        buttons = []
        for model in models:
            btn_text = f"{model.name}"
            if model.description:
                btn_text += f" ({model.description})"
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn_text},
                "type": "primary" if model.is_default else "default",
                "value": {
                    "action": "select_ttadk_model",
                    "tool_name": tool_name,
                    "model_name": model.name,
                    "project_id": project_id
                }
            })

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

        card = CardBuilder._wrap_card(f"🤖 {tool_name} 模型选择", "blue", elements)
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
            card_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": btn["type"],
                "value": {
                    "action": btn["action"],
                    "project_id": project_id
                }
            })

        elements = [
            CardBuilder._build_directory_element(project),
            {"tag": "hr"},
            {"tag": "markdown", "content": "**📱 常用指令菜单**"},
        ]
        elements.extend(build_responsive_layout(card_buttons))

        card = CardBuilder._wrap_card("📱 快捷菜单", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_help_card(
        project: Optional[ProjectContext] = None,
        category: str = "main",
        working_dir: Optional[str] = None,
        current_mode_str: str = "智能模式"
    ) -> tuple[str, str]:
        """Build a categorized help card."""
        
        # Extract primitives for caching
        project_name = project.project_name if project else None
        root_path = project.root_path if project else None
        project_id = project.project_id if project else None
        
        return CardBuilder._build_help_card_cached(
            project_name=project_name,
            root_path=root_path,
            project_id=project_id,
            category=category,
            working_dir=working_dir,
            current_mode_str=current_mode_str
        )

    @staticmethod
    @lru_cache(maxsize=64)
    def _build_help_card_cached(
        project_name: Optional[str],
        root_path: Optional[str],
        project_id: Optional[str],
        category: str,
        working_dir: Optional[str],
        current_mode_str: str
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
            category_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": cat["name"]},
                "type": "primary" if is_active else "default",
                "value": {
                    "action": "help_category",
                    "category": cat["id"],
                    "project_id": project_id
                }
            })

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
            {"tag": "markdown", "text_size": "notation",
             "content": f"**当前状态**  •  {current_mode_str}  •  `{working_dir or '~'}`  •  项目: {project_info}"},
            {"tag": "hr"},
        ]
        
        elements.extend(build_responsive_layout(category_buttons))
        
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "text_size": "normal", "content": content})

        card = CardBuilder._wrap_card("📖 GhostAP 使用帮助", "blue", elements)
        return "interactive", json.dumps(card, ensure_ascii=False)
