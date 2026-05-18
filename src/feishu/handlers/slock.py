"""Slock Engine handler — multi-Agent mouthpiece collaboration engine."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...slock_engine.models import SlockChannel
from ...slock_engine.slash_commands import (
    SlockCommandAction,
    is_slock_command,
    parse_slock_command,
)
from ...utils.errors import get_error_detail
from ..emoji import EmojiReaction
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

        return SlockEngineCallbacks(
            on_agent_wake=on_agent_wake,
            on_agent_running=on_agent_running,
            on_agent_done=on_agent_done,
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
        """
        import re
        import time as _time

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

        start_time = _time.time()

        if target_agent:
            # Direct agent execution
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            try:
                result = engine._execute_agent(target_agent, text, callbacks)
            except Exception as e:
                logger.error("Slock handle_message agent exec error: %s", repr(e))
                self.reply_text(message_id, f"❌ Agent 执行出错: {get_error_detail(e)}")
                return
        else:
            # Smart routing via engine.execute()
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            try:
                result = engine.execute(text, callbacks, sender_id="")
            except Exception as e:
                logger.error("Slock handle_message engine exec error: %s", repr(e))
                self.reply_text(message_id, f"❌ Slock 引擎执行出错: {get_error_detail(e)}")
                return

        if not result:
            return  # No output — stay silent

        # Send result as Interactive Card
        duration = _time.time() - start_time
        agent_used = target_agent
        if not agent_used:
            # Try to identify which agent responded from engine state
            channel_id = engine.channel.channel_id if engine.channel else chat_id
            agents = engine.registry.list_agents(channel_id=channel_id)
            agent_used = agents[0] if agents else None

        if agent_used:
            card_data = engine._mouthpiece.format_card(
                agent_used, result, model_info=agent_used.agent_type, duration_s=duration
            )
            card_json = json.dumps(card_data, ensure_ascii=False)
            self.send_card_to_chat(chat_id, card_json, origin_message_id=message_id)
        else:
            self.reply_text(message_id, result)

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

        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        engine = manager.get_or_create(
            chat_id, root_path, engine_name=engine_name,
        )

        # Create and activate channel
        channel = SlockChannel(
            channel_id=chat_id,
            name=project.project_name if project else "slock",
            team_name=project.project_name if project else "Team",
        )
        engine.activate_channel(channel)

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        content = (
            "🎭 **Slock 协作模式已激活**\n\n"
            f"**团队**: {channel.team_name}\n"
            f"**频道**: {channel.name}\n\n"
            "📌 **快速开始**:\n"
            "• `/new-role <名称>` — 创建虚拟 Agent\n"
            "• `/task assign <任务> <角色>` — 分配任务\n"
            "• `/slock status` — 查看团队状态\n"
            "• `/slock help` — 查看所有命令"
        )

        _msg_type, card_content = CardBuilder.build_info_card(
            project=project,
            title="🎭 Slock 协作团队",
            content=content,
            engine_name=engine_name,
            show_buttons=False,
        )
        session = self.create_static_card_session(chat_id, reply_to=message_id)
        session.send(json.loads(card_content))
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
            "• `/slock stop` — 停止协作\n\n"
            "**团队管理**\n"
            "• `/new-team <名称>` — 创建团队\n"
            "• `/team list` — 查看团队列表\n"
            "• `/team status <名称>` — 查看团队详情\n"
            "• `/team dissolve <名称>` — 解散团队\n\n"
            "**角色管理**\n"
            "• `/new-role <名称>` — 创建虚拟 Agent\n"
            "• `/role list` — 查看所有角色\n"
            "• `/role info <名称>` — 查看角色详情\n"
            "• `/role remove <名称>` — 移除角色\n\n"
            "**任务管理**\n"
            "• `/task list` — 查看任务列表\n"
            "• `/task assign <任务> <角色>` — 分配任务\n"
            "• `/task status` — 查看任务进度"
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
        group_name = f"{settings.slock_team_name_prefix}{name}" if settings.slock_team_name_prefix else name

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
            self.reply_text(message_id, f"❌ 创建团队群失败: {get_error_detail(e)}")
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
            )
            engine.activate_channel(channel)

            # Step 5: Register managed chat for event routing
            manager.register_managed_chat(new_chat_id)

            # Step 6: Send welcome message in the new group
            welcome_text = (
                f"🎭 **Slock 协作团队「{name}」已就绪**\n\n"
                "📌 **快速开始**:\n"
                "• `/new-role <名称>` — 创建虚拟 Agent\n"
                "• `/task assign <任务> <角色>` — 分配任务\n"
                "• `/slock status` — 查看团队状态\n"
                "• `/slock help` — 查看所有命令"
            )
            self.send_text_to_chat(new_chat_id, welcome_text)

            # Step 7: Send confirmation with jump link in original group
            self.reply_text(
                message_id,
                f"✅ 团队 **{name}** 已创建\n\n"
                f"已建立专属协作群「{group_name}」并激活 Slock 运行时\n"
                f"• 事件监听: ✓\n"
                f"• Agent 调度器: ✓\n"
                f"• 工作区目录: ✓\n\n"
                f"请前往新群开始协作 🚀",
            )

        except Exception as e:
            # Rollback: delete the created group on any activation failure
            logger.error("create_team: 激活失败, 回滚建群 chat=%s err=%s", new_chat_id, str(e))
            lark_client.delete_chat(new_chat_id)
            self.reply_text(message_id, f"❌ 团队激活失败已回滚: {get_error_detail(e)}")

    def list_teams(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """List active teams in the current chat."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)

        if not engine or not engine.channel:
            self.reply_text(message_id, "当前没有活跃的团队\n\n发送 `/slock` 激活协作模式")
            return

        channel = engine.channel
        agents = engine.registry.list_agents(channel_id=channel.channel_id)
        agent_count = len(agents)

        content = (
            f"📋 **团队列表**\n\n"
            f"• **{channel.team_name}** — {agent_count} 个 Agent · 频道: `{channel.name}`"
        )
        self.reply_text(message_id, content)

    def show_team_status(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Show status of a specific team."""
        self.show_slock_status(message_id, chat_id, project)

    def dissolve_team(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Dissolve (stop) a team."""
        self.stop_slock_engine(message_id, chat_id, project)

    # ------------------------------------------------------------------
    # Role / Agent management
    # ------------------------------------------------------------------

    # tool_type → default role inference mapping
    TOOL_TYPE_ROLE_MAP: dict[str, str] = {
        "codex": "coder",
        "claude": "reviewer",
        "coco": "writer",
        "gemini": "coder",
        "ttadk": "custom",
    }

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

        # Parse optional arguments from the name/args string
        import shlex

        try:
            tokens = shlex.split(name)
        except ValueError:
            tokens = name.split()

        role_name = tokens[0] if tokens else name
        tool_type = "coco"
        model_name = ""
        emoji = "🤖"
        system_prompt = ""
        explicit_role: str | None = None

        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--tool" and i + 1 < len(tokens):
                tool_type = tokens[i + 1]
                i += 2
            elif tok == "--model" and i + 1 < len(tokens):
                model_name = tokens[i + 1]
                i += 2
            elif tok == "--emoji" and i + 1 < len(tokens):
                emoji = tokens[i + 1]
                i += 2
            elif tok == "--role" and i + 1 < len(tokens):
                explicit_role = tokens[i + 1]
                i += 2
            elif tok == "--prompt" and i + 1 < len(tokens):
                system_prompt = tokens[i + 1]
                i += 2
            else:
                i += 1

        # Validate tool_type against whitelist
        from ...slock_engine.models import AGENT_ROLE_COLORS

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

        from ...slock_engine.models import AgentIdentity

        agent = AgentIdentity(
            name=role_name,
            emoji=emoji,
            agent_type=tool_type,
            model_name=model_name,
            system_prompt=system_prompt,
            role=role,
            owner_group=chat_id,
        )
        engine.registry.register(agent)

        self.reply_text(
            message_id,
            f"✅ 角色 **{agent.emoji} {agent.name}** 已创建 (ID: `{agent.agent_id[:8]}`)\n"
            f"   工具: `{tool_type}` | 模型: `{model_name or '默认'}` | 角色: `{role}` | Emoji: {emoji}",
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
        info = (
            f"{agent.emoji} **{agent.name}**\n\n"
            f"• ID: `{agent.agent_id[:8]}`\n"
            f"• 类型: `{agent.agent_type}`\n"
            f"• 模型: `{agent.model_name or 'default'}`\n"
            f"• 角色: {agent.role or '(未设置)'}\n"
            f"• 状态: `{status.value}`\n"
            f"• 权限: `{', '.join(agent.permissions) if agent.permissions else '默认'}`"
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

        task = engine.add_task(content)

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

            # Notify user that execution is starting
            self.reply_text(
                message_id,
                f"⏳ 任务已分配给 {agent.emoji} **{agent.name}**，正在执行...\n"
                f"• ID: `{task.task_id[:8]}`\n"
                f"• 内容: {content[:80]}",
            )

            # Execute the task (claim → execute → complete/rollback)
            import time as _time
            start_time = _time.time()
            try:
                callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
                result = engine.execute_task(task.task_id, agent.agent_id, callbacks)
            except Exception as e:
                logger.error("assign_task execute_task error: %s", repr(e))
                self.reply_text(
                    message_id,
                    f"❌ 任务执行失败: {get_error_detail(e)}\n"
                    f"• 任务已回退为 TODO，可重新分配",
                )
                return

            if result:
                # Success — send result as card
                duration = _time.time() - start_time
                card_data = engine._mouthpiece.format_card(
                    agent, result, model_info=agent.agent_type, duration_s=duration
                )
                card_json = json.dumps(card_data, ensure_ascii=False)
                self.send_card_to_chat(chat_id, card_json, origin_message_id=message_id)
            else:
                self.reply_text(
                    message_id,
                    f"⚠️ 任务执行完成但无输出\n"
                    f"• 任务已回退为 TODO，可重新分配",
                )
            return

        # No role specified — just create the task
        self.reply_text(
            message_id,
            f"✅ 任务已创建（未分配）\n"
            f"• ID: `{task.task_id[:8]}`\n"
            f"• 内容: {content[:80]}\n"
            f"• 发送 `/task assign <任务> <角色>` 分配给角色",
        )

    def show_task_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Show task board with status summary."""
        self.list_tasks(message_id, chat_id, project)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop_slock_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Stop the slock engine, unregister managed chat, and clean up."""
        manager = self._get_engine_manager()

        # Deactivate the engine if it exists
        engine = manager.get_activated_engine(chat_id)
        if engine:
            engine.deactivate()

        # Unregister managed chat so dispatcher stops routing to this engine
        manager.unregister_managed_chat(chat_id)

        self._safe_lifecycle_action(
            lambda: self._stop_engine_generic(message_id, chat_id, project),
            "stop", chat_id, message_id, project,
        )

    # ------------------------------------------------------------------
    # Card action handler
    # ------------------------------------------------------------------

    def _refresh_status_card(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Rebuild and update the status panel card in-place."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            return
        team_name = engine.channel.team_name if engine.channel else ""
        status_card = engine.get_status_card(team_name=team_name)
        card_content = json.dumps(status_card, ensure_ascii=False)
        self.update_card(message_id, card_content)

    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle slock_* card actions."""
        project_id = value.get("project_id", "")
        target_project = (
            self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
        )

        slock_actions = {
            "slock_stop": self.stop_slock_engine,
            "slock_refresh_status": self._refresh_status_card,
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
