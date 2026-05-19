"""Slock Engine handler — multi-Agent mouthpiece collaboration engine."""

from __future__ import annotations

import json
import logging
import shlex
from typing import TYPE_CHECKING, Optional

from ...acp.helper import fetch_acp_models
from ...card import CardBuilder
from ...card.actions import dispatch as action_ids
from ...model_selection import is_default_model_option
from ...slock_engine.bounded_executor import QueueFullError
from ...slock_engine.models import SlockChannel
from ...slock_engine.slash_commands import (
    SlockCommandAction,
    is_slock_command,
    parse_slock_command,
)
from ...utils.errors import get_error_detail, safe_error_message
from ...utils.redact import redact_sensitive
from ..emoji import EmojiReaction
from ..user_cache import resolve_display_name
from .base import CardActionContext
from .engine_base import BaseEngineHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class SlockHandler(BaseEngineHandler):
    """Manages the full lifecycle of Slock Engine (multi-Agent mouthpiece) tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        # Rate-limit tracker: key = "chat_id:sender_id" → list of timestamps
        self._rate_limit_tracker: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # BaseEngineHandler abstract method implementations
    # ------------------------------------------------------------------

    def _get_engine_manager(self):
        return self.ctx.slock_engine_manager

    def _get_engine_name_prefix(self) -> str:
        return "Slock"

    def _get_task_type(self) -> str:
        return "slock_engine"

    def _show_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.show_slock_status(message_id, chat_id, project)

    def _create_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str, root_path: str
    ):
        from ...slock_engine.engine import SlockEngineCallbacks

        def on_agent_wake(agent):
            logger.debug("Slock agent waking: %s in chat %s", agent.name, chat_id)

        def on_agent_running(agent, msg):
            logger.debug("Slock agent running: %s task=%s", agent.name, msg[:80])

        def on_agent_done(agent, result):
            logger.debug("Slock agent done: %s result_len=%d", agent.name, len(result))

        def on_error(err_msg):
            logger.error("Slock engine error in chat %s: %s", chat_id, err_msg)

        def on_escalation(esc):
            """Send escalation card to chat and write back message_id."""
            manager = self._get_engine_manager()
            engine = manager.get_active_engine(chat_id)
            if not engine:
                logger.warning("on_escalation: engine not found for chat %s", chat_id)
                return
            card = engine.get_escalation_card(esc)
            if not card:
                logger.warning("on_escalation: failed to build card for esc %s", esc.escalation_id)
                return
            import json as _json
            card_json = _json.dumps(card) if isinstance(card, dict) else card
            sent_msg_id = self.send_card_to_chat(chat_id, card_json)
            if sent_msg_id:
                esc.card_message_id = sent_msg_id
            else:
                logger.warning("on_escalation: send_card_to_chat returned None for esc %s", esc.escalation_id)

        return SlockEngineCallbacks(
            on_agent_wake=on_agent_wake,
            on_agent_running=on_agent_running,
            on_agent_done=on_agent_done,
            on_escalation=on_escalation,
            on_error=on_error,
        )

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------

    def handle_slock_command(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Route slock-related commands to the appropriate handler method."""
        cmd = parse_slock_command(text)

        dispatch: dict[SlockCommandAction, callable] = {
            SlockCommandAction.ACTIVATE: lambda: self.activate_slock(message_id, chat_id, cmd.args, project),
            SlockCommandAction.STATUS: lambda: self.show_slock_status(message_id, chat_id, project),
            SlockCommandAction.STOP: lambda: self.stop_slock_engine(message_id, chat_id, project),
            SlockCommandAction.HELP: lambda: self.show_slock_help(message_id),
            SlockCommandAction.NEW_TEAM: lambda: self.create_team(message_id, chat_id, cmd.args, project),
            SlockCommandAction.TEAM_LIST: lambda: self.list_teams(message_id, chat_id, project),
            SlockCommandAction.TEAM_STATUS: lambda: self.show_team_status(message_id, chat_id, cmd.target, project),
            SlockCommandAction.TEAM_DISSOLVE: lambda: self.dissolve_team(message_id, chat_id, cmd.target, project),
            SlockCommandAction.NEW_ROLE: lambda: self.create_role(message_id, chat_id, cmd.args, project),
            SlockCommandAction.ROLE_LIST: lambda: self.list_roles(message_id, chat_id, project),
            SlockCommandAction.ROLE_REMOVE: lambda: self.remove_role(message_id, chat_id, cmd.target, project),
            SlockCommandAction.ROLE_INFO: lambda: self.show_role_info(message_id, chat_id, cmd.target, project),
            SlockCommandAction.TASK_LIST: lambda: self.list_tasks(message_id, chat_id, project),
            SlockCommandAction.TASK_ASSIGN: lambda: self.assign_task(message_id, chat_id, cmd.args, cmd.target, project),
            SlockCommandAction.TASK_STATUS: lambda: self.show_task_status(message_id, chat_id, project),
        }

        handler = dispatch.get(cmd.action)
        if handler:
            handler()
        else:
            self.show_slock_help(message_id)

    # ------------------------------------------------------------------
    # Async execution helper (shared by handle_message & _submit_task_execution)
    # ------------------------------------------------------------------

    def _execute_async(
        self,
        *,
        engine,
        execute_fn,
        placeholder_card_json: str,
        result_card_fn,
        error_card_fn,
        empty_card_fn,
        busy_card_fn,
        message_id: str,
        chat_id: str,
    ) -> None:
        """Submit async execution with placeholder→update card pattern.

        This method encapsulates the common pattern:
          1. Send placeholder card immediately
          2. Submit execute_fn to BoundedExecutor
          3. On success: update card via result_card_fn(result, duration)
          4. On empty result: update via empty_card_fn()
          5. On exception: update via error_card_fn(exception)
          6. On queue full: update via busy_card_fn()
          7. On queue wait timeout: update with timeout card

        Callbacks:
          execute_fn() -> Optional[str] — the actual work
          result_card_fn(result: str, duration: float) -> str — JSON card for success
          error_card_fn(exc: Exception) -> str — JSON card for error
          empty_card_fn() -> str — JSON card for empty result
          busy_card_fn() -> str — JSON card for busy/full
        """
        card_message_id = self.send_card_to_chat(
            chat_id, placeholder_card_json, origin_message_id=message_id
        )

        executor = engine._get_executor()

        def _async_work():
            import time as _time

            start_time = _time.time()

            # Check queue wait timeout
            if self._check_queue_wait_timeout(future, start_time, card_message_id, chat_id):
                return

            try:
                result = execute_fn()
            except Exception as e:
                logger.error("Slock _execute_async error: %s", repr(e), exc_info=True)
                error_card = error_card_fn(e)
                if card_message_id:
                    self.update_card(card_message_id, error_card)
                return

            if not result:
                empty_card = empty_card_fn()
                if card_message_id:
                    self.update_card(card_message_id, empty_card)
                return

            duration = _time.time() - start_time
            success_card = result_card_fn(result, duration)
            if card_message_id:
                self.update_card(card_message_id, success_card)
            else:
                self.send_card_to_chat(chat_id, success_card, origin_message_id=message_id)

        future = None  # Assigned by executor.submit(); closure reads after thread start
        try:
            future = executor.submit(_async_work)
        except (QueueFullError, RuntimeError) as e:
            logger.warning("Slock executor submit rejected for chat %s: %s", chat_id, repr(e))
            busy_card = busy_card_fn()
            if card_message_id:
                self.update_card(card_message_id, busy_card)

    # ------------------------------------------------------------------
    # Message routing (non-command messages in slock-active chats)
    # ------------------------------------------------------------------

    def handle_message(
        self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None
    ):
        """Route a non-command message in a slock-active chat.

        Three routing modes:
        1. @AgentName → precise route to named agent
        2. /task keyword → redirect to handle_slock_command
        3. Normal text → engine.execute() smart routing

        Execution is submitted asynchronously to the engine's thread pool.
        A placeholder card is sent immediately and updated in-place upon completion.
        """
        import re

        # Check for /task keyword — redirect to command handler
        if text and text.strip().lower().startswith("/task"):
            self.handle_slock_command(message_id, chat_id, text, project)
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            return  # passthrough silently if no engine

        # Check for @AgentName mention — precise routing
        at_match = re.search(r"@([\w\-]+)", text or "")
        target_agent = None
        if at_match:
            agent_name = at_match.group(1)
            target_agent = engine.registry.find_by_name(agent_name)

        # --- Callbacks for _execute_async ---
        def _execute():
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            if target_agent:
                return engine._execute_agent(target_agent, text, callbacks)
            return engine.execute(text, callbacks, sender_id="")

        def _result_card(result: str, duration: float) -> str:
            agent_used = target_agent
            if not agent_used:
                channel_id = engine.channel.channel_id if engine.channel else chat_id
                agents = engine.registry.list_agents(channel_id=channel_id)
                agent_used = agents[0] if agents else None

            if agent_used:
                card_data = engine._mouthpiece.format_card(
                    agent_used, result, model_info=agent_used.agent_type, duration_s=duration
                )
                return json.dumps(card_data, ensure_ascii=False)
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "💬 Agent 回复"}, "template": "blue"},
                "body": {"elements": [{"tag": "markdown", "content": result}]},
            }, ensure_ascii=False)

        def _error_card(exc: Exception) -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "❌ 执行出错"}, "template": "red"},
                "body": {"elements": [{"tag": "markdown", "content": f"Agent 执行出错: {safe_error_message(exc)}"}]},
            }, ensure_ascii=False)

        def _empty_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "✅ 处理完成"}, "template": "green"},
                "body": {"elements": [{"tag": "markdown", "content": "Agent 已处理，无额外输出。"}]},
            }, ensure_ascii=False)

        def _busy_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 团队繁忙"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": "当前所有 Agent 均在忙碌中，请稍后重试。"}]},
            }, ensure_ascii=False)

        placeholder_card = json.dumps({
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "⏳ 正在处理..."}, "template": "indigo"},
            "body": {"elements": [{"tag": "markdown", "content": "Agent 正在思考中，请稍候..."}]},
        }, ensure_ascii=False)

        self._execute_async(
            engine=engine,
            execute_fn=_execute,
            placeholder_card_json=placeholder_card,
            result_card_fn=_result_card,
            error_card_fn=_error_card,
            empty_card_fn=_empty_card,
            busy_card_fn=_busy_card,
            message_id=message_id,
            chat_id=chat_id,
        )

    # ------------------------------------------------------------------
    # Slock activation
    # ------------------------------------------------------------------

    def activate_slock(
        self, message_id: str, chat_id: str, requirement: str = "", project: Optional["ProjectContext"] = None
    ):
        """Activate slock mode for the current chat."""
        project = self._ensure_project(message_id, chat_id, project)
        if not project:
            return

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        manager = self._get_engine_manager()

        # Check if already active
        existing = manager.get_activated_engine(chat_id)
        if existing:
            self.reply_text(
                message_id,
                "⚠️ 当前已有 Slock 协作团队在运行\n\n"
                "发送 `/slock status` 查看状态\n"
                "发送 `/slock stop` 停止",
            )
            return

        from ...thread.manager import get_current_sender_id

        sender_open_id = get_current_sender_id() or ""

        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        engine = manager.get_or_create(
            chat_id, root_path, engine_name=engine_name,
        )

        # Create and activate channel
        channel = SlockChannel(
            channel_id=chat_id,
            name=project.project_name if project else "slock",
            team_name=project.project_name if project else "Team",
            owner_id=sender_open_id,
        )
        engine.activate_channel(channel)
        manager.register_managed_chat(chat_id)

        # Wire UI callbacks for escalation timeout notifications
        engine.set_escalation_ui_callbacks(
            update_card_fn=self.update_card,
            send_text_fn=self.send_text_to_chat,
        )

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        # Use unified welcome card with team/channel info prepended
        from ...slock_engine.card_templates import build_welcome_card

        welcome_card = build_welcome_card(team_name=channel.team_name)
        # Prepend team/channel metadata to the welcome card body
        team_info_element = {
            "tag": "markdown",
            "content": (
                f"**团队**: {channel.team_name}\n"
                f"**频道**: {channel.name}"
            ),
        }
        welcome_card["body"]["elements"].insert(0, team_info_element)
        welcome_card["header"]["title"]["content"] = "🎭 Slock 协作模式已激活"

        session = self.create_static_card_session(chat_id, reply_to=message_id)
        session.send(welcome_card)
        session.close()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def show_slock_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show slock engine status."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))

        if not engine:
            _msg_type, card_content = CardBuilder.build_info_card(
                project=project,
                title="🎭 Slock 状态",
                content="当前没有活跃的 Slock 协作团队\n\n发送 `/slock` 激活协作模式",
                engine_name=engine_name,
                show_buttons=False,
            )
            self.reply_card(message_id, card_content)
            return

        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        card_content = json.dumps(status_card, ensure_ascii=False)
        self.reply_card(message_id, card_content)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def show_slock_help(self, message_id: str):
        """Show slock help information."""
        help_text = (
            "🎭 **Slock 协作模式 — 命令帮助**\n\n"
            "**激活 & 状态**\n"
            "• `/slock` — 激活协作模式\n"
            "• `/slock status` — 查看团队状态\n"
            "• `/slock list` / `/slocks` — 在主对话查询所有 Slock 群并跳转\n"
            "• `/slock stop` — 停止协作\n\n"
            "**团队管理**\n"
            "• `/new-team <名称>` — 创建带 `[Slock]` 后缀的协作团队群\n"
            "• `/team list` — 查看团队列表\n"
            "• `/team status <名称>` — 查看团队详情\n"
            "• `/team dissolve <名称>` — 解散团队\n\n"
            "**角色管理**\n"
            "• `/new-role <名称>` — 打开工具选择卡片，再选择模型创建虚拟 Agent\n"
            "• `/new-role <名称> --tool codex --model <模型> --role coder` — 命令式指定工具/模型/角色类型\n"
            "• `/new-role <名称> --template coder` — 从内置模板创建 Agent\n"
            "• `/new-role <名称> --fork <已有角色>` — 复制角色的指令、记忆和技能画像\n"
            "• `/role list` — 查看所有角色\n"
            "• `/role info <名称>` — 查看角色记忆、任务统计和技能画像\n"
            "• `/role remove <名称>` — 移除角色\n\n"
            "**任务管理**\n"
            "• `/task list` — 查看任务列表\n"
            "• `/task assign <任务> [角色]` — 分配任务；省略角色时按技能画像自动选择\n"
            "• `/task assign \"多词任务\" \"角色名\"` — 支持引号包裹多词任务和角色\n"
            "• `/task status` — 查看 Kanban 任务进度"
        )
        self.reply_text(message_id, help_text)

    # ------------------------------------------------------------------
    # Team management
    # ------------------------------------------------------------------

    def create_team(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Create a new Feishu group and activate slock runtime inside it.

        Flow: validate → create group → promote sender → init engine →
              activate channel (workspace) → register managed chat →
              send welcome in new group → send jump card in original group.
        On failure: rollback by deleting the created group.
        """
        if not name:
            self.reply_text(message_id, "请提供团队名称\n\n用法: `/new-team <团队名称>`")
            return

        from ...project_chat.lark_chat_client import LarkChatClient
        from ...thread.manager import get_current_sender_id

        sender_open_id = get_current_sender_id() or ""
        if not sender_open_id:
            self.reply_text(message_id, "❌ 无法获取发送者信息，请重试")
            return

        settings = self.ctx.settings
        group_name = self._format_slock_group_name(name, getattr(settings, "slock_team_name_suffix", "[Slock]"))

        # Step 1: Create Feishu group
        lark_client = LarkChatClient(api_client_factory=self.ctx.api_client_factory)
        try:
            result = lark_client.create_chat(
                name=group_name,
                description=f"Slock 协作团队: {name}",
                user_id_list=[sender_open_id],
            )
        except Exception as e:
            logger.error("create_team: 建群失败 name=%s err=%s", name, str(e))
            self.reply_text(message_id, f"❌ 创建团队群失败: {safe_error_message(e)}")
            return

        new_chat_id = result.chat_id

        try:
            # Step 2: Promote sender to group manager
            lark_client.add_managers(new_chat_id, [sender_open_id])

            # Step 3: Initialize slock engine for the new group
            root_path = project.root_path if project else self.get_working_dir(chat_id)
            manager = self._get_engine_manager()
            engine_name = self.get_engine_name(
                new_chat_id, project_id=(project.project_id if project else None)
            )
            engine = manager.get_or_create(new_chat_id, root_path, engine_name=engine_name)

            # Step 4: Activate channel (creates workspace directory + marker)
            channel = SlockChannel(
                channel_id=new_chat_id,
                name=group_name,
                team_name=name,
                owner_id=sender_open_id,
            )
            engine.activate_channel(channel)

            # Wire UI callbacks for escalation timeout notifications
            engine.set_escalation_ui_callbacks(
                update_card_fn=self.update_card,
                send_text_fn=self.send_text_to_chat,
            )

            # Step 5: Register managed chat for event routing
            manager.register_managed_chat(new_chat_id)

            # Step 6: Send welcome card in the new group
            from ...slock_engine.card_templates import build_welcome_card

            welcome_card = build_welcome_card(team_name=name)
            self.send_card_to_chat(new_chat_id, json.dumps(welcome_card, ensure_ascii=False))

            # Step 7: Send confirmation with jump link in original group
            from ...slock_engine.card_templates import build_team_created_card

            card = build_team_created_card(
                team_name=name,
                group_name=group_name,
                channel_id=new_chat_id,
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

        except Exception as e:
            # Rollback: delete the created group on any activation failure
            logger.error("create_team: 激活失败, 回滚建群 chat=%s err=%s", new_chat_id, str(e))
            lark_client.delete_chat(new_chat_id)
            self.reply_text(message_id, f"❌ 团队激活失败已回滚: {safe_error_message(e)}")

    @staticmethod
    def _format_slock_group_name(name: str, suffix: str = "[Slock]") -> str:
        """Format Slock team group names with a visible suffix marker."""
        clean_name = (name or "").strip()
        clean_suffix = (suffix or "").strip()
        if not clean_suffix:
            return clean_name
        if clean_name.casefold().endswith(clean_suffix.casefold()):
            return clean_name
        separator = "" if suffix.startswith((" ", "-", "_", "/", "|", "｜", ":", "：")) else " "
        return f"{clean_name}{separator}{suffix}"

    def list_teams(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all active Slock teams."""
        manager = self._get_engine_manager()
        engines = manager.list_activated_engines()

        if not engines:
            self.reply_text(message_id, "当前没有活跃的团队\n\n发送 `/slock` 激活协作模式")
            return

        teams = []
        for engine in sorted(engines, key=lambda item: (item.channel.team_name if item.channel else "")):
            channel = engine.channel
            if not channel:
                continue
            agents = engine.registry.list_agents(channel_id=channel.channel_id)
            agent_count = len(agents)
            task_count = len(engine.tasks)
            teams.append(
                {
                    "team_name": channel.team_name or channel.name or channel.channel_id,
                    "name": channel.name,
                    "channel_id": channel.channel_id,
                    "agent_count": agent_count,
                    "task_count": task_count,
                }
            )

        from ...slock_engine.card_templates import build_team_list_card

        self.reply_card(message_id, json.dumps(build_team_list_card(teams), ensure_ascii=False))

    def show_team_status(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Show status of a specific team."""
        manager = self._get_engine_manager()
        engine = manager.find_team(name) if name else manager.get_activated_engine(chat_id)
        if not engine or not engine.channel:
            self.reply_text(message_id, f"未找到团队: **{name}**" if name else "当前没有活跃的团队")
            return

        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        self.reply_card(message_id, json.dumps(status_card, ensure_ascii=False))

    def dissolve_team(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Dissolve (stop) a team."""
        manager = self._get_engine_manager()
        engine = manager.find_team(name) if name else manager.get_activated_engine(chat_id)
        if not engine or not engine.channel:
            self.reply_text(message_id, f"未找到团队: **{name}**" if name else "当前没有活跃的团队")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        target_chat_id = engine.channel.channel_id
        team_name = engine.channel.team_name or engine.channel.name or target_chat_id
        engine.deactivate()
        manager.unregister_managed_chat(target_chat_id)
        manager.remove(target_chat_id, engine.root_path)
        self.reply_text(message_id, f"✅ 团队 **{team_name}** 已停止并归档本地状态")

    # ------------------------------------------------------------------
    # Role / Agent management
    # ------------------------------------------------------------------

    # tool_type → default role inference mapping
    TOOL_TYPE_ROLE_MAP: dict[str, str] = {
        "codex": "coder",
        "claude": "reviewer",
        "coco": "writer",
        "aiden": "coder",
        "gemini": "coder",
        "ttadk": "custom",
    }
    TOOL_SELECT_OPTIONS: tuple[dict[str, str], ...] = (
        {"name": "coco", "label": "Coco", "emoji": "🥥", "description": "默认协作工具"},
        {"name": "codex", "label": "Codex", "emoji": "🧠", "description": "代码实现"},
        {"name": "aiden", "label": "Aiden", "emoji": "🎯", "description": "AI 编程助手"},
        {"name": "claude", "label": "Claude", "emoji": "📝", "description": "评审与长文"},
        {"name": "gemini", "label": "Gemini", "emoji": "💎", "description": "多模态与代码"},
    )

    def create_role(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Create a new virtual agent role.

        Supports parameter syntax:
            /new-role <name> [--tool <type>] [--model <model>] [--emoji <e>] [--role <role>] [--prompt <text>]
        """
        if not name:
            self.reply_text(
                message_id,
                "请提供角色名称\n\n用法: `/new-role <角色名称>` [--tool codex] [--model o3-pro] "
                "[--emoji 🔧] [--role coder] [--prompt <text>]",
            )
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        # Permission gate: only admin or channel owner may create roles.
        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        # Parse optional arguments from the name/args string
        try:
            tokens = shlex.split(name)
        except ValueError:
            tokens = name.split()

        role_name = tokens[0] if tokens else name
        if len(tokens) == 1:
            self.show_new_role_tool_selection(message_id, role_name, project)
            return

        tool_type = "coco"
        model_name = ""
        emoji = "🤖"
        system_prompt = ""
        explicit_role: str | None = None
        template_name = ""
        fork_name = ""
        tool_explicit = False
        model_explicit = False
        emoji_explicit = False
        prompt_explicit = False

        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--tool" and i + 1 < len(tokens):
                tool_type = tokens[i + 1]
                tool_explicit = True
                i += 2
            elif tok == "--model" and i + 1 < len(tokens):
                model_name = tokens[i + 1]
                model_explicit = True
                i += 2
            elif tok == "--emoji" and i + 1 < len(tokens):
                emoji = tokens[i + 1]
                emoji_explicit = True
                i += 2
            elif tok == "--role" and i + 1 < len(tokens):
                explicit_role = tokens[i + 1]
                i += 2
            elif tok == "--prompt" and i + 1 < len(tokens):
                system_prompt = tokens[i + 1]
                prompt_explicit = True
                i += 2
            elif tok == "--template" and i + 1 < len(tokens):
                template_name = tokens[i + 1]
                i += 2
            elif tok == "--fork" and i + 1 < len(tokens):
                fork_name = tokens[i + 1]
                i += 2
            else:
                i += 1

        # Validate tool_type against whitelist
        from ...slock_engine.models import AGENT_ROLE_COLORS, AgentIdentity, SlockMemory

        template_data: dict = {}
        if template_name:
            raw_template = engine.memory.read_agent_template(template_name)
            template_data = raw_template if isinstance(raw_template, dict) else {}
            if not template_data:
                self.reply_text(message_id, f"❌ 未找到 Agent 模板: `{template_name}`")
                return
            if not tool_explicit:
                tool_type = template_data.get("tool_type", tool_type)
            if not model_explicit:
                model_name = template_data.get("model_name", model_name)
            if not emoji_explicit:
                emoji = template_data.get("emoji", emoji)
            if explicit_role is None:
                explicit_role = template_data.get("role") or explicit_role
            if not prompt_explicit:
                system_prompt = template_data.get("system_prompt", system_prompt)

        fork_source: AgentIdentity | None = None
        if fork_name:
            fork_source = engine.registry.find_by_name(fork_name, channel_id=chat_id) or engine.registry.find_by_name(fork_name)
            if not isinstance(fork_source, AgentIdentity):
                self.reply_text(message_id, f"❌ 未找到可 fork 的角色: `{fork_name}`")
                return
            if not tool_explicit:
                tool_type = fork_source.agent_type
            if not model_explicit:
                model_name = fork_source.model_name
            if not emoji_explicit:
                emoji = fork_source.emoji
            if explicit_role is None:
                explicit_role = fork_source.role
            if not prompt_explicit:
                system_prompt = fork_source.system_prompt

        VALID_TOOLS = set(self.TOOL_TYPE_ROLE_MAP.keys())
        if tool_type not in VALID_TOOLS:
            self.reply_text(
                message_id,
                f"❌ 无效的 tool_type: `{tool_type}`\n"
                f"合法值: {', '.join(sorted(VALID_TOOLS))}",
            )
            return

        # Validate explicit role against whitelist
        VALID_ROLES = set(AGENT_ROLE_COLORS.keys())
        if explicit_role and explicit_role not in VALID_ROLES:
            self.reply_text(
                message_id,
                f"❌ 无效的 role: `{explicit_role}`\n"
                f"合法值: {', '.join(sorted(VALID_ROLES))}",
            )
            return

        # Determine role: explicit --role takes priority, otherwise infer from tool_type
        if explicit_role:
            role = explicit_role
        else:
            role = self.TOOL_TYPE_ROLE_MAP.get(tool_type, "custom")

        agent_id = f"{tool_type}:{model_name or 'default'}:{role_name}"
        existing_raw = engine.registry.get(agent_id)
        existing_agent = existing_raw if isinstance(existing_raw, AgentIdentity) else None
        if existing_agent and not system_prompt:
            system_prompt = existing_agent.system_prompt
        if not system_prompt:
            system_prompt = self._build_default_directive(
                role_name=role_name,
                role=role,
                tool_type=tool_type,
                model_name=model_name,
                team_name=(engine.channel.team_name if engine.channel else chat_id),
            )

        agent = AgentIdentity(
            agent_id=agent_id,
            name=role_name,
            emoji=emoji,
            agent_type=tool_type,
            model_name=model_name,
            system_prompt=system_prompt,
            role=role,
            owner_group=chat_id,
            member_groups=[chat_id],
        )
        memory_path = engine.memory.agent_memory_path(agent.agent_id)
        agent.memory_path = memory_path
        if not existing_agent:
            if fork_source is not None:
                source_memory = engine.memory.read_agent_memory(fork_source.agent_id)
                active_context = source_memory.active_context
                fork_entry = f"Forked from {fork_source.agent_id} into Slock team {chat_id}."
                active_context = f"{active_context}\n{fork_entry}".strip() if active_context else fork_entry
                engine.memory.write_agent_memory(
                    agent.agent_id,
                    SlockMemory(
                        role=source_memory.role or system_prompt,
                        key_knowledge=source_memory.key_knowledge,
                        active_context=active_context,
                    ),
                )
                source_profiles = engine.memory.read_skill_profiles(fork_source.agent_id)
                engine.memory.write_skill_profiles(agent.agent_id, source_profiles)
            else:
                key_knowledge = template_data.get("key_knowledge") or (
                    f"tool_type={tool_type}\nmodel={model_name or 'default'}\nrole={role}"
                )
                engine.memory.write_agent_memory(
                    agent.agent_id,
                    SlockMemory(
                        role=system_prompt,
                        key_knowledge=key_knowledge,
                        active_context=f"Created in Slock team {chat_id}.",
                    ),
                )
        else:
            engine.memory.update_agent_context(agent.agent_id, f"Joined Slock team {chat_id}.")
        engine.registry.register(agent)

        self.reply_text(
            message_id,
            f"✅ 角色 **{agent.emoji} {agent.name}** 已创建 (ID: `{agent.agent_id[:8]}`)\n"
            f"   工具: `{tool_type}` | 模型: `{model_name or '默认'}` | 角色: `{role}` | Emoji: {emoji}",
        )

    def show_new_role_tool_selection(
        self,
        message_id: str,
        role_name: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Show the first `/new-role` interactive step: choose a backing tool."""

        _, card_content = CardBuilder.build_slock_role_tool_select_card(
            role_name,
            list(self.TOOL_SELECT_OPTIONS),
            project_id=(project.project_id if project else None),
        )
        self.reply_card(message_id, card_content)

    def handle_new_role_select_tool(
        self,
        message_id: str,
        chat_id: str,
        value: dict,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Handle `/new-role` tool selection and show the ACP model picker."""

        role_name = str(value.get("role_name") or "").strip()
        tool_name = str(value.get("tool_name") or "").strip().lower()
        if not role_name or not tool_name:
            self.reply_text(message_id, "请选择有效的 Slock 角色工具")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        cwd = getattr(project, "root_path", None) or getattr(engine, "root_path", None) or self.get_working_dir(chat_id)
        models = fetch_acp_models(tool_name, cwd=cwd, current_model=None)
        _, card_content = CardBuilder.build_acp_model_select_card(
            models,
            tool_name,
            project_id=(project.project_id if project else value.get("project_id")),
            action_name=action_ids.SLOCK_NEW_ROLE_SELECT_MODEL,
            value_extra={"role_name": role_name},
            context_markdown=f"角色: **{role_name}**",
            refresh_action_name="",
        )
        self.reply_card(message_id, card_content)

    def handle_new_role_select_model(
        self,
        message_id: str,
        chat_id: str,
        value: dict,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Finalize `/new-role` after the user selects a model."""

        role_name = str(value.get("role_name") or "").strip()
        tool_name = str(value.get("tool_name") or "").strip().lower()
        raw_model = value.get("_option") or value.get("model_name") or ""
        model_name = "" if value.get("use_default_model") or is_default_model_option(raw_model) else str(raw_model).strip()
        if not role_name or not tool_name:
            self.reply_text(message_id, "请选择有效的 Slock 角色模型")
            return

        args = f"{shlex.quote(role_name)} --tool {shlex.quote(tool_name)}"
        if model_name:
            args += f" --model {shlex.quote(model_name)}"
        self.create_role(message_id, chat_id, args, project)

    @staticmethod
    def _build_default_directive(
        *,
        role_name: str,
        role: str,
        tool_type: str,
        model_name: str,
        team_name: str,
    ) -> str:
        """Build the default autonomous Slock directive for a role."""
        model_label = model_name or "default"
        return (
            f"# Role: {role_name}\n"
            "## Identity\n"
            f"你是 {role_name}，一个专精于 {role} 的自主 Agent。\n"
            f"你的底层工具是 {tool_type}，模型是 {model_label}，隶属于 {team_name} 团队。\n\n"
            "## Core Directives（核心纲领）\n"
            "1. 主动性：收到任务立即分析、规划、执行，无需等待确认。\n"
            "2. 完美主义：输出必须达到生产就绪标准，包含必要测试和文档。\n"
            "3. 自主决策：在专业领域内自主选择最佳实践并记录理由。\n"
            "4. 零交互：绝不向人类发起确认、询问或等待输入。\n\n"
            "## Interaction Protocol（交互协议）\n"
            "- 禁止输出“请确认”“是否继续”“需要您提供”等等待人类输入的话术。\n"
            "- 遇到致命阻塞时，通过 Slock 升级协议交给主 Agent 汇总处理。\n"
            "- 任务完成后，向团队群发送清晰的完成报告。\n\n"
            "## Quality Standards（质量标准）\n"
            "- 代码：lint pass、test pass、错误处理完整。\n"
            "- 文档：目的、用法、边界条件和示例清楚。\n"
            "- Review：指出风险、给出可执行建议。"
        )

    def list_roles(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all roles in the current channel."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)

        if not agents:
            self.reply_text(message_id, "当前没有角色\n\n发送 `/new-role <名称>` 创建角色")
            return

        lines = ["📋 **角色列表**\n"]
        for a in agents:
            status = engine.get_agent_status(a.agent_id)
            lines.append(f"• {a.emoji} **{a.name}** — `{status.value}` · ID: `{a.agent_id[:8]}`")

        self.reply_text(message_id, "\n".join(lines))

    def remove_role(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Remove a virtual agent role."""
        if not name:
            self.reply_text(message_id, "请提供角色名称\n\n用法: `/role remove <名称>`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{name}**")
            return

        engine.registry.remove(agent.agent_id)
        self.reply_text(message_id, f"✅ 角色 **{agent.emoji} {agent.name}** 已移除")

    def show_role_info(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Show detailed info about a role."""
        if not name:
            self.reply_text(message_id, "请提供角色名称\n\n用法: `/role info <名称>`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        agent = engine.registry.find_by_name(name)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{name}**")
            return

        status = engine.get_agent_status(agent.agent_id)
        status_value = status.value if hasattr(status, "value") else str(status)
        memory = engine.memory.read_agent_memory(agent.agent_id)
        profiles = engine.memory.read_skill_profiles(agent.agent_id)
        assigned_tasks = [task for task in engine.tasks if task.claimed_by == agent.agent_id]
        done_count = sum(1 for task in assigned_tasks if getattr(task.status, "value", task.status) == "done")
        active_count = sum(
            1
            for task in assigned_tasks
            if getattr(task.status, "value", task.status) in {"in_progress", "in_review"}
        )
        memory_lines: list[str] = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        if memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")
        profile_lines = [
            f"• {profile.tag}: 成功率 {profile.success_rate:.0f}% · {profile.total_tasks} 次"
            for profile in profiles[:6]
        ]
        info = (
            f"{agent.emoji} **{agent.name}**\n\n"
            f"• ID: `{agent.agent_id[:8]}`\n"
            f"• 类型: `{agent.agent_type}`\n"
            f"• 模型: `{agent.model_name or 'default'}`\n"
            f"• 角色: {agent.role or '(未设置)'}\n"
            f"• 状态: `{status_value}`\n"
            f"• 权限: `{', '.join(agent.permissions) if agent.permissions else '默认'}`\n\n"
            "**记忆摘要**\n"
            f"{chr(10).join(memory_lines) if memory_lines else '• 暂无记忆'}\n\n"
            "**历史任务**\n"
            f"• 总数: {len(assigned_tasks)} · 已完成: {done_count} · 进行中: {active_count}\n\n"
            "**技能画像**\n"
            f"{chr(10).join(profile_lines) if profile_lines else '• 暂无技能画像'}"
        )
        self.reply_text(message_id, info)

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------

    def list_tasks(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List all tasks in the current slock session."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        tasks = engine.tasks
        if not tasks:
            self.reply_text(message_id, "当前没有任务\n\n发送 `/task assign <任务> <角色>` 分配任务")
            return

        lines = ["📋 **任务列表**\n"]
        for t in tasks:
            assignee = t.claimed_by[:8] if t.claimed_by else "未分配"
            lines.append(f"• `{t.task_id[:8]}` — {t.content[:60]} · `{t.status.value}` · 🧑 {assignee}")

        self.reply_text(message_id, "\n".join(lines))

    def assign_task(
        self,
        message_id: str,
        chat_id: str,
        content: str = "",
        role_name: str = "",
        project: Optional["ProjectContext"] = None,
    ):
        """Assign a task to a specific role and execute it.

        If role_name is provided: claim → execute → complete/rollback.
        If no role_name: create task only (unassigned).
        """
        if not content:
            self.reply_text(message_id, "请提供任务内容\n\n用法: `/task assign <任务内容> <角色名称>`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        # Rate-limit check for non-admin/non-owner users
        if not self._check_assign_rate_limit(engine, message_id, chat_id):
            return

        task = engine.add_task(content)
        if task is None:
            self.reply_text(
                message_id,
                "❌ 任务创建失败（任务队列已满或内部错误），请稍后重试",
            )
            return

        if role_name:
            agent = engine.registry.find_by_name(role_name)
            if not agent:
                self.reply_text(
                    message_id,
                    f"⚠️ 任务已创建但未找到角色 **{role_name}**\n"
                    f"• 任务 ID: `{task.task_id[:8]}`\n"
                    f"• 发送 `/role list` 查看可用角色",
                )
                return

            # Claim the task
            if not engine.claim_task(task.task_id, agent.agent_id):
                self.reply_text(message_id, f"❌ 任务 claim 失败，{agent.name} 可能正在执行其他任务")
                return

            # Submit async execution — send placeholder, update on completion
            self._submit_task_execution(
                engine, task, agent, message_id, chat_id, content, project
            )
            return

        # No role specified — use skill-based automatic assignment.
        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = list(engine.registry.list_agents(channel_id=channel_id))
        if not agents:
            self.reply_text(
                message_id,
                f"✅ 任务已创建（等待分配）\n"
                f"• ID: `{task.task_id[:8]}`\n"
                f"• 内容: {content[:80]}\n"
                f"• 当前没有可用角色，发送 `/new-role <名称>` 创建角色",
            )
            return

        agent = engine.router.route_message(content, agents)
        if not agent:
            self.reply_text(
                message_id,
                f"✅ 任务已创建（等待分配）\n"
                f"• ID: `{task.task_id[:8]}`\n"
                f"• 内容: {content[:80]}\n"
                f"• 暂无匹配角色，发送 `/role list` 查看可用角色",
            )
            return

        if not engine.claim_task(task.task_id, agent.agent_id):
            self.reply_text(message_id, f"❌ 任务 claim 失败，{agent.name} 可能正在执行其他任务")
            return

        self._submit_task_execution(
            engine, task, agent, message_id, chat_id, content, project, auto_routed=True
        )

    def _submit_task_execution(
        self,
        engine,
        task,
        agent,
        message_id: str,
        chat_id: str,
        content: str,
        project: Optional["ProjectContext"],
        *,
        auto_routed: bool = False,
    ) -> None:
        """Submit task execution to the engine's thread pool asynchronously.

        Sends a placeholder card immediately, then updates it with the result
        or error when execution completes.
        """
        prefix = "自动分配给" if auto_routed else "已分配给"

        def _execute():
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            return engine.execute_task(task.task_id, agent.agent_id, callbacks)

        def _result_card(result: str, duration: float) -> str:
            card_data = engine._mouthpiece.format_card(
                agent, result, model_info=agent.agent_type, duration_s=duration
            )
            return json.dumps(card_data, ensure_ascii=False)

        def _error_card(exc: Exception) -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "❌ 任务执行失败"}, "template": "red"},
                "body": {"elements": [{"tag": "markdown", "content": (
                    f"Agent: {agent.emoji} {agent.name}\n"
                    f"任务: {content[:60]}\n"
                    f"错误: {safe_error_message(exc)}\n\n"
                    "任务已回退为 TODO，可重新分配"
                )}]},
            }, ensure_ascii=False)

        def _empty_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 任务完成无输出"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": (
                    f"Agent: {agent.emoji} {agent.name}\n"
                    f"任务: {content[:60]}\n\n"
                    "任务已回退为 TODO，可重新分配"
                )}]},
            }, ensure_ascii=False)

        def _busy_card() -> str:
            return json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 团队繁忙"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": "当前所有 Agent 均在忙碌中，请稍后重试。"}]},
            }, ensure_ascii=False)

        placeholder_card = json.dumps({
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"⏳ 任务{prefix} {agent.emoji} {agent.name}"},
                "template": "indigo",
            },
            "body": {"elements": [{"tag": "markdown", "content": (
                f"**任务**: {content[:80]}\n"
                f"**ID**: `{task.task_id[:8]}`\n\n"
                "Agent 正在执行中..."
            )}]},
        }, ensure_ascii=False)

        self._execute_async(
            engine=engine,
            execute_fn=_execute,
            placeholder_card_json=placeholder_card,
            result_card_fn=_result_card,
            error_card_fn=_error_card,
            empty_card_fn=_empty_card,
            busy_card_fn=_busy_card,
            message_id=message_id,
            chat_id=chat_id,
        )

    def show_task_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show task board with status summary."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "当前没有活跃的 Slock 团队")
            return

        from ...slock_engine.card_templates import build_task_board_card

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)
        team_name = engine.channel.team_name if engine.channel else ""
        card = build_task_board_card(engine.tasks, agents, team_name=team_name, channel_id=channel_id)
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop_slock_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Stop the slock engine, unregister managed chat, and clean up."""
        manager = self._get_engine_manager()

        # Deactivate the engine if it exists
        engine = manager.get_activated_engine(chat_id)
        if engine:
            if not self._check_slock_permission(engine, message_id, chat_id):
                return
            engine.deactivate()

        # Unregister managed chat so dispatcher stops routing to this engine
        manager.unregister_managed_chat(chat_id)

        self._safe_lifecycle_action(
            lambda: self._stop_engine_generic(message_id, chat_id, project),
            "stop", chat_id, message_id, project,
        )

    def _stop_single_agent(self, message_id: str, chat_id: str, value: dict):
        """Stop a single agent by agent_id from card action value.

        Falls back to full engine stop if agent_id is not provided (backwards compat).
        """
        agent_id = value.get("agent_id", "")

        if not agent_id:
            # Fallback: no agent_id means legacy card — stop the whole engine
            self.stop_slock_engine(message_id, chat_id)
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式。")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        stopped = engine.stop_agent(agent_id)
        if stopped:
            self.send_text_to_chat(chat_id, "⏹ Agent 已停止，状态已重置为 IDLE。")
        else:
            self.send_text_to_chat(chat_id, "⚠️ 未找到该 Agent 或其已处于空闲状态。")

    # ------------------------------------------------------------------
    # Card action handler
    # ------------------------------------------------------------------

    def _refresh_status_card(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Rebuild and update the status panel card in-place."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式，无法刷新状态。")
            return
        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        card_content = json.dumps(status_card, ensure_ascii=False)
        self.update_card(message_id, card_content)

    def _refresh_task_board_card(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Rebuild and update the task board card in-place."""
        from ...slock_engine.card_templates import build_task_board_card

        manager = self._get_engine_manager()
        engine = manager.get_active_engine(chat_id)
        if not engine:
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式，无法刷新任务看板。")
            return
        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agents = engine.registry.list_agents(channel_id=channel_id)
        team_name = engine.channel.team_name if engine.channel else ""
        card = build_task_board_card(engine.tasks, agents, team_name=team_name, channel_id=channel_id)
        self.update_card(message_id, json.dumps(card, ensure_ascii=False))

    def _check_slock_permission(self, engine, message_id: str, chat_id: str) -> bool:
        """Check if current operator is admin or channel owner. Returns True if authorized."""
        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        operator_id = get_current_sender_id() or ""
        settings = get_settings()
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()
        channel_owner_id = ""
        if engine.channel:
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

        is_authorized = (
            (operator_id and operator_id in admin_ids)
            or (operator_id and channel_owner_id and operator_id == channel_owner_id)
        )
        if not is_authorized:
            perm_msg = "⚠️ 权限不足，仅管理员或团队创建者可执行此操作。"
            if not self.reply_text(message_id, perm_msg):
                self.send_text_to_chat(chat_id, perm_msg)
            return False
        return True

    def _check_assign_rate_limit(self, engine, message_id: str, chat_id: str) -> bool:
        """Check rate-limit for task assignment. Admin/owner bypass. Returns True if allowed."""
        import time as _time

        from ...config import get_settings
        from ...thread.manager import get_current_sender_id

        operator_id = get_current_sender_id() or ""
        settings = get_settings()
        admin_ids = settings.admin_user_ids if hasattr(settings, "admin_user_ids") else frozenset()
        channel_owner_id = ""
        if engine.channel:
            channel_owner_id = getattr(engine.channel, "owner_id", "") or ""

        # Admin and owner bypass rate-limit
        is_privileged = (
            (operator_id and operator_id in admin_ids)
            or (operator_id and channel_owner_id and operator_id == channel_owner_id)
        )
        if is_privileged:
            return True

        # Rate-limit for regular users: sliding window of 60s
        rate_limit = settings.slock_assign_rate_limit
        tracker_key = f"{chat_id}:{operator_id}"
        now = _time.time()
        window = 60.0

        timestamps = self._rate_limit_tracker.get(tracker_key, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if now - t < window]

        if len(timestamps) >= rate_limit:
            self.reply_text(
                message_id,
                f"⚠️ 任务提交频率超限（每分钟最多 {rate_limit} 次），请稍后重试。",
            )
            self._rate_limit_tracker[tracker_key] = timestamps
            return False

        timestamps.append(now)
        self._rate_limit_tracker[tracker_key] = timestamps
        return True

    def _check_queue_wait_timeout(self, future, start_time: float, card_message_id: str, chat_id: str) -> bool:
        """Check if task waited too long in queue. Returns True if timed out (caller should abort).

        If future is None (e.g. synchronous executor in tests where the work runs
        before submit() returns), skip timeout detection entirely — the task is
        already executing, so queue-wait is irrelevant.
        """
        import json as _json
        import time

        from ...config import get_settings as _get_settings

        if future is None:
            return False

        _settings = _get_settings()
        _enqueue_time = getattr(future, "enqueue_time", None)
        if _enqueue_time is None:
            _enqueue_time = start_time
        enqueue_elapsed = time.time() - _enqueue_time
        if enqueue_elapsed > _settings.slock_queue_wait_timeout:
            logger.warning(
                "Slock queue wait timeout (%.1fs > %ds) for chat %s",
                enqueue_elapsed, _settings.slock_queue_wait_timeout, chat_id,
            )
            timeout_card = _json.dumps({
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "⏱️ 排队超时"}, "template": "orange"},
                "body": {"elements": [{"tag": "markdown", "content": "消息在队列中等待过久，已自动取消。请稍后重试。"}]},
            }, ensure_ascii=False)
            if card_message_id:
                self.update_card(card_message_id, timeout_card)
            return True
        return False

    def _resolve_escalation(self, message_id: str, chat_id: str, value: dict):
        """Handle admin clicking Retry/Skip/Abort on an escalation card.

        Enhanced flow:
        (a) Get operator via thread-local
        (b) Permission check via _check_slock_permission
        (c) Resolution whitelist validation
        (d) Resolve via engine
        (e) Update card to resolved state
        (f) Fallback to text if card update fails
        (g) Handle duplicate clicks gracefully
        """
        from ...slock_engine.card_templates import build_resolved_escalation_card
        from ...thread.manager import get_current_sender_id

        escalation_id = value.get("escalation_id", "")
        resolution = value.get("resolution", "")

        if not escalation_id or not resolution:
            logger.warning("Escalation resolve missing params: escalation_id=%s resolution=%s", escalation_id, resolution)
            self.send_text_to_chat(chat_id, "⚠️ 升级请求处理参数缺失，请重试或联系管理员。")
            return

        # (a) Get operator identity
        operator_id = get_current_sender_id() or ""
        operator_display = resolve_display_name(operator_id) if operator_id else ""

        # (b) Permission check — admin or team owner (unified)
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            logger.warning("No active engine for escalation resolve in chat %s", chat_id)
            self.send_text_to_chat(chat_id, "⚠️ 当前群组未激活 Slock 模式，无法处理升级请求。")
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        # (c) Resolution whitelist validation
        escalation = engine.get_escalation(escalation_id)
        if not escalation:
            self.send_text_to_chat(chat_id, f"⚠️ Escalation `{escalation_id}` 不存在。")
            return

        # Handle duplicate clicks — already resolved
        if escalation.resolved:
            self.send_text_to_chat(chat_id, f"ℹ️ 此升级请求已处理（{escalation.resolution}），无需重复操作。")
            return

        allowed_options = escalation.options or ["重试", "跳过", "中止"]
        resolution_stripped = resolution.strip()
        if resolution_stripped not in allowed_options:
            logger.warning(
                "Escalation resolve invalid resolution: '%s' not in %s",
                resolution, allowed_options,
            )
            self.send_text_to_chat(
                chat_id,
                f"⚠️ 无效的解决选项 `{resolution}`，允许的选项: {', '.join(allowed_options)}",
            )
            return

        # (d) Execute resolve
        resolved = engine.resolve_escalation(escalation_id, resolution_stripped)
        if not resolved:
            # Race condition: resolved between our check and this call
            self.send_text_to_chat(chat_id, "ℹ️ 此升级请求已处理，无需重复操作。")
            return

        # (e) Build resolved card and update in-place
        resolved_card = build_resolved_escalation_card(
            escalation,
            resolved_by=operator_display or operator_id,
            resolution=resolution_stripped,
            resolved_at=resolved.resolved_at,
            channel_id=chat_id,
        )
        card_json = json.dumps(resolved_card, ensure_ascii=False)
        card_updated = self.update_card(message_id, card_json)

        # (f) Fallback to text confirmation if card update fails
        if not card_updated:
            logger.error(
                "Failed to update escalation card: message_id=%s chat_id=%s",
                message_id, chat_id,
            )
            confirm_text = (
                f"✅ Escalation resolved: **{resolved.agent_name}** — {resolution_stripped}\n"
                f"Reason: {redact_sensitive(resolved.reason)}"
            )
            self.send_text_to_chat(chat_id, confirm_text)

        # (g) Trigger agent recovery based on resolution
        engine.resume_after_escalation(resolved)

    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle slock_* card actions."""
        # Escalation resolve needs extra params from value — handle before standard dispatch
        if action_type == "slock_escalation_resolve":
            self._resolve_escalation(open_message_id, open_chat_id, value)
            return

        # Per-agent stop needs agent_id from value — handle before standard dispatch
        if action_type == "slock_stop_agent":
            self._stop_single_agent(open_message_id, open_chat_id, value)
            return

        project_id = value.get("project_id", "")
        target_project = (
            self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
        )

        slock_actions = {
            "slock_stop": self.stop_slock_engine,
            "slock_refresh_status": self._refresh_status_card,
            "slock_refresh_task_board": self._refresh_task_board_card,
        }

        self._dispatch_standard_card_action(CardActionContext(
            open_message_id=open_message_id,
            open_chat_id=open_chat_id,
            action_type=action_type,
            value=value,
            prefix="slock",
            action_map=slock_actions,
            toggle_log_method=self._toggle_log,
            switch_mode_method=self._switch_card_mode,
            toggle_ac_method=self._toggle_ac,
            project=target_project,
        ))

    # ------------------------------------------------------------------
    # Static command detection
    # ------------------------------------------------------------------

    @staticmethod
    def is_slock_command(text: str, chat_id: str | None = None, manager=None) -> bool:
        """Check if text is any slock-related command."""
        return is_slock_command(text, chat_id=chat_id, manager=manager)
