import json
import time
from typing import Optional
from ...project.context import ProjectContext
from ..shared import (
    get_theme,
    apply_compact_style,
    build_mode_buttons,
    build_responsive_layout,
    get_button_size,
)

class CoreCardBuilder:
    @staticmethod
    def _apply_compact_button_style(button: dict) -> dict:
        return apply_compact_style(button)

    @staticmethod
    def _build_button_grid(buttons: list[dict], columns: int = 2) -> list[dict]:
        from ..shared import _build_button_grid
        return _build_button_grid(buttons, columns)

    @staticmethod
    def _build_button_row_action(buttons: list[dict]) -> list[dict]:
        from ..shared import _build_button_row_action
        return _build_button_row_action(buttons)

    @staticmethod
    def _build_buttons_responsive(buttons: list[dict]) -> list[dict]:
        return build_responsive_layout(buttons)

    @staticmethod
    def _build_content_element(content: str, with_title: Optional[str] = None, max_chars: int = 4000) -> dict:
        full_content = f"**{with_title}**\n\n{content}" if with_title else content
        
        # Hard truncation to prevent API errors
        if len(full_content) > max_chars:
            # Keep the tail as it usually contains the latest logs/status
            keep_chars = max_chars - 100 # Reserve space for warning
            
            # Check context at cut point
            cut_index = len(full_content) - keep_chars
            pre_cut_content = full_content[:cut_index]
            pre_cut_markers = pre_cut_content.count("```")
            is_inside_code_block = (pre_cut_markers % 2 != 0)
            
            truncated_content = full_content[-keep_chars:]
            
            parts = [f"...(前文已截断，仅显示最后 {keep_chars} 字符)...\n"]
            
            if is_inside_code_block:
                parts.append("```\n")
                
            parts.append(truncated_content)
            
            # Check if we need to close the block at the end
            # Current markers count: (1 if we added start) + markers in truncated
            current_markers = (1 if is_inside_code_block else 0) + truncated_content.count("```")
            
            if current_markers % 2 != 0:
                parts.append("\n```")
                
            full_content = "".join(parts)
            
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
