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
            self.reply_error(
                message_id,
                "未知的 Workflow 命令\n\n可用命令:\n• `/wf <需求>` - 启动 Workflow\n• `/wf_status` - 查看进度\n• `/stop_wf` - 停止任务",
                title="未知命令",
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
        """Handle entry card 'start' button — prompt user to provide requirement."""
        self.reply_text(
            message_id,
            "请发送 `/wf <需求描述>` 来启动一个新的 Workflow。\n\n例如：\n`/wf 重构用户模块的错误处理逻辑`\n`/wf code-audit path=src/utils`",
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

        # Also block if awaiting confirmation or tool selection
        from ...workflow_engine.models import WorkflowStatus
        if existing and existing.project and existing.project.status in {
            WorkflowStatus.AWAITING_CONFIRM,
            WorkflowStatus.AWAITING_TOOL_SELECT,
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

        if not RuntimeBridge.check_node_available():
            self.reply_error(
                message_id,
                "Workflow 模式需要 Node.js >= 18。\n"
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

        # Check if requirement is a saved workflow name (skip tool selection)
        from ...workflow_engine.templates import discover_templates

        parts = requirement.strip().split(None, 1)
        template_name = parts[0] if parts else ""
        templates = discover_templates(root_path)
        template_names = {t.name for t in templates}

        if template_name in template_names:
            # Template path — skip tool selection, generate directly
            self._generate_and_show_confirm_card(
                message_id=message_id,
                chat_id=chat_id,
                requirement=requirement,
                project=project,
                root_path=root_path,
                selected_tools=None,  # Template defines its own tools
            )
            return

        # AI generation path: show tool selection first
        self._show_tool_selection_card(
            message_id=message_id,
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
            category: One of "session_expired", "invalid_state", "invalid_argument", "forbidden"
            detail: Optional detail string for invalid_argument category (replaces {detail} placeholder)
        """
        from ...card import CardBuilder
        from ...card.ui_text import UI_TEXT

        title_key = f"workflow_error_{category}_title"
        body_key = f"workflow_error_{category}_body"

        title = UI_TEXT.get(title_key, "操作失败")
        body = UI_TEXT.get(body_key, "发生未知错误，请重试。")

        # Replace {detail} placeholder for invalid_argument
        if category == "invalid_argument" and detail:
            body = body.format(detail=detail)

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
            engine.project.status = WorkflowStatus.AWAITING_TOOL_SELECT
            engine.project.pending = PendingConfirmation(
                requirement=requirement,
                initiator_user_id=get_current_sender_id() or "",
                engine_session_key=uuid.uuid4().hex,
                selected_tools=list(default_selected),
                script_path=None,
                meta=None,
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

        This is called after tool selection is confirmed, or directly for templates.
        """
        from ...card import CardBuilder

        # Send transitional "generating" card
        from ...card.ui_text import UI_TEXT
        from ...thread import get_current_sender_id
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

        parts = requirement.strip().split(None, 1)
        template_name = parts[0] if parts else ""
        templates = discover_templates(root_path)
        template_names = {t.name for t in templates}

        script_path: str
        meta: dict[str, Any] | None = None
        is_fallback = False

        if template_name in template_names:
            # Template path — resolve directly
            content = load_template(root_path, template_name)
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
                    requirement, root_path, selected_tools
                )
        else:
            # AI generation path with selected tools
            script_path, meta, is_fallback = self._generate_script_via_ai(
                requirement, root_path, selected_tools
            )

        # Store pending state in engine
        engine_name = self.get_engine_name(
            chat_id, project_id=(project.project_id if project else None)
        )
        engine = self.ctx.workflow_engine_manager.get_or_create(
            chat_id,
            root_path,
            engine_name=engine_name,
        )
        if engine.project:
            engine.project.status = WorkflowStatus.AWAITING_CONFIRM
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
            self.reply_text(message_id, "当前没有运行中的 Workflow 任务。")
            return

        # Validate: only initiator or admin can stop — fail-closed
        from ...thread import get_current_sender_id

        current_user = get_current_sender_id()
        stored_initiator = getattr(engine.project, "initiator_user_id", None)
        admin_ids: list[str] = getattr(self.ctx.settings, "admin_user_ids", []) or []

        # Fail-closed: missing initiator or operator → deny
        if not stored_initiator or not current_user:
            self.reply_text(message_id, "⚠️ 无法验证操作者身份，停止请求被拒绝。")
            return

        if current_user != stored_initiator and current_user not in admin_ids:
            self.reply_text(message_id, "⚠️ 只有 Workflow 发起者或管理员才能停止此任务。")
            return

        engine.stop()
        self.reply_text(message_id, "Workflow 任务已停止。")

    # ------------------------------------------------------------------
    # Confirm / Cancel actions (card button callbacks)
    # ------------------------------------------------------------------

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
        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = project.root_path if project else self._get_root_path(chat_id, None)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self.reply_text(message_id, "Workflow 已过期或不存在，请重新发送 `/wf`。")
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
            self.reply_text(message_id, "请至少选择一个工具。")
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
            self.reply_text(message_id, "请至少选择一个工具。")
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
        selected_budget = engine.project.pending.budget if engine.project.pending else None
        meta = engine.project.pending.meta if engine.project.pending else None

        if not script_path:
            self.reply_text(message_id, "无法获取待执行脚本，请重新发送 `/wf`。")
            return

        # Validate script path exists (defense-in-depth)
        import os
        if not os.path.isfile(script_path):
            self.reply_text(message_id, "脚本文件不存在，请重新发送 `/wf` 生成。")
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
        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            self._reply_workflow_error(message_id, "session_expired")
            return

        from ...workflow_engine.models import WorkflowStatus

        valid_statuses = (WorkflowStatus.AWAITING_CONFIRM, WorkflowStatus.AWAITING_TOOL_SELECT)
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
        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            return

        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        valid_statuses = (WorkflowStatus.AWAITING_CONFIRM, WorkflowStatus.AWAITING_TOOL_SELECT)
        if engine.project.status not in valid_statuses:
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
                selected_budget=engine.project.pending.budget,
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
        """Select a budget tier in the pending workflow confirm card.

        Security: validates engine_session_key and initiator_user_id.
        Updates the confirm card to reflect the new budget selection.
        """
        project_id = project_id or value.get("project_id", "")
        project = self._resolve_project_from_id(project_id, chat_id)
        root_path = self._get_root_path(chat_id, project)
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            return

        from ...workflow_engine.models import PendingConfirmation, WorkflowStatus

        if engine.project.status != WorkflowStatus.AWAITING_CONFIRM:
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

        budget_tokens = value.get("budget_tokens")
        if not isinstance(budget_tokens, int) or budget_tokens <= 0:
            return

        if engine.project.pending is None:
            engine.project.pending = PendingConfirmation()
        engine.project.pending.budget = budget_tokens

        # Re-render the confirm card with updated budget
        _script_content = self._read_pending_script(engine)
        confirm_card = self._build_confirm_card(
            meta=engine.project.pending.meta if engine.project.pending else None,
            requirement=engine.project.pending.requirement if engine.project.pending else "",
            engine_session_key=engine.project.pending.engine_session_key if engine.project.pending else "",
            chat_id=chat_id,
            project_id=project_id,
            is_fallback=engine.project.pending.is_fallback if engine.project.pending else False,
            selected_tools=engine.project.pending.selected_tools if engine.project.pending else None,
            selected_budget=engine.project.pending.budget if engine.project.pending else None,
            script_content=_script_content,
        )
        self.update_card(message_id, confirm_card)

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
            "1. 发送 `/wf` + 需求 → AI 生成编排脚本\n"
            "2. 预览确认卡片（显示阶段、工具、预算）\n"
            "3. 点击「确认执行」→ 多阶段自动执行\n"
            "4. 实时进度卡片更新 → 执行完成通知\n\n"
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
        """
        root_path = self._get_root_path(chat_id, project)
        parts = args.strip().split() if args else []

        if not parts:
            self.reply_text(
                message_id,
                "用法: `/wf_save <模板名> [--global]`\n\n"
                "保存最近执行的 Workflow 脚本为可复用模板。\n"
                "• 默认保存到项目级 (`.ghostap/workflows/`)\n"
                "• `--global` 保存到用户级 (`~/.ghostap/workflows/`)",
            )
            return

        name = parts[0]
        global_scope = "--global" in parts

        # Validate name
        if not name.replace("-", "").replace("_", "").isalnum():
            self.reply_text(message_id, "模板名称只能包含字母、数字、连字符和下划线。")
            return

        # Find the script to save — check pending or last execution
        engine = self.ctx.workflow_engine_manager.get(chat_id, root_path)
        script_content = None

        if engine and engine.project:
            script_path = (engine.project.pending.script_path if engine.project.pending else None) or engine.project.script_path
            if script_path:
                try:
                    with open(script_path, "r", encoding="utf-8") as f:
                        script_content = f.read()
                except OSError:
                    pass

        if not script_content:
            self.reply_text(message_id, "没有可保存的 Workflow 脚本。请先执行 `/wf` 生成脚本。")
            return

        from ...workflow_engine.templates import save_template

        try:
            save_template(root_path, name, script_content, global_scope=global_scope)
            scope_label = "用户级" if global_scope else "项目级"
            self.reply_text(message_id, f"✅ 模板 `{name}` 已保存（{scope_label}）\n\n使用: `/wf {name}`")
        except OSError as exc:
            self.reply_text(message_id, f"保存失败: {exc}")

    def _handle_wf_list(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"]
    ) -> None:
        """List available workflow templates."""
        root_path = self._get_root_path(chat_id, project)

        from ...workflow_engine.templates import discover_templates

        templates = discover_templates(root_path)

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
        """
        root_path = self._get_root_path(chat_id, project)
        parts = args.strip().split() if args else []

        if not parts:
            self.reply_text(message_id, "用法: `/wf_delete <模板名> [--global]`")
            return

        name = parts[0]
        global_scope = "--global" in parts

        from ...workflow_engine.templates import delete_template

        deleted = delete_template(root_path, name, global_scope=global_scope)
        if deleted:
            self.reply_text(message_id, f"✅ 模板 `{name}` 已删除。")
        else:
            self.reply_text(message_id, f"模板 `{name}` 不存在或无法删除。")

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
        self, requirement: str, root_path: str, selected_tools: list[str] | None = None
    ) -> tuple[str, dict[str, Any] | None, bool]:
        """Generate a workflow script via AI with fallback to simple generation.

        Args:
            requirement: The user's requirement text.
            root_path: Project root path.
            selected_tools: Optional list of tools selected by the user. If provided,
                the script generator will be encouraged to use these tools.

        Returns:
            Tuple of (script_path, meta_dict_or_None, is_fallback).
        """
        from ...agent_session import close_session_safely, create_engine_session
        from ...workflow_engine.constants import AGENT_CALL_TIMEOUT_S, DEFAULT_BUDGET_TOKENS, SCRIPT_GEN_AGENT_TYPE
        from ...workflow_engine.script_gen import (
            build_script_gen_prompt,
            extract_meta_from_script,
            validate_generated_script,
        )

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
        )

        # Attempt AI generation via one-shot ACP session
        session = None
        try:
            session = create_engine_session(
                agent_type=SCRIPT_GEN_AGENT_TYPE,
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
        from ...card.ui_text import UI_TEXT
        from ...workflow_engine.constants import BUDGET_OPTIONS, DEFAULT_BUDGET_TOKENS
        from ...workflow_engine.tool_registry import get_available_tools

        # Extract meta info
        script_name = (meta or {}).get("name", "generated-workflow")
        description = (meta or {}).get("description", requirement[:100])
        phases = (meta or {}).get("phases", [])
        tools = (meta or {}).get("tools", ["coco"])
        phase_tool_mapping: dict = (meta or {}).get("phase_tool_mapping", {})

        # Build elements
        elements: list[dict] = []

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

        # Phase list
        if phases:
            phase_lines = []
            for i, p in enumerate(phases, 1):
                title = p.get("title", f"Phase {i}")
                detail = p.get("detail", "")
                line = f"{i}. **{title}**"
                if detail:
                    line += f" — {detail}"
                # Append tool tags from phase_tool_mapping
                phase_tools = phase_tool_mapping.get(title) or phase_tool_mapping.get(str(i))
                if phase_tools:
                    tool_tags = " ".join(f"`{t}`" for t in phase_tools)
                    line += f"  🔧 {tool_tags}"
                phase_lines.append(line)
            elements.append({
                "tag": "markdown",
                "content": "📋 **执行阶段**:\n" + "\n".join(phase_lines),
            })
        else:
            elements.append({
                "tag": "markdown",
                "content": "📋 **执行阶段**: Planning → Execution",
            })

        # Workflow refs (sub-workflow calls) display
        workflow_refs = (meta or {}).get("workflow_refs", [])
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

        # Script preview section (collapsible code block)
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
                            "content": "📜 编排脚本预览 (点击展开)",
                        },
                    },
                    "vertical_spacing": "8px",
                    "elements": [
                        {"tag": "markdown", "content": preview},
                    ],
                })

        # Tools section — distinguish between script-planned and allowed tools
        tool_descriptions = get_available_tools()
        all_tool_names = list(tool_descriptions.keys())
        allowed_tools = set(selected_tools) if selected_tools else set(tools)
        script_tools = set(tools)

        # Check for mismatch
        has_mismatch = bool(script_tools - allowed_tools)
        if has_mismatch:
            unmatched = sorted(script_tools - allowed_tools)
            elements.append({
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"⚠️ 脚本计划使用的工具 {unmatched} 不在允许列表中。请添加这些工具或点击「重新生成编排」。",
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

        # Tier 1: Script-planned tools (prioritized)
        recommended_tools = [t for t in all_tool_names if t in script_tools]
        # Tier 2: Other available tools not in script
        other_tools = [t for t in all_tool_names if t not in script_tools]

        # Tier 1 buttons (script-planned)
        tool_buttons = []
        for t in recommended_tools:
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

        # Tier 2: Other available tools (collapsible)
        if other_tools:
            other_display = " | ".join(
                f"**[✓ `{t}`]**" if t in allowed_tools else f"`{t}`"
                for t in other_tools
            )
            other_buttons = []
            for t in other_tools:
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

        # Budget selection buttons
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

        # Divider before buttons
        elements.append({"tag": "hr"})

        from ...card.actions.dispatch import WORKFLOW_REGENERATE_SCRIPT

        # Action buttons: Regenerate / Confirm / Cancel
        regenerate_value = {
            "action": WORKFLOW_REGENERATE_SCRIPT,
            "chat_id": chat_id,
            "project_id": project_id,
            "engine_session_key": engine_session_key,
        }
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

        # Determine confirm button state based on mismatch
        confirm_disabled = has_mismatch

        action_buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 重新生成编排"},
                "type": "default",
                "value": regenerate_value,
                "behaviors": [{"type": "callback", "value": regenerate_value}],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 确认执行"},
                "type": "primary" if not confirm_disabled else "default",
                "value": confirm_value,
                "behaviors": [{"type": "callback", "value": confirm_value}],
                "disabled": confirm_disabled,
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "❌ 取消"},
                "type": "danger",
                "value": cancel_value,
                "behaviors": [{"type": "callback", "value": cancel_value}],
                "confirm": {
                    "title": {"tag": "plain_text", "content": UI_TEXT["workflow_btn_confirm_cancel_title"]},
                    "text": {"tag": "plain_text", "content": UI_TEXT["workflow_btn_confirm_cancel_body"]},
                },
            },
        ]
        elements.extend(build_responsive_button_row(action_buttons, mobile_force_vertical=True))

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
