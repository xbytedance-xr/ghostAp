"""Slock role and agent management callback handlers.

Extracted from slock.py to reduce handler size. Contains /role, /new-role,
agent creation, deletion, movement, and role info display.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import TYPE_CHECKING, Optional

from ...acp.helper import fetch_acp_models
from ...card import CardBuilder
from ...card.actions import dispatch as action_ids
from ...model_selection import is_default_model_option
from ...utils.redact import redact_sensitive
from ..user_cache import resolve_display_name_nonblocking

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class SlockRoleMixin:
    """Mixin providing role/agent management handlers."""

    # tool_type → default role inference mapping
    TOOL_TYPE_ROLE_MAP: dict[str, str] = {
        "traex": "coder",
        "codex": "coder",
        "claude": "reviewer",
        "coco": "writer",
        "aiden": "coder",
        "gemini": "coder",
    }

    # role → default personality traits (used when --traits not specified)
    DEFAULT_PERSONALITY_TRAITS: dict[str, list[str]] = {
        "coder": ["严谨", "注重细节"],
        "reviewer": ["批判性思维", "追求质量"],
        "tester": ["细致", "追求覆盖"],
        "planner": ["全局视角", "有条理"],
        "architect": ["抽象思维", "系统设计"],
        "writer": ["表达清晰", "注重结构"],
        "custom": [],
    }
    TOOL_SELECT_OPTIONS: tuple[dict[str, str], ...] = (
        {"name": "traex", "label": "Traex", "emoji": "🚀", "description": "默认编程工具"},
        {"name": "coco", "label": "Coco", "emoji": "🥥", "description": "协作工具"},
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

        tool_type = "traex"
        model_name = ""
        emoji = "🤖"
        system_prompt = ""
        explicit_role: str | None = None
        template_name = ""
        fork_name = ""
        explicit_traits: str | None = None
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
            elif tok == "--traits" and i + 1 < len(tokens):
                explicit_traits = tokens[i + 1]
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

        # Assign unique emoji if not explicitly provided
        if not emoji_explicit:
            from ...slock_engine.role_bootstrap import pick_unique_emoji
            used_emojis = {a.emoji for a in engine.registry.list_agents() if hasattr(a, 'emoji')}
            emoji = pick_unique_emoji(role, used_emojis)

        runtime_agent_type = self._resolve_slock_runtime_agent_type(tool_type)
        agent_id = f"{runtime_agent_type}:{model_name or 'default'}:{role_name}"
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

        # Determine personality traits: explicit --traits overrides default role mapping
        if explicit_traits:
            personality_traits = [t.strip() for t in explicit_traits.replace("，", ",").split(",") if t.strip()]
        else:
            personality_traits = list(self.DEFAULT_PERSONALITY_TRAITS.get(role, []))

        agent = AgentIdentity(
            agent_id=agent_id,
            name=role_name,
            emoji=emoji,
            agent_type=runtime_agent_type,
            model_name=model_name,
            system_prompt=system_prompt,
            role=role,
            owner_group=chat_id,
            member_groups=[chat_id],
            personality_traits=personality_traits,
        )
        workspace_paths = engine.memory.initialize_agent_workspace(agent.agent_id)
        agent.memory_path = workspace_paths.get("memory_path") or engine.memory.agent_memory_path(agent.agent_id)
        agent.notes_path = workspace_paths.get("notes_path") or engine.memory.agent_notes_path(agent.agent_id)
        agent.workspace_path = workspace_paths.get("workspace_path") or engine.memory.agent_workspace_path(agent.agent_id)
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
                    f"tool_type={tool_type}\nruntime_agent_type={runtime_agent_type}\n"
                    f"model={model_name or 'default'}\nrole={role}"
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
            f"   工具: `{tool_type}` | 运行时: `{runtime_agent_type}` | 模型: `{model_name or '默认'}` | "
            f"角色: `{role}` | Emoji: {emoji}",
        )

    @staticmethod
    def _resolve_slock_runtime_agent_type(tool_type: str) -> str:
        """Map role creation tool choice to an executable agent session backend."""
        return tool_type

    def _selectable_role_tool_options(self) -> list[dict[str, str]]:
        """Return role-creation tools executable in the current environment."""
        try:
            from ...workflow_engine.tool_registry import get_available_tools

            available = set(get_available_tools(require_available=True).keys())
        except Exception:
            logger.debug("failed to resolve selectable role tools", exc_info=True)
            available = set()
        if not available:
            return []
        return [
            option
            for option in self.TOOL_SELECT_OPTIONS
            if str(option.get("name") or "") in available
        ]

    def show_new_role_tool_selection(
        self,
        message_id: str,
        role_name: str,
        project: Optional["ProjectContext"] = None,
    ) -> None:
        """Show the first `/new-role` interactive step: choose a backing tool."""

        tool_options = self._selectable_role_tool_options()
        if not tool_options:
            self.reply_text(message_id, "当前环境未检测到可用的 Slock 编程工具，请安装 Traex/Claude/Codex 等 CLI 后重试。")
            return

        _, card_content = CardBuilder.build_slock_role_tool_select_card(
            role_name,
            tool_options,
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
        if tool_name not in self.TOOL_TYPE_ROLE_MAP:
            self.reply_text(message_id, f"请选择有效的 Slock 角色工具: `{tool_name}`")
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            self.reply_text(message_id, "请先激活 Slock 模式: `/slock`")
            return

        cwd = getattr(project, "root_path", None) or getattr(engine, "root_path", None) or self.get_working_dir(chat_id)
        try:
            model_page = int(value.get("model_page", 0) or 0)
        except (TypeError, ValueError):
            model_page = 0
        models = fetch_acp_models(tool_name, cwd=cwd, current_model=None)
        _, card_content = CardBuilder.build_acp_model_select_card(
            models,
            tool_name,
            project_id=(project.project_id if project else value.get("project_id")),
            action_name=action_ids.SLOCK_NEW_ROLE_SELECT_MODEL,
            value_extra={"role_name": role_name},
            context_markdown=f"角色: **{role_name}**",
            refresh_action_name=action_ids.SLOCK_NEW_ROLE_SELECT_TOOL,
            model_page=model_page,
        )
        # When the click carries a page change, update the existing card in
        # place; the initial tool selection (page 0) replies a fresh card.
        if model_page and self.update_card(message_id, card_content):
            return
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
        """List all roles in the current channel using a structured card."""
        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👥 角色列表", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        agent_list = engine.registry.list_agents(channel_id=channel_id)

        if not agent_list:
            from ...slock_engine.card_templates.common import build_callback_button, build_empty_state_card
            create_btn = build_callback_button(
                "➕ 创建角色", "slock_new_role_hint",
                channel_id=chat_id, button_type="primary",
            )
            card = build_empty_state_card(
                "👥 角色列表",
                "当前没有角色，发送 `/new-role <名称>` 创建角色",
                guide_buttons=[create_btn],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        from ...slock_engine.card_templates import build_role_list_card
        from ...slock_engine.models import AgentStatus as AgentStatusEnum
        from ...slock_engine.models import TaskStatus

        agents: list[tuple] = []
        current_tasks: dict = {}
        skill_profiles: dict = {}
        for agent in agent_list:
            status = engine.get_agent_status(agent.agent_id) or AgentStatusEnum.IDLE
            agents.append((agent, status))
            for task in engine.tasks:
                if task.claimed_by == agent.agent_id and task.status == TaskStatus.IN_PROGRESS:
                    current_tasks[agent.agent_id] = task
                    break
            profiles = engine.memory.read_skill_profiles(agent.agent_id)
            if profiles:
                skill_profiles[agent.agent_id] = [
                    {"tag": p.tag, "success_rate": p.success_rate, "total_tasks": p.total_tasks}
                    for p in profiles
                ]

        team_name = engine.channel.team_name if engine.channel else ""
        card = build_role_list_card(
            agents=agents,
            team_name=team_name,
            channel_id=channel_id,
            current_tasks=current_tasks,
            skill_profiles=skill_profiles,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

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
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👥 角色管理", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{redact_sensitive(name)}**")
            return

        engine.registry.remove(agent.agent_id)
        self.reply_text(message_id, f"✅ 角色 **{agent.emoji} {agent.name}** 已移除")

    def move_role(
        self,
        message_id: str,
        chat_id: str,
        name: str = "",
        target_team_name: str = "",
        project: Optional["ProjectContext"] = None,
    ):
        """Move an agent from the current team to a target team.

        Flow: validate → permission(source+target) → try_lock_for_move (atomic
        IDLE→MOVING) → registry.move_agent → unlock → refresh target cache →
        notify target (best-effort) → append L1 context → reply confirm card.

        Design: 'move first, notify second' — registry persistence is the
        authoritative operation; notification card is best-effort UI.  If the
        notification fails, the move still stands and source group receives a
        degraded warning instead of a rollback.
        """
        import time as _time

        from ...slock_engine.card_templates import (
            build_agent_move_confirm_card,
            build_agent_move_departure_card,
            build_agent_move_notification_card,
        )
        from ...thread.manager import get_current_sender_id

        if not name or not target_team_name:
            self.reply_text(
                message_id,
                "请提供角色名称和目标团队\n\n用法: `/role move <角色名> <目标团队名>`",
            )
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👥 角色管理", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        # Permission check: source group
        if not self._check_slock_permission(engine, message_id, chat_id):
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{redact_sensitive(name)}**")
            return

        # Find target team
        target_engine = manager.find_team(target_team_name)
        if not target_engine or not target_engine.channel:
            self.reply_text(message_id, f"未找到目标团队: **{redact_sensitive(target_team_name)}**")
            return

        target_channel_id = target_engine.channel.channel_id
        if target_channel_id == chat_id:
            self.reply_text(message_id, "⚠️ 目标团队与当前团队相同，无需移动")
            return

        # Permission check: target group (operator must be global admin or target owner)
        if not self._check_slock_permission(target_engine, message_id, chat_id):
            return

        # Atomic: check IDLE and lock agent in MOVING state
        if not engine.try_lock_for_move(agent.agent_id):
            status = engine.get_agent_status(agent.agent_id)
            self.reply_text(
                message_id,
                f"⚠️ 角色 **{agent.name}** 当前状态为 {status.value}，仅 IDLE 状态可移动",
            )
            return

        source_team_display = (engine.channel.team_name if engine.channel else chat_id) or chat_id
        target_team_display = target_engine.channel.team_name or target_team_name
        operator_id = get_current_sender_id() or ""

        try:
            # Step 1: Perform atomic move in registry (authoritative operation)
            outcome = engine.registry.move_agent(agent.agent_id, chat_id, target_channel_id)
            if not outcome.success:
                from ...slock_engine.agent_registry import MoveResult

                if outcome.status == MoveResult.NOT_FOUND:
                    err_msg = f"❌ 移动失败：角色 **{redact_sensitive(name)}** 未找到"
                elif outcome.status == MoveResult.NOT_IN_SOURCE:
                    err_msg = "❌ 移动失败，请确认角色属于当前团队"
                elif outcome.status == MoveResult.DUPLICATE_NAME:
                    err_msg = "❌ 移动失败：目标团队已存在同名角色，请先修改角色名称"
                elif outcome.status == MoveResult.PERSIST_FAILED:
                    err_msg = "❌ 迁移失败：数据持久化异常，角色仍留在原团队，请稍后重试"
                else:
                    err_msg = "❌ 移动失败，请确认角色属于当前团队"
                logger.warning(
                    "slock move_role: registry move_agent failed | agent=%s source=%s target=%s operator=%s status=%s",
                    agent.agent_id, chat_id, target_channel_id, operator_id, outcome.status.value,
                )
                self.reply_text(message_id, err_msg)
                return

            # Step 1.5: Redact active_context to prevent source-group history leakage
            import hashlib as _hashlib

            try:
                _pre_memory = engine.memory.read_agent_memory(agent.agent_id)
                _ctx_len = len(_pre_memory.active_context) if _pre_memory.active_context else 0
                _ctx_md5 = _hashlib.md5(_pre_memory.active_context.encode()).hexdigest() if _pre_memory.active_context else ""
                logger.info(
                    "slock move_role: pre-redact audit | agent=%s context_chars=%d md5=%s",
                    agent.agent_id, _ctx_len, _ctx_md5,
                )
                engine.memory.redact_active_context_for_move(agent.agent_id, chat_id, target_channel_id)
            except Exception as exc:
                logger.warning(
                    "slock move_role: redact_active_context failed (non-fatal) | agent=%s error=%s",
                    agent.agent_id, exc,
                )
        finally:
            engine.unlock_after_move(agent.agent_id)

        # Refresh target engine's registry cache so it can discover the agent
        target_engine.registry.refresh_agent(agent.agent_id)

        # Verify L1 memory integrity after move (user-facing degradation notice)
        _l1_memory_degraded = False
        try:
            _moved_memory = target_engine.memory.read_agent_memory(agent.agent_id)
            if agent.system_prompt and not _moved_memory.role and not _moved_memory.key_knowledge:
                logger.error(
                    "slock move_role: L1 memory empty after move — persona may be inconsistent | agent=%s",
                    agent.agent_id,
                )
                _l1_memory_degraded = True
        except Exception as exc:
            logger.error(
                "slock move_role: L1 memory read failed after move | agent=%s error=%s",
                agent.agent_id, exc,
            )
            _l1_memory_degraded = True

        # Resolve operator display name for notification card
        operator_display = (
            resolve_display_name_nonblocking(operator_id, self.ctx.api_client_factory)
            if operator_id
            else ""
        )

        # Step 2: Send notification card to target group (best-effort)
        notification_card = build_agent_move_notification_card(
            agent=agent,
            source_team=source_team_display,
            target_team=target_team_display,
            operator_display=operator_display,
        )
        sent_msg_id = self.send_card_to_chat(
            target_channel_id, json.dumps(notification_card, ensure_ascii=False)
        )
        if not sent_msg_id:
            logger.warning(
                "slock move_role: notification card send failed (non-fatal) | agent=%s source=%s target=%s operator=%s",
                agent.agent_id, chat_id, target_channel_id, operator_id,
            )
            self.reply_text(
                message_id,
                "✅ 移动成功，但目标群通知发送失败。目标群可通过 /role list 查看新成员。",
            )
            # Do NOT return — continue to append context and send confirm card

        # Step 2.5: Send departure notification card to source group (best-effort)
        try:
            departure_card = build_agent_move_departure_card(
                agent=agent,
                target_team=target_team_display,
            )
            self.send_card_to_chat(chat_id, json.dumps(departure_card, ensure_ascii=False))
        except Exception as exc:
            logger.warning(
                "slock move_role: departure card send failed (non-fatal) | agent=%s source=%s error=%s",
                agent.agent_id, chat_id, exc,
            )

        # Step 3: Append migration record to L1 active context
        context_record = (
            f"[{_time.strftime('%Y-%m-%d %H:%M')}] "
            f"Moved from {source_team_display} to {target_team_display}"
        )
        try:
            engine.memory.update_agent_context(agent.agent_id, context_record)
        except Exception as exc:
            logger.warning(
                "slock move_role: update_agent_context failed (non-fatal) | agent=%s error=%s",
                agent.agent_id, exc,
            )

        # Step 4: Reply confirm card to source group with jump button
        confirm_card = build_agent_move_confirm_card(
            agent=agent,
            source_team=source_team_display,
            target_team=target_team_display,
            target_channel_id=target_channel_id,
        )
        self.reply_card(message_id, json.dumps(confirm_card, ensure_ascii=False))

        if _l1_memory_degraded:
            self.reply_text(
                message_id,
                "⚠️ 注意：角色记忆加载异常，人格一致性可能受影响。请在目标群执行 /role info 确认。",
            )

        logger.info(
            "slock move_role: success | agent=%s source=%s target=%s operator=%s",
            agent.agent_id, chat_id, target_channel_id, operator_id,
        )

    def show_role_info(
        self, message_id: str, chat_id: str, name: str = "", project: Optional["ProjectContext"] = None
    ):
        """Show detailed info about a role using structured card.

        Permission-aware: admin/owner sees full details including Active Context
        and permissions; regular members see only non-sensitive identity info.
        """
        if not name:
            from ...slock_engine.card_templates.common import build_usage_hint_card
            card = build_usage_hint_card(
                "/role info <名称>",
                ["/role info coder", "/role info reviewer", "/r info tester"],
            )
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        manager = self._get_engine_manager()
        engine = manager.get_activated_engine(chat_id)
        if not engine:
            from ...slock_engine.card_templates.common import build_error_state_card
            card = build_error_state_card("👤 角色详情", "当前没有活跃的 Slock 团队，请先 /slock activate")
            self.reply_card(message_id, json.dumps(card, ensure_ascii=False))
            return

        agent = engine.registry.find_by_name(name, channel_id=chat_id)
        if not agent:
            self.reply_text(message_id, f"未找到角色: **{redact_sensitive(name)}**")
            return

        from ...slock_engine.card_templates import build_role_info_card
        from ...slock_engine.models import AgentStatus as AgentStatusEnum
        from ...slock_engine.models import TaskStatus

        channel_id = engine.channel.channel_id if engine.channel else chat_id
        status = engine.get_agent_status(agent.agent_id) or AgentStatusEnum.IDLE
        memory = engine.memory.read_agent_memory(agent.agent_id)
        profiles = engine.memory.read_skill_profiles(agent.agent_id)
        skill_profiles = [
            {"tag": p.tag, "success_rate": p.success_rate, "total_tasks": p.total_tasks}
            for p in profiles
        ]

        # Current and recent tasks
        current_task = None
        recent_tasks: list = []
        for task in engine.tasks:
            if task.claimed_by == agent.agent_id:
                if task.status == TaskStatus.IN_PROGRESS:
                    current_task = task
                elif task.status == TaskStatus.DONE:
                    recent_tasks.append(task)
        recent_tasks = recent_tasks[-3:]  # Last 3 completed

        card = build_role_info_card(
            agent,
            status=status,
            memory=memory,
            skill_profiles=skill_profiles,
            current_task=current_task,
            recent_tasks=recent_tasks,
            channel_id=channel_id,
        )
        self.reply_card(message_id, json.dumps(card, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------
