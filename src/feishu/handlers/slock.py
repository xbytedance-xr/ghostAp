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
        manager.register_managed_chat(chat_id)

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
        """List all active Slock teams."""
        manager = self._get_engine_manager()
        engines = manager.list_activated_engines()

        if not engines:
            self.reply_text(message_id, "当前没有活跃的团队\n\n发送 `/slock` 激活协作模式")
            return

        lines = ["📋 **团队列表**\n"]
        for engine in sorted(engines, key=lambda item: (item.channel.team_name if item.channel else "")):
            channel = engine.channel
            if not channel:
                continue
            agents = engine.registry.list_agents(channel_id=channel.channel_id)
            agent_count = len(agents)
            task_count = len(engine.tasks)
            lines.append(
                f"• **{channel.team_name or channel.name or channel.channel_id}** — "
                f"{agent_count} 个 Agent · {task_count} 个任务 · 频道: `{channel.channel_id}`"
            )

        self.reply_text(message_id, "\n".join(lines))

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
                    "⚠️ 任务执行完成但无输出\n"
                    "• 任务已回退为 TODO，可重新分配",
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

        self.reply_text(
            message_id,
            f"⏳ 任务已自动分配给 {agent.emoji} **{agent.name}**，正在执行...\n"
            f"• ID: `{task.task_id[:8]}`\n"
            f"• 内容: {content[:80]}",
        )

        import time as _time
        start_time = _time.time()
        try:
            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)
            result = engine.execute_task(task.task_id, agent.agent_id, callbacks)
        except Exception as e:
            logger.error("assign_task auto execute_task error: %s", repr(e))
            self.reply_text(
                message_id,
                f"❌ 任务执行失败: {get_error_detail(e)}\n"
                f"• 任务已回退为 TODO，可重新分配",
            )
            return

        if result:
            duration = _time.time() - start_time
            card_data = engine._mouthpiece.format_card(
                agent, result, model_info=agent.agent_type, duration_s=duration
            )
            card_json = json.dumps(card_data, ensure_ascii=False)
            self.send_card_to_chat(chat_id, card_json, origin_message_id=message_id)
        else:
            self.reply_text(
                message_id,
                "⚠️ 任务执行完成但无输出\n"
                "• 任务已回退为 TODO，可重新分配",
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
        card = build_task_board_card(engine.tasks, agents, team_name=team_name)
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
