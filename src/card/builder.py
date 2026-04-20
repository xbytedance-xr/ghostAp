from typing import TYPE_CHECKING, Optional

from ..project.context import ProjectContext
from .builders.core import CoreBuilder
from .builders.deep import DeepBuilder
from .builders.project import ProjectBuilder
from .builders.system import SystemBuilder
from .builders.worktree import WorktreeBuilder
from .models import EngineCardState
from .shared import apply_compact_style

if TYPE_CHECKING:
    from ..sandbox.executor import ExecutionResult


class CardBuilder:
    """
    Facade class that delegates card building to specialized builders.
    Maintains backward compatibility with the original CardBuilder API.
    """

    # --- Core Delegates ---

    @staticmethod
    def _build_button_grid(buttons: list[dict], columns: int = 2) -> list[dict]:
        from .shared import _build_button_grid

        return _build_button_grid(buttons, columns)

    @staticmethod
    def _build_button_row_action(buttons: list[dict]) -> list[dict]:
        from .shared import _build_button_row_action

        return _build_button_row_action(buttons)

    @staticmethod
    def _truncate_markdown(content: str, max_chars: int) -> str:
        return CoreBuilder._truncate_markdown(content, max_chars)

    @staticmethod
    def _build_content_element(content: str, with_title: Optional[str] = None, max_chars: int = 4000) -> dict:
        return CoreBuilder._build_content_element(content, with_title, max_chars)

    @staticmethod
    def _build_header_title(
        project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False
    ) -> str:
        return CoreBuilder._build_header_title(project, is_coco_mode, is_claude_mode)

    @staticmethod
    def _build_directory_element(project: Optional[ProjectContext], working_dir: Optional[str] = None) -> dict:
        return CoreBuilder._build_directory_element(project, working_dir)

    @staticmethod
    def _build_footer_buttons(
        project: Optional[ProjectContext], is_coco_mode: bool = False, is_claude_mode: bool = False
    ) -> list[dict]:
        return CoreBuilder._build_footer_buttons(project, is_coco_mode, is_claude_mode)

    @staticmethod
    def _build_footer_note(project: Optional[ProjectContext], working_dir: Optional[str] = None) -> Optional[dict]:
        return CoreBuilder._build_footer_note(project, working_dir)

    @staticmethod
    def _wrap_card(header_title: str, header_template: str, elements: list[dict]) -> dict:
        return CoreBuilder._wrap_card(header_title, header_template, elements)

    @staticmethod
    def _build_image_elements(image_keys: list[str]) -> list[dict]:
        return CoreBuilder._build_image_elements(image_keys)

    @staticmethod
    def _format_time_ago(timestamp: float) -> str:
        return CoreBuilder._format_time_ago(timestamp)

    @staticmethod
    def _build_banner_element(message: str, type: str = "success") -> dict:
        return CoreBuilder._build_banner_element(message, type)

    # --- Project Delegates ---

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
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        return ProjectBuilder._build_response_card_inner(
            project,
            title,
            content,
            working_dir,
            show_buttons,
            is_coco_mode,
            is_claude_mode,
            is_ttadk_mode,
            extra_buttons,
            footer,
            image_keys,
        )

    @staticmethod
    def build_coco_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        return ProjectBuilder.build_coco_response_card(project, title, content, working_dir, show_buttons)

    @staticmethod
    def build_smart_response_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        working_dir: Optional[str] = None,
        show_buttons: bool = True,
    ) -> tuple[str, str]:
        return ProjectBuilder.build_smart_response_card(project, title, content, working_dir, show_buttons)

    @staticmethod
    def build_project_response_card(
        project: ProjectContext,
        title: str,
        content: str,
        show_buttons: bool = True,
        extra_buttons: Optional[list[dict]] = None,
        footer: Optional[str] = None,
        image_keys: Optional[list[str]] = None,
        banner: Optional[dict] = None,
    ) -> tuple[str, str]:
        return ProjectBuilder.build_project_response_card(
            project, title, content, show_buttons, extra_buttons, footer, image_keys, banner=banner
        )

    @staticmethod
    def build_status_board_card(
        projects: list[ProjectContext],
        current_project_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[str, str]:
        return ProjectBuilder.build_status_board_card(projects, current_project_id, page, page_size)

    @staticmethod
    def build_notification_card(
        project: ProjectContext,
        notification_type: str,
        title: str,
        content: str,
        suggestions: Optional[list[str]] = None,
        buttons: Optional[list[dict]] = None,
    ) -> tuple[str, str]:
        return ProjectBuilder.build_notification_card(project, notification_type, title, content, suggestions, buttons)

    @staticmethod
    def _build_resume_card(project: ProjectContext, mode: str) -> tuple[str, str]:
        return ProjectBuilder._build_resume_card(project, mode)

    @staticmethod
    def build_coco_resume_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder.build_coco_resume_card(project)

    @staticmethod
    def build_claude_resume_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder.build_claude_resume_card(project)

    @staticmethod
    def build_ttadk_resume_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder.build_ttadk_resume_card(project)

    @staticmethod
    def build_project_created_card(project: ProjectContext) -> tuple[str, str]:
        return ProjectBuilder.build_project_created_card(project)

    # --- System Delegates ---

    @staticmethod
    def build_error_card(
        exc: Exception | str,
        title: str = "操作失败",
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        return SystemBuilder.build_error_card(exc, title, project)

    @staticmethod
    def build_shell_result_card(
        cmd: str,
        result: "ExecutionResult",
        working_dir: Optional[str] = None,
        project: Optional[ProjectContext] = None,
    ) -> tuple[str, str]:
        return SystemBuilder.build_shell_result_card(cmd, result, working_dir, project)

    @staticmethod
    def build_ttadk_tool_select_card(
        tools: list, project_id: Optional[str] = None, yolo_enabled: bool = False
    ) -> tuple[str, str]:
        return SystemBuilder.build_ttadk_tool_select_card(tools, project_id, yolo_enabled=yolo_enabled)

    @staticmethod
    def build_ttadk_model_select_card(
        models: list, tool_name: str, project_id: Optional[str] = None, yolo_enabled: bool = False
    ) -> tuple[str, str]:
        return SystemBuilder.build_ttadk_model_select_card(models, tool_name, project_id, yolo_enabled=yolo_enabled)

    @staticmethod
    def build_ttadk_combined_select_card(
        tools: list,
        models_by_tool: dict,
        project_id: Optional[str] = None,
        yolo_enabled: bool = False,
    ) -> tuple[str, str]:
        return SystemBuilder.build_ttadk_combined_select_card(tools, models_by_tool, project_id, yolo_enabled=yolo_enabled)

    @staticmethod
    def build_ttadk_soft_failure_card(
        message: str,
        project_id: Optional[str] = None,
        *,
        action: str = "show_ttadk_menu",
        button_text: str = "🔄 重新进入TTADK",
    ) -> tuple[str, str]:
        return SystemBuilder.build_ttadk_soft_failure_card(
            message,
            project_id,
            action=action,
            button_text=button_text,
        )

    @staticmethod
    def build_ttadk_soft_failure_card_for(
        reason: str,
        project_id: Optional[str] = None,
        *,
        action: str = "show_ttadk_menu",
        button_text: str = "继续进入TTADK",
    ) -> tuple[str, str]:
        return SystemBuilder.build_ttadk_soft_failure_card_for(
            reason,
            project_id,
            action=action,
            button_text=button_text,
        )

    @staticmethod
    def build_acp_tool_select_card(tools: list, project_id: Optional[str] = None) -> tuple[str, str]:
        return SystemBuilder.build_acp_tool_select_card(tools, project_id)

    @staticmethod
    def build_acp_model_select_card(models: list, tool_name: str, project_id: Optional[str] = None) -> tuple[str, str]:
        return SystemBuilder.build_acp_model_select_card(models, tool_name, project_id)

    @staticmethod
    def build_command_menu_card(project: Optional[ProjectContext] = None) -> tuple[str, str]:
        return SystemBuilder.build_command_menu_card(project)

    @staticmethod
    def build_help_card(
        project: Optional[ProjectContext] = None,
        category: str = "main",
        working_dir: Optional[str] = None,
        current_mode_str: str = "智能模式",
    ) -> tuple[str, str]:
        return SystemBuilder.build_help_card(project, category, working_dir, current_mode_str)

    @staticmethod
    def _build_help_card_cached(
        project_name: Optional[str],
        root_path: Optional[str],
        project_id: Optional[str],
        category: str,
        working_dir: Optional[str],
        current_mode_str: str,
    ) -> tuple[str, str]:
        return SystemBuilder._build_help_card_cached(
            project_name, root_path, project_id, category, working_dir, current_mode_str
        )

    # --- Deep Delegates ---

    @staticmethod
    def _build_deep_header_title(project: Optional[ProjectContext], engine_name: str = "Coco") -> str:
        return DeepBuilder._build_deep_header_title(project, engine_name)

    @staticmethod
    def _pick_deep_template(engine_name: str, status: str = "running") -> str:
        return DeepBuilder._pick_deep_template(engine_name, status)

    @staticmethod
    def _build_deep_buttons(state: EngineCardState) -> list[dict]:
        return DeepBuilder._build_deep_buttons(state)

    @staticmethod
    def build_engine_card(
        project: Optional[ProjectContext],
        state: Optional[EngineCardState] = None,
        *,
        title: str = "",
        content: str = "",
        engine_name: str = "Coco",
        show_buttons: bool = True,
        working_dir: Optional[str] = None,
        progress_bar: Optional[str] = None,
        project_id: Optional[str] = None,
        engine_project_id: Optional[str] = None,
        is_executing: bool = False,
        is_paused: bool = False,
        status_line: Optional[str] = None,
        duration_line: Optional[str] = None,
        criteria_section: Optional[str] = None,
        footer_note: Optional[str] = None,
        compact: bool = False,
        expanded: bool = False,
        expand_ac: bool = False,
        action_prefix: str = "deep",
        extra_buttons: Optional[list[dict]] = None,
        warning_banner: Optional[str] = None,
    ) -> tuple[str, str]:
        if state is None:
            state = EngineCardState(
                title=title,
                content=content,
                progress_bar=progress_bar,
                project_id=project_id,
                engine_project_id=engine_project_id,
                is_executing=is_executing,
                is_paused=is_paused,
                engine_name=engine_name,
                show_buttons=show_buttons,
                working_dir=working_dir,
                status_line=status_line,
                duration_line=duration_line,
                criteria_section=criteria_section,
                footer_note=footer_note,
                compact=compact,
                expanded=expanded,
                expand_ac=expand_ac,
                action_prefix=action_prefix,
                extra_buttons=extra_buttons,
                warning_banner=warning_banner,
            )
        return DeepBuilder.build_engine_card(project, state)

    build_deep_card = build_engine_card

    @staticmethod
    def build_history_list_card(
        project: Optional[ProjectContext],
        title: str,
        content: str,
        history_buttons: list[dict],
        page: int,
        has_next: bool,
        deep_project_id: Optional[str] = None,
        engine_name: str = "Coco",
    ) -> tuple[str, str]:
        return DeepBuilder.build_history_list_card(
            project, title, content, history_buttons, page, has_next, deep_project_id, engine_name
        )

    @staticmethod
    def build_worktree_result_card(
        selected_items: list,
        unit_summary_lines: list[str],
        project_id: Optional[str] = None,
        merge_entry_ready: bool = False,
        message: str = "",
    ) -> tuple[str, str]:
        import json

        elements = [
            CoreBuilder._build_content_element(
                f"**🔀 工作单元结果**\n\n{message}\n\n" + "\n".join(unit_summary_lines)
            ),
        ]
        if merge_entry_ready:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看集成项"},
                            "type": "primary",
                            "value": {
                                "action": "show_worktree_merge_entry",
                                "project_id": project_id or "",
                            },
                        }
                    ],
                }
            )
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "工作单元结果"}, "template": "turquoise"},
            "elements": elements,
        }
        return "interactive", json.dumps(card, ensure_ascii=False)

    @staticmethod
    def build_worktree_merge_entry_card(
        merge_notes: list[str],
        project_id: Optional[str] = None,
        base_branch: str = "main",
    ) -> tuple[str, str]:
        import json

        content = f"**🔀 待集成项**\n\n目标分支: `{base_branch}`\n\n" + "\n".join(merge_notes)
        elements = [CoreBuilder._build_content_element(content)]
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "待集成项"}, "template": "purple"},
            "elements": elements,
        }
        return "interactive", json.dumps(card, ensure_ascii=False)

    # --- Worktree Selection / Execution Delegates ---

    build_worktree_tool_select_card = staticmethod(WorktreeBuilder.build_worktree_tool_select_card)
    build_worktree_model_select_card = staticmethod(WorktreeBuilder.build_worktree_model_select_card)
    build_worktree_confirm_card = staticmethod(WorktreeBuilder.build_worktree_confirm_card)
    build_worktree_progress_card = staticmethod(WorktreeBuilder.build_worktree_progress_card)
    build_worktree_cleanup_card = staticmethod(WorktreeBuilder.build_worktree_cleanup_card)
