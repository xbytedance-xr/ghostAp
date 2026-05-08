import logging
from typing import Any, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .ws_client import FeishuWSClient

from ..card import CardBuilder
from ..card.ui_text import UI_TEXT
from .slash_command_parser import SlashCommandParser

logger = logging.getLogger(__name__)


class _RetryDispatchAdapter:
    """Adapter that bridges ``FeishuWSClient`` to ``RetryDispatchProtocol``.

    Each method delegates to the corresponding (often private) attribute on
    the concrete client, so ``RetryCommandHandler`` never touches them
    directly.  This is the **only** place that accesses ``ws_client``
    internals for retry dispatch.
    """

    def __init__(self, client: "FeishuWSClient") -> None:
        self._client = client

    def reply_text(self, message_id: str, text: str) -> None:
        self._client._reply_text(message_id, text)

    def try_block_with_chat_lock(
        self, chat_id: str, sender_id: str, message_id: str, *, raw_text: str = "",
    ) -> bool:
        # Best-effort: keep retry dispatch compatible with the main gate API.
        # Parse once and pass the structured CommandMatch to the gate.
        try:
            m = SlashCommandParser.parse(raw_text)
        except Exception:
            m = None
        return self._client._chat_lock_gate.check(
            chat_id, sender_id, message_id, command_match=m,
        )

    def get_project_for_chat(self, project_id: str, chat_id: str) -> Any:
        return self._client._project_manager.get_project_for_chat(project_id, chat_id)

    def get_active_project(self, chat_id: str) -> Any:
        return self._client._project_manager.get_active_project(chat_id)

    def get_repo_lock_manager(self) -> Any:
        ctx = self._client._handler_ctx
        if ctx is None:
            return None
        return getattr(ctx, "repo_lock_manager", None)

    def process_with_intent(
        self, message_id: str, chat_id: str, text: str, project: Any,
    ) -> None:
        self._client._process_with_intent(message_id, chat_id, text, project)

    def send_lock_conflict_card(
        self, e: Any, message_id: str, command_text: str, *, retry_count: int = 0,
    ) -> None:
        self._client.send_lock_conflict_card(e, message_id, command_text, retry_count=retry_count)


def _resolve_project(client: "FeishuWSClient", pid: str | None, cid: str):
    """Resolve project from pid+cid, returning None when pid is absent."""
    return client._project_manager.get_project_for_chat(pid, cid) if pid else None


def init_action_registry(client: 'FeishuWSClient') -> None:
    """Initialize all card action handlers and register them to the client's action dispatcher."""
    register_programming_mode_actions(client)

    # Project
    client._register_action(
        lambda mid, cid, pid, val: client._show_project_status(
            mid, cid, _resolve_project(client, pid, cid)
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
            mid, cid, _resolve_project(client, pid, cid), origin_message_id=mid
        ),
        exact="show_detail",
    )

    def _handle_switch_to(mid, cid, pid, val):
        project = _resolve_project(client, pid, cid)
        if project:
            client._switch_project(mid, cid, project.project_name)
        else:
            client._reply_text(mid, UI_TEXT["lock_project_not_found_hint"])

    client._register_action(_handle_switch_to, exact="switch_to")

    def _handle_continue_dev(mid, cid, pid, val):
        project = _resolve_project(client, pid, cid)
        if project:
            client._project_manager.set_active_project(cid, pid)
            content = f"继续在 **{project.project_name}** 项目中开发\n\n📂 项目目录: `{project.root_path}`\n\n直接发送命令或消息即可"
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, "继续开发", content, show_buttons=True
            )
            response_id = client._reply_card(mid, card_content)
            if response_id:
                client._register_message_project(response_id, project)
        else:
            client._reply_text(mid, UI_TEXT["lock_project_not_found_hint"])

    client._register_action(_handle_continue_dev, exact="continue_dev")

    def _handle_list_files(mid, cid, pid, val):
        project = _resolve_project(client, pid, cid)
        if project:
            client._project_manager.set_active_project(cid, pid)
            client._submit_shell_command(mid, cid, "ls -la", project.root_path, project)
        else:
            client._reply_text(mid, UI_TEXT["lock_project_not_found_hint"])

    client._register_action(_handle_list_files, exact="list_files")

    client._register_action(
        lambda mid, cid, pid, val: client._reply_text(
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


def register_programming_mode_actions(client: 'FeishuWSClient') -> None:
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
            _resolve_project(client, pid, cid),
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
            _resolve_project(client, pid, cid),
        ),
        exact="select_ttadk_combined",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_select_ttadk_combined_tool(
            mid, cid, val.get("_option", ""), _resolve_project(client, pid, cid),
        ),
        exact="select_ttadk_combined_tool",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_ttadk_command(
            mid, cid, _resolve_project(client, pid, cid), True
        ),
        exact="show_ttadk_menu",
    )

    # Worktree
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_command(
            mid, cid, _resolve_project(client, pid, cid), True
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
        lambda mid, cid, pid, val: client._handle_worktree_remove_item(mid, cid, pid, val),
        exact="worktree_remove_item",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_clear_items(mid, cid, pid, val),
        exact="worktree_clear_items",
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
        lambda mid, cid, pid, val: client._handle_show_worktree_merge_entry(mid, cid, pid, val),
        exact="show_worktree_merge_entry",
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
    client._register_action(
        lambda mid, cid, pid, val: client._handle_worktree_retry_all(mid, cid, pid, val),
        exact="worktree_retry_all",
    )

    # ACP
    client._register_action(
        lambda mid, cid, pid, val: client._handle_acp_command(
            mid, cid, _resolve_project(client, pid, cid)
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
            _resolve_project(client, pid, cid),
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
            mid, cid, _resolve_project(client, pid, cid)
        ),
        exact="show_help_menu",
    )
    client._register_action(lambda mid, cid, pid, val: client._handle_deep_prompt(mid, cid), exact="enter_deep_prompt")
    client._register_action(
        lambda mid, cid, pid, val: client._handle_force_release_repo_lock(mid, cid, pid, val),
        exact="force_release_repo_lock",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_confirm_lock(mid, cid, pid, val),
        exact="confirm_lock",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_cancel_lock(mid, cid, pid, val),
        exact="cancel_lock",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_confirm_force_release(mid, cid, pid, val),
        exact="confirm_force_release",
    )
    client._register_action(
        lambda mid, cid, pid, val: client._handle_cancel_force_release(mid, cid, pid, val),
        exact="cancel_force_release",
    )

    from .retry_handler import RetryCommandHandler
    client._register_action(RetryCommandHandler(_RetryDispatchAdapter(client)), exact="retry_command")

    # Approval — dispatches APPROVAL_RESOLVED event to update CardSession state
    def _handle_approval(mid, cid, pid, val, *, approved: bool):
        logger.info("Approval action: approved=%s, message_id=%s, chat_id=%s", approved, mid, cid)
        client._reply_text(mid, "✅ 已批准操作" if approved else "❌ 已拒绝操作")
        # Dispatch APPROVAL_RESOLVED to the engine that rendered the approval card
        try:
            from ..card.events import CardEvent, CardEventType
            engine_type = val.get("engine_type", "")
            approval_val = {**val, "approved": approved}
            if engine_type == "deep":
                client._deep_handler.handle_card_action(mid, cid, "deep_approval_resolved", approval_val)
            elif engine_type == "loop":
                client._loop_handler.handle_card_action(mid, cid, "loop_approval_resolved", approval_val)
            elif engine_type == "spec":
                client._spec_handler.handle_card_action(mid, cid, "spec_approval_resolved", approval_val)
            else:
                # Fallback: try all engines (approval may not have engine_type in value)
                logger.debug("approval: no engine_type, trying deep→loop→spec")
                client._deep_handler.handle_card_action(mid, cid, "deep_approval_resolved", approval_val)
        except Exception as exc:
            logger.warning("Failed to dispatch APPROVAL_RESOLVED for message_id=%s: %s", mid, repr(exc))

    client._register_action(
        lambda mid, cid, pid, val: _handle_approval(mid, cid, pid, val, approved=True),
        exact="approve_action",
    )
    client._register_action(
        lambda mid, cid, pid, val: _handle_approval(mid, cid, pid, val, approved=False),
        exact="reject_action",
    )

    client._register_action(
        lambda mid, cid, pid, val: client._handle_help_category(
            mid,
            cid,
            val.get("category", "main"),
            _resolve_project(client, pid, cid),
            origin_message_id=mid,
        ),
        exact="help_category",
    )

    # Deep Engine
    client._register_action(
        lambda mid, cid, pid, val: client._show_deep_status(
            mid, cid, _resolve_project(client, pid, cid), origin_message_id=mid
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

    # Generic ENGINE_STOP — routes to correct handler based on engine_type in value
    def _handle_engine_stop(mid, cid, pid, val):
        engine_type = val.get("engine_type", "")
        # Remap to engine-specific stop action and delegate to the correct handler
        if engine_type == "deep":
            client._deep_handler.handle_card_action(mid, cid, "deep_stop", val)
        elif engine_type == "loop":
            client._loop_handler.handle_card_action(mid, cid, "loop_stop", val)
        elif engine_type == "spec":
            client._spec_handler.handle_card_action(mid, cid, "spec_stop", val)
        else:
            logger.warning("engine_stop: unknown engine_type=%s, trying all handlers", engine_type)
            # Fallback: try each handler (deep → loop → spec)
            client._deep_handler.handle_card_action(mid, cid, "deep_stop", val)

    client._register_action(_handle_engine_stop, exact="engine_stop")
