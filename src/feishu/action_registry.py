import logging
from typing import Any, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .ws_client import FeishuWSClient

from ..card import CardBuilder

logger = logging.getLogger(__name__)

def init_action_registry(client: 'FeishuWSClient'):
    """Initialize all card action handlers and register them to the client's action dispatcher."""
    register_programming_mode_actions(client)

    # Project
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_status(
            mid, cid, client._project_manager.get_project(pid) if pid else None
        ),
        exact="show_status",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_board(mid, cid, origin_message_id=mid), exact="switch_project"
    )
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_board(mid, cid, origin_message_id=mid), exact="show_board"
    )
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_board(mid, cid, origin_message_id=mid), exact="refresh_board"
    )
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_board(
            mid, cid, origin_message_id=mid, page=val.get("page", 1)
        ),
        exact="switch_board_page",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_status(
            mid, cid, client._project_manager.get_project(pid) if pid else None, origin_message_id=mid
        ),
        exact="show_detail",
    )

    def _handle_switch_to(mid, cid, pid, val):
        if pid:
            project = client._project_manager.get_project(pid)
            if project:
                client._switch_project(mid, cid, project.project_name)

    client._register_action(_handle_switch_to, exact="switch_to")

    def _handle_continue_dev(mid, cid, pid, val):
        project = client._project_manager.get_project(pid) if pid else None
        if project:
            client._project_manager.set_active_project(cid, pid)
            content = f"继续在 **{project.project_name}** 项目中开发\n\n📂 项目目录: `{project.root_path}`\n\n直接发送命令或消息即可"
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, "继续开发", content, show_buttons=True
            )
            response_id = client._reply_message_with_id(mid, card_content, msg_type)
            if response_id:
                client._register_message_project(response_id, project)

    client._register_action(_handle_continue_dev, exact="continue_dev")

    def _handle_list_files(mid, cid, pid, val):
        project = client._project_manager.get_project(pid) if pid else None
        if project:
            client._project_manager.set_active_project(cid, pid)
            client._submit_shell_command(mid, cid, "ls -la", project.root_path, project)

    client._register_action(_handle_list_files, exact="list_files")

    client._register_action(
        lambda mid, cid, pid, val: client._reply_message(
            mid, "📝 创建新项目\n\n请发送: `/new 项目名 路径`\n\n例如: `/new myApp ~/workspace/myApp`"
        ),
        exact="new_project_prompt",
    )

    client._register_action(
        lambda mid, cid, pid, val: client._handle_select_ttadk_tool(
            mid, cid, val.get("_option") or val.get("tool_name", ""), pid
        ),
        exact="select_ttadk_tool",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_toggle_ttadk_yolo(
            mid,
            cid,
            bool(val.get("enabled")),
            val.get("view", "tool_select"),
            val.get("tool_name", ""),
            pid,
        ),
        exact="toggle_ttadk_yolo",
    )

def register_programming_mode_actions(client: 'FeishuWSClient'):
    """Register enter/exit/resume/new actions for all programming modes."""
    mode_names = ("coco", "claude", "aiden", "codex", "gemini", "ttadk")
    for mode in mode_names:
        enter = getattr(client, f"_handle_card_enter_{mode}")
        exit_ = getattr(client, f"_handle_card_exit_{mode}")
        resume = getattr(client, f"_handle_card_resume_{mode}")
        new = getattr(client, f"_handle_card_new_{mode}")

        client._register_action(enter, exact=f"enter_{mode}")
        client._register_action(exit_, exact=f"exit_{mode}")
        client._register_action(
            lambda mid, cid, pid, val, _resume=resume: _resume(mid, cid, pid, val.get("session_id", "")),
            exact=f"resume_{mode}",
        )
        client._register_action(new, exact=f"new_{mode}")
    client._register_action(
        lambda mid, cid, pid, val: client._handle_select_ttadk_model(
            mid,
            cid,
            val.get("tool_name", ""),
            val.get("_option") or val.get("model_name", ""),
            client._project_manager.get_project(pid) if pid else None,
        ),
        exact="select_ttadk_model",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_refresh_ttadk_models(mid, cid, val.get("tool_name", ""), pid),
        exact="refresh_ttadk_models",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_select_ttadk_combined(
            mid,
            cid,
            val.get("tool_name", ""),
            val.get("_option") or val.get("model_name", ""),
            client._project_manager.get_project(pid) if pid else None,
        ),
        exact="select_ttadk_combined",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_ttadk_command(
            mid, cid, client._project_manager.get_project(pid) if pid else None, True
        ),
        exact="show_ttadk_menu",
    )

    # Worktree
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_command(
            mid, cid, client._project_manager.get_project(pid) if pid else None, True
        ),
        exact="show_worktree_menu",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_finish_worktree_selection(mid, cid, pid, val),
        exact="worktree_finish_selection",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_select_tool(mid, cid, pid, val),
        exact="worktree_select_tool",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_select_model(mid, cid, pid, val),
        exact="worktree_select_model",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_confirm_start(mid, cid, pid, val),
        exact="worktree_confirm_start",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_merge(mid, cid, pid, val),
        exact="worktree_merge",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_cleanup(mid, cid, pid, val),
        exact="worktree_cleanup",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_execute_action(mid, cid, pid, val),
        exact="worktree_execute_action",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_retry_failed(mid, cid, pid, val),
        exact="worktree_retry_failed",
    )

    # ACP
    client._register_action(
        lambda mid, cid, pid, val: client._handle_acp_command(
            mid, cid, client._project_manager.get_project(pid) if pid else None
        ),
        exact="show_acp_menu",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_select_acp_tool(mid, cid, val.get("tool_name", ""), pid),
        exact="select_acp_tool",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_select_acp_model(
            mid,
            cid,
            val.get("tool_name", ""),
            val.get("model_name", ""),
            client._project_manager.get_project(pid) if pid else None,
        ),
        exact="select_acp_model",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_refresh_acp_models(mid, cid, val.get("tool_name", ""), pid),
        exact="refresh_acp_models",
    )

    # System
    client._register_action(
        lambda mid, cid, pid, val: client._show_full_help(
            mid, cid, client._project_manager.get_project(pid) if pid else None
        ),
        exact="show_help_menu",
    )
    client._register_action(lambda mid, cid, pid, val: client._handle_deep_prompt(mid, cid), exact="enter_deep_prompt")
    client._register_action(
        lambda mid, cid, pid, val: client._handle_help_category(
            mid,
            cid,
            val.get("category", "main"),
            client._project_manager.get_project(pid) if pid else None,
            origin_message_id=mid,
        ),
        exact="help_category",
    )

    # Streaming
    def _handle_load_more(mid, cid, pid, val):
        msg_id = (val.get("message_id") or "").strip() or mid
        manager = client._get_streaming_manager()
        manager.increase_pagination(msg_id)

    client._register_action(_handle_load_more, exact="load_more")

    def _handle_load_prev(mid, cid, pid, val):
        msg_id = (val.get("message_id") or "").strip() or mid
        manager = client._get_streaming_manager()
        manager.decrease_pagination(msg_id)

    client._register_action(_handle_load_prev, exact="load_prev")

    # Deep Engine
    client._register_action(
        lambda mid, cid, pid, val: client._show_deep_status(
            mid, cid, client._project_manager.get_project(pid) if pid else None, origin_message_id=mid
        ),
        exact="show_deep_status",
    )
    client._register_action(
        lambda mid, cid, pid, val, type=None: client._deep_handler.handle_card_action(mid, cid, type, val),
        prefix="deep_",
    )

    # Loop Engine
    client._register_action(
        lambda mid, cid, pid, val, type=None: client._loop_handler.handle_card_action(mid, cid, type, val),
        prefix="loop_",
    )

    # Spec Engine
    client._register_action(
        lambda mid, cid, pid, val, type=None: client._spec_handler.handle_card_action(mid, cid, type, val),
        prefix="spec_",
    )
