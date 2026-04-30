"""ProjectChatService — orchestrator for /new-chat command."""

import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from ..config import get_settings
from ..project.context import ProjectContext
from ..project.manager import ProjectManager
from .errors import BindError, CreateChatError, ProjectChatError
from .group_naming import format_group_name, validate_name_part
from .lark_chat_client import LarkChatClient

logger = logging.getLogger(__name__)

# Per-(chat_id, path) lock to prevent concurrent /new-chat races
_creation_locks: dict[str, threading.Lock] = {}
_creation_locks_guard = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock


def _get_creation_lock(chat_id: str, path: str) -> threading.Lock:
    key = f"{chat_id}:{os.path.normpath(path)}"
    with _creation_locks_guard:
        if key not in _creation_locks:
            _creation_locks[key] = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        return _creation_locks[key]


class ProjectChatService:
    """Orchestrates /new-chat: parse → idempotency check → create chat → bind project."""

    def __init__(
        self,
        project_manager: ProjectManager,
        lark_chat_client: LarkChatClient,
        reply_fn: Callable[[str, str, Optional[str]], Any],
        send_to_chat_fn: Callable[[str, str, str, Optional[str]], Any],
    ):
        self._pm = project_manager
        self._lark = lark_chat_client
        self._reply = reply_fn
        self._send_to_chat = send_to_chat_fn

    def handle(
        self,
        message_id: str,
        chat_id: str,
        sender_open_id: str,
        data: dict,
    ) -> None:
        """Main entry point for /new-chat command."""
        settings = get_settings()

        # 1. Parse defaults
        path = data.get("path") or os.getcwd()
        path = os.path.expanduser(os.path.abspath(path))
        name = data.get("name") or os.path.basename(os.path.normpath(path)) or f"project_{int(time.time())}"
        suffix = data.get("suffix") or settings.project_chat_suffix

        # Validate name/suffix
        err = validate_name_part(name)
        if err:
            self._reply(message_id, f"❌ 项目名无效: {err}", None)
            return
        err = validate_name_part(suffix)
        if err:
            self._reply(message_id, f"❌ 后缀无效: {err}", None)
            return

        # 2. Acquire per-(chat, path) lock
        lock = _get_creation_lock(chat_id, path)
        if not lock.acquire(timeout=5):
            self._reply(message_id, "⏳ 正在处理中，请稍后再试", None)
            return

        try:
            self._handle_locked(message_id, chat_id, sender_open_id, name, suffix, path)
        finally:
            lock.release()

    def _handle_locked(
        self,
        message_id: str,
        chat_id: str,
        sender_open_id: str,
        name: str,
        suffix: str,
        path: str,
    ) -> None:
        # 3. Idempotency check — chat_id=None to skip visibility filter
        ctx = self._pm.find_project_by_path(path, chat_id=None)

        if ctx and ctx.bound_chat_id:
            # Branch A: already bound → ensure originating chat can see it, then return jump card
            if chat_id and chat_id not in ctx.allowed_chat_ids:
                ctx.add_chat_id(chat_id)
                self._pm._save_projects()
            self._reply_jump_card(message_id, ctx)
            return

        group_name = format_group_name(name, suffix)
        description = self._build_description(name, path)

        # 4. Create chat
        try:
            result = self._lark.create_chat(
                name=group_name,
                description=description,
                user_id_list=[sender_open_id],
            )
        except CreateChatError as e:
            logger.warning("create_chat failed for path=%s: %s", path, str(e))
            self._reply(message_id, f"❌ 建群失败: {e}", None)
            return

        new_chat_id = result.chat_id
        new_chat_name = result.name

        # 4.5 Promote sender to group manager (best-effort, enables dissolve permission)
        self._lark.add_managers(new_chat_id, [sender_open_id])

        # 5. Bind
        try:
            if ctx:
                # Branch B: legacy project without bound chat
                ctx.project_name = name  # respect user-specified name
                ctx.bound_chat_id = new_chat_id
                ctx.bound_chat_name = new_chat_name
                ctx.bound_chat_created_at = time.time()
                ctx.add_chat_id(new_chat_id)
                # Ensure the originating chat can still see this project
                if chat_id != new_chat_id:
                    ctx.add_chat_id(chat_id)
                self._pm._save_projects()
            else:
                # Branch C: new project
                success, msg, ctx_new = self._pm.create_project(
                    project_id=None,
                    project_name=name,
                    root_path=path,
                    chat_id=new_chat_id,
                )
                if not success or not ctx_new:
                    # Rollback: delete the created chat
                    self._lark.delete_chat(new_chat_id)
                    self._reply(message_id, f"❌ 创建项目失败: {msg}", None)
                    return
                ctx_new.bound_chat_id = new_chat_id
                ctx_new.bound_chat_name = new_chat_name
                ctx_new.bound_chat_created_at = time.time()
                # Ensure the originating chat can also see this project
                if chat_id != new_chat_id:
                    ctx_new.add_chat_id(chat_id)
                self._pm._save_projects()
                ctx = ctx_new
        except Exception as e:
            # Rollback chat on any bind failure
            logger.error("bind failed, rolling back chat %s: %s", new_chat_id[:12], str(e))
            self._lark.delete_chat(new_chat_id)
            self._reply(message_id, f"❌ 绑定失败: {e}", None)
            return

        # 6. Reply in main chat + welcome in new chat
        self._reply_jump_card(message_id, ctx)
        self._send_welcome(new_chat_id, ctx)

    def _build_description(self, name: str, path: str) -> str:
        git_remote = self._detect_git_remote(path)
        lines = [
            f"🎯 项目: {name}",
            f"📁 目录: {path}",
        ]
        if git_remote:
            lines.append(f"🔗 仓库: {git_remote}")
        lines.append("🤖 在这个群直接对话即可：默认 Coco / 显式 /claude /codex 等。")
        return "\n".join(lines)

    @staticmethod
    def _detect_git_remote(path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", path, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=3,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    def _reply_jump_card(self, message_id: str, ctx: ProjectContext) -> None:
        """Reply with a jump card pointing to the bound chat."""
        from ..card.builders.project import ProjectBuilder

        msg_type, card_json = ProjectBuilder.build_project_chat_jump_card(ctx)
        self._reply(message_id, card_json, msg_type)

    def _send_welcome(self, chat_id: str, ctx: ProjectContext) -> None:
        """Send welcome message in the newly created group."""
        text = (
            f"🎉 项目 **{ctx.project_name}** 专属群已就绪\n"
            f"📂 目录: `{ctx.root_path}`\n\n"
            f"直接在这里对话即可开始编程：\n"
            f"• 直接发消息 → 默认 Coco\n"
            f"• `/claude` → Claude 模式\n"
            f"• `/codex` → Codex 模式\n"
            f"• `/deep <需求>` → Deep 深度执行"
        )
        try:
            self._send_to_chat(chat_id, "text", text, None)
        except Exception as e:
            logger.warning("send_welcome to %s failed: %s", chat_id[:12], str(e))
