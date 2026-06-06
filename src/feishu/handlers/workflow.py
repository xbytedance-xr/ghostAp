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

    def _build_workflow_stepper(self, current: int, total: int = 4) -> dict[str, Any]:
        """Build a stepper element for the four-step orchestration flow
        (agent → tools → roles → confirm/execution)."""
        from ...card.ui_text import UI_TEXT

        steps = [
            UI_TEXT["workflow_stepper_step_agent"],
            UI_TEXT["workflow_stepper_step_tool"],
            UI_TEXT["workflow_stepper_step_role"],
            UI_TEXT["workflow_stepper_step_confirm"],
        ]
        clamped = max(1, min(total, current))
        lines = []
        for idx, label in enumerate(steps, start=1):
            if idx < clamped:
                lines.append(f"✓ {idx}. ~~{label}~~")
            elif idx == clamped:
                lines.append(f"▶ {idx}. **{label}**")
            else:
                lines.append(f"○ {idx}. {label}")
        header = UI_TEXT["workflow_stepper_current_label"].format(current=clamped, total=total)
        return {
            "tag": "markdown",
            "content": f"**{header}**\n" + "\n".join(lines),
        }

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
        """Show the Workflow entry help card when user sends `/wf` without arguments.

        The primary "开始编排任务" button carries an empty `requirement` field
        so that its handler can prompt the user to type a task description inline
        before launching the three-step orchestration flow. The previous dead-end
        behavior (sending users back to the chat to type `/wf <需求>`) has been
        replaced with an inline requirement entry.
        """
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
            "requirement": "",
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

    def _show_requirement_input_card(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        root_path: str,
    ) -> None:
        """Prompt the user to enter a Workflow task description.

        Unlike earlier versions (which only printed guidance text), we
        now bind the topic to ``workflow`` mode so the user's very next
        free-text message is routed directly to :meth:`start_workflow`.
        We also emit an ``entry-cancel`` session key so the cancel
        handler can distinguish this prompt from a real pending session
        and update the card instead of reporting ``session_expired``.
        """
        from ...card import CardBuilder
        from ...card.actions.dispatch import WORKFLOW_CANCEL
        from ...card.render.buttons import build_responsive_button_row
        from ...thread import get_current_sender_id

        # Ensure a project exists for the topic binding
        if project is None:
            project = self._ensure_project(message_id, chat_id, None)
            if project is None:
                self._reply_workflow_error(message_id, "invalid_argument")
                return

        # Bind topic engine context so the *next* free-text message in
        # this chat is routed through WorkflowHandler.handle_message.
        # Users can still cancel the entry flow through the cancel
        # button below; the topic remains bound so subsequent messages
        # keep going through the workflow handler.
        self._ensure_topic_engine_context(
            mode="workflow",
            message_id=message_id,
            chat_id=chat_id,
            project=project,
        )

        project_id = project.project_id if project else ""

        elements: list[dict[str, Any]] = []
        elements.append({
            "tag": "markdown",
            "content": (
                "**请在下方聊天框中直接输入你想让 Workflow 完成的任务描述**\n\n"
                "本聊天已切换到 Workflow 编排模式，下一条消息会被当作需求描述自动进入三步编排流程。\n\n"
                "示例：\n"
                "• `审查 src/handlers 目录中的错误处理一致性`\n"
                "• `为 feature-checkout 分支生成端到端集成测试`\n\n"
                "也可以直接发送：\n`/wf <你的任务描述>`"
            ),
        })
        elements.append({"tag": "hr"})

        cancel_value = {
            "action": WORKFLOW_CANCEL,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": "entry-cancel",
            "initiator_user_id": get_current_sender_id() or "",
        }
        cancel_btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消"},
            "type": "default",
            "value": cancel_value,
            "behaviors": [{"type": "callback", "value": cancel_value}],
        }
        elements.extend(build_responsive_button_row([cancel_btn], mobile_force_vertical=True))

        card = CardBuilder._wrap_card(
            header_title="Workflow — 输入任务描述",
            header_template="blue",
            elements=elements,
        )
        self.reply_card(message_id, card)

    def handle_show_workflow_menu(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Handle entry card "开始编排任务" button — launch the inline
        requirement-entry flow which then leads into three-step orchestration.

        When no task description is attached (the usual entry-card state), we
        prompt the user to enter a description inline via a structured input
        card. When a task description is already provided, we proceed directly
        to agent selection. Users can still skip the entry card entirely by
        typing ``/wf <描述>`` directly in chat.
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

        # Check if requirement is already provided
        has_requirement = bool(value and isinstance(value, dict) and (value.get("requirement") or "").strip())
        if not has_requirement:
            # Prompt user to enter requirement inline via structured input
            self._show_requirement_input_card(
                message_id=message_id,
                chat_id=chat_id,
                project=project,
                root_path=root_path,
            )
            return

        requirement = (value.get("requirement") or "").strip()

        self._show_agent_selection_card(
            chat_id=chat_id,
            requirement=requirement,
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
            self._reply_workflow_error(
                message_id,
                "invalid_state",
                detail="当前项目已有 Workflow 任务在执行中。发送 `/wf_status` 查看进度，或 `/stop_wf` 停止任务。",
            )
            return

        # Also block if awaiting confirmation, tool selection, or agent selection
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

        # Check Node.js availability
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

        The supplied *detail* is always passed through the shared sanitizer
        (``sanitize_for_reply``) so that file paths, tracebacks, and internal
        module names never leak to the user. Only the sanitized user-facing
        message is rendered; raw details are logged but never shown.

        Args:
            category: One of "session_expired", "invalid_state", "invalid_argument", "forbidden", "internal_error"
            detail: Optional raw detail string — sanitized before rendering.
        """
        from ...card import CardBuilder
        from ...card.ui_text import UI_TEXT
        from ...workflow_engine.errors import (
            ErrorCategory,
            _strip_internal_details,
        )

        title_key = f"workflow_error_{category}_title"
        body_key = f"workflow_error_{category}_body"

        title = UI_TEXT.get(title_key, "操作失败")
        # Strip anything that looks like an internal traceback / file path
        # from the raw *detail* before rendering.  We intentionally do NOT
        # use ``sanitize_for_reply`` here - that helper already wraps the
        # message in a category-specific template, which would collide with
        # the UI_TEXT body template below and produce a duplicated prefix.
        safe_detail = _strip_internal_details(detail or "")

        raw_body = UI_TEXT.get(body_key, "")
        if raw_body and "{detail}" in raw_body:
            # Don't use format() — avoid unexpected kwargs when raw_body has
            # other placeholders. Do a simple literal replace instead.
            body = raw_body.replace("{detail}", safe_detail)
        elif raw_body:
            body = raw_body
        else:
            body = safe_detail or "操作失败，请稍后重试。"

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
        from ...workflow_engine.constants import DEFAULT_BUDGET_TOKENS
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
                selected_budget=DEFAULT_BUDGET_TOKENS,
                budget=DEFAULT_BUDGET_TOKENS,
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

        # --- Stepper ---
        elements.append(self._build_workflow_stepper(current=2))

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

        # --- Budget selection (折叠在可展开面板中，默认收起，显示当前值) ---
        # 用户在工具选择阶段即可确定预算档位；后续 script 生成与执行
        # 都使用这里选中的预算。与确认卡中的 budget 选择保持一致。
        from ...card.actions.dispatch import WORKFLOW_SELECT_BUDGET
        from ...workflow_engine.constants import BUDGET_OPTIONS, DEFAULT_BUDGET_TOKENS

        current_budget = (
            engine.project.pending.selected_budget
            if engine.project and engine.project.pending
            else DEFAULT_BUDGET_TOKENS
        )
        current_budget_label = next(
            (label for label, tokens in BUDGET_OPTIONS if tokens == current_budget),
            f"{current_budget} tokens",
        )

        budget_buttons = []
        for label, budget_tokens in BUDGET_OPTIONS:
            is_active = budget_tokens == current_budget
            value = {
                "action": WORKFLOW_SELECT_BUDGET,
                "budget_tokens": budget_tokens,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": session_key,
            }
            budget_buttons.append({
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": ("[✓] " if is_active else "[  ] ") + label,
                },
                "type": "primary" if is_active else "default",
                "value": value,
                "behaviors": [{"type": "callback", "value": value}],
            })
        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"💰 预算档位：{current_budget_label}",
                },
            },
            "vertical_spacing": "8px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": "选择一个预算档位（不同档位影响 AI 生成深度与允许的 agent 数）:",
                },
                *build_responsive_button_row(budget_buttons, mobile_force_vertical=True),
            ],
        })

        elements.append({"tag": "hr"})

        # --- Action buttons: Cancel + Confirm Tools ---
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

        # Build agent selection card — recommended agent on top with full
        # details; other agents collapsed to single-line entries with their
        # own short descriptors. Each agent still has its own CTA so the
        # selection flow remains a single tap.
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

        # --- Stepper (vertical layout; current = 1) ---
        elements.append(self._build_workflow_stepper(current=1))

        # Requirement — must be present before we ever reach this card.
        req_display = requirement.strip() or '（任务描述缺失，请重新发送 "/wf <你的需求>"）'
        elements.append({
            "tag": "markdown",
            "content": f"**需求**：{req_display[:200]}",
        })
        elements.append({
            "tag": "markdown",
            "content": "**请选择主编排 Agent**（点击下方任一 Agent 即进入下一步）：",
        })

        # --- Split: recommended agent on top, rest collapsed ---
        recommended_type = DEFAULT_ORCHESTRATOR_AGENT
        other_options = [(t, name, short) for (t, name, short) in ORCHESTRATOR_AGENT_OPTIONS if t != recommended_type]

        def _agent_btn_value(agent_type: str) -> dict[str, Any]:
            return {
                "action": WORKFLOW_SELECT_AGENT,
                "agent_type": agent_type,
                "engine_session_key": session_key,
                "project_id": project_id,
                "chat_id": chat_id,
                "root_id": root_path,
            }

        # Recommended agent — full mini-card + prominent CTA.
        for agent_type, display_name, _short_desc in ORCHESTRATOR_AGENT_OPTIONS:
            if agent_type != recommended_type:
                continue
            summary, subagent_affinity, scenarios = agent_meta.get(
                agent_type,
                ("通用 Agent", "可调度 subagent", "一般任务"),
            )
            elements.append({
                "tag": "markdown",
                "content": (
                    f"★ **{display_name}**（默认推荐）\n"
                    f"> **定位**: {summary}\n"
                    f"> **Subagent 能力**: {subagent_affinity}\n"
                    f"> **适用场景**: {scenarios}"
                ),
            })
            btn_value = _agent_btn_value(agent_type)
            cta = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"使用 {display_name}"},
                "type": "primary",
                "value": btn_value,
                "behaviors": [{"type": "callback", "value": btn_value}],
            }
            elements.extend(build_responsive_button_row([cta], mobile_force_vertical=True))

        # Other agents — collapsed list. Each entry is a single line with
        # the name, a short position tag, and an inline button to pick it.
        if other_options:
            elements.append({
                "tag": "markdown",
                "content": "**其他 Agent**（点击按钮选择）：",
            })

            # Show top 3 others with brief one-line cards to keep the card
            # short on mobile; the rest are surfaced through a single note
            # with names so users do not miss them.
            short_map: dict[str, str] = {
                "claude": "深度推理 / 复杂任务",
                "aiden": "代码审查 / 架构",
                "codex": "OpenAI / 标准编程",
                "gemini": "多模态 / 文档+图像",
                "traex": "高并发 / 轻量",
            }
            shown = other_options[:3]
            remaining = other_options[3:]

            for agent_type, display_name, _short in shown:
                tag_line = short_map.get(agent_type, "通用 Agent")
                elements.append({
                    "tag": "markdown",
                    "content": f"• **{display_name}** — {tag_line}",
                })
                btn_value = _agent_btn_value(agent_type)
                cta = {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"选择 {display_name}"},
                    "type": "default",
                    "value": btn_value,
                    "behaviors": [{"type": "callback", "value": btn_value}],
                }
                elements.extend(build_responsive_button_row([cta], mobile_force_vertical=True))

            if remaining:
                rest_names = "，".join(name for (_, name, _) in remaining)
                remaining_btns = []
                for agent_type, display_name, _short in remaining:
                    btn_value = _agent_btn_value(agent_type)
                    remaining_btns.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"选择 {display_name}"},
                        "type": "default",
                        "value": btn_value,
                        "behaviors": [{"type": "callback", "value": btn_value}],
                    })
                elements.append({
                    "tag": "markdown",
                    "content": f"更多（{rest_names}）：",
                })
                elements.extend(build_responsive_button_row(remaining_btns, mobile_force_vertical=True))

        # --- Cancel at bottom (visually separated from agent CTAs) ---
        cancel_value = {
            "action": WORKFLOW_CANCEL,
            "engine_session_key": session_key,
            "project_id": project_id,
            "chat_id": chat_id,
            "root_id": root_path,
        }
        cancel = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消"},
            "type": "default",
            "value": cancel_value,
            "behaviors": [{"type": "callback", "value": cancel_value}],
        }
        elements.append({"tag": "hr"})
        elements.extend(build_responsive_button_row([cancel], mobile_force_vertical=True))

        card = CardBuilder._wrap_card(
            header_title="Workflow — 选择主编排 Agent",
            header_template=UI_TEXT["workflow_header_colors"].get("agent_select", "blue"),
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
            # Keep roles the user selected on the role-selection page so
            # re-generating the confirmation card (e.g. after a budget change)
            # does not silently drop the chosen roles.
            preserved_selected_roles = (
                list(existing_pending.selected_roles)
                if existing_pending and getattr(existing_pending, "selected_roles", None)
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
                # Keep role-selection preferences across re-generations
                selected_roles=preserved_selected_roles,
            )

        # Build and send confirmation card
        project_id = project.project_id if project else ""
        _script_content = self._read_pending_script(engine)
        # Budget fallback: selected_budget > budget > DEFAULT_BUDGET_TOKENS.
        # Always pass a valid budget so the confirm card reflects the user's
        # selection; later start_execution uses engine.project.pending.selected_budget.
        from ...workflow_engine.constants import DEFAULT_BUDGET_TOKENS, is_valid_budget
        _effective_budget: int | None = None
        if engine.project and engine.project.pending:
            _effective_budget = engine.project.pending.selected_budget
            if _effective_budget is None:
                _effective_budget = getattr(engine.project.pending, "budget", None)
            if not is_valid_budget(_effective_budget):
                _effective_budget = DEFAULT_BUDGET_TOKENS
            # Keep pending authoritative field populated so execution picks
            # it up even if it was previously None.
            if engine.project.pending.selected_budget is None:
                engine.project.pending.selected_budget = _effective_budget
        confirm_card = self._build_confirm_card(
            meta=meta,
            requirement=requirement,
            engine_session_key=engine.project.pending.engine_session_key if engine.project and engine.project.pending else "",
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=is_fallback,
            selected_tools=engine.project.pending.selected_tools if engine.project and engine.project.pending else None,
            selected_budget=_effective_budget,
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

        # Transition to the role-selection card (step 3: role selection
        # before script generation so the user can narrow down which
        # personas to include in the orchestration. The script generation
        # step only suggests roles the user did not opt-in to.
        engine.project.status = WorkflowStatus.AWAITING_ROLE_SELECT
        role_card = self._build_role_selection_card(
            engine=engine,
            requirement=requirement,
            chat_id=chat_id,
            project_id=project_id,
            engine_session_key=stored_session_key,
        )
        self.update_card(message_id, role_card)

    def _build_role_selection_card(
        self,
        engine: Any,
        requirement: str,
        chat_id: str,
        project_id: str,
        engine_session_key: str,
    ) -> dict:
        """Render the role selection card (step 3 of the 4-step orchestration
        flow: agent → tools → roles → confirm)."""
        from ...card import CardBuilder
        from ...card.actions.dispatch import (
            WORKFLOW_CANCEL,
            WORKFLOW_SELECT_ROLE,
            WORKFLOW_CONFIRM_ROLES_AND_GENERATE,
        )
        from ...card.render.buttons import build_responsive_button_row
        from ...workflow_engine.roles import get_all_role_ids, get_role_display_name

        # Determine the currently selected roles. Default to the curated
        # set used by ``_generate_script_via_ai`` so users see pre-selected
        # toggles that match what would have been used before role selection
        # was introduced.
        pending = engine.project.pending if engine and engine.project else None
        if pending and getattr(pending, "selected_roles", None):
            selected_roles = list(pending.selected_roles)
        else:
            selected_roles = [
                "architect",
                "security_auditor",
                "correctness_auditor",
                "adversarial_verifier",
                "code_quality_reviewer",
                "bug_hunter",
                "migration_validator",
                "compatibility_reviewer",
            ]

        # Safety: keep ids in sync with the authoritative list
        all_role_ids = get_all_role_ids() or selected_roles
        selected_roles = [r for r in selected_roles if r in all_role_ids]

        # --- Build body elements: stepper + requirement + role toggles ---
        elements: list[dict] = [self._build_workflow_stepper(current=3)]
        if requirement:
            elements.append({
                "tag": "markdown",
                "content": f"**需求**: {requirement[:120]}",
            })
        elements.append({
            "tag": "markdown",
            "content": "**选择角色**（勾选的角色会出现在编排脚本中，未勾选的不会被建议使用）:",
        })

        role_buttons = []
        for role_id in all_role_ids:
            is_selected = role_id in selected_roles
            display = get_role_display_name(role_id) or role_id
            btn_value = {
                "action": WORKFLOW_SELECT_ROLE,
                "role_id": role_id,
                "chat_id": chat_id,
                "project_id": project_id,
                "engine_session_key": engine_session_key,
            }
            role_buttons.append({
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"{'✓ ' if is_selected else '○ '}{display}",
                },
                "type": "primary" if is_selected else "default",
                "value": btn_value,
                "behaviors": [{"type": "callback", "value": btn_value}],
            })

        if role_buttons:
            elements.extend(build_responsive_button_row(role_buttons, mobile_force_vertical=True))

        # Footer: confirm roles + cancel
        confirm_value = {
            "action": WORKFLOW_CONFIRM_ROLES_AND_GENERATE,
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
        footer_buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消"},
                "type": "default",
                "value": cancel_value,
                "behaviors": [{"type": "callback", "value": cancel_value}],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "确认角色并生成脚本 →"},
                "type": "primary",
                "value": confirm_value,
                "behaviors": [{"type": "callback", "value": confirm_value}],
            },
        ]
        elements.extend(build_responsive_button_row(footer_buttons, mobile_force_vertical=True))

        return CardBuilder._wrap_card(
            header_title="Workflow · 选择角色",
            header_template="blue",
            elements=elements,
        )

    def handle_workflow_select_role(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Toggle a single role on the role selection card.

        Security: validates engine_session_key and initiator_user_id.
        Re-renders the role selection card with the updated selection so
        the user can keep toggling without losing state.
        """
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
        from ...workflow_engine.roles import get_all_role_ids

        if engine.project.status != WorkflowStatus.AWAITING_ROLE_SELECT:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # --- Security validation (fail-closed)
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

        role_id = value.get("role_id", "")
        if not role_id or role_id not in get_all_role_ids():
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail=f"未知角色: {role_id}",
            )
            return

        # --- Toggle selection
        if engine.project.pending.selected_roles is None:
            # Initialize to the curated default set so the first toggle
            # matches what the role-selection card already pre-selected
            # for the user. This avoids "appearing to un-select" a role
            # that was visually pre-selected and ending up with a single
            # role selection instead of all defaults minus one.
            engine.project.pending.selected_roles = [
                "architect",
                "security_auditor",
                "correctness_auditor",
                "adversarial_verifier",
                "code_quality_reviewer",
                "bug_hunter",
                "migration_validator",
                "compatibility_reviewer",
            ]
        current = set(engine.project.pending.selected_roles)
        if role_id in current:
            current.discard(role_id)
        else:
            current.add(role_id)
        engine.project.pending.selected_roles = sorted(current)

        # Re-render so the toggle is reflected immediately
        role_card = self._build_role_selection_card(
            engine=engine,
            requirement=engine.project.pending.requirement or "",
            chat_id=chat_id,
            project_id=project_id,
            engine_session_key=stored_session_key,
        )
        self.update_card(message_id, role_card)

    def handle_workflow_confirm_roles_and_generate(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Finalize role selection and proceed to script generation.

        Transitions the engine project to AWAITING_CONFIRM after producing
        a script. ``pending.selected_roles`` is preserved so the script
        generation prompt only mentions those roles.
        """
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
        from ...thread import get_current_sender_id

        if engine.project.status != WorkflowStatus.AWAITING_ROLE_SELECT:
            self._reply_workflow_error(message_id, "invalid_state")
            return

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

        # Proceed directly to script generation / confirmation using the
        # existing ``_generate_and_show_confirm_card`` path; role selection
        # is already reflected in ``engine.project.pending.selected_roles``.
        requirement = engine.project.pending.requirement if engine.project.pending else ""
        selected_tools = list(engine.project.pending.selected_tools or []) if engine.project.pending else []

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
                self._reply_workflow_error(
                    message_id,
                    "invalid_argument",
                    detail=(
                        f"脚本计划使用的工具 {sorted(unmatched)} 不在允许的工具列表中。\n"
                        f"请点击「重新生成编排」按钮基于当前工具选择重新生成脚本，\n"
                        f"或在工具选择中添加缺失的工具。"
                    ),
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
        """Handle cancel button click — abort the pending workflow or
        dismiss an entry prompt.

        ``entry-cancel`` sessions represent a requirement-entry prompt:
        no engine/pending state is required to cancel them, and the
        handler simply replaces the card with a "cancelled" notice.
        For all other sessions we validate engine_session_key and
        initiator_user_id before allowing cancellation.
        """
        # Strip unknown fields from button callback to prevent forged fields
        # (e.g. a client-side ``value.confirmed`` bypass).
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        button_session_key = value.get("engine_session_key", "")

        # Entry-prompt cancel: validates the initiator before allowing
        # cancellation so a forged cancel button from a different user
        # cannot drop the topic workflow mode. The entry card writes the
        # original sender into ``initiator_user_id``; we match that value
        # against the current callback's sender.
        if button_session_key == "entry-cancel":
            from ...card import CardBuilder
            from ...mode import InteractionMode, set_topic_mode
            from ...thread import get_current_sender_id

            stored_initiator = value.get("initiator_user_id", "")
            current_user = get_current_sender_id() or ""
            if not stored_initiator or current_user != stored_initiator:
                self._reply_workflow_error(message_id, "forbidden")
                return

            # Unbind the topic from ``workflow`` mode so free text after
            # cancel is not routed to the workflow orchestrator.
            try:
                set_topic_mode(chat_id, None)
            except Exception:
                logger.debug("set_topic_mode unavailable during entry-cancel; ignoring")

            cancel_card = CardBuilder._wrap_card(
                header_title="🔄 Workflow — 已取消",
                header_template="grey",
                elements=[{
                    "tag": "markdown",
                    "content": (
                        "已取消输入。当前对话已退出 Workflow 模式。\n"
                        "若需开始新任务，请显式发送：\n"
                        "• `/wf <你的任务描述>` — 基于需求开始编排\n"
                        "• `/wf` — 重新查看入口卡片\n"
                        "（自由文本将走原有路由，不再被当作 Workflow 需求）"
                    ),
                }],
            )
            self.update_card(message_id, cancel_card)
            return

        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        valid_statuses = (
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
            WorkflowStatus.AWAITING_AGENT_SELECT,
            WorkflowStatus.AWAITING_ROLE_SELECT,
        )
        if engine.project.status not in valid_statuses:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # Security: validate session key (fail-closed)
        stored_session_key = engine.project.pending.engine_session_key if engine.project.pending else ""
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

        Accepted in AWAITING_CONFIRM (post-script) AND AWAITING_TOOL_SELECT
        (pre-script) so users can lock in a budget tier before script
        generation.

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

        if engine.project.status not in {
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
        }:
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

        # Route the re-render to the appropriate card based on current stage
        if engine.project.status == WorkflowStatus.AWAITING_TOOL_SELECT:
            # Tool selection stage — refresh tool selection card, keep
            # recommended/other tools so the selectable tool buttons remain
            # visible after a budget re-selection.
            all_tools, recommended_tools, other_tools, _ = self._resolve_tool_lists()
            card = self._build_tool_selection_card(
                engine=engine,
                requirement=engine.project.pending.requirement or "",
                chat_id=chat_id,
                project_id=project_id,
                session_key=engine.project.pending.engine_session_key or "",
                all_tools=all_tools,
                recommended_tools=recommended_tools,
                other_tools=other_tools,
                default_selected=list(engine.project.pending.selected_tools or []),
            )
            self.update_card(message_id, card)
            return

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
            # warning text. We route this through the unified error surface
            # so the presentation is consistent with other workflow errors.
            self._reply_workflow_error(
                message_id,
                "invalid_argument",
                detail=(
                    "⚠️ 补齐工具时发现脚本声明的工具中存在未知名称，已被过滤。\n"
                    f"已过滤: {', '.join(_rejected)}\n"
                    f"已保留: {', '.join(merged) or '(空)'}"
                ),
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

    # ------------------------------------------------------------------
    # Workflow ref (sub-workflow) handlers
    # ------------------------------------------------------------------

    def handle_workflow_view_workflow_ref(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Preview a sub-workflow reference as an inline card reply.

        Reads the referenced script (from ``meta.workflow_refs``) and shows
        its content and metadata. Does not mutate the pending workflow
        state — this is purely informational.
        """
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus
        from ...workflow_engine.script_gen import extract_meta_from_script
        from ...workflow_engine.templates import load_template

        if engine.project.status not in (
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
            WorkflowStatus.AWAITING_ROLE_SELECT,
        ):
            self._reply_workflow_error(message_id, "invalid_state")
            return

        # Session key + sender validation
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

        raw_idx = value.get("ref_index")
        try:
            ref_index = int(raw_idx) if raw_idx is not None else -1
        except (TypeError, ValueError):
            ref_index = -1

        workflow_refs = (
            (engine.project.pending.meta or {}).get("workflow_refs", [])
            if engine.project.pending
            else []
        )

        if not isinstance(workflow_refs, list) or ref_index < 0 or ref_index >= len(workflow_refs):
            self._reply_workflow_error(message_id, "invalid_argument", detail="子 Workflow 引用索引无效")
            return

        ref = workflow_refs[ref_index]
        if isinstance(ref, dict):
            ref_name = ref.get("name", "unknown")
            ref_path = ref.get("path", ref.get("script_path", ""))
        else:
            ref_name = str(ref)
            ref_path = ""

        # Try to resolve a script body for display.
        script_body = ""
        resolved_path: Optional[str] = None
        sender_id = current_user or ""
        if ref_path and os.path.isfile(ref_path):
            resolved_path = ref_path
        else:
            try:
                resolved_path = load_template(root_path, ref_name, user_id=sender_id)
            except Exception:
                resolved_path = None

        if resolved_path:
            try:
                with open(resolved_path, "r", encoding="utf-8") as f:
                    script_body = f.read()
            except OSError:
                script_body = ""

        meta_extract: Optional[dict[str, Any]] = None
        if script_body:
            try:
                meta_extract = extract_meta_from_script(script_body)
            except Exception:
                meta_extract = None

        preview_lines: list[str] = []
        preview_lines.append(f"**{ref_name}**")
        if resolved_path:
            preview_lines.append(f"路径: `{resolved_path}`")
        else:
            preview_lines.append("路径: _（未找到对应脚本文件）_")
        if meta_extract:
            desc = (meta_extract or {}).get("description")
            if desc:
                preview_lines.append(f"描述: {desc}")
            meta_tools = (meta_extract or {}).get("tools")
            if meta_tools:
                preview_lines.append("工具: " + ", ".join(f"`{t}`" for t in meta_tools))

        preview_text = "\n".join(preview_lines)

        # Render the script preview as a collapsible code block when present.
        elements: list[dict[str, Any]] = []
        elements.append({"tag": "markdown", "content": preview_text})

        if script_body:
            from ...workflow_engine.renderer import render_script_preview
            preview = render_script_preview(script_body) or ""
            if preview:
                elements.append({
                    "tag": "collapsible_panel",
                    "header": {"title": {"tag": "plain_text", "content": "📜 脚本内容"}, "template": "grey"},
                    "expanded": False,
                    "elements": [{"tag": "markdown", "content": preview}],
                })

        from ...card import CardBuilder
        # Build a Feishu card directly (header + element list).
        # The builtin helpers on CardBuilder do not accept an `elements`
        # kwarg; we construct the minimal card dict by hand so this keeps
        # working when the orchestrator isn't available.
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": f"子 Workflow 预览：{ref_name}",
                },
            },
            "elements": elements,
        }
        self.update_card(message_id, card)

    def handle_workflow_remove_workflow_ref(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Remove a sub-workflow reference from ``meta.workflow_refs``.

        Only accepted in AWAITING_CONFIRM / AWAITING_TOOL_SELECT /
        AWAITING_ROLE_SELECT. After mutation, re-renders the confirm card.
        """
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        valid_statuses = (
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
            WorkflowStatus.AWAITING_ROLE_SELECT,
        )
        if engine.project.status not in valid_statuses:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        if engine.project.pending is None:
            engine.project.pending = PendingConfirmation()

        # Session key + sender validation
        from ...thread import get_current_sender_id

        stored_session_key = engine.project.pending.engine_session_key or ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id or ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        raw_idx = value.get("ref_index")
        try:
            ref_index = int(raw_idx) if raw_idx is not None else -1
        except (TypeError, ValueError):
            ref_index = -1

        current_meta = engine.project.pending.meta or {}
        workflow_refs = current_meta.get("workflow_refs", []) or []
        if not isinstance(workflow_refs, list) or ref_index < 0 or ref_index >= len(workflow_refs):
            self._reply_workflow_error(message_id, "invalid_argument", detail="子 Workflow 引用索引无效")
            return

        new_refs = [ref for i, ref in enumerate(workflow_refs) if i != ref_index]
        new_meta = dict(current_meta)
        new_meta["workflow_refs"] = new_refs
        engine.project.pending.meta = new_meta

        # Re-render the confirm card so the user sees the updated refs list.
        self._rerender_confirm_or_tool_card(message_id, chat_id, project_id, engine)

    def handle_workflow_add_workflow_ref(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        value: dict[str, Any],
    ) -> None:
        """Open a selection card to add a sub-workflow reference.

        Lists available templates as buttons. Tapping one appends it to
        ``meta.workflow_refs`` (de-duplicated by name) and re-renders the
        confirm card.
        """
        from ...card.events.payloads import filter_workflow_button_value
        value = filter_workflow_button_value(value)

        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus
        from ...workflow_engine.templates import TemplateInfo, discover_templates

        valid_statuses = (
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
            WorkflowStatus.AWAITING_ROLE_SELECT,
        )
        if engine.project.status not in valid_statuses:
            self._reply_workflow_error(message_id, "invalid_state")
            return

        if engine.project.pending is None:
            engine.project.pending = PendingConfirmation()

        # Session key + sender validation
        from ...thread import get_current_sender_id

        stored_session_key = engine.project.pending.engine_session_key or ""
        button_session_key = value.get("engine_session_key", "")
        if not stored_session_key or button_session_key != stored_session_key:
            self._reply_workflow_error(message_id, "session_expired")
            return

        current_user = get_current_sender_id() or ""
        stored_initiator = engine.project.pending.initiator_user_id or ""
        if not stored_initiator or not current_user or current_user != stored_initiator:
            self._reply_workflow_error(message_id, "forbidden")
            return

        # A button may carry a specific template name to pick — this happens
        # when the user taps one of the template candidates in the
        # selection card. When no template name is given, show the selector.
        template_name = value.get("template_name")
        if template_name:
            self._apply_add_workflow_ref(
                message_id=message_id,
                chat_id=chat_id,
                project_id=project_id,
                engine=engine,
                template_name=str(template_name),
                root_path=root_path,
                sender_id=current_user,
            )
            return

        # Present the template selector card.
        sender_id = current_user or ""
        templates: list[TemplateInfo] = []
        try:
            templates = discover_templates(root_path, user_id=sender_id)
        except Exception:
            templates = []

        existing_refs: list[Any] = []
        current_meta = engine.project.pending.meta or {}
        existing_refs = current_meta.get("workflow_refs", []) or []
        existing_names = {
            ref.get("name") if isinstance(ref, dict) else str(ref)
            for ref in existing_refs
        }

        from ...card.actions.dispatch import WORKFLOW_ADD_WORKFLOW_REF

        elements: list[dict[str, Any]] = []
        elements.append({
            "tag": "markdown",
            "content": (
                "**选择要添加为子 Workflow 引用的模板**\n\n"
                "已引用的模板会在下方显示为「已添加」状态，避免重复。"
            ),
        })

        if not templates:
            elements.append({"tag": "markdown", "content": "_当前没有可用的 Workflow 模板_。"})
        else:
            for tpl in templates:
                is_added = tpl.name in existing_names
                tpl_lines: list[str] = [f"**{tpl.name}**  _{tpl.scope}_"]
                if tpl.description:
                    tpl_lines.append(f"— {tpl.description}")
                elements.append({"tag": "markdown", "content": "\n".join(tpl_lines)})

                if is_added:
                    elements.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✓ 已添加"},
                        "type": "primary",
                        "value": {"action": "__noop__"},
                        "disabled": True,
                    })
                    continue

                pick_value = {
                    "action": WORKFLOW_ADD_WORKFLOW_REF,
                    "template_name": tpl.name,
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "engine_session_key": stored_session_key,
                }
                pick_button = [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "➕ 添加"},
                    "type": "default",
                    "value": pick_value,
                    "behaviors": [{"type": "callback", "value": pick_value}],
                }]
                elements.extend(build_responsive_button_row(pick_button, mobile_force_vertical=True))

        from ...card import CardBuilder
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": "添加子 Workflow 引用",
                },
            },
            "elements": elements,
        }
        self.update_card(message_id, card)

    def _apply_add_workflow_ref(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        engine: Any,
        template_name: str,
        root_path: str,
        sender_id: str,
    ) -> None:
        """Append a template reference to ``meta.workflow_refs`` and patch
        the pending script with a ``workflow(template_name, {})`` call so
        it actually executes the referenced workflow at runtime.

        Safety: template names are validated through
        ``validate_template_name``, restricted to those returned by
        ``discover_templates``, and resolved via ``resolve_template_path``
        so forged path-traversal names are rejected before touching disk.
        """
        from ...workflow_engine.script_gen import extract_meta_from_script
        from ...workflow_engine.templates import (
            discover_templates,
            resolve_template_path,
            validate_template_name,
        )

        # 1) Reject invalid / path-traversal names (fail-closed).
        ok, reason = validate_template_name(template_name)
        if not ok:
            logger.warning(
                "Rejected template reference '%s' during add-workflow-ref: %s",
                template_name,
                reason,
            )
            self._reply_workflow_error(
                message_id, "invalid_argument", detail=f"非法模板名称: {template_name}"
            )
            return

        # 2) Only accept names discoverable for this user / project root.
        try:
            discoverable = {t.name for t in discover_templates(root_path, user_id=sender_id)}
        except Exception as exc:
            logger.warning("Cannot enumerate templates for add-workflow-ref: %s", exc)
            self._reply_workflow_error(
                message_id, "invalid_argument", detail="无法枚举可用模板"
            )
            return
        if template_name not in discoverable:
            self._reply_workflow_error(
                message_id, "invalid_argument", detail=f"模板不在可用列表中: {template_name}"
            )
            return

        # 3) Resolve to an absolute path; used below for the ref meta only.
        resolved_path = resolve_template_path(root_path, template_name, user_id=sender_id)

        description = ""
        if resolved_path:
            try:
                with open(resolved_path, "r", encoding="utf-8") as f:
                    ref_script = f.read()
                extracted = extract_meta_from_script(ref_script) or {}
                description = extracted.get("description", "") or ""
            except Exception:
                description = ""

        current_meta = engine.project.pending.meta or {}
        workflow_refs = list(current_meta.get("workflow_refs", []) or [])

        # De-duplicate by name so users don't see identical references twice.
        already_present = False
        for ref in workflow_refs:
            existing_name = ref.get("name") if isinstance(ref, dict) else str(ref)
            if existing_name == template_name:
                already_present = True
                break

        if not already_present:
            workflow_refs.append({
                "name": template_name,
                "path": resolved_path or "",
                "description": description,
            })

        # 4) Patch the pending script to include a ``workflow(template_name, {})``
        #    call so the reference actually drives runtime behaviour.
        script_path = getattr(engine.project.pending, "script_path", None)
        existing_script = ""
        if script_path:
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    existing_script = f.read()
            except OSError as exc:
                logger.warning("Cannot read pending script for ref patch: %s", exc)
                existing_script = ""

        if existing_script and f'workflow("{template_name}"' not in existing_script:
            patch = (
                f"\n// 🔗 Sub-workflow reference: {template_name}\n"
                f"try {{ await workflow('{template_name}', {{}}); }} "
                f"catch (e) {{ console.log('sub-workflow {template_name} skipped:', e); }}\n"
            )
            try:
                with open(script_path, "a", encoding="utf-8") as f:
                    f.write(patch)
            except OSError as exc:
                logger.warning("Cannot patch pending script with ref: %s", exc)

        new_meta = dict(current_meta)
        new_meta["workflow_refs"] = workflow_refs
        engine.project.pending.meta = new_meta

        self._rerender_confirm_or_tool_card(message_id, chat_id, project_id, engine)

    def _rerender_confirm_or_tool_card(
        self,
        message_id: str,
        chat_id: str,
        project_id: str,
        engine: Any,
    ) -> None:
        """Re-render the confirm card (or tool card, depending on state)."""
        from ...workflow_engine.models import WorkflowStatus

        if not engine.project.pending:
            return

        status = engine.project.status
        if status == WorkflowStatus.AWAITING_TOOL_SELECT:
            all_tools, recommended_tools, other_tools, default_selected = self._resolve_tool_lists()
            card = self._build_tool_selection_card(
                engine=engine,
                requirement=engine.project.pending.requirement or "",
                chat_id=chat_id,
                project_id=project_id,
                session_key=engine.project.pending.engine_session_key or "",
                all_tools=all_tools,
                recommended_tools=recommended_tools,
                other_tools=other_tools,
                default_selected=default_selected,
            )
            self.update_card(message_id, card)
            return

        # Default path: re-render the confirm card (covers AWAITING_CONFIRM /
        # AWAITING_ROLE_SELECT since both display the same confirm-style card).
        _script_content = self._read_pending_script(engine)
        selected_budget = (
            engine.project.pending.selected_budget
            if getattr(engine.project.pending, "selected_budget", None) is not None
            else engine.project.pending.budget
        )
        confirm_card = self._build_confirm_card(
            meta=engine.project.pending.meta,
            requirement=engine.project.pending.requirement or "",
            engine_session_key=engine.project.pending.engine_session_key or "",
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=engine.project.pending.is_fallback,
            selected_tools=engine.project.pending.selected_tools,
            selected_budget=selected_budget,
            script_content=_script_content,
        )
        self.update_card(message_id, confirm_card)


    def _resolve_project_from_id(
        self, project_id: str, chat_id: str
    ) -> Optional["ProjectContext"]:
        """Resolve a ProjectContext from project_id, scoped to the chat.

        Uses ``project_manager.get_project_for_chat`` so that a forged
        ``project_id`` carried in a card action payload cannot be used
        to interact with projects the chat is not allowed to see. Returns
        ``None`` for unknown or invisible projects.
        """
        if not project_id:
            return None
        try:
            return self.ctx.project_manager.get_project_for_chat(project_id, chat_id)
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

        # 权限：必须是 workflow 发起者或管理员才能保存。
        # 非发起者保存他人工作流脚本会被拒绝，防止越权复用。
        from ...thread import get_current_sender_id
        from ...workflow_engine.templates import is_admin_user

        caller_id = get_current_sender_id() or ""
        admin_ids_checked = getattr(getattr(self.ctx, "settings", None), "admin_user_ids", None) or []
        stored_initiator = ""
        if engine and engine.project:
            pending_initiator = (
                engine.project.pending.initiator_user_id
                if engine.project.pending
                else ""
            )
            stored_initiator = pending_initiator or getattr(
                engine.project, "initiator_user_id", ""
            )
        if (
            stored_initiator
            and caller_id != stored_initiator
            and not is_admin_user(caller_id, admin_ids_checked)
        ):
            self._reply_workflow_error(
                message_id,
                "forbidden",
                detail="只有 workflow 发起者或管理员可保存当前脚本；如需保存，请发起者执行保存或联系管理员",
            )
            return

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

        # Build the role list. Honors explicit user selection from the
        # role selection card (``pending.selected_roles``), falling back
        # to a curated default set for backward compatibility. The role
        # list is injected verbatim into the script-generation prompt so
        # that the orchestrator agent only suggests roles the user
        # actually wants.
        user_selected_roles: list[str] = []
        if engine and engine.project and engine.project.pending:
            user_selected_roles = list(
                getattr(engine.project.pending, "selected_roles", None) or []
            )
        if user_selected_roles:
            available_roles = [r for r in user_selected_roles if isinstance(r, str)]
        else:
            available_roles = [
                "architect", "security_auditor", "correctness_auditor",
                "adversarial_verifier", "code_quality_reviewer", "bug_hunter",
                "migration_validator", "compatibility_reviewer",
            ]

        prompt = build_script_gen_prompt(
            requirement=requirement,
            available_tools=available_tools,
            available_roles=available_roles,
            budget_total=budget_for_gen,
            budget_tokens=None,
            orchestrator_agent=agent_type,
        )

        # Attempt AI generation via one-shot ACP session
        #
        # SECURITY: Script generation runs an untrusted model against a
        # user-supplied requirement. We disable ``auto_approve`` and attach
        # a read-only tool filter so the generation session cannot mutate
        # the filesystem, execute arbitrary commands, or reach the network
        # even if the model tries to. The user confirms the *generated*
        # script later in the workflow card.
        session = None
        try:
            session = create_engine_session(
                agent_type=agent_type,
                cwd=root_path,
                thread_id="workflow_script_gen",
                auto_approve=False,
                require_tool_filter=True,
            )
            if session is None:
                logger.warning("Failed to create script-gen session; using fallback")
                return self._write_fallback_script(script_path, requirement), None, True

            # Apply read-only tool filter for script generation. The allowed
            # set is intentionally small: only read-side filesystem + search
            # introspection. Mutation tools (write, shell, network, code
            # execution) are rejected so a compromised model cannot bypass
            # the user confirmation step.
            _MUTATING_TOOLS: frozenset[str] = frozenset([
                "execute_command",
                "create_terminal",
                "run_terminal",
                "run_shell",
                "shell",
                "bash",
                "write_file",
                "write_text_file",
                "delete_file",
                "remove_file",
                "mkdir",
                "patch_file",
                "apply_diff",
                "edit_file",
                "write_to_file",
                "http_request",
                "http_get",
                "http_post",
                "fetch",
                "download",
                "upload",
                "network_request",
                "url_open",
                "send_message",
                "send_email",
                "create_issue",
            ])

            def _script_gen_tool_filter(tool_name: str, _params: dict | None) -> bool:
                if not isinstance(tool_name, str):
                    return False
                norm = tool_name.lower().strip()
                if norm in _MUTATING_TOOLS:
                    return False
                # Reject any tool whose name hints at a mutation.
                if any(token in norm for token in ("write", "delete", "remove", "exec", "run", "patch", "post", "upload", "send", "create")):
                    return False
                return True

            try:
                session.set_tool_filter(_script_gen_tool_filter)
            except (AttributeError, TypeError, Exception) as exc:
                # FAIL-CLOSED: If we cannot enforce the read-only tool filter
                # on the script-gen session, we must not proceed to call the
                # model — otherwise a compromised or confused model could
                # mutate the filesystem or execute arbitrary commands before
                # the user has confirmed the script. Instead, fall back to a
                # static pre-vetted fallback script so the workflow path still
                # reaches the confirmation card.
                logger.warning(
                    "Applying script-gen tool filter failed (%s); fail-closed to "
                    "static fallback script; model prompt is NOT sent.",
                    type(exc).__name__,
                )
                from ...workflow_engine.script_gen import FALLBACK_SCRIPT
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(FALLBACK_SCRIPT)
                if session is not None:
                    close_session_safely(session)
                meta = {"budget_tokens": budget_for_gen, "name": "fallback-orchestration"}
                return script_path, meta, True

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

        # Shared helpers
        phase_count = len(phases)
        tool_count = len(selected_tools) if selected_tools else len(tools)
        budget = selected_budget if selected_budget is not None else DEFAULT_BUDGET_TOKENS
        budget_short = f"{budget // 1_000_000}M" if budget >= 1_000_000 else f"{budget // 1000}K"
        budget_display = f"{budget_short}\n<font color='grey' size='10'>{budget:,} tokens</font>"

        # Build elements — unified layout across normal & truncated modes.
        elements: list[dict] = []

        # --- 1. Stepper (vertical, one-line per step) ---
        elements.append(self._build_workflow_stepper(current=4))

        if is_fallback:
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "⚠️ AI 脚本生成失败，已使用默认模板。结果可能不完全匹配需求。"},
                ],
            })

        # --- 2. Requirement summary (first screen) ---
        req_trim = requirement[:300] if len(requirement) > 300 else requirement
        elements.append({
            "tag": "markdown",
            "content": f"**需求**\n> {req_trim}\n\n**Token 预算**：{budget:,} tokens（约 {budget_short}）",
        })

        # --- 3. Stats: phases / tools / budget (first screen) ---
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

        # --- 4. Tool-mismatch status bar + single primary fix action ---
        if has_mismatch:
            missing = sorted(script_tools - allowed_tools)
            missing_display = ", ".join(f"`{m}`" for m in missing)
            elements.append({
                "tag": "note",
                "elements": [{
                    "tag": "plain_text",
                    "content": (
                        f"⚠️ 脚本需要这些工具但尚未启用：{missing_display}。"
                        " 点击下方『一键补齐缺失工具』即可放行执行。"
                    ),
                }],
            })

        # --- 5. Primary CTA block — confirm start / cancel / mismatch fix ---
        # Visible on the first screen. Users do NOT need to open any
        # collapsible panel to unblock execution.
        from ...card.actions.dispatch import WORKFLOW_CONFIRM_START, WORKFLOW_FILL_MISSING_TOOLS, WORKFLOW_BACK_TO_TOOLS

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
            primary_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "➕ 一键补齐缺失工具"},
                "type": "primary",
                "value": fill_missing_value,
                "behaviors": [{"type": "callback", "value": fill_missing_value}],
            })
            primary_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "↩️ 返回工具选择"},
                "type": "default",
                "value": back_tools_value,
                "behaviors": [{"type": "callback", "value": back_tools_value}],
            })

        confirm_disabled = has_mismatch
        confirm_disabled_tips = (
            "脚本需要的工具尚未全部启用，请先点击『一键补齐缺失工具』"
            if confirm_disabled
            else None
        )
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

        # --- 6. Phases panel (top-level collapsible) ---
        if phases:
            phase_elements = []
            for i, p in enumerate(phases, 1):
                title = p.get("title", p.get("name", f"Phase {i}"))
                detail = p.get("detail", "")
                line = f"**{i}. {title}**"
                if detail:
                    line += f"\n   {detail[:100]}"
                phase_tools = phase_tool_mapping.get(title) or phase_tool_mapping.get(str(i))
                if phase_tools:
                    tool_tags = ", ".join(f"`{t}`" for t in phase_tools)
                    line += f"\n   工具: {tool_tags}"
                phase_elements.append({"tag": "markdown", "content": line})
            elements.append({
                "tag": "collapsible_panel",
                "header": {
                    "title": {"tag": "plain_text", "content": f"📋 阶段列表 ({len(phases)})"},
                    "template": "blue",
                },
                "expanded": False,
                "elements": phase_elements,
            })
        else:
            elements.append({
                "tag": "markdown",
                "content": "📋 **执行阶段**: Planning → Execution",
            })

        # --- 7. Script preview panel (top-level collapsible) ---
        if script_content:
            from ...workflow_engine.renderer import render_script_preview

            preview = render_script_preview(script_content)
            if preview:
                elements.append({
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "plain_text", "content": "📜 编排脚本预览"},
                        "template": "grey",
                    },
                    "vertical_spacing": "8px",
                    "elements": [{"tag": "markdown", "content": preview}],
                })

        # --- 8. Collapsible: Advanced options (sub-workflows / tools / budget / regen) ---
        # Everything below here is truly secondary; users open this only for
        # deeper inspection before confirming.
        advanced_elements: list[dict] = []

        # 8a. Sub-workflow refs (interactive: preview / remove; plus add)
        from ...card.actions.dispatch import (
            WORKFLOW_VIEW_WORKFLOW_REF,
            WORKFLOW_REMOVE_WORKFLOW_REF,
            WORKFLOW_ADD_WORKFLOW_REF,
        )

        refs_header = f"🔗 **子 Workflow 引用**（{len(workflow_refs)}）"
        if workflow_refs:
            for idx, ref in enumerate(workflow_refs):
                if isinstance(ref, dict):
                    ref_name = ref.get("name", "unknown")
                    ref_path = ref.get("path", ref.get("script_path", ""))
                else:
                    ref_name = str(ref)
                    ref_path = ""

                ref_header_line = f"• `{ref_name}`"
                if ref_path:
                    ref_header_line += f"  <font color='grey' size='10'>{ref_path}</font>"
                advanced_elements.append({"tag": "markdown", "content": ref_header_line})

                ref_buttons: list[dict] = []
                view_value = {
                    "action": WORKFLOW_VIEW_WORKFLOW_REF,
                    "ref_index": idx,
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "engine_session_key": engine_session_key,
                }
                ref_buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "👁 预览"},
                    "type": "default",
                    "value": view_value,
                    "behaviors": [{"type": "callback", "value": view_value}],
                })
                remove_value = {
                    "action": WORKFLOW_REMOVE_WORKFLOW_REF,
                    "ref_index": idx,
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "engine_session_key": engine_session_key,
                }
                ref_buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🗑 移除"},
                    "type": "default",
                    "value": remove_value,
                    "behaviors": [{"type": "callback", "value": remove_value}],
                    "confirm": {
                        "title": {"tag": "plain_text", "content": "移除子 Workflow 引用？"},
                        "text": {"tag": "plain_text", "content": f"确定移除「{ref_name}」？"},
                    },
                })
                advanced_elements.extend(build_responsive_button_row(ref_buttons, mobile_force_vertical=True))

        add_value = {
            "action": WORKFLOW_ADD_WORKFLOW_REF,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": engine_session_key,
        }
        add_button = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "➕ 添加子 Workflow 引用"},
            "type": "default",
            "value": add_value,
            "behaviors": [{"type": "callback", "value": add_value}],
        }]
        advanced_elements.append({
            "tag": "markdown",
            "content": refs_header,
        })
        advanced_elements.extend(build_responsive_button_row(add_button, mobile_force_vertical=True))

        # 8b. Tools detail + interactive toggle
        tool_descriptions = get_available_tools()
        recommended_order = ["coco", "claude", "codex", "aiden", "gemini", "traex", "ttadk"]
        tier1_tools = [t for t in recommended_order if t in allowed_tools]
        tier2_tools = [t for t in sorted(allowed_tools) if t not in recommended_order]

        tool_detail_elements: list[dict] = []
        tool_detail_elements.append({
            "tag": "markdown",
            "content": "📝 **脚本计划使用**: " + (" | ".join(f"`{t}`" for t in sorted(script_tools))),
        })
        tool_detail_elements.append({
            "tag": "markdown",
            "content": "✅ **允许执行的工具**（点击切换，脚本只能使用勾选的工具）：",
        })

        if tier1_tools:
            for tool in tier1_tools:
                desc = tool_descriptions.get(tool, tool)
                tool_detail_elements.append({"tag": "markdown", "content": f"- `{tool}`: {desc}"})

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
                tool_detail_elements.extend(build_responsive_button_row(tool_buttons, mobile_force_vertical=True))

        if tier2_tools:
            tier2_elements = []
            for tool in tier2_tools:
                desc = tool_descriptions.get(tool, tool)
                tier2_elements.append({"tag": "markdown", "content": f"- `{tool}`: {desc}"})
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
            tool_detail_elements.append({
                "tag": "collapsible_panel",
                "header": {
                    "title": {"tag": "plain_text", "content": f"🔧 更多工具 ({len(tier2_tools)})"},
                    "template": "grey",
                },
                "expanded": False,
                "elements": [
                    *tier2_elements,
                    *build_responsive_button_row(other_buttons, mobile_force_vertical=True),
                ],
            })

        # 6e. Budget selection + budget regen (advanced)
        from ...card.actions.dispatch import WORKFLOW_SELECT_BUDGET, WORKFLOW_REGENERATE_SCRIPT, WORKFLOW_APPLY_BUDGET_REGENERATE
        from ...workflow_engine.constants import is_valid_budget

        budget_elements: list[dict] = []
        budget_elements.append({
            "tag": "markdown",
            "content": f"💰 **Token 预算**: {budget_display} tokens（点击下方档位切换）",
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
            budget_elements.extend(build_responsive_button_row(budget_buttons, mobile_force_vertical=True))

        # Detect armed state for budget regen (visible warning only inside
        # the advanced panel so first-screen stays clean).
        engine_manager = None
        armed = False
        try:
            _fake_project = self._resolve_project_from_id(project_id, chat_id)
            _root = self._get_root_path(chat_id, _fake_project)
            engine_manager = self.ctx.workflow_engine_manager.get(chat_id, _root)
            if engine_manager and engine_manager.project and engine_manager.project.pending:
                armed = bool(getattr(engine_manager.project.pending, "armed_for_regen", False))
        except Exception:
            armed = False

        if armed:
            budget_elements.append({
                "tag": "note",
                "elements": [{
                    "tag": "plain_text",
                    "content": (
                        "⚠️ 已武装：再次点击『应用预算并重新生成』将真正调用 AI。"
                        " 如不想重新生成，请直接点击上方『确认执行』或『取消』。"
                    ),
                }],
            })

        if selected_budget is not None:
            _raw_meta_budget = (meta or {}).get("budget_tokens")
            current_budget_tokens = (
                _raw_meta_budget if is_valid_budget(_raw_meta_budget) else DEFAULT_BUDGET_TOKENS
            )
            budget_elements.append({
                "tag": "markdown",
                "content": (
                    f"💡 当前脚本按 {current_budget_tokens:,} tokens 预算生成"
                    f"（本次选择 {selected_budget:,} tokens）。"
                    " 如需调整预算后重新生成，请使用下方按钮。"
                ),
            })

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
        estimated_tokens = int(new_budget_for_popup * 1.2)

        regen_buttons: list[dict] = []
        regen_buttons.append({
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

        regen_label = (
            "✴️ 确认：按新预算重新生成（将消耗 token）"
            if armed
            else "💰 应用预算并重新生成"
        )
        regen_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": regen_label},
            "type": "primary" if armed else "default",
            "value": apply_regen_value,
            "behaviors": [{"type": "callback", "value": apply_regen_value}],
            "confirm": {
                "title": {"tag": "plain_text", "content": "确认使用新预算重新生成编排脚本？"},
                "text": {
                    "tag": "plain_text",
                    "content": (
                        f"当前脚本预算: {current_budget_for_popup:,} tokens\n"
                        f"新预算: {new_budget_for_popup:,} tokens\n"
                        f"预估消耗: 约 {estimated_tokens:,} tokens\n\n"
                        "点击「确定」将重新调用 AI 生成编排脚本。"
                    ),
                },
            },
        })
        budget_elements.append({
            "tag": "collapsible_panel",
            "header": {
                "title": {"tag": "plain_text", "content": "🔄 重新生成 & 预算调整"},
                "template": "grey",
            },
            "expanded": False,
            "elements": build_responsive_button_row(regen_buttons, mobile_force_vertical=True),
        })

        # --- Combine all advanced sections into one collapsed panel ---
        # Phase/tools/budget/regen are all non-essential for quick
        # confirmation. Grouping them under one collapsed panel keeps the
        # first screen focused on the decision (confirm / cancel / fix).
        combined_panel_elements: list[dict] = []
        combined_panel_elements.extend(advanced_elements)
        combined_panel_elements.append({"tag": "hr"})
        combined_panel_elements.extend(tool_detail_elements)
        combined_panel_elements.append({"tag": "hr"})
        combined_panel_elements.extend(budget_elements)

        elements.append({
            "tag": "collapsible_panel",
            "header": {
                "title": {"tag": "plain_text", "content": "⚙️ 查看详细信息 / 更多操作（阶段 / 工具 / 脚本 / 预算）"},
                "template": "grey",
            },
            "expanded": False,
            "elements": combined_panel_elements,
        })

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
