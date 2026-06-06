"""Workflow Engine handler — /wf, /workflow, /stop_wf, /wf_status, /wf_help commands."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Optional

from src.card.render.buttons import build_responsive_button_row

from ...utils.text import generate_task_id
from ..emoji import EmojiReaction
from .engine_base import BaseEngineHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class WorkflowHandler(BaseEngineHandler):
    """Manages the full lifecycle of Workflow Engine tasks.

    Commands:
        /wf <requirement>       — Start a new workflow (AI generates script)
        /wf <template> [args]   — Start from a built-in/saved template
        /stop_wf                — Cancel the running workflow
        /wf_status              — Show current workflow progress
    """

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        # Workflow uses its own renderer (card JSON comes from WorkflowProgressRenderer)
        from ...workflow_engine.renderer import WorkflowProgressRenderer  # noqa: F401

    # ------------------------------------------------------------------
    # Topic-engine free-text entry point
    # ------------------------------------------------------------------
    def handle_message(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Handle free-text messages when auto_enter_mode == 'workflow'.

        Treats the entire text as a workflow requirement and starts generation.
        """
        text_stripped = text.strip()
        if not text_stripped:
            return
        self.start_workflow(message_id, chat_id, text_stripped, project)

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_workflow_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Route /wf, /workflow, /stop_wf, /wf_status commands."""
        from ...utils.command_parser import CommandParser

        cmd = CommandParser.parse_basic(text)
        command = cmd.command

        if command in ("/stop_wf", "/stop_workflow"):
            self.stop_workflow(message_id, chat_id, project)
        elif command in ("/wf_status", "/workflow_status"):
            self.show_workflow_status(message_id, chat_id, project)
        elif command in ("/wf_help", "/workflow_help"):
            self.show_workflow_help(message_id)
        elif command in ("/wf_save", "/workflow_save"):
            self._handle_wf_save(message_id, chat_id, cmd.args, project)
        elif command in ("/wf_list", "/workflow_list"):
            self._handle_wf_list(message_id, chat_id, project)
        elif command in ("/wf_delete", "/workflow_delete"):
            self._handle_wf_delete(message_id, chat_id, cmd.args, project)
        elif command in ("/wf_history", "/workflow_history"):
            self._handle_wf_history(message_id, chat_id, project)
        elif command in ("/wf", "/workflow"):
            arg = cmd.args
            if arg:
                self.start_workflow(message_id, chat_id, arg, project)
            else:
                self._show_workflow_entry_card(message_id, chat_id, project)
        else:
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail=(
                    "未知命令。可用命令列表:\n"
                    "• `/wf <需求>` — 基于需求生成编排脚本并执行\n"
                    "• `/wf <模板名> [key=value ...]` — 使用已保存的模板\n"
                    "• `/wf_save <名称> [--global]` — 保存脚本为模板\n"
                    "• `/wf_list` — 列出可用模板\n"
                    "• `/wf_delete <名称> [--global]` — 删除模板\n"
                    "• `/wf_history` — 查看执行历史\n"
                    "• `/wf_status` — 查看当前 Workflow 进度\n"
                    "• `/wf_help` — 查看完整帮助\n"
                    "• `/stop_wf` — 停止正在执行的 Workflow\n"
                    "\n发送 `/wf_help` 查看完整说明。"
                ),
            )

    def _get_engine_manager(self):
        return self.ctx.workflow_engine_manager

    def _get_engine_name_prefix(self) -> str:
        return "Workflow"

    def _get_task_type(self) -> str:
        return "workflow_engine"

    def _show_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.show_workflow_status(message_id, chat_id, project)

    def _create_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str, root_path: str
    ):
        return self._build_workflow_callbacks(message_id, chat_id, project)

    # ------------------------------------------------------------------
    # Entry card
    # ------------------------------------------------------------------

    def _show_workflow_entry_card(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Show the Workflow entry help card when user sends `/wf` without arguments."""
        from ...card import CardBuilder
        from ...card.actions.dispatch import (
            SHOW_WORKFLOW_MENU,
            WORKFLOW_LIST_TEMPLATES,
            WORKFLOW_SHOW_HELP,
        )
        from ...card.ui_text import UI_TEXT

        project_id = project.project_id if project else ""

        start_value = {
            "action": SHOW_WORKFLOW_MENU,
            "chat_id": chat_id,
            "project_id": project_id,
        }
        templates_value = {
            "action": WORKFLOW_LIST_TEMPLATES,
            "chat_id": chat_id,
            "project_id": project_id,
        }
        help_value = {
            "action": WORKFLOW_SHOW_HELP,
            "chat_id": chat_id,
            "project_id": project_id,
        }

        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": UI_TEXT["workflow_entry_btn_start"]},
                "type": "primary",
                "value": start_value,
                "behaviors": [{"type": "callback", "value": start_value}],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": UI_TEXT["workflow_entry_btn_templates"]},
                "type": "default",
                "value": templates_value,
                "behaviors": [{"type": "callback", "value": templates_value}],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": UI_TEXT["workflow_entry_btn_help"]},
                "type": "default",
                "value": help_value,
                "behaviors": [{"type": "callback", "value": help_value}],
            },
        ]

        card_content = CardBuilder._wrap_card(
            header_title=UI_TEXT["workflow_entry_title"],
            header_template="turquoise",
            elements=[
                {"tag": "markdown", "content": UI_TEXT["workflow_entry_body"]},
                {"tag": "hr"},
                *build_responsive_button_row(buttons, mobile_force_vertical=True),
            ],
        )
        self.reply_card(message_id, card_content)

    def handle_show_workflow_menu(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle entry card "start" button — directly launch the agent-selection flow.

        The previous version used to prompt the user to manually type
        ``/wf <需求>``, which added an unnecessary round-trip. Now the
        "开始编排任务" entry button triggers the three-step orchestration
        flow (agent → tools → budget → confirm → execute) in-place. Users
        can still type ``/wf <描述>`` directly to skip the entry card.
        """
        project = self._resolve_project_from_id(project_id, chat_id) if project_id else None
        root_path = self._get_root_path(chat_id, project)

        # Check Node.js availability before showing anything (fail-fast UX).
        from ...workflow_engine.bridge import RuntimeBridge
        from ...workflow_engine.constants import NODE_MIN_VERSION

        if not RuntimeBridge.check_node_available():
            major = NODE_MIN_VERSION[0] if NODE_MIN_VERSION else 20
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail=f"Workflow 模式需要 Node.js >= {major}。请安装 Node.js 并确保 `node` 在 PATH 中。",
            )
            return

        # Check for existing running workflow
        existing = self.ctx.workflow_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self._reply_workflow_error(
                message_id,
                "invalid_state",
                detail="当前项目已有 Workflow 任务在执行中。发送 `/wf_status` 查看进度，或 `/stop_wf` 停止任务。",
            )
            return
        from ...workflow_engine.models import WorkflowStatus
        if existing and existing.project and existing.project.status in {
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
            WorkflowStatus.AWAITING_AGENT_SELECT,
        }:
            self._reply_workflow_error(
                message_id,
                "invalid_state",
                detail="已有 Workflow 等待操作。请先完成或取消当前流程后再开始新任务。",
            )
            return

        # Launch the first step of the three-step flow with an empty
        # requirement — the orchestrator selection card is shown, the user
        # picks an agent, then proceeds to tool selection.
        self._show_agent_selection_card(
            chat_id=chat_id,
            requirement="",
            project=project,
            root_path=root_path,
        )

    def handle_workflow_list_templates(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle entry card 'templates' button — list available templates."""
        project = self._resolve_project_from_id(project_id, chat_id)
        self._handle_wf_list(message_id, chat_id, project)

    def handle_workflow_show_help(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle entry card 'help' button — show workflow help."""
        self.show_workflow_help(message_id)

    # ------------------------------------------------------------------
    # Start workflow
    # ------------------------------------------------------------------

    def start_workflow(
        self,
        message_id: str,
        chat_id: str,
        requirement: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Start a workflow: show tool selection first, then generate script.

        Flow (tool-selection-first):
        1. User sends /wf <requirement>
        2. Show tool selection card with recommended tools
        3. User confirms tool selection → generate script based on selected tools
        4. Show confirmation card with script preview and tool distinction
        5. User can regenerate script with different tools, or confirm to execute

        If `requirement` matches a known template name, skips tool selection and
        goes directly to script generation (template tools are fixed).
        """
        project = self._ensure_project(message_id, chat_id, project)
        if not project:
            return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        # Check for existing running workflow
        existing = self.ctx.workflow_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_error(
                message_id,
                "当前项目已有 Workflow 任务在执行中\n\n"
                "发送 `/wf_status` 查看进度\n"
                "发送 `/stop_wf` 停止任务",
                title="任务冲突",
            )
            return

        # Also block if awaiting confirmation, tool selection, or agent selection
        from ...workflow_engine.models import WorkflowStatus
        if existing and existing.project and existing.project.status in {
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
            WorkflowStatus.AWAITING_AGENT_SELECT,
        }:
            self.reply_error(
                message_id,
                "已有 Workflow 等待操作。\n"
                "请先完成或取消当前流程。",
                title="等待操作中",
            )
            return

        # Check Node.js availability
        from ...workflow_engine.bridge import RuntimeBridge
        from ...workflow_engine.constants import NODE_MIN_VERSION

        if not RuntimeBridge.check_node_available():
            major = NODE_MIN_VERSION[0] if NODE_MIN_VERSION else 20
            self.reply_error(
                message_id,
                f"Workflow 模式需要 Node.js >= {major}。\n"
                "请安装 Node.js 并确保 `node` 在 PATH 中。",
                title="环境缺失",
            )
            return

        # Input validation: requirement must be non-trivial
        _req_stripped = requirement.strip()
        if len(_req_stripped) < 4:
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail="需求描述过短，请提供更详细的说明（至少 4 个字符）。",
            )
            return
        if len(_req_stripped) > 4000:
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail="需求描述过长（超过 4000 字符），请精简后重试。",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        # Bind engine mode to topic for auto-routing
        self._ensure_topic_engine_context(
            mode="workflow",
            message_id=message_id,
            chat_id=chat_id,
            project=project,
        )

        # Templates now also go through the standard three-step flow:
        #   agent selection → tool selection → confirmation.
        # This keeps templates consistent with AI-generated workflows and
        # gives the user a chance to inspect / override the template's
        # default tools before execution.
        # We detect whether `requirement` is a template name here so
        # downstream handlers can record it in pending state (e.g. to
        # initialize default tool selection from template meta.tools).
        from ...workflow_engine.templates import discover_templates
        from ...thread import get_current_sender_id

        sender_id = get_current_sender_id() or ""
        parts = requirement.strip().split(None, 1)
        template_name = parts[0] if parts else ""
        try:
            templates = discover_templates(root_path, user_id=sender_id)
            template_names = {t.name for t in templates}
            is_template = template_name in template_names
        except Exception:
            # Template discovery failure should not block the workflow;
            # just treat this as an AI generation path.
            template_names = set()
            is_template = False

        # Initialize pending state early so we can mark this as a
        # template launch (needed for default tool selection hint).
        engine_name = self.get_engine_name(
            chat_id, project_id=(project.project_id if project else None)
        )
        pre_engine = self.ctx.workflow_engine_manager.get_or_create(
            chat_id,
            root_path,
            engine_name=engine_name,
        )
        if is_template and pre_engine.project:
            pending = pre_engine.project.pending
            if pending is None:
                from ...workflow_engine.models import PendingConfirmation

                pending = PendingConfirmation()
                pre_engine.project.pending = pending
            pending.is_template_hint = template_name

        # Standard three-step flow regardless of template vs AI path.
        self._show_agent_selection_card(
            chat_id=chat_id,
            requirement=requirement,
            project=project,
            root_path=root_path,
        )

    # ------------------------------------------------------------------
    # Error surface unification
    # ------------------------------------------------------------------

    def _build_error_card(
        self,
        category: str,
        *,
        detail: str = "",
    ) -> dict[str, Any]:
        """Build a standardized error card using the unified four-category surface.

        Args:
            category: One of "session_expired", "invalid_state", "invalid_argument", "forbidden", "internal_error"
            detail: Optional detail string for invalid_argument or internal_error categories (replaces {detail} placeholder)
        """
        from ...card import CardBuilder
        from ...card.ui_text import UI_TEXT

        title_key = f"workflow_error_{category}_title"
        body_key = f"workflow_error_{category}_body"

        title = UI_TEXT.get(title_key, "操作失败")
        body = UI_TEXT.get(body_key, "发生未知错误，请重试。")

        # Replace {detail} placeholder for any category that includes it
        if detail and "{detail}" in body:
            body = body.format(detail=detail)
        elif detail:
            # For categories whose UI_TEXT body does not include a {detail}
            # placeholder, append the caller-supplied detail so it still
            # surfaces to the user in a structured card.
            body = f"{body}\n\n**详情:** {detail}"

        # Header color by category
        header_template = "red"  # default for errors

        return CardBuilder._wrap_card(
            header_title=title,
            header_template=header_template,
            elements=[
                {"tag": "markdown", "content": body},
            ],
        )

    def _reply_workflow_error(
        self,
        message_id: str,
        category: str,
        *,
        detail: str = "",
    ) -> None:
        """Reply with a standardized error card."""
        card = self._build_error_card(category, detail=detail)
        self.reply_card(message_id, card)

    @staticmethod
    def _resolve_tool_lists() -> tuple[dict[str, str], list[str], list[str], list[str]]:
        """Resolve available tools, recommended order, other tools, and default selection in one call.

        Returns:
            tuple of (all_tools_dict, recommended_tools_list, other_tools_list, default_selected_list)
        """
        from ...workflow_engine.tool_registry import get_available_tools

        all_tools = get_available_tools() or {"coco": "全栈编程·支持 subagent"}
        all_tool_names = list(all_tools.keys())

        # Simple recommendation: prioritize coco, claude, codex for general tasks
        recommended_order = ["coco", "claude", "codex", "aiden", "gemini", "traex", "ttadk"]
        recommended_tools = [t for t in recommended_order if t in all_tool_names]
        other_tools = [t for t in all_tool_names if t not in recommended_tools]

        # Default selection: top 3 recommended
        default_selected = recommended_tools[:3] if recommended_tools else all_tool_names[:1]

        return all_tools, recommended_tools, other_tools, default_selected

    def _init_tool_selection_state(
        self,
        chat_id: str,
        requirement: str,
        project: Optional["ProjectContext"],
        root_path: str,
        all_tools: dict[str, str],
        recommended_tools: list[str],
        default_selected: list[str],
    ) -> tuple[Any, str, str]:
        """Initialize tool selection state (separated from card building for update_card reuse).

        Args:
            all_tools: Resolved tools dict from _resolve_tool_lists()
            recommended_tools: Resolved recommended tools list from _resolve_tool_lists()
            default_selected: Resolved default selection list from _resolve_tool_lists()

        Returns:
            Tuple of (engine, project_id, session_key)
        """
        from ...thread import get_current_sender_id
        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        # Store pending state
        engine_name = self.get_engine_name(
            chat_id, project_id=(project.project_id if project else None)
        )
        engine = self.ctx.workflow_engine_manager.get_or_create(
            chat_id,
            root_path,
            engine_name=engine_name,
        )
        if engine.project:
            # --- Template-aware default tool selection ---
            # When launched via `/wf <template_name>` we prefer the
            # template's meta.tools (intersected with currently
            # available tools). The user can still add/remove tools
            # before confirming execution.
            existing_pending = engine.project.pending
            existing_orchestrator = existing_pending.orchestrator_agent if existing_pending else None
            template_hint = existing_pending.is_template_hint if existing_pending else None
            template_tools: list[str] = []
            if template_hint:
                try:
                    from ...workflow_engine.templates import load_template
                    from ...workflow_engine.script_gen import extract_meta_from_script

                    template_content = load_template(
                        root_path, template_hint,
                        user_id=get_current_sender_id() or None,
                    )
                    if template_content:
                        template_meta = extract_meta_from_script(template_content)
                        candidate_tools = list((template_meta or {}).get("tools", []))
                        template_tools = [t for t in candidate_tools if t in all_tools]
                except Exception:
                    template_tools = []
            if template_tools:
                # Keep template-preferred tools first, then append the
                # usual default selection without duplicates.
                combined: list[str] = []
                for t in list(template_tools) + list(default_selected):
                    if t not in combined:
                        combined.append(t)
                effective_default = combined
            else:
                effective_default = list(default_selected)

            engine.project.status = WorkflowStatus.AWAITING_TOOL_SELECT
            engine.project.pending = PendingConfirmation(
                requirement=requirement,
                initiator_user_id=get_current_sender_id() or "",
                engine_session_key=uuid.uuid4().hex,
                selected_tools=effective_default,
                script_path=None,
                meta=None,
                orchestrator_agent=existing_orchestrator,
                is_template_hint=template_hint,
            )

        project_id = project.project_id if project else ""
        session_key = engine.project.pending.engine_session_key if engine.project and engine.project.pending else ""

        return engine, project_id, session_key

    def _build_tool_selection_card(
        self,
        engine: Any,
        requirement: str,
        chat_id: str,
        project_id: str,
        session_key: str,
        all_tools: dict[str, str],
        recommended_tools: list[str],
        other_tools: list[str],
        default_selected: list[str],
    ) -> dict:
        """Build the tool selection card (pure rendering — no state already initialized).

        Uses update_card-friendly format — ready for both initial send and in-place updates.

        Args:
            all_tools: Resolved tools dict from _resolve_tool_lists()
            recommended_tools: Resolved recommended tools list from _resolve_tool_lists()
            other_tools: Resolved other tools list from _resolve_tool_lists()
            default_selected: Resolved default selection list from _resolve_tool_lists()
        """
        from ...card import CardBuilder
        from ...card.actions.dispatch import WORKFLOW_CANCEL, WORKFLOW_CONFIRM_TOOLS, WORKFLOW_SELECT_TOOL
        from ...card.render.buttons import build_responsive_button_row
        from ...card.ui_text import UI_TEXT

        # Build card
        elements: list[dict] = []

        # Requirement
        elements.append({
            "tag": "markdown",
            "content": f"**需求**:\n> {requirement[:200]}",
        })
        elements.append({
            "tag": "markdown",
            "content": "**请选择此 Workflow 允许使用的工具**（脚本生成时将优先使用选中的工具）：",
        })
        elements.append({"tag": "hr"})

        # Recommended tools section
        active_tools = set(engine.project.pending.selected_tools) if engine.project and engine.project.pending else set(default_selected)

        if recommended_tools:
            rec_display = " | ".join(
                f"**[✓ `{t}`]**" if t in active_tools else f"`{t}`"
                for t in recommended_tools
            )
            elements.append({
                "tag": "markdown",
                "content": f"⭐ **推荐工具**: {rec_display}",
            })

            rec_buttons = []
            for t in recommended_tools:
                is_selected = t in active_tools
                btn_value = {
                    "action": WORKFLOW_SELECT_TOOL,
                    "tool_name": t,
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "engine_session_key": session_key,
                }
                rec_buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"{'✓ ' if is_selected else '○ '}{t}"},
                    "type": "primary" if is_selected else "default",
                    "value": btn_value,
                    "behaviors": [{"type": "callback", "value": btn_value}],
                })
            elements.extend(build_responsive_button_row(rec_buttons, mobile_force_vertical=True))

        # Other tools section
        if other_tools:
            other_display = " | ".join(
                f"**[✓ `{t}`]**" if t in active_tools else f"`{t}`"
                for t in other_tools
            )
            other_buttons = []
            for t in other_tools:
                is_selected = t in active_tools
                btn_value = {
                    "action": WORKFLOW_SELECT_TOOL,
                    "tool_name": t,
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "engine_session_key": session_key,
                }
                other_buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"{'✓ ' if is_selected else '○ '}{t}"},
                    "type": "primary" if is_selected else "default",
                    "value": btn_value,
                    "behaviors": [{"type": "callback", "value": btn_value}],
                })
            elements.append({
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🔧 更多工具 ({len(other_tools)})",
                    },
                },
                "vertical_spacing": "8px",
                "elements": [
                    {"tag": "markdown", "content": other_display},
                    *build_responsive_button_row(other_buttons, mobile_force_vertical=True),
                ],
            })

        elements.append({"tag": "hr"})

        # Action buttons: Cancel + Confirm Tools
        cancel_value = {
            "action": WORKFLOW_CANCEL,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": session_key,
        }
        confirm_value = {
            "action": WORKFLOW_CONFIRM_TOOLS,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": session_key,
        }
        action_buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消"},
                "type": "default",
                "value": cancel_value,
                "behaviors": [{"type": "callback", "value": cancel_value}],
                "confirm": {
                    "title": {"tag": "plain_text", "content": UI_TEXT["workflow_btn_confirm_cancel_title"]},
                    "text": {"tag": "plain_text", "content": UI_TEXT["workflow_btn_confirm_cancel_body"]},
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "确认工具并生成脚本 →"},
                "type": "primary",
                "value": confirm_value,
                "behaviors": [{"type": "callback", "value": confirm_value}],
            },
        ]
        elements.extend(build_responsive_button_row(action_buttons, mobile_force_vertical=True))

        from ...card.ui_text import UI_TEXT
        tool_select_card = CardBuilder._wrap_card(
            header_title="🔧 Workflow — 选择工具",
            header_template=UI_TEXT["workflow_header_colors"].get("tool_select", "turquoise"),
            elements=elements,
        )
        return tool_select_card

    def _show_agent_selection_card(
        self,
        chat_id: str,
        requirement: str,
        project: Optional["ProjectContext"],
        root_path: str,
    ) -> None:
        """Show the orchestrator agent selection card before tool selection."""
        from ...card import CardBuilder
        from ...card.actions.dispatch import WORKFLOW_SELECT_AGENT, WORKFLOW_CANCEL
        from ...card.render.buttons import build_responsive_button_row
        from ...card.ui_text import UI_TEXT
        from ...thread import get_current_sender_id
        from ...workflow_engine.constants import (
            DEFAULT_ORCHESTRATOR_AGENT,
            ORCHESTRATOR_AGENT_OPTIONS,
        )
        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        # Initialize pending state
        engine_name = self.get_engine_name(
            chat_id, project_id=(project.project_id if project else None)
        )
        engine = self.ctx.workflow_engine_manager.get_or_create(
            chat_id,
            root_path,
            engine_name=engine_name,
        )

        session_key = uuid.uuid4().hex
        # Preserve is_template_hint across step transitions so the
        # downstream tool-selection stage can initialize default
        # selected_tools from the template meta.tools when applicable.
        previous_hint: Optional[str] = None
        if engine.project and engine.project.pending:
            previous_hint = getattr(engine.project.pending, "is_template_hint", None)
        if engine.project:
            engine.project.status = WorkflowStatus.AWAITING_AGENT_SELECT
            engine.project.pending = PendingConfirmation(
                requirement=requirement,
                initiator_user_id=get_current_sender_id() or "",
                engine_session_key=session_key,
                orchestrator_agent=DEFAULT_ORCHESTRATOR_AGENT,
                is_template_hint=previous_hint,
            )

        project_id = project.project_id if project else ""

        # Build agent selection card
        # Build agent selection card — card-style list, vertical on mobile.
        # Mapping agent_type -> (summary, subagent_affinity, scenarios)
        agent_meta: dict[str, tuple[str, str, str]] = {
            "coco": ("全栈编程 · 默认推荐", "擅长 subagent 调度 / 并行执行", "日常开发 · 多工具编排 · 快速原型"),
            "claude": ("深度推理 · 复杂任务", "支持 subagent · 推理链更强", "长脚本生成 · 复杂决策 · 长程规划"),
            "aiden": ("代码审查 · 架构设计", "subagent 用于分段审查", "代码审计 · 重构方案 · 架构评估"),
            "codex": ("OpenAI 自主编程", "可启用 subagent 扩展", "标准 Python/TS 开发 · 工具桥接"),
            "gemini": ("多模态推理", "subagent 可处理图像/文本混合", "包含图像或文档上下文的任务"),
            "traex": ("高并发推理 · 轻量", "高吞吐 subagent", "短平快任务 · 并行 fan-out"),
        }

        elements: list[dict] = []

        # Requirement
        req_display = requirement.strip() or "（稍后填写，可直接选择 Agent）"
        elements.append({
            "tag": "markdown",
            "content": f"**需求**:\n> {req_display[:200]}",
        })
        elements.append({
            "tag": "markdown",
            "content": (
                "**请选择主编排 Agent**（各 Agent 能力差异直接影响脚本生成与 subagent 调度方式）："
            ),
        })

        # --- Card-style list: each agent is its own mini-card + CTA. ---
        for agent_type, display_name, _short_desc in ORCHESTRATOR_AGENT_OPTIONS:
            is_default = agent_type == DEFAULT_ORCHESTRATOR_AGENT
            summary, subagent_affinity, scenarios = agent_meta.get(
                agent_type,
                ("通用 Agent", "可调度 subagent", "一般任务"),
            )
            btn_value = {
                "action": WORKFLOW_SELECT_AGENT,
                "agent_type": agent_type,
                "engine_session_key": session_key,
                "project_id": project_id,
            }
            star = "★ " if is_default else ""
            badge = "（默认推荐）" if is_default else ""
            card_body = (
                f"**{star}{display_name}** {badge}\n"
                f"· **定位**: {summary}\n"
                f"· **Subagent 能力**: {subagent_affinity}\n"
                f"· **适用场景**: {scenarios}"
            )
            elements.append({"tag": "markdown", "content": card_body})

            # Single-row CTA button for this agent (vertical stacking).
            cta = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"使用 {display_name}"},
                "type": "primary" if is_default else "default",
                "value": btn_value,
                "behaviors": [{"type": "callback", "value": btn_value}],
            }
            elements.extend(build_responsive_button_row([cta], mobile_force_vertical=True))

        # --- Cancel at bottom ---
        cancel_value = {
            "action": WORKFLOW_CANCEL,
            "engine_session_key": session_key,
            "project_id": project_id,
        }
        cancel = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消"},
            "type": "default",
            "value": cancel_value,
            "behaviors": [{"type": "callback", "value": cancel_value}],
        }
        elements.extend(build_responsive_button_row([cancel], mobile_force_vertical=True))

        card = CardBuilder._wrap_card(
            header_title="Workflow — 选择主编排 Agent",
            header_template="blue",
            elements=elements,
        )

        self.send_card_to_chat(chat_id, card)

    def _show_tool_selection_card(
        self,
        message_id: str,
        chat_id: str,
        requirement: str,
        project: Optional["ProjectContext"],
        root_path: str,
    ) -> None:
        """Show the initial tool selection card before script generation.

        Splits state initialization from card building so update_card can reuse
        the builder without resetting state (fixes tool selection state reset bug).

        Resolves tool lists once via _resolve_tool_lists() to eliminate duplicate
        get_available_tools() calls and race conditions between init and build.
        """
        all_tools, recommended_tools, other_tools, default_selected = self._resolve_tool_lists()

        engine, project_id, session_key = self._init_tool_selection_state(
            chat_id=chat_id,
            requirement=requirement,
            project=project,
            root_path=root_path,
            all_tools=all_tools,
            recommended_tools=recommended_tools,
            default_selected=default_selected,
        )
        card = self._build_tool_selection_card(
            engine=engine,
            requirement=requirement,
            chat_id=chat_id,
            project_id=project_id,
            session_key=session_key,
            all_tools=all_tools,
            recommended_tools=recommended_tools,
            other_tools=other_tools,
            default_selected=default_selected,
        )
        self.send_card_to_chat(chat_id, card)

    def _generate_and_show_confirm_card(
        self,
        message_id: str,
        chat_id: str,
        requirement: str,
        project: Optional["ProjectContext"],
        root_path: str,
        selected_tools: list[str] | None,
    ) -> None:
        """Generate script (template or AI) and show confirmation card.

        Called after tool selection is confirmed, or directly for templates.
        """
        from ...card import CardBuilder

        # --- Guard: requirement must be non-empty and meet minimum length.
        # start_workflow already validates the happy path; this line keeps the
        # second check prevents bypasses via direct handler-calling paths.
        if not requirement or len(requirement.strip()) < 4:
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail="需求描述过短（至少 4 个字符），请重新发送 `/wf <需求描述>` 并提供更清晰的任务目标。",
            )
            return

        # Send transitional "generating" card
        from ...card.ui_text import UI_TEXT
        from ...thread import get_current_sender_id
        from ...workflow_engine.constants import DEFAULT_ORCHESTRATOR_AGENT
        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus
        from ...workflow_engine.script_gen import extract_meta_from_script
        from ...workflow_engine.templates import discover_templates, load_template
        gen_card = CardBuilder._wrap_card(
            header_title="🔄 Workflow — 生成脚本中...",
            header_template=UI_TEXT["workflow_header_colors"].get("generating", "blue"),
            elements=[{
                "tag": "markdown",
                "content": f"正在基于选定工具生成编排脚本，请稍候...\n\n**需求**: {requirement[:200]}",
            }],
        )
        gen_msg_id = self.send_card_to_chat(chat_id, gen_card)

        # Create engine first so we can access pending.orchestrator_agent for script generation
        engine_name = self.get_engine_name(
            chat_id, project_id=(project.project_id if project else None)
        )
        engine = self.ctx.workflow_engine_manager.get_or_create(
            chat_id,
            root_path,
            engine_name=engine_name,
        )

        parts = requirement.strip().split(None, 1)
        template_name = parts[0] if parts else ""
        sender_id = get_current_sender_id() or ""
        templates = discover_templates(root_path, user_id=sender_id)
        template_names = {t.name for t in templates}

        script_path: str
        meta: dict[str, Any] | None = None
        is_fallback = False

        if template_name in template_names:
            # Template path — resolve directly
            content = load_template(root_path, template_name, user_id=sender_id)
            if content:
                # Validate template content
                from ...workflow_engine.script_gen import validate_generated_script

                is_valid, errors = validate_generated_script(content)
                if not is_valid:
                    logger.warning("Template '%s' failed validation: %s", template_name, errors)
                    self.reply_error(
                        message_id,
                        f"模板 `{template_name}` 未通过安全校验:\n" + "\n".join(f"• {e}" for e in errors[:5]),
                        title="模板校验失败",
                    )
                    return

                script_dir = os.path.join(root_path, ".ghostap", "workflow_scripts")
                os.makedirs(script_dir, exist_ok=True)
                script_path = os.path.join(script_dir, f"{template_name}.js")

                if len(parts) > 1:
                    from ...workflow_engine.templates import inject_args
                    args = self._parse_template_args(parts[1])
                    content = inject_args(content, args)

                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(content)
                meta = extract_meta_from_script(content)
            else:
                # Template load failed — fallback to AI
                script_path, meta, is_fallback = self._generate_script_via_ai(
                    requirement, root_path, selected_tools, engine
                )
        else:
            # AI generation path with selected tools
            script_path, meta, is_fallback = self._generate_script_via_ai(
                requirement, root_path, selected_tools, engine
            )
        if engine.project:
            engine.project.status = WorkflowStatus.AWAITING_CONFIRM
            # Preserve fields from the existing pending state so that
            # regenerating the script (e.g. via "重新生成脚本" or budget
            # changes) keeps the user's chosen orchestrator agent and
            # budget rather than resetting to defaults.
            existing_pending = engine.project.pending
            preserved_orchestrator = (
                existing_pending.orchestrator_agent
                if existing_pending and getattr(existing_pending, "orchestrator_agent", None)
                else DEFAULT_ORCHESTRATOR_AGENT
            )
            preserved_budget = (
                existing_pending.selected_budget
                if existing_pending and getattr(existing_pending, "selected_budget", None) is not None
                else None
            )
            preserved_template_hint = (
                existing_pending.is_template_hint
                if existing_pending and getattr(existing_pending, "is_template_hint", None)
                else None
            )
            # Keep selected tools as the allow list; meta.tools is what script plans to use
            if selected_tools is not None:
                sel_tools = list(selected_tools)
            else:
                # For templates, initialize from meta
                sel_tools = list((meta or {}).get("tools", ["coco"]))
            # Track tools mismatch for warning
            script_tools = set((meta or {}).get("tools", []))
            allowed_tools = set(sel_tools or [])
            tools_mismatch = bool(script_tools - allowed_tools)
            engine.project.pending = PendingConfirmation(
                script_path=script_path,
                requirement=requirement,
                meta=meta,
                is_fallback=is_fallback,
                # Security: bind confirmation to initiator and session key
                initiator_user_id=get_current_sender_id() or "",
                engine_session_key=uuid.uuid4().hex,
                selected_tools=sel_tools,
                tools_mismatch=tools_mismatch,
                orchestrator_agent=preserved_orchestrator,
                selected_budget=preserved_budget,
                is_template_hint=preserved_template_hint,
            )

        # Build and send confirmation card
        project_id = project.project_id if project else ""
        _script_content = self._read_pending_script(engine)
        confirm_card = self._build_confirm_card(
            meta=meta,
            requirement=requirement,
            engine_session_key=engine.project.pending.engine_session_key if engine.project and engine.project.pending else "",
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=is_fallback,
            selected_tools=engine.project.pending.selected_tools if engine.project and engine.project.pending else None,
            script_content=_script_content,
        )
        if gen_msg_id:
            self.update_card(gen_msg_id, confirm_card)
        else:
            self.send_card_to_chat(chat_id, confirm_card)

    # ------------------------------------------------------------------
    # Stop workflow
    # ------------------------------------------------------------------

    def stop_workflow(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Stop the running workflow for the current project.

        Security: only the workflow initiator or a configured admin can stop it.
        """
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.is_running:
            self._reply_workflow_error(message_id, "invalid_state", detail="当前没有运行中的 Workflow 任务")
            return

        # Validate: only initiator or admin can stop — fail-closed
        from ...thread import get_current_sender_id

        current_user = get_current_sender_id()
        stored_initiator = getattr(engine.project, "initiator_user_id", None)
        admin_ids: list[str] = getattr(self.ctx.settings, "admin_user_ids", []) or []

        # Fail-closed: missing initiator or operator → deny
        if not stored_initiator or not current_user:
            self._reply_workflow_error(message_id, "forbidden", detail="无法验证操作者身份，停止请求被拒绝")
            return

        if current_user != stored_initiator and current_user not in admin_ids:
            self._reply_workflow_error(message_id, "forbidden", detail="只有 Workflow 发起者或管理员才能停止此任务")
            return

        engine.stop()
        self.reply_text(message_id, "Workflow 任务已停止。")

    # ------------------------------------------------------------------
    # Confirm / Cancel actions (card button callbacks)
    # ------------------------------------------------------------------

    def handle_workflow_select_agent(
        self,
        message_id: str,
        chat_id: str,
        project_id: str | None,
        value: dict[str, Any],
    ) -> None:
        """Handle orchestrator agent selection callback.

        Pending engine 是在 ``_show_agent_selection_card`` 中以
        ``project.root_path`` 作为 key 创建的。为了在 ``project.root_path``
        与 ``chat.working_dir`` 不同的场景下也能正确取回 pending engine，
        这里优先从按钮 ``value`` 中的 ``project_id`` 解析项目对象，并使用
        它的 ``root_path`` 去 manager 中查找；若 ``value`` 中没有
        ``project_id``（旧卡片兼容），再回退到 chat 工作目录。
        """
        from ...thread import get_current_sender_id
        from ...workflow_engine.models import WorkflowStatus

        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        # 第一步：从 value 解析 project_id -> 拿到 project -> 用 project.root_path 作为 engine key
        project_id = value.get("project_id", "") or ""
        project = self._resolve_project_from_id(project_id, chat_id) if project_id else None
        root_path = project.root_path if project else self._get_root_path(chat_id, None)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        if engine.project.status != WorkflowStatus.AWAITING_AGENT_SELECT:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # Security validation
        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # Get selected agent
        agent_type = value.get("agent_type", "")
        if not agent_type:
            self._reply_workflow_error(message_id, "invalid_argument", detail="缺少 agent_type 参数")
            return

        # Whitelist: only accept known orchestrator agent types to prevent forged
        # callbacks from injecting arbitrary agent identifiers.
        from ...workflow_engine.constants import ORCHESTRATOR_AGENT_OPTIONS

        valid_agent_types = {option[0] for option in ORCHESTRATOR_AGENT_OPTIONS}
        if agent_type not in valid_agent_types:
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail=f"Unsupported orchestrator agent: {agent_type}",
            )
            return

        # Store selected agent in pending state
        if engine.project.pending is None:
            from ...workflow_engine.models import PendingConfirmation
            engine.project.pending = PendingConfirmation()
        engine.project.pending.orchestrator_agent = agent_type

        # Proceed to tool selection
        requirement = engine.project.pending.requirement if engine.project.pending else ""
        self._show_tool_selection_card(message_id, chat_id, requirement, project, root_path)

    def handle_workflow_confirm_tools(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle confirm tools button — generate script based on selected tools.

        Security: validates engine_session_key and initiator_user_id.
        Transitions from AWAITING_TOOL_SELECT to script generation.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = project.root_path if project else self._get_root_path(chat_id, None)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_TOOL_SELECT:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (fail-closed) ---
        from ...thread import get_current_sender_id

        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # Get selected tools and requirement
        selected_tools = list(engine.project.pending.selected_tools or []) if engine.project.pending else []
        requirement = engine.project.pending.requirement if engine.project.pending else ""

        if not selected_tools:
            self._reply_workflow_error(message_id, "invalid_argument", detail="请至少选择一个工具")
            return

        # Generate script and show confirm card
        self._generate_and_show_confirm_card(
            message_id=message_id,
            chat_id=chat_id,
            requirement=requirement,
            project=project,
            root_path=root_path,
            selected_tools=selected_tools,
        )

    def handle_workflow_regenerate_script(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle regenerate script button — regenerate with current tool selection.

        Security: validates engine_session_key and initiator_user_id.
        Only available in AWAITING_CONFIRM state after tool changes.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = project.root_path if project else self._get_root_path(chat_id, None)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (fail-closed) ---
        from ...thread import get_current_sender_id

        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # Get current state
        selected_tools = list(engine.project.pending.selected_tools or []) if engine.project.pending else []
        requirement = engine.project.pending.requirement if engine.project.pending else ""

        if not selected_tools:
            self._reply_workflow_error(message_id, "invalid_argument", detail="请至少选择一个工具")
            return

        # Clean up old script file
        old_script_path = engine.project.pending.script_path if engine.project.pending else None
        if old_script_path:
            try:
                os.remove(old_script_path)
            except OSError:
                pass

        # Regenerate script with current tool selection
        self._generate_and_show_confirm_card(
            message_id=message_id,
            chat_id=chat_id,
            requirement=requirement,
            project=project,
            root_path=root_path,
            selected_tools=selected_tools,
        )

    def handle_workflow_confirm_start(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle confirm button click — start workflow execution.

        Security: validates initiator_user_id and engine_session_key before
        allowing execution to prevent cross-user confirmation hijacking.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        # Route to correct project using project_id from button value
        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = project.root_path if project else self._get_root_path(chat_id, None)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation ---
        from ...thread import get_current_sender_id

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")

        # Fail-closed: reject if session key missing or mismatched
        if not stored_session_key or button_session_key != stored_session_key:
            logger.warning(
                "Workflow confirm rejected: session_key mismatch (button=%s, stored=%s)",
                button_session_key[:8],
                stored_session_key[:8],
            )
            self._reply_workflow_error(message_id, "session_expired")
            return

        # Fail-closed: reject if initiator unknown or user mismatch
        if not stored_initiator or not current_user or current_user != stored_initiator:
            logger.warning(
                "Workflow confirm rejected: user mismatch (operator=%s, initiator=%s)",
                current_user[:8],
                stored_initiator[:8],
            )
            self._reply_workflow_error(message_id, "forbidden")
            return

        # Retrieve pending state
        script_path = engine.project.pending.script_path if engine.project.pending else None
        requirement = engine.project.pending.requirement if engine.project.pending else ""
        selected_tools = list(engine.project.pending.selected_tools or []) if engine.project.pending else []
        _pending = engine.project.pending if engine.project else None
        selected_budget = None
        if _pending is not None:
            selected_budget = (
                _pending.selected_budget
                if getattr(_pending, "selected_budget", None) is not None
                else _pending.budget
            )
        meta = engine.project.pending.meta if engine.project.pending else None

        if not script_path:
            self._reply_workflow_error(message_id, "invalid_state", detail="无法获取待执行脚本，请重新发送 `/wf`")
            return

        # Validate script path exists (defense-in-depth)
        import os
        if not os.path.isfile(script_path):
            self._reply_workflow_error(message_id, "internal_error", detail=f"脚本文件不存在 ({script_path})，请重新发送 `/wf` 生成")
            return

        # Validate tool consistency: script tools must be subset of allowed tools
        script_tools = set((meta or {}).get("tools", []))
        allowed_tools = set(selected_tools)
        if script_tools and allowed_tools:
            unmatched = script_tools - allowed_tools
            if unmatched:
                self.reply_error(
                    message_id,
                    f"脚本计划使用的工具 {sorted(unmatched)} 不在允许的工具列表中。\n"
                    f"请点击「重新生成编排」按钮基于当前工具选择重新生成脚本，\n"
                    f"或在工具选择中添加缺失的工具。",
                    title="工具不匹配",
                )
                return

        # Clear pending state and set running
        engine.project.start_execution()

        # Use project already resolved above for engine_name
        engine_name = self.get_engine_name(
            chat_id, project_id=project_id or None
        )

        project_name = (project.project_name if project else "") or os.path.basename(root_path)
        task_id = generate_task_id(project_name or "workflow")

        def run_workflow():
            def _executor():
                callbacks = self._build_workflow_callbacks(message_id, chat_id, project)
                budget_kwargs = {}
                if selected_budget is not None:
                    budget_kwargs["budget_tokens"] = selected_budget
                engine.execute_workflow(
                    requirement=requirement,
                    script_path=script_path,
                    callbacks=callbacks,
                    selected_tools=selected_tools or None,
                    initiator_user_id=stored_initiator or None,
                    **budget_kwargs,
                )

            self._safe_execute_engine(
                executor_func=_executor,
                task_id=task_id,
                chat_id=chat_id,
                message_id=message_id,
                project=project,
                engine_name=engine_name,
                reporter=self.ctx.progress_reporter,
                request_id=self.ensure_request_id(
                    message_id,
                    chat_id=chat_id,
                    project_id=project_id or None,
                ),
                action_prefix="workflow",
                command_text=f"/wf {requirement}",
            )

        self._submit_engine_task(
            run_workflow,
            chat_id,
            message_id,
            project,
            request_id=None,
            task_id=task_id,
        )

    def handle_workflow_cancel(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle cancel button click — abort the pending workflow.

        Security: validates engine_session_key and initiator_user_id before
        allowing cancellation.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        valid_statuses = (WorkflowStatus.AWAITING_CONFIRM, WorkflowStatus.AWAITING_TOOL_SELECT, WorkflowStatus.AWAITING_AGENT_SELECT)
        if engine.project.status not in valid_statuses:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # Security: validate session key (fail-closed)
        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        # Security: validate initiator (fail-closed)
        from ...thread import get_current_sender_id

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # Clean up pending script file
        script_path = engine.project.pending.script_path if engine.project.pending else None
        if script_path:
            try:
                os.remove(script_path)
            except OSError:
                pass

        # Reset state
        engine.project.status = WorkflowStatus.IDLE
        engine.project.pending = None

        # Update card to show cancelled
        from ...card import CardBuilder

        cancel_card = CardBuilder._wrap_card(
            header_title="🔄 Workflow — 已取消",
            header_template="grey",
            elements=[{
                "tag": "markdown",
                "content": "Workflow 已取消。如需重新开始，请发送 `/wf <需求>`。",
            }],
        )
        self.update_card(message_id, cancel_card)

    @staticmethod
    def _validate_tools_against_registry(
        candidate_tools: list[str],
    ) -> tuple[list[str], list[str]]:
        """Return ``(kept, rejected)`` lists from ``candidate_tools``.

        Tool names must appear in the authoritative ``tool_registry
        .get_available_tools()`` to be kept. Unknown names are dropped and
        returned in ``rejected``.  Callers should surface a visible warning
        when ``rejected`` is non-empty — unknown tools must never reach the
        executor.
        """
        try:
            from ...workflow_engine.tool_registry import get_available_tools

            available = set(get_available_tools().keys())
        except Exception:
            logger.exception("tool_registry.get_available_tools() failed; falling back to empty set")
            available = set()

        kept: list[str] = []
        rejected: list[str] = []
        seen: set[str] = set()
        for name in candidate_tools:
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            if name in available:
                kept.append(name)
            else:
                rejected.append(name)
        return kept, rejected

    def handle_workflow_select_tool(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Toggle a tool in the pending workflow's selected tools list.

        Handles two states:
        - AWAITING_TOOL_SELECT: initial tool selection before script generation
        - AWAITING_CONFIRM: tool adjustment after script generation (triggers regenerate prompt)

        Security: validates engine_session_key and initiator_user_id.
        Updates the card to reflect the new selection state.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        valid_statuses = (WorkflowStatus.AWAITING_CONFIRM, WorkflowStatus.AWAITING_TOOL_SELECT)
        if engine.project.status not in valid_statuses:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (fail-closed) ---
        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...thread import get_current_sender_id

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        tool_name = value.get("tool_name", "")
        if not tool_name:
            self._reply_workflow_error(message_id, "invalid_argument", detail="缺少 tool_name 参数")
            return

        # --- Registry validation: unknown tool names are rejected at the boundary.
        # This prevents malicious callers from widening the tool set via forged
        # button payloads.
        _kept, _rejected = self._validate_tools_against_registry([tool_name])
        if _rejected:
            logger.warning(
                "[workflow] Rejected unknown tool_name=%s at handle_workflow_select_tool",
                tool_name,
            )
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail=f"工具「{tool_name}」不在当前可用工具列表中，已被过滤；可用工具：{', '.join(sorted(_kept)) or '(空)'}",
            )
            return

        # Initialize selected tools from meta if not yet set (only for AWAITING_CONFIRM)
        if engine.project.pending is None:
            engine.project.pending = PendingConfirmation()
        if engine.project.pending.selected_tools is None:
            if engine.project.status == WorkflowStatus.AWAITING_CONFIRM:
                meta_tools = (engine.project.pending.meta or {}).get("tools", ["coco"])
                engine.project.pending.selected_tools = list(meta_tools)
            else:
                engine.project.pending.selected_tools = []

        # Toggle
        if tool_name in engine.project.pending.selected_tools:
            engine.project.pending.selected_tools.remove(tool_name)
        else:
            engine.project.pending.selected_tools.append(tool_name)

        # Ensure at least one tool is selected
        if not engine.project.pending.selected_tools:
            engine.project.pending.selected_tools.append(tool_name)
            return  # No change — don't re-render

        # Re-render based on current state
        if engine.project.status == WorkflowStatus.AWAITING_TOOL_SELECT:
            # In-place update via update_card — state is already mutated, just rebuild card
            # Using _build_tool_selection_card directly (not _show_tool_selection_card)
            # avoids re-initializing pending.selected_tools and preserves user selection
            all_tools, recommended_tools, other_tools, default_selected = self._resolve_tool_lists()
            card = self._build_tool_selection_card(
                engine=engine,
                requirement=engine.project.pending.requirement or "",
                chat_id=chat_id,
                project_id=project_id,
                session_key=stored_session_key,
                all_tools=all_tools,
                recommended_tools=recommended_tools,
                other_tools=other_tools,
                default_selected=default_selected,
            )
            self.update_card(message_id, card)
        else:
            # AWAITING_CONFIRM: mark tools as dirty and re-render confirm card
            script_tools = set((engine.project.pending.meta or {}).get("tools", []))
            allowed_tools = set(engine.project.pending.selected_tools or [])
            engine.project.pending.tools_mismatch = bool(script_tools - allowed_tools)

            _script_content = self._read_pending_script(engine)
            confirm_card = self._build_confirm_card(
                meta=engine.project.pending.meta,
                requirement=engine.project.pending.requirement or "",
                engine_session_key=engine.project.pending.engine_session_key or "",
                chat_id=chat_id,
                project_id=project_id,
                is_fallback=engine.project.pending.is_fallback,
                selected_tools=engine.project.pending.selected_tools,
                selected_budget=(
                    engine.project.pending.selected_budget
                    if getattr(engine.project.pending, "selected_budget", None) is not None
                    else engine.project.pending.budget
                ),
                script_content=_script_content,
            )
            self.update_card(message_id, confirm_card)

    def handle_workflow_select_budget(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle budget selection callback — updates ONLY the selection state,
        without triggering script regeneration. Script regeneration is gated on
        the explicit "Apply budget and regenerate" button so users don't burn
        tokens accidentally while exploring budget tiers.

        Security: validates engine_session_key and initiator_user_id.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (fail-closed) ---
        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...thread import get_current_sender_id

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        from ...workflow_engine.constants import is_valid_budget

        budget_tokens = value.get("budget_tokens")
        if not is_valid_budget(budget_tokens):
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail="预算值必须是预定义档位（50万 / 150万 / 200万 / 500万），请重新选择",
            )
            return

        if engine.project.pending is None:
            engine.project.pending = PendingConfirmation()

        # --- Selection-only update: no AI call, no script regeneration ---
        # Write to `selected_budget` (the authoritative field) and mirror to
        # `budget` for backwards-compatibility with any remaining readers.
        engine.project.pending.selected_budget = budget_tokens
        engine.project.pending.budget = budget_tokens

        _script_content = self._read_pending_script(engine)
        confirm_card = self._build_confirm_card(
            meta=engine.project.pending.meta,
            requirement=engine.project.pending.requirement or "",
            engine_session_key=engine.project.pending.engine_session_key or "",
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=engine.project.pending.is_fallback,
            selected_tools=engine.project.pending.selected_tools,
            selected_budget=budget_tokens,
            script_content=_script_content,
        )
        self.update_card(message_id, confirm_card)

    def handle_workflow_apply_budget_regenerate(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle "apply budget and regenerate" button.

        Two-step server-side state gate (never trusts a client-side
        ``confirmed`` payload):

        1. First click (pending.armed_for_regen is False/unset):
           - Validates session key + initiator_user_id.
           - Writes ``armed_for_regen`` on the pending state (server-owned).
           - Re-renders the confirm card so the button label reflects the
             armed state.

        2. Second click (pending.armed_for_regen is True):
           - Re-validates session + initiator_user_id.
           - Actually invokes ``_generate_script_via_ai`` under the new
             budget.

        The native Feishu confirm pop-up is still shown to give the user a
        human-readable cost summary, but the server does not trust any
        payload field to decide whether the generation runs.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (fail-closed on either check) ---
        from ...thread import get_current_sender_id

        stored_session_key = (
            engine.project.pending.engine_session_key if engine.project.pending else ""
        )
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = (
            engine.project.pending.initiator_user_id if engine.project.pending else ""
        )
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # --- Two-step server-side gate ---
        pending = engine.project.pending
        armed = bool(getattr(pending, "armed_for_regen", False))
        if not armed:
            pending.armed_for_regen = True
            _script_content = self._read_pending_script(engine)
            confirm_card = self._build_confirm_card(
                meta=pending.meta,
                requirement=pending.requirement or "",
                engine_session_key=stored_session_key,
                chat_id=chat_id,
                project_id=project_id,
                is_fallback=pending.is_fallback,
                selected_tools=pending.selected_tools,
                selected_budget=(
                    pending.selected_budget
                    if getattr(pending, "selected_budget", None) is not None
                    else pending.budget
                ),
                script_content=_script_content,
            )
            self.update_card(message_id, confirm_card)
            return

        # Second click — actually regenerate.
        # Reset the armed gate so the user must re-arm if they want another
        # regeneration (prevents a single-armed-token burst).
        pending.armed_for_regen = False

        from ...workflow_engine.constants import DEFAULT_BUDGET_TOKENS, is_valid_budget

        _regen_pending = pending
        if _regen_pending is not None and getattr(
            _regen_pending, "selected_budget", None
        ) is not None:
            budget_tokens = _regen_pending.selected_budget
        elif _regen_pending is not None:
            budget_tokens = _regen_pending.budget
        else:
            budget_tokens = None

        # Guard against invalid budget values coming from template meta or
        # legacy state — fall back to the default tier instead of crashing.
        if not is_valid_budget(budget_tokens):
            logger.warning(
                "Regenerate: pending budget %r is not a valid tier; falling back to DEFAULT_BUDGET_TOKENS=%d",
                budget_tokens,
                DEFAULT_BUDGET_TOKENS,
            )
            budget_tokens = DEFAULT_BUDGET_TOKENS

        requirement = pending.requirement or ""
        selected_tools = pending.selected_tools

        try:
            old_script_path = pending.script_path
            if old_script_path and os.path.exists(old_script_path):
                os.remove(old_script_path)

            script_path, meta, is_fallback = self._generate_script_via_ai(
                requirement,
                root_path,
                selected_tools,
                engine,
                override_budget_tokens=budget_tokens,
            )

            pending.script_path = script_path
            pending.meta = meta
            pending.is_fallback = is_fallback
        except Exception:
            logger.exception("Failed to regenerate script with new budget")
            self._reply_workflow_error(
                message_id,
                "internal_error",
                detail="根据新预算重新生成编排脚本失败，请重试或调整需求后再次尝试。",
            )
            return

        _script_content = self._read_pending_script(engine)
        confirm_card = self._build_confirm_card(
            meta=pending.meta,
            requirement=requirement,
            engine_session_key=stored_session_key,
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=engine.project.pending.is_fallback,
            selected_tools=engine.project.pending.selected_tools,
            selected_budget=budget_tokens,
            script_content=_script_content,
        )
        self.update_card(message_id, confirm_card)

    def handle_workflow_fill_missing_tools(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """One-click fill-in of missing tools the script needs but the user
        hasn't yet selected. Updates pending.selected_tools to the union of
        existing user selection and the script-declared tool list, then
        re-renders the confirm card.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation ---
        from ...thread import get_current_sender_id

        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id if engine.project.pending else ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        script_tools = list((engine.project.pending.meta or {}).get("tools", []))
        # --- Registry filter: unknown names in meta.tools must not enter
        # selected_tools. ``kept`` contains only names present in the
        # authoritative registry; ``rejected`` is surfaced to the user.
        _kept, _rejected = WorkflowHandler._validate_tools_against_registry(script_tools)
        current = list(engine.project.pending.selected_tools or [])
        # Merge existing selected tools (already validated at selection time)
        # with the registry-validated script-declared tools.
        merged_set: set[str] = set(current) | set(_kept)
        merged = sorted(merged_set)
        engine.project.pending.selected_tools = merged

        _script_content = self._read_pending_script(engine)

        if _rejected:
            logger.warning(
                "[workflow] fill_missing_tools dropped unknown tool names: %s",
                _rejected,
            )
            # Surface a visible notice on the confirm card via the inline
            # warning text.
            self.reply_text(
                message_id,
                "⚠️ 补齐工具时发现脚本声明的工具中存在未知名称，已被过滤。\n"
                f"已过滤: {', '.join(_rejected)}\n"
                f"已保留: {', '.join(merged) or '(空)'}",
            )

        confirm_card = self._build_confirm_card(
            meta=engine.project.pending.meta,
            requirement=engine.project.pending.requirement or "",
            engine_session_key=stored_session_key,
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=engine.project.pending.is_fallback,
            selected_tools=merged,
            selected_budget=(
                engine.project.pending.selected_budget
                if getattr(engine.project.pending, "selected_budget", None) is not None
                else engine.project.pending.budget
            ),
            script_content=_script_content,
        )
        self.update_card(message_id, confirm_card)

    def handle_workflow_back_to_tools(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Returns the user to the tool-selection card.

        This is the explicit "back" path: when the user sees a tool mismatch
        and wants to go back to toggle tools manually (rather than fill-in
        auto-magically). Keeps the existing pending state so we don't re-run
        the AI, re-generate a session key, or overwrite the user's already
        selected tools / budget / script meta.

        The card is re-built via `_build_tool_selection_card` (pure rendering)
        and sent via `update_card`. We deliberately do NOT call
        `_show_tool_selection_card` because that helper runs
        `_init_tool_selection_state` which would reset engine_session_key and
        discard the current pending content.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (session key + sender) ---
        from ...thread import get_current_sender_id

        stored_session_key = (
            engine.project.pending.engine_session_key if engine.project.pending else ""
        )
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = (
            engine.project.pending.initiator_user_id if engine.project.pending else ""
        )
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # --- Transit state back to tool-selection WITHOUT resetting session key ---
        engine.project.status = WorkflowStatus.AWAITING_TOOL_SELECT
        # Keep existing pending intact (selected_tools, meta, script_path,
        # initiator_user_id, engine_session_key, budget, etc.).

        # --- Build card (pure rendering — no state mutation beyond status) ---
        all_tools, recommended_tools, other_tools, default_selected = (
            self._resolve_tool_lists()
        )

        resolved_project_id = (
            project.project_id if project is not None else (project_id or "")
        )
        card = self._build_tool_selection_card(
            engine=engine,
            requirement=(
                engine.project.pending.requirement if engine.project.pending else ""
            ),
            chat_id=chat_id,
            project_id=resolved_project_id,
            session_key=stored_session_key,
            all_tools=all_tools,
            recommended_tools=recommended_tools,
            other_tools=other_tools,
            default_selected=default_selected,
        )
        self.update_card(message_id, card)


    def _resolve_project_from_id(
        self, project_id: str, chat_id: str
    ) -> Optional["ProjectContext"]:
        """Resolve a ProjectContext from project_id, or return None."""
        if not project_id:
            return None
        try:
            return self.ctx.project_manager.get_project(project_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def show_workflow_status(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Show current workflow progress."""
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine:
            self.reply_text(message_id, "当前没有 Workflow 任务。")
            return

        status_text = engine.get_status_text()
        card_data = engine.get_progress_card()

        if card_data:
            from ...card import CardBuilder

            card_content = CardBuilder.build_info_card(
                title=card_data.get("header", {}).get("title", {}).get("content", "Workflow"),
                elements=card_data.get("elements", []),
            )
            self.reply_card(message_id, card_content)
        else:
            self.reply_text(message_id, status_text)

    def show_workflow_help(self, message_id: str) -> None:
        """Show workflow mode help and usage guide."""
        help_text = (
            "**🔄 Workflow 模式帮助**\n\n"
            "Workflow 模式通过 AI 编排脚本自动拆解复杂任务为多阶段、多智能体协同执行。\n\n"
            "**命令列表**:\n"
            "• `/wf <需求描述>` — AI 生成编排脚本并执行\n"
            "• `/wf <模板名> [key=value ...]` — 使用内置/保存的模板\n"
            "• `/wf_status` — 查看当前 Workflow 进度\n"
            "• `/wf_save <名称> [--global]` — 保存脚本为模板\n"
            "• `/wf_list` — 列出可用模板\n"
            "• `/wf_delete <名称> [--global]` — 删除模板\n"
            "• `/wf_history` — 查看执行历史\n"
            "• `/wf_help` — 显示本帮助\n"
            "• `/stop_wf` — 停止正在执行的 Workflow\n\n"
            "**执行流程**:\n"
            "1. Agent 选择 → 选择主编排 Agent（coco/claude/aiden 等）\n"
            "2. 工具选择 → 选择允许使用的工具（coco/claude/aiden/codex/traex 等）\n"
            "3. 预算选择 → 设置 Token 预算上限（可选）\n"
            "4. 确认执行 → 预览脚本、工具和预算，点击「确认执行」\n"
            "5. 自动执行 → 多阶段并行执行，实时进度卡片更新\n\n"
            "**特性**:\n"
            "• 多工具并行调度（coco/claude/aiden/codex/traex）\n"
            "• Journal 缓存避免重复执行\n"
            "• Token 预算控制与实时消耗监控\n"
            "• 子任务自动拆分与依赖编排"
        )
        self.reply_text(message_id, help_text)

    # ------------------------------------------------------------------
    # Template management commands
    # ------------------------------------------------------------------

    def _handle_wf_save(
        self, message_id: str, chat_id: str, args: str, project: Optional["ProjectContext"]
    ) -> None:
        """Save the last executed/pending workflow script as a named template.

        Usage: /wf_save <name> [--global]

        安全:
        - 统一调用 validate_template_name + resolve_safe_template_path 校验名称与目标路径
        - --global 仅限管理员使用（is_admin_user(sender_id, settings.admin_user_ids)）
        """
        root_path = self._get_root_path(chat_id, project)
        parts = args.strip().split() if args else []

        if not parts:
            self.reply_text(
                message_id,
                "用法: `/wf_save <模板名> [--global]`\n\n"
                "保存最近执行的 Workflow 脚本为可复用模板。\n"
                "• 默认保存到项目级 (`.ghostap/workflow_templates/`)\n"
                "• `--global` 保存为全局模板（仅限管理员）",
            )
            return

        name = parts[0]
        global_scope = "--global" in parts

        # 安全 1: 名称合法性校验
        from ...workflow_engine.templates import (
            is_admin_user,
            resolve_safe_template_path,
            save_template,
            validate_template_name,
        )

        ok, err_msg = validate_template_name(name)
        if not ok:
            self._reply_workflow_error(message_id, "invalid_argument", detail=err_msg)
            return

        # 安全 2: --global 仅限管理员
        if global_scope:
            from ...thread import get_current_sender_id

            sender_id = get_current_sender_id() or ""
            admin_ids = getattr(getattr(self.ctx, "settings", None), "admin_user_ids", None) or []
            if not is_admin_user(sender_id, admin_ids):
                self._reply_workflow_error(
                    message_id,
                    "forbidden",
                    detail="`--global` 仅允许管理员使用；普通用户可保存为项目级模板（不带 --global）",
                )
                return

        # 安全 3: 目标路径必须位于模板根目录内
        safe_path, path_err = resolve_safe_template_path(
            root_path, name, global_scope=global_scope
        )
        if safe_path is None:
            self._reply_workflow_error(message_id, "invalid_argument", detail=path_err or "模板路径不合法")
            return

        # 查找脚本内容
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)
        script_content = None

        if engine and engine.project:
            script_path = (
                (engine.project.pending.script_path if engine.project.pending else None)
                or engine.project.script_path
            )
            if script_path:
                try:
                    with open(script_path, "r", encoding="utf-8") as f:
                        script_content = f.read()
                except OSError:
                    pass

        if not script_content:
            self._reply_workflow_error(message_id, "invalid_state", detail="没有可保存的 Workflow 脚本，请先执行 `/wf` 生成脚本")
            return

        # 权限：项目级模板若已存在，仅允许原 owner 或 admin 覆盖；
        #       --global 已在前面做过 admin 检查；新建模板则允许任何用户。
        from ...thread import get_current_sender_id
        from ...workflow_engine.templates import BUILTIN_TEMPLATES, can_delete_template

        sender_id = get_current_sender_id() or ""
        admin_ids = getattr(getattr(self.ctx, "settings", None), "admin_user_ids", None) or []

        if not global_scope:
            from pathlib import Path as _Path
            from ...workflow_engine.templates import resolve_safe_template_path as _rsp

            _target_path, _ = _rsp(root_path, name, global_scope=False)
            if _target_path and _Path(_target_path).is_file() and name not in BUILTIN_TEMPLATES:
                _allowed, _reason, _ = can_delete_template(
                    root_path, name, False, None,
                    sender_id=sender_id, admin_user_ids=admin_ids,
                )
                if not _allowed:
                    self._reply_workflow_error(
                        message_id,
                        "forbidden",
                        detail=f"{_reason}，需联系模板创建者或管理员覆盖",
                    )
                    return

        try:
            # save_template 内部也会再次执行名称 + 路径校验（defense-in-depth）
            save_template(
                root_path,
                name,
                script_content,
                global_scope=global_scope,
                owner_id=sender_id or None,
            )
            scope_label = "全局级" if global_scope else "项目级"
            self.reply_text(message_id, f"✅ 模板 `{name}` 已保存（{scope_label}）\n\n使用: `/wf {name}`")
        except (OSError, ValueError) as exc:
            self.reply_text(message_id, f"保存失败: {exc}")

    def _handle_wf_list(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"]
    ) -> None:
        """List available workflow templates."""
        root_path = self._get_root_path(chat_id, project)

        from ...thread import get_current_sender_id
        from ...workflow_engine.templates import discover_templates

        sender_id = get_current_sender_id() or ""
        templates = discover_templates(root_path, user_id=sender_id)

        if not templates:
            self.reply_text(
                message_id,
                "暂无可用模板。\n\n使用 `/wf_save <名称>` 保存当前脚本为模板。",
            )
            return

        lines = ["**📋 可用 Workflow 模板**:\n"]
        for t in templates:
            scope_icon = {"project": "📂", "global": "🌐", "builtin": "📦"}.get(t.scope, "")
            desc = f" — {t.description}" if t.description else ""
            lines.append(f"• `{t.name}` {scope_icon}{desc}")

        lines.append("\n使用: `/wf <模板名> [key=value ...]`")
        self.reply_text(message_id, "\n".join(lines))

    def _handle_wf_delete(
        self, message_id: str, chat_id: str, args: str, project: Optional["ProjectContext"]
    ) -> None:
        """Delete a saved workflow template.

        Usage: /wf_delete <name> [--global]

        安全:
        - 统一调用 validate_template_name + resolve_safe_template_path
        - --global 仅限管理员
        """
        root_path = self._get_root_path(chat_id, project)
        parts = args.strip().split() if args else []

        if not parts:
            self.reply_text(message_id, "用法: `/wf_delete <模板名> [--global]`")
            return

        name = parts[0]
        global_scope = "--global" in parts

        # 安全 1: 名称合法性
        from ...workflow_engine.templates import (
            delete_template,
            is_admin_user,
            resolve_safe_template_path,
            validate_template_name,
        )

        ok, err_msg = validate_template_name(name)
        if not ok:
            self._reply_workflow_error(message_id, "invalid_argument", detail=err_msg)
            return

        # 安全 2: --global 仅限管理员
        if global_scope:
            from ...thread import get_current_sender_id

            sender_id = get_current_sender_id() or ""
            admin_ids = getattr(getattr(self.ctx, "settings", None), "admin_user_ids", None) or []
            if not is_admin_user(sender_id, admin_ids):
                self._reply_workflow_error(
                    message_id,
                    "forbidden",
                    detail="`--global` 仅允许管理员使用；普通用户请删除项目级模板（不带 --global）",
                )
                return

        # 安全 3: 目标路径必须位于模板根目录内
        safe_path, path_err = resolve_safe_template_path(
            root_path, name, global_scope=global_scope
        )
        if safe_path is None:
            self._reply_workflow_error(message_id, "invalid_argument", detail=path_err or "模板路径不合法")
            return

        # 权限：项目级模板需 owner 或 admin；全局级模板需 admin（--global 已在上方检查）
        from ...thread import get_current_sender_id
        from ...workflow_engine.templates import can_delete_template

        sender_id = get_current_sender_id() or ""
        admin_ids = getattr(getattr(self.ctx, "settings", None), "admin_user_ids", None) or []

        allowed, reason, _tp = can_delete_template(
            root_path, name, global_scope, None,
            sender_id=sender_id, admin_user_ids=admin_ids,
        )
        if not allowed:
            self.reply_text(message_id, f"删除失败: {reason}。请联系模板创建者或管理员。")
            return

        try:
            deleted = delete_template(root_path, name, global_scope=global_scope)
            if deleted:
                self.reply_text(message_id, f"✅ 模板 `{name}` 已删除。")
            else:
                self._reply_workflow_error(message_id, "invalid_state", detail=f"模板 `{name}` 不存在或无法删除")
        except (OSError, ValueError) as exc:
            self._reply_workflow_error(message_id, "internal_error", detail=f"删除失败: {exc}")

    def _handle_wf_history(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"]
    ) -> None:
        """Show workflow execution history."""
        import time as _time

        from src.workflow_engine.history import WorkflowHistory

        root_path = self._get_root_path(chat_id, project)
        history = WorkflowHistory(root_path)
        entries = history.list_recent(limit=10)

        if not entries:
            self.reply_text(
                message_id,
                "📜 **执行历史**\n\n暂无历史记录。\n\n"
                "执行 `/wf <需求>` 启动第一个 Workflow。",
            )
            return

        lines: list[str] = ["📜 **执行历史** (最近 10 次)\n"]
        for entry in entries:
            status_icon = {"completed": "✅", "failed": "❌", "running": "🔄"}.get(
                entry.status, "⏸️"
            )
            # Format timestamp
            ts = _time.strftime("%m-%d %H:%M", _time.localtime(entry.started_at))
            # Duration
            dur = ""
            if entry.finished_at and entry.started_at:
                elapsed = entry.finished_at - entry.started_at
                if elapsed < 60:
                    dur = f" {elapsed:.0f}s"
                else:
                    dur = f" {elapsed / 60:.1f}min"
            # Token count
            tokens = ""
            if entry.total_tokens > 0:
                if entry.total_tokens >= 1_000_000:
                    tokens = f" {entry.total_tokens / 1_000_000:.1f}M tok"
                elif entry.total_tokens >= 1_000:
                    tokens = f" {entry.total_tokens / 1_000:.0f}K tok"
                else:
                    tokens = f" {entry.total_tokens} tok"

            err_suffix = f" — {entry.error[:40]}" if entry.error else ""
            lines.append(
                f"{status_icon} `{entry.name}` {ts}{dur}{tokens}"
                f" ({entry.total_agents} agents){err_suffix}"
            )

        self.reply_text(message_id, "\n".join(lines))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_root_path(self, chat_id: str, project: Optional["ProjectContext"]) -> str:
        """Resolve root_path from project or chat."""
        if project:
            return project.root_path
        return self.get_working_dir(chat_id)


    def _generate_script_via_ai(
        self,
        requirement: str,
        root_path: str,
        selected_tools: list[str] | None = None,
        engine: Any = None,
        override_budget_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any] | None, bool]:
        """Generate a workflow script via AI with fallback to simple generation.

        Args:
            requirement: The user's requirement text.
            root_path: Project root path.
            selected_tools: Optional list of tools selected by the user. If provided,
                the script generator will be encouraged to use these tools.
            engine: Optional workflow engine instance. If provided, the selected
                orchestrator_agent from pending state will be used for script generation.
            override_budget_tokens: Optional budget override. If provided, uses this
                value instead of pending.budget or DEFAULT_BUDGET_TOKENS for script
                generation prompt.

        Returns:
            Tuple of (script_path, meta_dict_or_None, is_fallback).
        """
        from ...agent_session import close_session_safely, create_engine_session
        from ...workflow_engine.constants import AGENT_CALL_TIMEOUT_S, DEFAULT_BUDGET_TOKENS, DEFAULT_ORCHESTRATOR_AGENT
        from ...workflow_engine.script_gen import (
            build_script_gen_prompt,
            extract_meta_from_script,
            validate_generated_script,
        )

        # Resolve agent type: use pending.orchestrator_agent if available, otherwise default
        agent_type = (
            engine.project.pending.orchestrator_agent
            if engine and engine.project and engine.project.pending and engine.project.pending.orchestrator_agent
            else DEFAULT_ORCHESTRATOR_AGENT
        )

        # Resolve budget tokens: use override if provided, otherwise use pending.selected_budget (or legacy budget) or default
        from ...workflow_engine.constants import is_valid_budget

        budget_for_gen = override_budget_tokens
        if budget_for_gen is None and engine and engine.project and engine.project.pending:
            if getattr(engine.project.pending, "selected_budget", None) is not None:
                budget_for_gen = engine.project.pending.selected_budget
            else:
                budget_for_gen = engine.project.pending.budget
        if not is_valid_budget(budget_for_gen):
            logger.warning(
                "_generate_script_via_ai: budget %r is not a valid tier; falling back to DEFAULT_BUDGET_TOKENS=%d",
                budget_for_gen,
                DEFAULT_BUDGET_TOKENS,
            )
            budget_for_gen = DEFAULT_BUDGET_TOKENS

        script_dir = os.path.join(root_path, ".ghostap", "workflow_scripts")
        os.makedirs(script_dir, exist_ok=True)
        script_path = os.path.join(script_dir, "generated_workflow.js")

        # Resolve available tools via dynamic registry
        from ...workflow_engine.tool_registry import get_available_tools

        available_tools = get_available_tools()
        if not available_tools:
            available_tools = {"coco": "全栈编程·支持 subagent"}

        # Filter available tools to selected ones if provided
        if selected_tools:
            available_tools = {
                k: v for k, v in available_tools.items()
                if k in selected_tools
            } or available_tools

        available_roles = [
            "architect", "security_auditor", "correctness_auditor",
            "adversarial_verifier", "code_quality_reviewer", "bug_hunter",
            "migration_validator", "compatibility_reviewer",
        ]

        prompt = build_script_gen_prompt(
            requirement=requirement,
            available_tools=available_tools,
            available_roles=available_roles,
            budget_total=DEFAULT_BUDGET_TOKENS,
            budget_tokens=budget_for_gen,
            orchestrator_agent=agent_type,
        )

        # Attempt AI generation via one-shot ACP session
        session = None
        try:
            session = create_engine_session(
                agent_type=agent_type,
                cwd=root_path,
                thread_id="workflow_script_gen",
                auto_approve=True,
            )
            if session is None:
                logger.warning("Failed to create script-gen session; using fallback")
                return self._write_fallback_script(script_path, requirement), None, True

            result = session.send_prompt(prompt, timeout=AGENT_CALL_TIMEOUT_S)

            if result and result.text:
                script_content = self._strip_markdown_fences(result.text.strip())

                is_valid, errors = validate_generated_script(script_content)
                if is_valid:
                    with open(script_path, "w", encoding="utf-8") as f:
                        f.write(script_content)
                    meta = extract_meta_from_script(script_content)
                    # Record the budget used to generate this script
                    if meta is None:
                        meta = {}
                    meta["budget_tokens"] = budget_for_gen
                    return script_path, meta, False
                else:
                    logger.warning("Generated script failed validation: %s", errors)
            else:
                logger.warning("AI returned empty script content")

        except Exception as exc:
            logger.error("Script generation via AI failed: %s", exc, exc_info=True)
        finally:
            if session is not None:
                close_session_safely(session)

        # Fallback
        return self._write_fallback_script(script_path, requirement), None, True

    @staticmethod
    def _strip_markdown_fences(content: str) -> str:
        """Remove markdown code fences from AI output if present."""
        if content.startswith("```"):
            lines = content.split("\n", 1)
            content = lines[1] if len(lines) > 1 else content
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3].rstrip()
        return content

    @staticmethod
    def _read_pending_script(engine: Any) -> str:
        """Read script content from pending.script_path for confirm card preview."""
        path = engine.project.pending.script_path if (engine.project and engine.project.pending) else None
        if not path:
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    @staticmethod
    def _write_fallback_script(script_path: str, requirement: str) -> str:
        """Write a simple fallback script and return its path."""
        from ...workflow_engine.script_gen import generate_simple_script

        script_content = generate_simple_script(requirement)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)
        return script_path

    def _build_confirm_card(
        self,
        meta: dict[str, Any] | None,
        requirement: str,
        engine_session_key: str,
        chat_id: str,
        project_id: str,
        is_fallback: bool = False,
        selected_tools: list[str] | None = None,
        selected_budget: int | None = None,
        script_content: str = "",
    ) -> dict:
        """Build a Feishu card showing the workflow script preview for confirmation.

        Returns a Feishu card JSON dict ready for reply_card/send_card_to_chat.
        """
        from ...card import CardBuilder
        from ...card.actions.dispatch import (
            WORKFLOW_CANCEL,
            WORKFLOW_CONFIRM_START,
            WORKFLOW_SELECT_BUDGET,
            WORKFLOW_SELECT_TOOL,
        )
        from ...card.render.budget import RenderBudget
        from ...card.ui_text import UI_TEXT
        from ...workflow_engine.constants import BUDGET_OPTIONS, DEFAULT_BUDGET_TOKENS, is_valid_budget
        from ...workflow_engine.tool_registry import get_available_tools

        # Extract meta info
        script_name = (meta or {}).get("name", "generated-workflow")
        description = (meta or {}).get("description", requirement[:100])
        phases = (meta or {}).get("phases", [])
        tools = (meta or {}).get("tools", ["coco"])
        phase_tool_mapping: dict = (meta or {}).get("phase_tool_mapping", {})
        workflow_refs = (meta or {}).get("workflow_refs", [])

        # Pre-compute has_mismatch for action button state (used in both modes)
        allowed_tools = set(selected_tools) if selected_tools else set(tools)
        script_tools = set(tools)
        has_mismatch = bool(script_tools - allowed_tools)

        # --- Node budget pre-check ---
        # Estimate element count and apply truncation if needed
        estimated_nodes = 0
        estimated_nodes += 5  # requirement, meta, hr, phases header, workflow refs
        if phases:
            estimated_nodes += len(phases)
        if script_content:
            estimated_nodes += 2  # script preview header + content
        if selected_tools:
            estimated_nodes += len(selected_tools)
        estimated_nodes += 10  # budget buttons, action buttons, etc.

        use_truncated_mode = estimated_nodes > RenderBudget.NODE_BUDGET * 0.8

        # Build elements
        elements: list[dict] = []

        if use_truncated_mode:
            # --- Truncated overview mode ---
            if is_fallback:
                elements.append({
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "⚠️ AI 脚本生成失败，已使用默认模板。结果可能不完全匹配需求。"},
                    ],
                })

            elements.append({
                "tag": "markdown",
                "content": f"**需求**:\n> {requirement[:300]}{'...' if len(requirement) > 300 else ''}",
            })

            # Quick stats in column_set
            phase_count = len(phases)
            tool_count = len(selected_tools) if selected_tools else len(tools)
            budget = selected_budget if selected_budget is not None else DEFAULT_BUDGET_TOKENS
            budget_display = f"{budget // 1_000_000}M" if budget >= 1_000_000 else f"{budget // 1000}K"

            elements.append({
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {"tag": "markdown", "content": f"**{phase_count}**\n<font color='grey'>阶段数</font>"},
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {"tag": "markdown", "content": f"**{tool_count}**\n<font color='grey'>工具数</font>"},
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {"tag": "markdown", "content": f"**{budget_display}**\n<font color='grey'>Token 预算</font>"},
                        ],
                    },
                ],
            })

            elements.append({"tag": "hr"})

            # Collapsible panel with full details
            full_details_elements = []

            # Full requirement
            full_details_elements.append({
                "tag": "markdown",
                "content": f"**完整需求**:\n> {requirement}",
            })

            # Full phases
            if phases:
                phase_text = "**阶段列表**:\n"
                for i, p in enumerate(phases, 1):
                    title = p.get("title", p.get("name", f"Phase {i}"))
                    phase_text += f"{i}. {title}\n"
                full_details_elements.append({"tag": "markdown", "content": phase_text})

            # Full tools
            allowed_tools = set(selected_tools) if selected_tools else set(tools)
            if allowed_tools:
                tools_text = "**允许使用的工具**:\n"
                for tool in sorted(allowed_tools):
                    tools_text += f"- `{tool}`\n"
                full_details_elements.append({"tag": "markdown", "content": tools_text})

            # Full script preview
            if script_content:
                preview = script_content[:3000] + ("\n// ..." if len(script_content) > 3000 else "")
                full_details_elements.append({
                    "tag": "markdown",
                    "content": f"**编排脚本**:\n```javascript\n{preview}\n```",
                })

            elements.append({
                "tag": "collapsible_panel",
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "📂 查看完整详情",
                    },
                    "template": "blue",
                },
                "expanded": False,  # 折叠后默认收起，让截断卡首屏只展示摘要
                "elements": full_details_elements,
            })

            elements.append({"tag": "hr"})

        else:
            # --- Normal mode ---
            # Header info section
            if is_fallback:
                elements.append({
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "⚠️ AI 脚本生成失败，已使用默认模板。结果可能不完全匹配需求。"},
                    ],
                })

            # Requirement in quoted block for visual distinction
            elements.append({
                "tag": "markdown",
                "content": f"**需求**:\n> {requirement[:200]}",
            })
            elements.append({
                "tag": "markdown",
                "content": (
                    f"**脚本名称**: `{script_name}`\n"
                    f"**描述**: {description}"
                ),
            })

            # Divider
            elements.append({"tag": "hr"})

            # Phase list — always expanded so users can see what
            # the script will do without further interaction.
            if phases:
                phase_elements = []
                for i, p in enumerate(phases, 1):
                    title = p.get("title", p.get("name", f"Phase {i}"))
                    detail = p.get("detail", "")
                    line = f"**{i}. {title}**"
                    if detail:
                        line += f"\n   {detail[:100]}"
                    # Append tool tags from phase_tool_mapping
                    phase_tools = phase_tool_mapping.get(title) or phase_tool_mapping.get(str(i))
                    if phase_tools:
                        tool_tags = ", ".join(f"`{t}`" for t in phase_tools)
                        line += f"\n   工具: {tool_tags}"
                    phase_elements.append({
                        "tag": "markdown",
                        "content": line,
                    })
                elements.append({
                    "tag": "collapsible_panel",
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"📋 阶段列表 ({len(phases)} 个阶段)",
                        },
                        "template": "blue",
                    },
                    "expanded": True,  # Default expanded so users can see what will execute
                    "elements": phase_elements,
                })
            else:
                elements.append({
                    "tag": "markdown",
                    "content": "📋 **执行阶段**: Planning → Execution",
                })

            # Workflow refs (sub-workflow calls) display
            if workflow_refs:
                ref_lines = []
                for ref in workflow_refs:
                    if isinstance(ref, dict):
                        ref_name = ref.get("name", "unknown")
                        # Normalize: read "path" with legacy fallback to "script_path"
                        ref_path = ref.get("path", ref.get("script_path", ""))
                    else:
                        ref_name = str(ref)
                        ref_path = ""
                    line = f"• `{ref_name}`"
                    if ref_path:
                        line += f" ({ref_path})"
                    ref_lines.append(line)
                elements.append({
                    "tag": "markdown",
                    "content": "🔗 **子 Workflow 引用**:\n" + "\n".join(ref_lines),
                })

            # Script preview section (collapsible code block) — collapsed by default
            if script_content:
                from ...workflow_engine.renderer import render_script_preview

                preview = render_script_preview(script_content)
                if preview:
                    elements.append({"tag": "hr"})
                    elements.append({
                        "tag": "collapsible_panel",
                        "expanded": False,
                        "header": {
                            "title": {
                                "tag": "plain_text",
                                "content": "📜 编排脚本预览",
                            },
                            "template": "grey",
                        },
                        "vertical_spacing": "8px",
                        "elements": [
                            {"tag": "markdown", "content": preview},
                        ],
                    })

            # Tools section — split into tier1 (recommended) and tier2 (other)
            tool_descriptions = get_available_tools()
            all_tool_names = list(tool_descriptions.keys())

            # Check for mismatch — highlight which tools are missing so users
            # can decide to fill-in automatically, return to tool selection, or
            # regenerate the script from scratch.
            if has_mismatch:
                missing = sorted(script_tools - allowed_tools)
                missing_display = ", ".join(f"`{m}`" for m in missing)
                elements.append({
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"⚠️ 脚本需要这些工具但尚未启用: {missing_display}。请点击下方的“一键补齐缺失工具”，或“返回工具选择”手动调整。",
                        },
                    ],
                })

            # Section 1: Script planned tools (what the script intends to use)
            planned_display = " | ".join(
                f"`{t}`" for t in sorted(script_tools)
            )
            elements.append({
                "tag": "markdown",
                "content": f"📝 **脚本计划使用**: {planned_display}",
            })

            # Section 2: Allowed tools (user-selected whitelist) — interactive
            elements.append({
                "tag": "markdown",
                "content": "✅ **允许执行的工具**（点击切换，脚本只能使用勾选的工具）：",
            })

            # Split into tier1 (recommended) and tier2 (other) based on recommended_order
            recommended_order = ["coco", "claude", "codex", "aiden", "gemini", "traex", "ttadk"]
            # Tier 1: recommended tools that are in selected_tools
            tier1_tools = [t for t in recommended_order if t in allowed_tools]
            # Tier 2: other selected tools not in recommended_order
            tier2_tools = [t for t in sorted(allowed_tools) if t not in recommended_order]

            # Tier 1 tools — always visible
            if tier1_tools:
                tools_text = ""
                for tool in tier1_tools:
                    desc = tool_descriptions.get(tool, tool)
                    tools_text += f"- `{tool}`: {desc}\n"
                elements.append({"tag": "markdown", "content": tools_text})

                # Tier 1 interactive buttons
                tool_buttons = []
                for t in tier1_tools:
                    is_selected = t in allowed_tools
                    btn_value = {
                        "action": WORKFLOW_SELECT_TOOL,
                        "tool_name": t,
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "engine_session_key": engine_session_key,
                    }
                    tool_buttons.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"{'✓ ' if is_selected else '○ '}{t}"},
                        "type": "primary" if is_selected else "default",
                        "value": btn_value,
                        "behaviors": [{"type": "callback", "value": btn_value}],
                    })
                if tool_buttons:
                    elements.extend(build_responsive_button_row(tool_buttons, mobile_force_vertical=True))

            # Tier 2 tools — behind "更多工具" collapsible panel
            if tier2_tools:
                tier2_elements = []
                for tool in tier2_tools:
                    desc = tool_descriptions.get(tool, tool)
                    tier2_elements.append({
                        "tag": "markdown",
                        "content": f"- `{tool}`: {desc}",
                    })

                # Tier 2 interactive buttons
                other_buttons = []
                for t in tier2_tools:
                    is_selected = t in allowed_tools
                    btn_value = {
                        "action": WORKFLOW_SELECT_TOOL,
                        "tool_name": t,
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "engine_session_key": engine_session_key,
                    }
                    other_buttons.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"{'✓ ' if is_selected else '○ '}{t}"},
                        "type": "primary" if is_selected else "default",
                        "value": btn_value,
                        "behaviors": [{"type": "callback", "value": btn_value}],
                    })

                elements.append({
                    "tag": "collapsible_panel",
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"🔧 更多工具 ({len(tier2_tools)} 个)",
                        },
                        "template": "grey",
                    },
                    "expanded": False,
                    "elements": [
                        *tier2_elements,
                        *build_responsive_button_row(other_buttons, mobile_force_vertical=True),
                    ],
                })

        # Budget selection buttons (shown in both modes)
        budget = selected_budget if selected_budget is not None else DEFAULT_BUDGET_TOKENS
        budget_display = f"{budget // 1_000_000}M" if budget >= 1_000_000 else f"{budget // 1000}K"
        elements.append({
            "tag": "markdown",
            "content": f"💰 **Token 预算**: {budget_display} tokens",
        })
        budget_buttons = []
        for label, value_tokens in BUDGET_OPTIONS:
            is_active = value_tokens == budget
            btn_value = {
                "action": WORKFLOW_SELECT_BUDGET,
                "budget_tokens": value_tokens,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": engine_session_key,
            }
            budget_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"{'● ' if is_active else '○ '}{label}"},
                "type": "primary" if is_active else "default",
                "value": btn_value,
                "behaviors": [{"type": "callback", "value": btn_value}],
            })
        if budget_buttons:
            elements.extend(build_responsive_button_row(budget_buttons, mobile_force_vertical=True))

        # Add budget generation notice
        if selected_budget is not None:
            _raw_meta_budget = (meta or {}).get("budget_tokens")
            current_budget_tokens = (
                _raw_meta_budget if is_valid_budget(_raw_meta_budget) else DEFAULT_BUDGET_TOKENS
            )
            elements.append({
                "tag": "markdown",
                "content": f"💡 **当前脚本按 {current_budget_tokens:,} tokens 预算生成**（本次选择 {selected_budget:,} tokens）。如需调整预算后重新生成，请点击下方按钮。",
            })

        # Divider before buttons
        elements.append({"tag": "hr"})

        from ...card.actions.dispatch import WORKFLOW_REGENERATE_SCRIPT, WORKFLOW_APPLY_BUDGET_REGENERATE, WORKFLOW_FILL_MISSING_TOOLS, WORKFLOW_BACK_TO_TOOLS
        from ...workflow_engine.constants import is_valid_budget

        # Apply-budget-and-regenerate button value.
        # Security note: ``confirmed`` is intentionally NOT included in the
        # button payload. The server-side ``pending.armed_for_regen`` flag is
        # the authoritative gate: first click arms it, second click runs the
        # AI generation. A Feishu native confirm pop-up is still shown for UX
        # but the resulting callback payload is not trusted.
        apply_regen_value = {
            "action": WORKFLOW_APPLY_BUDGET_REGENERATE,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": engine_session_key,
        }
        current_budget_for_popup = (meta or {}).get("budget_tokens")
        if not is_valid_budget(current_budget_for_popup):
            current_budget_for_popup = DEFAULT_BUDGET_TOKENS
        new_budget_for_popup = selected_budget if selected_budget is not None else current_budget_for_popup
        estimated_tokens = int(new_budget_for_popup * 1.2)  # rough estimate including prompt overhead

        # --- 更多操作 (collapsible): regenerate / fill / back-to-tools ---
        # These are secondary actions — hidden under a collapsible panel so
        # that "确认执行 / 取消" remain the primary visual focus at the bottom.
        advanced_buttons: list[dict] = []

        advanced_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 重新生成编排"},
            "type": "default",
            "value": {
                "action": WORKFLOW_REGENERATE_SCRIPT,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": engine_session_key,
            },
            "behaviors": [{
                "type": "callback",
                "value": {
                    "action": WORKFLOW_REGENERATE_SCRIPT,
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "engine_session_key": engine_session_key,
                },
            }],
        })

        advanced_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "💰 应用预算并重新生成"},
            "type": "default",
            "value": apply_regen_value,
            "behaviors": [{"type": "callback", "value": apply_regen_value}],
            "confirm": {
                "title": {"tag": "plain_text", "content": "确认使用新预算重新生成编排脚本？"},
                "text": {
                    "tag": "plain_text",
                    "content": f"当前脚本预算: {current_budget_for_popup:,} tokens\n新预算: {new_budget_for_popup:,} tokens\n预估消耗: 约 {estimated_tokens:,} tokens\n\n点击「确定」将重新调用 AI 生成编排脚本。",
                },
            },
        })

        # Mismatch-specific action buttons: one-click fill & back-to-tools
        if has_mismatch:
            fill_missing_value = {
                "action": WORKFLOW_FILL_MISSING_TOOLS,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": engine_session_key,
            }
            back_tools_value = {
                "action": WORKFLOW_BACK_TO_TOOLS,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": engine_session_key,
            }
            advanced_buttons.extend([
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "➕ 一键补齐缺失工具"},
                    "type": "default",
                    "value": fill_missing_value,
                    "behaviors": [{"type": "callback", "value": fill_missing_value}],
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "↩️ 返回工具选择"},
                    "type": "default",
                    "value": back_tools_value,
                    "behaviors": [{"type": "callback", "value": back_tools_value}],
                },
            ])

        # Render advanced-actions collapsible panel if there are any buttons
        if advanced_buttons:
            elements.append({
                "tag": "collapsible_panel",
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"⚙️ 更多操作 ({len(advanced_buttons)})",
                    },
                    "template": "grey",
                },
                "expanded": False,
                "elements": build_responsive_button_row(advanced_buttons, mobile_force_vertical=True),
            })

        # --- Primary action buttons (bottom of card): 确认执行 + 取消 ---
        # Determine confirm button state based on mismatch
        confirm_disabled = has_mismatch
        confirm_disabled_tips = (
            "脚本需要的工具尚未全部启用，请先点击『一键补齐缺失工具』或『返回工具选择』"
            if confirm_disabled
            else None
        )

        confirm_value = {
            "action": WORKFLOW_CONFIRM_START,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": engine_session_key,
        }
        cancel_value = {
            "action": WORKFLOW_CANCEL,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": engine_session_key,
        }

        primary_buttons: list[dict] = []

        confirm_btn: dict = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✅ 确认执行"},
            "type": "primary" if not confirm_disabled else "default",
            "value": confirm_value,
            "behaviors": [{"type": "callback", "value": confirm_value}],
            "disabled": confirm_disabled,
        }
        if confirm_disabled_tips:
            confirm_btn["disabled_tips"] = {
                "tag": "plain_text",
                "content": confirm_disabled_tips,
            }
        primary_buttons.append(confirm_btn)

        primary_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "❌ 取消"},
            "type": "danger",
            "value": cancel_value,
            "behaviors": [{"type": "callback", "value": cancel_value}],
            "confirm": {
                "title": {"tag": "plain_text", "content": UI_TEXT["workflow_btn_confirm_cancel_title"]},
                "text": {"tag": "plain_text", "content": UI_TEXT["workflow_btn_confirm_cancel_body"]},
            },
        })

        elements.extend(build_responsive_button_row(primary_buttons, mobile_force_vertical=True))

        return CardBuilder._wrap_card(
            header_title="🔄 Workflow 确认",
            header_template=UI_TEXT["workflow_header_colors"].get("confirm", "turquoise"),
            elements=elements,
        )

    @staticmethod
    def _parse_template_args(args_text: str) -> dict[str, Any]:
        """Parse 'key=value key2=value2' into a dict."""
        args: dict[str, Any] = {}
        for token in args_text.split():
            if "=" in token:
                key, _, value = token.partition("=")
                # Try to parse as JSON literal (number, bool, null)
                import json

                try:
                    args[key] = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    args[key] = value
            else:
                # Positional arg → store as "target"
                args.setdefault("target", token)
        return args

    def _build_workflow_callbacks(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
    ):
        """Build WorkflowEngineCallbacks that update the Feishu card."""
        from ...workflow_engine.engine import WorkflowEngineCallbacks

        card_message_id: list[str] = [message_id]  # Mutable ref for card updates

        def on_progress(card_data: dict[str, Any]) -> None:
            """Update the progress card in Feishu."""
            try:
                from ...card import CardBuilder

                elements = card_data.get("elements", [])
                header = card_data.get("header", {})
                title = header.get("title", {}).get("content", "Workflow")
                template = header.get("template", "blue")

                card_content = CardBuilder.build_info_card(
                    title=title,
                    elements=elements,
                    template=template,
                )
                if not self.update_card(card_message_id[0], card_content):
                    # If update fails, send a new card
                    new_id = self.send_card_to_chat(chat_id, card_content)
                    if new_id:
                        card_message_id[0] = new_id
            except Exception:
                logger.debug("Failed to update workflow progress card", exc_info=True)

        def on_done(wf_project) -> None:
            """Final completion — send a structured completion card."""
            try:
                from ...card import CardBuilder
                from ...workflow_engine.renderer import render_completion_card

                card_data = render_completion_card(wf_project)
                elements = card_data.get("elements", [])
                header = card_data.get("header", {})
                title = header.get("title", {}).get("content", "Workflow 完成")
                template = header.get("template", "green")

                card_content = CardBuilder.build_info_card(
                    title=title,
                    elements=elements,
                    template=template,
                )
                if not self.update_card(card_message_id[0], card_content):
                    self.send_card_to_chat(chat_id, card_content)
            except Exception:
                # Fallback to text if card rendering fails
                result = wf_project.result or ""
                summary = result[:500] if result else "Workflow completed."
                self.reply_text(message_id, f"✅ Workflow 完成\n\n{summary}")

        def on_error(error_msg: str) -> None:
            """Error notification — sanitize before showing to user."""
            from ...workflow_engine.errors import ErrorCategory, sanitize_for_reply

            # Classify the error for user-facing message selection
            lower = (error_msg or "").lower()
            if "budget" in lower or "exhausted" in lower:
                category = ErrorCategory.BUDGET_EXHAUSTED
            elif "limit exceeded" in lower:
                category = ErrorCategory.AGENT_LIMIT
            elif "timeout" in lower:
                category = ErrorCategory.RUNTIME_TIMEOUT
            elif "cancelled" in lower or "canceled" in lower:
                category = ErrorCategory.CANCELLED
            elif "not in allowed" in lower:
                category = ErrorCategory.TOOL_NOT_ALLOWED
            else:
                category = ErrorCategory.INTERNAL_ERROR

            safe_msg = sanitize_for_reply(error_msg, category)
            self.reply_error(message_id, safe_msg, title="Workflow 失败")

        def on_log(msg: str) -> None:
            logger.debug("[WorkflowHandler] log: %s", msg)

        return WorkflowEngineCallbacks(
            on_progress=on_progress,
            on_done=on_done,
            on_error=on_error,
            on_log=on_log,
        )
