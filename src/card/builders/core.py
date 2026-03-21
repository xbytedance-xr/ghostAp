import time
from typing import Optional

from src.project.context import ProjectContext

from ..shared import (
    build_mode_buttons,
)
from ..styles import UI_TEXT


class CoreBuilder:
    """Core card building utilities."""

    @staticmethod
    def _truncate_markdown(content: str, max_chars: int) -> str:
        """Truncate markdown content safely, closing code blocks and bold tags."""
        if len(content) <= max_chars:
            return content

        warning_msg = UI_TEXT.get("log_truncated_warning", "\n> ⚠️ **日志内容过长，已被截断**...\n")
        keep_chars = max_chars - len(warning_msg) - 20  # buffer

        truncated_content = content[-keep_chars:]

        # 1. Check code blocks (```)
        cut_index = len(content) - keep_chars
        pre_cut_content = content[:cut_index]
        code_block_markers = pre_cut_content.count("```")
        is_inside_code_block = code_block_markers % 2 != 0

        # 2. Check bold tags (**)
        bold_markers = pre_cut_content.count("**")
        is_inside_bold = bold_markers % 2 != 0

        parts = [warning_msg]

        if is_inside_code_block:
            parts.append("```\n")

        if is_inside_bold:
            parts.append("**")

        parts.append(truncated_content)

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
            full_content = CoreBuilder._truncate_markdown(full_content, max_chars)

        return {"tag": "markdown", "content": full_content}

    @staticmethod
    def _build_header_title(
        project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False, is_ttadk_mode: bool = False
    ) -> str:
        if not project:
            if is_claude_mode:
                return UI_TEXT.get("claude_mode_title")
            elif is_ttadk_mode:
                return UI_TEXT.get("system_mode_ttadk", "🎮 TTADK 多工具模式")
            mode_icon = "🤖" if is_coco_mode else "🧠"
            mode_name = UI_TEXT.get("coco_mode_title") if is_coco_mode else UI_TEXT.get("smart_mode_title")
            return f"{mode_icon} {mode_name}"

        if is_claude_mode or project.claude_mode:
            return f"🔮 {project.project_name} · Claude"
        elif is_ttadk_mode or project.ttadk_mode:
            return f"🎮 {project.project_name} · TTADK"
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

        return {"tag": "markdown", "content": f"📁 `{path}`"}

    @staticmethod
    def _build_ttadk_status_element(project: Optional[ProjectContext]) -> Optional[dict]:
        if not project:
            return None
        tool = str(getattr(project, "ttadk_tool_name", "") or "").strip() or "未设置"
        model = str(getattr(project, "ttadk_model_name", "") or "").strip() or "自动"
        return {"tag": "markdown", "content": f"🎮 **TTADK 状态** · 工具: `{tool}` · 模型: `{model}`", "text_size": "notation"}

    @staticmethod
    def _build_footer_buttons(
        project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False, is_ttadk_mode: bool = False
    ) -> list[dict]:
        project_id_raw = getattr(project, "project_id", None) if project else None
        project_id = str(project_id_raw) if isinstance(project_id_raw, (str, int)) else None
        return build_mode_buttons(is_coco_mode, project_id, is_claude_mode, is_ttadk_mode)

    @staticmethod
    def _build_footer_note(project: Optional[ProjectContext], working_dir: Optional[str] = None) -> Optional[dict]:
        if project:
            content = UI_TEXT.get("project_dir_label", "📂 项目目录: `{path}`").format(path=project.root_path)
            return {
                "tag": "markdown",
                "content": content,
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
            # Backward compatibility: some callers/tests still read top-level `elements`.
            "elements": elements,
            "body": {
                "elements": elements,
            },
        }

    @staticmethod
    def _build_image_elements(image_keys: list[str]) -> list[dict]:
        elements = []
        for i, key in enumerate(image_keys):
            alt_text = UI_TEXT.get("image_alt_text", "图片 {index}").format(index=i + 1)
            elements.append({"tag": "img", "img_key": key, "alt": {"tag": "plain_text", "content": alt_text}})
        return elements

    @staticmethod
    def _format_time_ago(timestamp: float) -> str:
        diff = time.time() - timestamp
        if diff < 60:
            return UI_TEXT.get("time_just_now", "刚刚")
        elif diff < 3600:
            minutes = int(diff / 60)
            return UI_TEXT.get("time_mins_ago", "{minutes} 分钟前").format(minutes=minutes)
        elif diff < 86400:
            hours = int(diff / 3600)
            return UI_TEXT.get("time_hours_ago", "{hours} 小时前").format(hours=hours)
        else:
            days = int(diff / 86400)
            return UI_TEXT.get("time_days_ago", "{days} 天前").format(days=days)
