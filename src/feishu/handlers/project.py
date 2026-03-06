"""Project management handler — create, switch, close, status, context preserve/restore."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...project import ContextEntryType, ContextSourceMode
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class ProjectHandler(BaseHandler):
    """Handles project CRUD and context preserve/restore."""

    def create_project(self, message_id: str, chat_id: str, name: str, path: str):
        project_id = name.lower().replace(" ", "_").replace("-", "_")

        success, msg, project = self.project_manager.create_project(
            project_id=project_id,
            project_name=name,
            root_path=path,
            chat_id=chat_id,
        )

        if success and project:
            msg_type, card_content = CardBuilder.build_project_created_card(project)
            response_id = self.reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self.register_message_project(response_id, project)
        else:
            msg_type, card_content = CardBuilder.build_error_card(msg)
            self.reply_message(message_id, card_content, msg_type)

    def show_project_board(self, message_id: str, chat_id: str):
        projects = self.project_manager.get_all_projects()
        active_project = self.project_manager.get_active_project(chat_id)
        current_id = active_project.project_id if active_project else None

        msg_type, content = CardBuilder.build_status_board_card(projects, current_id)
        response_id = self.reply_message_with_id(message_id, content, msg_type)

        if response_id and active_project:
            self.register_message_project(response_id, active_project)

    def show_current_project(self, message_id: str, chat_id: str, project: Optional["ProjectContext"]):
        if not project:
            self.reply_message(message_id, "📋 当前没有活跃项目\n\n发送 `/projects` 查看项目列表\n发送 `/new 项目名 路径` 创建新项目")
            return

        global_working_dir = self.get_working_dir(chat_id)
        content = (
            f"📁 **当前项目: {project.project_name}**\n\n"
            f"• 项目 ID: `{project.project_id}`\n"
            f"• 📂 项目目录: `{project.root_path}`\n"
            f"• 📁 工作目录: `{global_working_dir}`\n"
            f"• 状态: {project.get_status_emoji()} {project.status.value}\n"
            f"• Coco 模式: {'🤖 开启' if project.coco_mode else '关闭'}\n"
            f"• Claude 模式: {'🔮 开启' if project.claude_mode else '关闭'}"
        )

        msg_type, card_content = CardBuilder.build_project_response_card(
            project, "当前项目", content, show_buttons=True,
        )
        response_id = self.reply_message_with_id(message_id, card_content, msg_type)
        if response_id:
            self.register_message_project(response_id, project)

    def show_project_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"]):
        if not project:
            self.show_project_board(message_id, chat_id)
            return

        coco_info = ""
        if project.coco_mode and project.coco_session_snapshot:
            snap = project.coco_session_snapshot
            coco_info = f"\n\n🤖 **Coco 会话**\n• 会话 ID: `{snap.session_id}`\n• 对话数: {snap.query_count}"

        claude_info = ""
        if project.claude_mode and project.claude_session_snapshot:
            snap = project.claude_session_snapshot
            claude_info = f"\n\n🔮 **Claude 会话**\n• 会话 ID: `{snap.session_id}`\n• 对话数: {snap.query_count}"

        global_working_dir = self.get_working_dir(chat_id)
        content = (
            f"• 状态: {project.get_status_emoji()} {project.status.value}\n"
            f"• 📂 项目目录: `{project.root_path}`\n"
            f"• 📁 工作目录: `{global_working_dir}`\n"
            f"• 最后活跃: {CardBuilder._format_time_ago(project.last_active)}"
            f"{coco_info}{claude_info}"
        )

        msg_type, card_content = CardBuilder.build_project_response_card(
            project, "项目状态", content, show_buttons=True,
        )
        response_id = self.reply_message_with_id(message_id, card_content, msg_type)
        if response_id:
            self.register_message_project(response_id, project)

    # ------------------------------------------------------------------
    # Context preserve / restore
    # ------------------------------------------------------------------
    def preserve_project_context(self, chat_id: str, project: "ProjectContext"):
        from ...mode import InteractionMode

        pid = project.project_id
        current_mode = self.mode_manager.get_mode(chat_id, project_id=pid)
        logger.info("[%s] 保留项目上下文: project=%s, mode=%s", chat_id, project.project_name, current_mode.value if hasattr(current_mode, 'value') else current_mode)

        if current_mode == InteractionMode.COCO:
            session = self.ctx.coco_manager.get_session(chat_id, project_id=pid)
            if session:
                project.update_coco_snapshot(query=session.last_query, query_count=session.message_count)
                self.context_manager.update_context(
                    pid,
                    session_snapshot={"data": session.to_snapshot(), "source_mode": ContextSourceMode.COCO.value},
                )
        elif current_mode == InteractionMode.CLAUDE:
            session = self.ctx.claude_manager.get_session(chat_id, project_id=pid)
            if session:
                project.update_claude_snapshot(query=session.last_query, query_count=session.message_count, session_id=session.session_id)
                self.context_manager.update_context(
                    pid,
                    session_snapshot={"data": session.to_snapshot(), "source_mode": ContextSourceMode.CLAUDE.value},
                )

    def restore_project_context(self, project: "ProjectContext") -> dict:
        ctx = self.context_manager.store.get(project.project_id)
        if ctx is None:
            logger.debug("[恢复上下文] 项目 %s 无已有上下文", project.project_name)
            return {"has_context": False, "entry_count": 0, "version_count": 0, "last_mode": None, "has_bridge": False}

        last_mode = None
        transitions = ctx.get_entries_by_type(ContextEntryType.MODE_TRANSITION)
        if transitions:
            last_transition = transitions[-1]
            last_mode = last_transition.metadata.get("to_mode")

        info = {
            "has_context": True,
            "entry_count": ctx.entry_count,
            "version_count": len(ctx.versions),
            "last_mode": last_mode,
            "has_bridge": ctx.last_bridge_summary is not None,
        }
        logger.info("[恢复上下文] 项目 %s: %d 条记录, %d 版本, 上次模式=%s, 有桥接=%s",
                     project.project_name, info["entry_count"], info["version_count"],
                     info["last_mode"], info["has_bridge"])
        return info

    # ------------------------------------------------------------------
    # Switch / Close
    # ------------------------------------------------------------------
    def switch_project(self, message_id: str, chat_id: str, name: str, auto_enter_coco: bool = True,
                       coco_handler=None, claude_handler=None):
        """Switch active project.

        *coco_handler* and *claude_handler* are the programming-mode handlers
        used to exit the current mode safely when switching projects.
        """
        project = self.project_manager.find_project_by_name(name)
        if not project:
            results = self.project_manager.search_projects(name)
            if results:
                suggestions = "\n".join([f"• {p.project_name}" for p in results[:5]])
                self.reply_message(message_id, f"❌ 未找到项目: {name}\n\n**相似项目：**\n{suggestions}")
            else:
                self.reply_message(message_id, f"❌ 未找到项目: {name}\n\n发送 `/projects` 查看所有项目")
            return

        valid, path_msg = self.project_manager.validate_project_path(project.project_id)
        if not valid:
            self.reply_message(message_id, f"⚠️ {path_msg}\n\n请检查项目路径是否存在")
            return

        old_project = self.project_manager.get_active_project(chat_id)
        if old_project and old_project.project_id != project.project_id:
            self.preserve_project_context(chat_id, old_project)

            from ...mode import InteractionMode
            current_mode = self.mode_manager.get_mode(chat_id, project_id=old_project.project_id)
            if current_mode == InteractionMode.COCO and coco_handler:
                coco_handler.exit_mode(message_id, chat_id, project=old_project)
            elif current_mode == InteractionMode.CLAUDE and claude_handler:
                claude_handler.exit_mode(message_id, chat_id, project=old_project)

            old_ctx = self.context_manager.store.get(old_project.project_id)
            if old_ctx:
                old_ctx.create_version(
                    reason=f"project_switch: {old_project.project_name} -> {project.project_name}",
                    source_mode=ContextSourceMode.SMART,
                    summary=f"Switched to project {project.project_name}",
                )

        success, msg = self.project_manager.set_active_project(chat_id, project.project_id)
        if not success:
            self.reply_message(message_id, f"❌ {msg}")
            return

        restore_info = self.restore_project_context(project)
        self.context_manager.store.get_or_create(project.project_id)

        if auto_enter_coco and coco_handler:
            coco_handler.enter_mode(message_id, chat_id, project=project)
        else:
            context_info = ""
            if restore_info["has_context"]:
                context_info = f"\n\n📋 已恢复上下文: {restore_info['entry_count']} 条记录"
                if restore_info["last_mode"]:
                    context_info += f", 上次模式: {restore_info['last_mode']}"

            content = f"已切换到项目 **{project.project_name}**\n\n📂 项目目录: `{project.root_path}`{context_info}"

            if project.coco_session_snapshot and project.coco_session_snapshot.is_resumable:
                msg_type, card_content = CardBuilder.build_coco_resume_card(project)
            elif project.claude_session_snapshot and project.claude_session_snapshot.is_resumable:
                msg_type, card_content = CardBuilder.build_claude_resume_card(project)
            else:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "🔄 项目已切换", content, show_buttons=True,
                )

            response_id = self.reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self.register_message_project(response_id, project)

    def close_project(self, message_id: str, chat_id: str, name: str):
        project = self.project_manager.find_project_by_name(name)
        if not project:
            self.reply_message(message_id, f"❌ 未找到项目: {name}")
            return

        success, msg = self.project_manager.close_project(project.project_id)
        if success:
            self.reply_message(message_id, f"✅ {msg}")
        else:
            self.reply_message(message_id, f"❌ {msg}")
