import json
import time
import os
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse
from typing import Callable, Optional, Any
import threading
from concurrent.futures import ThreadPoolExecutor
from ..config import get_settings
from ..coco.session import CocoSessionManager
from ..claude.session import ClaudeSessionManager
from ..agent.intent_recognizer import IntentRecognizer, IntentType, IntentResult, TaskStep
from ..project import ProjectManager, ProjectContext, ProjectStatus, MessageProjectMapper
from ..card import CardBuilder
from ..card.streaming import StreamingCardManager
from ..deep_engine import DeepEngine, DeepEngineManager, DeepEngineCallbacks, ProgressReporter
from ..deep_engine.models import DeepProject, DeepProjectStatus, DeepTask, ExecutionResult
from .message_formatter import FeishuMessageFormatter as fmt
from .emoji import EmojiType, EmojiReaction
from .message_cache import MessageCache
from .image_handler import FeishuImageHandler


class FeishuWSClient:
    MESSAGE_EXPIRE_SECONDS = 30

    def __init__(self, message_callback: Callable[[str, str, str, Optional[str]], None]):
        self.settings = get_settings()
        self.message_callback = message_callback
        self._client: Optional[lark.ws.Client] = None
        self._api_client: Optional[lark.Client] = None
        self._coco_manager = CocoSessionManager()
        self._claude_manager = ClaudeSessionManager()
        self._intent_recognizer = IntentRecognizer()
        self._message_cache = MessageCache(ttl=300, max_size=1000, cleanup_interval=60)
        self._executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ghost_worker")
        self._working_dirs: dict[str, str] = {}

        self._project_manager = ProjectManager()
        self._message_mapper = MessageProjectMapper()
        
        from ..mode import ModeManager
        self._mode_manager = ModeManager()
        
        self._streaming_manager: Optional[StreamingCardManager] = None
        self._image_handler: Optional[FeishuImageHandler] = None
        self._pending_image_keys: dict[str, list[str]] = {}
        self._enable_streaming = self.settings.streaming_enabled
        
        self._deep_engine_manager = DeepEngineManager()
        self._progress_reporter = ProgressReporter()

    def _is_message_expired(self, create_time: int) -> bool:
        if not create_time:
            return False
        current_time = int(time.time() * 1000)
        message_age_ms = current_time - create_time
        return message_age_ms > self.MESSAGE_EXPIRE_SECONDS * 1000

    def _is_duplicate_message(self, message_id: str) -> bool:
        return self._message_cache.is_duplicate(message_id)

    def _get_api_client(self) -> lark.Client:
        if self._api_client is None:
            self._api_client = lark.Client.builder() \
                .app_id(self.settings.app_id) \
                .app_secret(self.settings.app_secret) \
                .log_level(lark.LogLevel.INFO) \
                .build()
        return self._api_client

    def _get_streaming_manager(self) -> StreamingCardManager:
        if self._streaming_manager is None:
            self._streaming_manager = StreamingCardManager(self._get_api_client())
        return self._streaming_manager

    def _get_image_handler(self) -> FeishuImageHandler:
        if self._image_handler is None:
            self._image_handler = FeishuImageHandler(self._get_api_client())
        return self._image_handler

    def _add_reaction(self, message_id: str, emoji_type: str):
        try:
            client = self._get_api_client()
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder()
                        .emoji_type(emoji_type)
                        .build())
                    .build()) \
                .build()

            response = client.im.v1.message_reaction.create(request)
            if not response.success():
                print(f"添加表情失败: {response.code} - {response.msg}")
        except Exception as e:
            print(f"添加表情异常: {e}")

    def _get_working_dir(self, chat_id: str) -> str:
        return self._working_dirs.get(chat_id, os.getcwd())

    def _set_working_dir(self, chat_id: str, path: str) -> tuple[bool, str]:
        expanded_path = os.path.expanduser(path)

        if not os.path.isabs(expanded_path):
            current_dir = self._get_working_dir(chat_id)
            expanded_path = os.path.normpath(os.path.join(current_dir, expanded_path))

        if os.path.isdir(expanded_path):
            self._working_dirs[chat_id] = expanded_path
            return True, expanded_path
        else:
            return False, f"目录不存在: {expanded_path}"

    def _resolve_project_from_message(self, message_id: str, chat_id: str, parent_id: Optional[str] = None) -> tuple[Optional[ProjectContext], Optional[str]]:
        auto_enter_mode = None

        if parent_id:
            project_id = self._message_mapper.get_project_id(parent_id)
            if project_id:
                project = self._project_manager.get_project(project_id)
                if project:
                    self._project_manager.set_active_project(chat_id, project_id)
                    print(f"📎 通过消息引用切换到项目: {project.project_name}")

                    if project.claude_mode:
                        auto_enter_mode = "claude"
                        print(f"🔮 自动进入 Claude 模式 (回复编程消息)")
                    elif project.coco_mode:
                        auto_enter_mode = "coco"
                        print(f"🤖 自动进入编程模式 (回复编程消息)")

                    return project, auto_enter_mode

        return self._project_manager.get_active_project(chat_id), None

    def _register_message_project(self, message_id: str, project: ProjectContext):
        self._message_mapper.register(message_id, project.project_id)

    def _handle_message(self, data: P2ImMessageReceiveV1):
        self._executor.submit(self._process_message_async, data)

    def _process_message_async(self, data: P2ImMessageReceiveV1):
        try:
            event = data.event
            message = event.message

            message_id = message.message_id
            chat_id = message.chat_id
            message_type = message.message_type
            content_str = message.content
            create_time = message.create_time

            parent_id = getattr(message, 'parent_id', None)
            root_id = getattr(message, 'root_id', None)

            if create_time and self._is_message_expired(int(create_time)):
                print(f"⏭️ 跳过过期消息: {message_id} (超过{self.MESSAGE_EXPIRE_SECONDS}秒)")
                return

            if self._is_duplicate_message(message_id):
                print(f"⏭️ 跳过重复消息: {message_id}")
                return

            supported_types = {"text", "image", "post"}
            if message_type not in supported_types:
                self._reply_message(message_id, "⚠️ 目前仅支持文本、图片和富文本消息")
                return

            image_handler = self._get_image_handler()
            parse_result = image_handler.parse_message(message_type, content_str)

            text = parse_result.text.strip()
            if text.startswith("@"):
                parts = text.split(None, 1)
                if len(parts) > 1:
                    text = parts[1].strip()
                else:
                    text = ""

            if parse_result.image_keys:
                # 保存原始 image_keys 供响应卡片展示图片
                self._pending_image_keys[message_id] = parse_result.image_keys

                project, auto_enter_mode = self._resolve_project_from_message(
                    message_id, chat_id, parent_id or root_id
                )
                save_dir = FeishuImageHandler.get_image_save_dir(
                    project.root_path if project else None,
                    self._get_working_dir(chat_id),
                )
                download_result = image_handler.download_images(
                    message_id, parse_result.image_keys, save_dir
                )
                if download_result.saved_paths:
                    ref_text = FeishuImageHandler.build_image_reference_text(
                        download_result.saved_paths
                    )
                    if text:
                        text += ref_text
                    else:
                        text = "用户发送了图片，请查看以下图片文件：" + ref_text
                if download_result.failed_keys:
                    print(f"部分图片下载失败: {download_result.failed_keys}")
            else:
                project = None
                auto_enter_mode = None

            if not text:
                # 编程模式下即使 text 为空（如图片下载失败），也路由到编程 handler
                from ..mode import InteractionMode
                current_mode = self._mode_manager.get_mode(chat_id)
                if current_mode == InteractionMode.CLAUDE:
                    if project is None:
                        project = self._project_manager.get_active_project(chat_id)
                    self._handle_claude_message(message_id, chat_id, text, project)
                    return
                elif current_mode == InteractionMode.COCO:
                    if project is None:
                        project = self._project_manager.get_active_project(chat_id)
                    self._handle_coco_message(message_id, chat_id, text, project)
                    return
                self._show_help(message_id, chat_id)
                return

            if project is None and auto_enter_mode is None:
                project, auto_enter_mode = self._resolve_project_from_message(
                    message_id, chat_id, parent_id or root_id
                )

            try:
                if auto_enter_mode == "claude":
                    # 自动进入时不要直接 set_mode：需要走 _enter_claude_mode 来确保会话/项目状态一致
                    self._enter_claude_mode(message_id, chat_id, silent=True, project=project)
                    self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                    self._add_reaction(message_id, EmojiReaction.on_processing())
                    self._handle_claude_message(message_id, chat_id, text, project)
                elif auto_enter_mode == "coco":
                    # 自动进入时不要直接 set_mode：需要走 _enter_coco_mode 来确保会话/项目状态一致
                    self._enter_coco_mode(message_id, chat_id, silent=True, project=project)
                    self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                    self._add_reaction(message_id, EmojiReaction.on_processing())
                    self._handle_coco_message(message_id, chat_id, text, project)
                else:
                    self._process_with_intent(message_id, chat_id, text, project)
            finally:
                self._pending_image_keys.pop(message_id, None)

        except Exception as e:
            print(f"处理消息异常: {e}")
            import traceback
            traceback.print_exc()

    def _show_project_board(self, message_id: str, chat_id: str):
        projects = self._project_manager.get_all_projects()
        active_project = self._project_manager.get_active_project(chat_id)
        current_id = active_project.project_id if active_project else None

        msg_type, content = CardBuilder.build_status_board_card(projects, current_id)
        response_id = self._reply_message_with_id(message_id, content, msg_type)

        if response_id and active_project:
            self._register_message_project(response_id, active_project)

    def _show_current_project(self, message_id: str, chat_id: str, project: Optional[ProjectContext]):
        if not project:
            self._reply_message(message_id, "📋 当前没有活跃项目\n\n发送 `/projects` 查看项目列表\n发送 `/new 项目名 路径` 创建新项目")
            return

        global_working_dir = self._get_working_dir(chat_id)
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
            project, "当前项目", content, show_buttons=True
        )
        response_id = self._reply_message_with_id(message_id, card_content, msg_type)
        if response_id:
            self._register_message_project(response_id, project)

    def _show_project_status(self, message_id: str, chat_id: str, project: Optional[ProjectContext]):
        if not project:
            self._show_project_board(message_id, chat_id)
            return

        coco_info = ""
        if project.coco_mode and project.coco_session_snapshot:
            snap = project.coco_session_snapshot
            coco_info = f"\n\n🤖 **Coco 会话**\n• 会话 ID: `{snap.session_id}`\n• 对话数: {snap.query_count}"

        claude_info = ""
        if project.claude_mode and project.claude_session_snapshot:
            snap = project.claude_session_snapshot
            claude_info = f"\n\n🔮 **Claude 会话**\n• 会话 ID: `{snap.session_id}`\n• 对话数: {snap.query_count}"

        global_working_dir = self._get_working_dir(chat_id)
        content = (
            f"• 状态: {project.get_status_emoji()} {project.status.value}\n"
            f"• 📂 项目目录: `{project.root_path}`\n"
            f"• 📁 工作目录: `{global_working_dir}`\n"
            f"• 最后活跃: {CardBuilder._format_time_ago(project.last_active)}"
            f"{coco_info}{claude_info}"
        )

        msg_type, card_content = CardBuilder.build_project_response_card(
            project, "项目状态", content, show_buttons=True
        )
        response_id = self._reply_message_with_id(message_id, card_content, msg_type)
        if response_id:
            self._register_message_project(response_id, project)

    def _create_project(self, message_id: str, chat_id: str, name: str, path: str):
        project_id = name.lower().replace(" ", "_").replace("-", "_")

        success, msg, project = self._project_manager.create_project(
            project_id=project_id,
            project_name=name,
            root_path=path,
            chat_id=chat_id
        )

        if success and project:
            msg_type, card_content = CardBuilder.build_project_created_card(project)
            response_id = self._reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self._register_message_project(response_id, project)
        else:
            msg_type, card_content = CardBuilder.build_error_card(msg)
            self._reply_message(message_id, card_content, msg_type)

    def _switch_project(self, message_id: str, chat_id: str, name: str, auto_enter_coco: bool = True):
        project = self._project_manager.find_project_by_name(name)
        if not project:
            results = self._project_manager.search_projects(name)
            if results:
                suggestions = "\n".join([f"• {p.project_name}" for p in results[:5]])
                self._reply_message(message_id, f"❌ 未找到项目: {name}\n\n**相似项目：**\n{suggestions}")
            else:
                self._reply_message(message_id, f"❌ 未找到项目: {name}\n\n发送 `/projects` 查看所有项目")
            return

        valid, path_msg = self._project_manager.validate_project_path(project.project_id)
        if not valid:
            self._reply_message(message_id, f"⚠️ {path_msg}\n\n请检查项目路径是否存在")
            return

        success, msg = self._project_manager.set_active_project(chat_id, project.project_id)
        if not success:
            self._reply_message(message_id, f"❌ {msg}")
            return

        if auto_enter_coco:
            self._enter_coco_mode(message_id, chat_id, project=project)
        else:
            content = f"已切换到项目 **{project.project_name}**\n\n� 项目目录: `{project.root_path}`"

            if project.coco_session_snapshot and project.coco_session_snapshot.is_resumable:
                msg_type, card_content = CardBuilder.build_coco_resume_card(project)
            elif project.claude_session_snapshot and project.claude_session_snapshot.is_resumable:
                msg_type, card_content = CardBuilder.build_claude_resume_card(project)
            else:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "🔄 项目已切换", content, show_buttons=True
                )

            response_id = self._reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self._register_message_project(response_id, project)

    def _close_project(self, message_id: str, chat_id: str, name: str):
        project = self._project_manager.find_project_by_name(name)
        if not project:
            self._reply_message(message_id, f"❌ 未找到项目: {name}")
            return

        success, msg = self._project_manager.close_project(project.project_id)
        if success:
            self._reply_message(message_id, f"✅ {msg}")
        else:
            self._reply_message(message_id, f"❌ {msg}")

    def _handle_card_action(self, data: P2CardActionTrigger) -> Optional[P2CardActionTriggerResponse]:
        try:
            header = data.header
            event = data.event
            action = event.action
            context = event.context
            value_preview = action.value
            if isinstance(value_preview, str):
                value_preview = value_preview[:500]
            else:
                try:
                    value_preview = json.dumps(value_preview, ensure_ascii=False)[:500]
                except Exception:
                    value_preview = str(value_preview)[:500]
            print(
                "📩 卡片回调收到: "
                f"event_id={header.event_id}, event_type={header.event_type}, "
                f"open_message_id={context.open_message_id}, open_chat_id={context.open_chat_id}, "
                f"action_tag={action.tag}, action_name={action.name}, value_type={type(action.value).__name__}, "
                f"value_preview={value_preview}"
            )
        except Exception as e:
            print(f"卡片回调基础信息解析失败: {e}")
        self._executor.submit(self._process_card_action_async, data)
        return None

    def _process_card_action_async(self, data: Any):
        try:
            start_time = time.perf_counter()
            action = data.event.action
            value_raw = action.value
            operator = data.event.operator
            open_message_id = data.event.context.open_message_id
            open_chat_id = data.event.context.open_chat_id
            print(
                "🧾 卡片回调上下文: "
                f"operator_open_id={getattr(operator, 'open_id', None)}, "
                f"operator_user_id={getattr(operator, 'user_id', None)}, "
                f"value_raw_type={type(value_raw).__name__}"
            )

            if isinstance(value_raw, dict):
                value = value_raw
            elif isinstance(value_raw, str):
                try:
                    value = json.loads(value_raw)
                except (json.JSONDecodeError, TypeError):
                    print(f"⚠️ 卡片 value 解析失败: value_raw={value_raw[:500]}")
                    value = {"action": value_raw}
            else:
                value = {"action": str(value_raw)}

            action_type = value.get("action", "")
            project_id = value.get("project_id", "")

            print(
                "🔘 卡片按钮点击: "
                f"action={action_type}, project_id={project_id}, "
                f"value_keys={list(value.keys())}"
            )

            if action_type == "enter_coco":
                self._handle_card_enter_coco(open_message_id, open_chat_id, project_id)
            elif action_type == "exit_coco":
                self._handle_card_exit_coco(open_message_id, open_chat_id, project_id)
            elif action_type == "show_status":
                project = self._project_manager.get_project(project_id) if project_id else None
                self._show_project_status(open_message_id, open_chat_id, project)
            elif action_type == "switch_project":
                self._show_project_board(open_message_id, open_chat_id)
            elif action_type == "switch_to":
                if project_id:
                    project = self._project_manager.get_project(project_id)
                    if project:
                        self._switch_project(open_message_id, open_chat_id, project.project_name)
            elif action_type == "continue_dev":
                project = self._project_manager.get_project(project_id) if project_id else None
                if project:
                    self._project_manager.set_active_project(open_chat_id, project_id)
                    content = f"继续在 **{project.project_name}** 项目中开发\n\n� 项目目录: `{project.root_path}`\n\n直接发送命令或消息即可"
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project, "继续开发", content, show_buttons=True
                    )
                    response_id = self._reply_message_with_id(open_message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
            elif action_type == "show_board":
                self._show_project_board(open_message_id, open_chat_id)
            elif action_type == "refresh_board":
                self._show_project_board(open_message_id, open_chat_id)
            elif action_type == "show_detail":
                project = self._project_manager.get_project(project_id) if project_id else None
                self._show_project_status(open_message_id, open_chat_id, project)
            elif action_type == "list_files":
                project = self._project_manager.get_project(project_id) if project_id else None
                if project:
                    self._project_manager.set_active_project(open_chat_id, project_id)
                    self.message_callback(open_message_id, open_chat_id, "ls -la", project.root_path)
            elif action_type == "resume_coco":
                session_id = value.get("session_id", "")
                self._handle_card_resume_coco(open_message_id, open_chat_id, project_id, session_id)
            elif action_type == "new_coco":
                self._handle_card_new_coco(open_message_id, open_chat_id, project_id)
            elif action_type == "enter_claude":
                self._handle_card_enter_claude(open_message_id, open_chat_id, project_id)
            elif action_type == "exit_claude":
                self._handle_card_exit_claude(open_message_id, open_chat_id, project_id)
            elif action_type == "resume_claude":
                session_id = value.get("session_id", "")
                self._handle_card_resume_claude(open_message_id, open_chat_id, project_id, session_id)
            elif action_type == "new_claude":
                self._handle_card_new_claude(open_message_id, open_chat_id, project_id)
            elif action_type == "deep_pause":
                self._pause_deep_engine(open_message_id, open_chat_id)
            elif action_type == "deep_resume":
                self._resume_deep_engine(open_message_id, open_chat_id)
            elif action_type == "deep_stop":
                self._stop_deep_engine(open_message_id, open_chat_id)
            elif action_type == "new_project_prompt":
                self._reply_message(open_message_id, "📝 创建新项目\n\n请发送: `/new 项目名 路径`\n\n例如: `/new myApp ~/workspace/myApp`")
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            print(f"⏱️ 卡片回调处理耗时: {elapsed_ms}ms")

        except Exception as e:
            print(f"处理卡片回调异常: {e}")
            import traceback
            traceback.print_exc()

    def _handle_card_enter_coco(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self._project_manager.get_project(project_id)
            if project:
                self._project_manager.set_active_project(chat_id, project_id)

                if project.coco_session_snapshot and project.coco_session_snapshot.is_resumable:
                    msg_type, card_content = CardBuilder.build_coco_resume_card(project)
                    response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
                    return

                # 重要：按钮入口必须把 project 透传进去，否则会按 working_dir 自动选错项目
                self._enter_coco_mode(message_id, chat_id, project=project)
                return

        self._enter_coco_mode(message_id, chat_id)

    def _handle_card_exit_coco(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self._project_manager.get_project(project_id)
            if project and project.coco_mode:
                project.set_coco_mode(False)

            # 透传 project，确保 snapshot 能更新、状态能落盘
            self._exit_coco_mode(message_id, chat_id, project=project)
            return

        self._exit_coco_mode(message_id, chat_id)

    def _handle_card_resume_coco(self, message_id: str, chat_id: str, project_id: str, session_id: str):
        project = self._project_manager.get_project(project_id) if project_id else None
        if project:
            self._project_manager.set_active_project(chat_id, project_id)

        self._add_reaction(message_id, EmojiReaction.on_coco_enter())

        session = self._coco_manager.start_session(chat_id)
        session.session_id = session_id

        if project:
            project.set_coco_mode(True, session_id)
            content = f"🔄 已恢复 Coco 会话\n\n会话 ID: `{session_id}`\n\n现在可以继续之前的对话了"
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, "Coco 会话已恢复", content, show_buttons=True
            )
            response_id = self._reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self._register_message_project(response_id, project)
        else:
            self._reply_message(message_id, f"🔄 已恢复 Coco 会话: `{session_id}`")

    def _handle_card_new_coco(self, message_id: str, chat_id: str, project_id: str):
        project = self._project_manager.get_project(project_id) if project_id else None
        if project:
            self._project_manager.set_active_project(chat_id, project_id)
            project.coco_session_snapshot = None

            self._enter_coco_mode(message_id, chat_id, project=project)
            return

        self._enter_coco_mode(message_id, chat_id)

    def _handle_card_enter_claude(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self._project_manager.get_project(project_id)
            if project:
                self._project_manager.set_active_project(chat_id, project_id)

                if project.claude_session_snapshot and project.claude_session_snapshot.is_resumable:
                    msg_type, card_content = CardBuilder.build_claude_resume_card(project)
                    response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
                    return

                # 重要：按钮入口必须把 project 透传进去，否则会按 working_dir 自动选错项目
                self._enter_claude_mode(message_id, chat_id, project=project)
                return

        self._enter_claude_mode(message_id, chat_id)

    def _handle_card_exit_claude(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self._project_manager.get_project(project_id)
            if project and project.claude_mode:
                project.set_claude_mode(False)

            # 透传 project，确保 snapshot 能更新、状态能落盘
            self._exit_claude_mode(message_id, chat_id, project=project)
            return

        self._exit_claude_mode(message_id, chat_id)

    def _handle_card_resume_claude(self, message_id: str, chat_id: str, project_id: str, session_id: str):
        project = self._project_manager.get_project(project_id) if project_id else None
        if project:
            self._project_manager.set_active_project(chat_id, project_id)

        self._add_reaction(message_id, EmojiReaction.on_coco_enter())

        session = self._claude_manager.start_session(chat_id, session_id=session_id)
        session.is_resumed = True

        # 恢复会话时也要保持项目模式互斥
        if project and project.coco_mode:
            project.set_coco_mode(False)

        self._mode_manager.enter_claude_mode(chat_id)

        if project:
            project.set_claude_mode(True, session_id)
            content = f"🔄 已恢复 Claude 会话\n\n会话 ID: `{session_id}`\n\n现在可以继续之前的对话了"
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, "Claude 会话已恢复", content, show_buttons=True
            )
            response_id = self._reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self._register_message_project(response_id, project)
        else:
            self._reply_message(message_id, f"🔄 已恢复 Claude 会话: `{session_id}`")

    def _handle_card_new_claude(self, message_id: str, chat_id: str, project_id: str):
        project = self._project_manager.get_project(project_id) if project_id else None
        if project:
            self._project_manager.set_active_project(chat_id, project_id)
            project.claude_session_snapshot = None

            self._enter_claude_mode(message_id, chat_id, project=project)
            return

        self._enter_claude_mode(message_id, chat_id)

    def _process_with_intent(self, message_id: str, chat_id: str, text: str, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode
        
        current_mode = self._mode_manager.get_mode(chat_id)
        is_in_coco = current_mode == InteractionMode.COCO
        is_in_claude = current_mode == InteractionMode.CLAUDE
        
        if is_in_coco:
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._exit_current_mode(message_id, chat_id, project=project)
                return
            
            if self._is_deep_command(text):
                self._add_reaction(message_id, EmojiReaction.on_smart_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                self._handle_deep_command(message_id, chat_id, text, project)
                return
            
            self._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_coco_message(message_id, chat_id, text, project)
            return

        if is_in_claude:
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._exit_current_mode(message_id, chat_id, project=project)
                return
            
            if self._is_deep_command(text):
                self._add_reaction(message_id, EmojiReaction.on_smart_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                self._handle_deep_command(message_id, chat_id, text, project)
                return
            
            self._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_claude_message(message_id, chat_id, text, project)
            return

        self._add_reaction(message_id, EmojiReaction.on_smart_mode())
        self._add_reaction(message_id, EmojiReaction.on_processing())

        try:
            intent_result = self._intent_recognizer.recognize(text, current_mode.value)
        except Exception as e:
            print(f"意图识别异常: {e}")
            working_dir = self._get_working_dir(chat_id)
            self.message_callback(message_id, chat_id, text, working_dir)
            return

        print(f"🧠 意图识别: {intent_result.primary_intent.value} (置信度: {intent_result.confidence:.2f}, 任务数: {len(intent_result.tasks)})")

        if intent_result.is_multi_task:
            self._execute_multi_tasks(message_id, chat_id, intent_result, project)
        else:
            self._execute_single_task(message_id, chat_id, intent_result.tasks[0] if intent_result.tasks else None, text, project)

    def _execute_multi_tasks(self, message_id: str, chat_id: str, intent_result: IntentResult, project: Optional[ProjectContext] = None):
        tasks = intent_result.tasks

        task_list = [{"description": task.description or self._get_task_description(task)} for task in tasks]
        plan_msg = fmt.format_multi_task_plan(task_list)
        self._reply_message(message_id, plan_msg)

        self._add_reaction(message_id, EmojiReaction.on_multi_task_start())

        all_success = True
        for i, task in enumerate(tasks, 1):
            success = self._execute_task_step(message_id, chat_id, task, step_num=i, total_steps=len(tasks), project=project)

            if task.intent == IntentType.ENTER_COCO:
                break

            if not success:
                all_success = False
                self._reply_message(message_id, f"⚠️ 步骤 {i} 执行失败，后续步骤已取消")
                break

        if all_success:
            self._add_reaction(message_id, EmojiReaction.on_multi_task_done())
        else:
            self._add_reaction(message_id, EmojiReaction.on_error())

    def _execute_single_task(self, message_id: str, chat_id: str, task: Optional[TaskStep], original_text: str, project: Optional[ProjectContext] = None):
        if not task:
            self._reply_message(message_id, "🤔 无法理解你的意图")
            return

        intent = task.intent
        data = task.data

        if intent == IntentType.ENTER_COCO:
            self._enter_coco_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_COCO:
            self._exit_coco_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_MODE:
            self._exit_current_mode(message_id, chat_id, project=project)

        elif intent == IntentType.CHANGE_DIR:
            path = data.get("path", "")
            self._change_directory(message_id, chat_id, path, project)

        elif intent == IntentType.COCO_MESSAGE:
            if data.get("command") == "info":
                self._show_coco_info(message_id, chat_id, project)
            else:
                self._handle_coco_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.ENTER_CLAUDE:
            self._enter_claude_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_CLAUDE:
            self._exit_claude_mode(message_id, chat_id, project=project)

        elif intent == IntentType.CLAUDE_MESSAGE:
            if data.get("command") == "info":
                self._show_claude_info(message_id, chat_id, project)
            else:
                self._handle_claude_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.SHOW_HELP:
            self._show_full_help(message_id, chat_id, project)

        elif intent == IntentType.CREATE_PROJECT:
            name = data.get("name", "")
            path = data.get("path", "")
            working_dir = self._get_working_dir(chat_id)
            
            if not path:
                path = working_dir
            
            if not name:
                name = os.path.basename(os.path.normpath(path))
                if not name or name in (".", "/", "~"):
                    name = f"project_{int(time.time())}"
            
            self._create_project(message_id, chat_id, name, path)

        elif intent == IntentType.SWITCH_PROJECT:
            name = data.get("name", "")
            if name:
                self._switch_project(message_id, chat_id, name)
            else:
                self._show_project_board(message_id, chat_id)

        elif intent == IntentType.LIST_PROJECTS:
            self._show_project_board(message_id, chat_id)

        elif intent == IntentType.CLOSE_PROJECT:
            name = data.get("name", "")
            if name:
                self._close_project(message_id, chat_id, name)
            else:
                self._reply_message(message_id, "❌ 请指定要关闭的项目名称")

        elif intent == IntentType.PROJECT_STATUS:
            self._show_project_status(message_id, chat_id, project)

        elif intent == IntentType.ENTER_DEEP:
            requirement = data.get("requirement") or original_text
            self._start_deep_engine(message_id, chat_id, requirement, project)

        elif intent == IntentType.DEEP_STATUS:
            self._show_deep_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_DEEP:
            self._stop_deep_engine(message_id, chat_id, project)

        elif intent == IntentType.SHELL_COMMAND:
            working_dir = self._get_working_dir(chat_id)
            cmd = data.get("command") or original_text
            self.message_callback(message_id, chat_id, cmd, working_dir)

            if project:
                project.add_conversation("user", cmd, message_id)

        elif intent == IntentType.UNKNOWN:
            self._reply_message(message_id, fmt.format_unknown_intent())

    def _execute_task_step(self, message_id: str, chat_id: str, task: TaskStep, step_num: int, total_steps: int, project: Optional[ProjectContext] = None) -> bool:
        intent = task.intent
        data = task.data
        desc = task.description or self._get_task_description(task)

        print(f"📌 执行步骤 {step_num}/{total_steps}: {desc}")

        try:
            if intent == IntentType.ENTER_COCO:
                self._enter_coco_mode(message_id, chat_id, silent=True, project=project)
                self._reply_message(message_id, f"✅ 步骤 {step_num}: 已进入 Coco 模式")
                return True

            elif intent == IntentType.EXIT_COCO:
                success = self._coco_manager.end_session(chat_id)
                if success:
                    self._reply_message(message_id, f"✅ 步骤 {step_num}: 已退出 Coco 模式")
                return success

            elif intent == IntentType.CHANGE_DIR:
                path = data.get("path", "")
                if not path:
                    current_dir = self._get_working_dir(chat_id)
                    self._reply_message(message_id, f"✅ 步骤 {step_num}: 当前目录 {current_dir}")
                    return True

                success, result = self._set_working_dir(chat_id, path)
                if success:
                    self._reply_message(message_id, f"✅ 步骤 {step_num}: 已切换到 {result}")
                else:
                    self._reply_message(message_id, f"❌ 步骤 {step_num}: {result}")
                return success

            elif intent == IntentType.CREATE_PROJECT:
                name = data.get("name", "")
                path = data.get("path", "")
                if not name:
                    name = f"project_{int(time.time())}"
                if not path:
                    path = self._get_working_dir(chat_id)
                project_id = name.lower().replace(" ", "_").replace("-", "_")
                success, msg, new_project = self._project_manager.create_project(
                    project_id=project_id,
                    project_name=name,
                    root_path=path,
                    chat_id=chat_id
                )
                if success:
                    self._reply_message(message_id, f"✅ 步骤 {step_num}: 已创建项目 {name}")
                    project = new_project
                else:
                    self._reply_message(message_id, f"❌ 步骤 {step_num}: {msg}")
                return success

            elif intent == IntentType.SWITCH_PROJECT:
                name = data.get("name", "")
                if name:
                    found_project = self._project_manager.find_project_by_name(name)
                    if found_project:
                        success, msg = self._project_manager.set_active_project(chat_id, found_project.project_id)
                        if success:
                            self._reply_message(message_id, f"✅ 步骤 {step_num}: 已切换到项目 {name}")
                        return success
                return False

            elif intent == IntentType.SHELL_COMMAND:
                cmd = data.get("command", task.description)
                if cmd:
                    working_dir = self._get_working_dir(chat_id)
                    self.message_callback(message_id, chat_id, cmd, working_dir)
                return True

            else:
                return False

        except Exception as e:
            print(f"执行步骤 {step_num} 异常: {e}")
            return False

    def _get_task_description(self, task: TaskStep) -> str:
        intent = task.intent
        data = task.data

        if intent == IntentType.ENTER_COCO:
            return "进入 Coco 编程模式"
        elif intent == IntentType.EXIT_COCO:
            return "退出 Coco 模式"
        elif intent == IntentType.CHANGE_DIR:
            path = data.get("path", "")
            return f"切换到目录: {path}" if path else "查看当前目录"
        elif intent == IntentType.CREATE_PROJECT:
            name = data.get("name", "")
            return f"创建项目: {name}" if name else "创建新项目"
        elif intent == IntentType.SWITCH_PROJECT:
            name = data.get("name", "")
            return f"切换到项目: {name}" if name else "切换项目"
        elif intent == IntentType.LIST_PROJECTS:
            return "查看项目列表"
        elif intent == IntentType.CLOSE_PROJECT:
            name = data.get("name", "")
            return f"关闭项目: {name}" if name else "关闭项目"
        elif intent == IntentType.PROJECT_STATUS:
            return "查看项目状态"
        elif intent == IntentType.SHELL_COMMAND:
            return f"执行命令"
        else:
            return "未知操作"

    def _enter_coco_mode(self, message_id: str, chat_id: str, silent: bool = False, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode

        if self._mode_manager.is_coco_mode(chat_id):
            if not silent:
                info = self._coco_manager.get_session_info(chat_id)
                self._reply_message(
                    message_id,
                    fmt.format_warning(f"已经在编程模式中\n\n{info}\n\n说「退出模式」或发送 /exit 退出")
                )
            return

        # If in Claude mode, exit it first (mutual exclusion)
        if self._mode_manager.is_claude_mode(chat_id):
            self._exit_claude_mode(message_id, chat_id, project=project)

        self._mode_manager.enter_coco_mode(chat_id)
        self._add_reaction(message_id, EmojiReaction.on_coco_enter())

        if not project:
            working_dir = self._get_working_dir(chat_id)
            try:
                project, is_new = self._project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    print(f"📁 自动创建项目: {project.project_name} @ {project.root_path}")
            except Exception as e:
                print(f"自动创建项目失败: {e}")

        session = self._coco_manager.start_session(chat_id)

        if project:
            valid, path_msg = self._project_manager.validate_project_path(project.project_id)
            if not valid:
                if not silent:
                    self._reply_message(message_id, f"⚠️ {path_msg}\n\n请切换到有效目录后重试")
                return

            if project.coco_session_snapshot and project.coco_session_snapshot.is_resumable:
                session.session_id = project.coco_session_snapshot.session_id
                session.is_resumed = True
                project.set_coco_mode(True, session.session_id, project.coco_session_snapshot.query_count)
                if not silent:
                    content = (
                        f"🔄 已恢复 Coco 会话\n\n"
                        f"• 会话 ID: `{session.session_id}`\n"
                        f"• 历史对话: {project.coco_session_snapshot.query_count} 条\n\n"
                        f"继续之前的对话吧！"
                    )
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project, "Coco 会话已恢复", content, show_buttons=True,
                        footer=f"� 项目目录: {project.root_path}"
                    )
                    response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
            else:
                project.set_coco_mode(True, session.session_id)
                if not silent:
                    content = "🤖 已进入编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出"
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project, "🤖 编程模式", content, show_buttons=True,
                        footer=f"📂 项目目录: {project.root_path}"
                    )
                    response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
        else:
            if not silent:
                self._reply_message(message_id, fmt.format_coco_enter())

    def _exit_coco_mode(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        session = self._coco_manager.get_session(chat_id)

        if project:
            if session:
                project.update_coco_snapshot(
                    query=session.last_query,
                    query_count=session.message_count
                )
            # 无论会话是否存在，都要把项目状态切回非 Coco，避免卡片/按钮显示错乱
            project.set_coco_mode(False)

        self._mode_manager.exit_to_smart(chat_id)
        
        if self._coco_manager.end_session(chat_id):
            self._add_reaction(message_id, EmojiReaction.on_coco_exit())

            if project:
                content = "👋 已退出编程模式\n\n会话已保存，下次可以恢复\n\n当前为 🧠 智能模式"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "已退出编程模式", content, show_buttons=True
                )
                response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)
            else:
                self._reply_message(message_id, "👋 已退出编程模式\n\n当前为 🧠 智能模式")
        else:
            self._reply_message(message_id, fmt.format_warning("当前不在编程模式中"))

    def _is_exit_command(self, text: str) -> bool:
        text_lower = text.lower().strip()
        exit_commands = {"/exit", "/quit", "/end_coco", "/exit_coco", "/end_claude", "/exit_claude"}
        exit_keywords = {"退出模式", "退出编程模式", "退出编程", "结束编程", "退出claude", "退出coco"}
        
        if text_lower in exit_commands:
            return True
        
        return any(kw in text_lower for kw in exit_keywords)

    def _is_deep_command(self, text: str) -> bool:
        text_lower = text.lower().strip()
        return text_lower.startswith("/deep") or text_lower.startswith("/stop_deep")

    def _handle_deep_command(self, message_id: str, chat_id: str, text: str, project: Optional[ProjectContext] = None):
        text_lower = text.lower().strip()
        
        if text_lower == "/deep_status":
            self._show_deep_status(message_id, chat_id, project)
        elif text_lower == "/stop_deep":
            self._stop_deep_engine(message_id, chat_id, project)
        elif text_lower.startswith("/deep "):
            requirement = text[6:].strip()
            self._start_deep_engine(message_id, chat_id, requirement, project)
        elif text_lower == "/deep":
            self._reply_message(message_id, "📝 请提供需求描述\n\n用法: `/deep <你的需求描述>`\n\n例如: `/deep 帮我写一个 Python 爬虫，爬取豆瓣电影 Top250`")
        else:
            self._reply_message(message_id, "❓ 未知的 Deep 命令\n\n可用命令:\n• `/deep <需求>` - 启动 Deep Engine\n• `/deep_status` - 查看进度\n• `/stop_deep` - 停止任务")

    def _exit_current_mode(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode
        
        current_mode = self._mode_manager.get_mode(chat_id)
        
        if current_mode == InteractionMode.COCO:
            self._exit_coco_mode(message_id, chat_id, project)
        elif current_mode == InteractionMode.CLAUDE:
            self._exit_claude_mode(message_id, chat_id, project)
        else:
            self._reply_message(message_id, "🧠 当前已经在智能模式中")

    def _show_coco_info(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        info = self._coco_manager.get_session_info(chat_id)
        if info:
            if project:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "Coco 会话信息", info, show_buttons=True
                )
                response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)
            else:
                self._reply_message(message_id, info)
        else:
            self._reply_message(message_id, fmt.format_warning("当前不在 Coco 模式中"))

    def _enter_claude_mode(self, message_id: str, chat_id: str, silent: bool = False, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode

        if self._mode_manager.is_claude_mode(chat_id):
            if not silent:
                info = self._claude_manager.get_session_info(chat_id)
                self._reply_message(
                    message_id,
                    fmt.format_warning(f"已经在 Claude 编程模式中\n\n{info}\n\n说「退出模式」或发送 /exit 退出")
                )
            return

        # If in Coco mode, exit it first (mutual exclusion)
        if self._mode_manager.is_coco_mode(chat_id):
            self._exit_coco_mode(message_id, chat_id, project=project)

        self._mode_manager.enter_claude_mode(chat_id)
        self._add_reaction(message_id, EmojiReaction.on_coco_enter())

        if not project:
            working_dir = self._get_working_dir(chat_id)
            try:
                project, is_new = self._project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    print(f"📁 自动创建项目: {project.project_name} @ {project.root_path}")
            except Exception as e:
                print(f"自动创建项目失败: {e}")

        session = self._claude_manager.start_session(chat_id)

        if project:
            valid, path_msg = self._project_manager.validate_project_path(project.project_id)
            if not valid:
                if not silent:
                    self._reply_message(message_id, f"⚠️ {path_msg}\n\n请切换到有效目录后重试")
                return

            if project.claude_session_snapshot and project.claude_session_snapshot.is_resumable:
                session.session_id = project.claude_session_snapshot.session_id
                session.is_resumed = True
                project.set_claude_mode(True, session.session_id, project.claude_session_snapshot.query_count)
                if not silent:
                    content = (
                        f"🔄 已恢复 Claude 会话\n\n"
                        f"• 会话 ID: `{session.session_id}`\n"
                        f"• 历史对话: {project.claude_session_snapshot.query_count} 条\n\n"
                        f"继续之前的对话吧！"
                    )
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project, "Claude 会话已恢复", content, show_buttons=True,
                        footer=f"📂 项目目录: {project.root_path}"
                    )
                    response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
            else:
                project.set_claude_mode(True, session.session_id)
                if not silent:
                    content = "🔮 已进入 Claude 编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出"
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project, "🔮 Claude 编程模式", content, show_buttons=True,
                        footer=f"📂 项目目录: {project.root_path}"
                    )
                    response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self._register_message_project(response_id, project)
        else:
            if not silent:
                self._reply_message(message_id, "🔮 已进入 Claude 编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出")

    def _exit_claude_mode(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        session = self._claude_manager.get_session(chat_id)

        if project:
            if session:
                project.update_claude_snapshot(
                    query=session.last_query,
                    query_count=session.message_count,
                    session_id=session.session_id
                )
            # 无论会话是否存在，都要把项目状态切回非 Claude，避免卡片/按钮显示错乱
            project.set_claude_mode(False)

        self._mode_manager.exit_to_smart(chat_id)

        if self._claude_manager.end_session(chat_id):
            self._add_reaction(message_id, EmojiReaction.on_coco_exit())

            if project:
                content = "👋 已退出 Claude 编程模式\n\n会话已保存，下次可以恢复\n\n当前为 🧠 智能模式"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "已退出 Claude 编程模式", content, show_buttons=True
                )
                response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)
            else:
                self._reply_message(message_id, "👋 已退出 Claude 编程模式\n\n当前为 🧠 智能模式")
        else:
            self._reply_message(message_id, fmt.format_warning("当前不在 Claude 编程模式中"))

    def _show_claude_info(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        info = self._claude_manager.get_session_info(chat_id)
        if info:
            if project:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "Claude 会话信息", info, show_buttons=True
                )
                response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)
            else:
                self._reply_message(message_id, info)
        else:
            self._reply_message(message_id, fmt.format_warning("当前不在 Claude 模式中"))

    def _handle_claude_message(self, message_id: str, chat_id: str, text: str, project: Optional[ProjectContext] = None):
        session = self._claude_manager.get_session(chat_id)
        
        if not session:
            if project:
                self._enter_claude_mode(message_id, chat_id, project=project)
                session = self._claude_manager.get_session(chat_id)
                if not session:
                    return
            else:
                self._reply_message(message_id, fmt.format_warning("Claude 会话已过期，请发送 /claude 重新开始"))
                return

        global_working_dir = self._get_working_dir(chat_id)
        
        if project:
            claude_cwd = project.root_path
        else:
            claude_cwd = global_working_dir

        if self._enable_streaming:
            self._handle_claude_streaming(message_id, chat_id, text, session, project, claude_cwd, global_working_dir)
        else:
            self._handle_claude_normal(message_id, chat_id, text, session, project, claude_cwd, global_working_dir)

    def _handle_claude_normal(self, message_id: str, chat_id: str, text: str, session, project, claude_cwd: str, global_working_dir: str):
        response = session.send_prompt(text, cwd=claude_cwd)

        if project:
            project.update_claude_snapshot(text, session.message_count, session.session_id)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", response[:200])

        self._add_reaction(message_id, EmojiReaction.on_coco_response())

        if project:
            footer = (
                f"📂 项目目录: {project.root_path}\n"
                f"📁 工作目录: {global_working_dir}"
            )
            image_keys = self._pending_image_keys.get(message_id)
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, "🔮 Claude", response, show_buttons=True, footer=footer,
                image_keys=image_keys,
            )
            response_id = self._reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self._register_message_project(response_id, project)
        else:
            response_with_dir = f"{response}\n\n---\n📁 工作目录: `{global_working_dir}`"
            self._reply_message(message_id, response_with_dir)

    def _handle_claude_streaming(self, message_id: str, chat_id: str, text: str, session, project, claude_cwd: str, global_working_dir: str):
        streaming_manager = self._get_streaming_manager()

        project_name = project.project_name if project else None
        project_path = project.root_path if project else global_working_dir
        project_id = project.project_id if project else None
        image_keys = self._pending_image_keys.get(message_id)

        print(f"🎬 开始 Claude 流式输出: project={project_name}, path={project_path}")

        streaming_card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project_name,
            project_path=project_path,
            project_id=project_id,
            initial_content="🔮 Claude 正在思考...",
            is_coco_mode=False,
            is_claude_mode=True,
            reply_to_message_id=message_id,
            image_keys=image_keys,
        )

        if not streaming_card:
            print("⚠️ 创建流式卡片失败，回退到普通模式")
            self._handle_claude_normal(message_id, chat_id, text, session, project, claude_cwd, global_working_dir)
            return

        card_message_id = streaming_manager.send_streaming_card(streaming_card)
        if not card_message_id:
            print("⚠️ 发送流式卡片失败，回退到普通模式")
            self._handle_claude_normal(message_id, chat_id, text, session, project, claude_cwd, global_working_dir)
            return

        update_count = [0]

        def on_chunk(content: str):
            update_count[0] += 1
            streaming_manager.update_content(streaming_card, content)

        final_response = session.send_prompt_streaming(
            text,
            on_chunk=on_chunk,
            cwd=claude_cwd,
            chunk_interval=0.3
        )

        print(f"🎬 Claude 流式输出完成: 更新次数={update_count[0]}, 最终长度={len(final_response)}")

        streaming_manager.close_streaming(streaming_card, final_content=final_response)

        if project:
            project.update_claude_snapshot(text, session.message_count, session.session_id)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", final_response[:200])

        self._add_reaction(message_id, EmojiReaction.on_coco_response())

        if card_message_id and project:
            self._register_message_project(card_message_id, project)

    def _change_directory(self, message_id: str, chat_id: str, path: str, project: Optional[ProjectContext] = None):
        current_dir = self._get_working_dir(chat_id)
        
        if not path:
            self._add_reaction(message_id, EmojiReaction.on_dir_changed())

            if project:
                content = (
                    f"📂 **项目目录**: `{project.root_path}`\n"
                    f"📁 **工作目录**: `{current_dir}`"
                )
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "目录信息", content, show_buttons=True
                )
                response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)
            else:
                self._reply_message(message_id, fmt.format_current_dir(current_dir))
            return

        success, result = self._set_working_dir(chat_id, path)
        if success:
            self._add_reaction(message_id, EmojiReaction.on_dir_changed())

            if project:
                content = f"✅ 已切换到: `{result}`"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "目录已切换", content, show_buttons=True
                )
                response_id = self._reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)
            else:
                self._reply_message(message_id, fmt.format_dir_change(result, True))
        else:
            self._add_reaction(message_id, EmojiReaction.on_error())
            self._reply_message(message_id, fmt.format_error(result))

    def _handle_coco_message(self, message_id: str, chat_id: str, text: str, project: Optional[ProjectContext] = None):
        session = self._coco_manager.get_session(chat_id)
        
        if project and project.coco_session_snapshot:
            project_session_id = project.coco_session_snapshot.session_id
            if not session or session.session_id != project_session_id:
                session = self._coco_manager.resume_session(chat_id, project_session_id)
                print(f"🔄 切换到项目 {project.project_name} 的 Coco 会话: {project_session_id}")
        
        if not session:
            if project:
                self._enter_coco_mode(message_id, chat_id, project=project)
                session = self._coco_manager.get_session(chat_id)
                if not session:
                    return
            else:
                self._reply_message(message_id, fmt.format_warning("Coco 会话已过期，请说「帮我写代码」重新开始"))
                return

        global_working_dir = self._get_working_dir(chat_id)
        
        if project:
            coco_cwd = project.root_path
        else:
            coco_cwd = global_working_dir

        if self._enable_streaming:
            self._handle_coco_streaming(message_id, chat_id, text, session, project, coco_cwd, global_working_dir)
        else:
            self._handle_coco_normal(message_id, chat_id, text, session, project, coco_cwd, global_working_dir)

    def _handle_coco_normal(self, message_id: str, chat_id: str, text: str, session, project, coco_cwd: str, global_working_dir: str):
        response = session.send_prompt(text, cwd=coco_cwd)

        if project:
            project.update_coco_snapshot(text, session.message_count)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", response[:200])

        self._add_reaction(message_id, EmojiReaction.on_coco_response())

        if project:
            footer = (
                f"📂 项目目录: {project.root_path}\n"
                f"📁 工作目录: {global_working_dir}"
            )
            image_keys = self._pending_image_keys.get(message_id)
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, "🤖 Coco", response, show_buttons=True, footer=footer,
                image_keys=image_keys,
            )
            response_id = self._reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self._register_message_project(response_id, project)
        else:
            response_with_dir = f"{response}\n\n---\n📁 工作目录: `{global_working_dir}`"
            self._reply_message(message_id, fmt.format_coco_response(response_with_dir))

    def _handle_coco_streaming(self, message_id: str, chat_id: str, text: str, session, project, coco_cwd: str, global_working_dir: str):
        streaming_manager = self._get_streaming_manager()

        project_name = project.project_name if project else None
        project_path = project.root_path if project else global_working_dir
        project_id = project.project_id if project else None
        image_keys = self._pending_image_keys.get(message_id)

        print(f"🎬 开始流式输出: project={project_name}, path={project_path}")

        streaming_card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project_name,
            project_path=project_path,
            project_id=project_id,
            initial_content="🤔 正在思考...",
            is_coco_mode=True,
            reply_to_message_id=message_id,
            image_keys=image_keys,
        )

        if not streaming_card:
            print("⚠️ 创建流式卡片失败，回退到普通模式")
            self._handle_coco_normal(message_id, chat_id, text, session, project, coco_cwd, global_working_dir)
            return

        card_message_id = streaming_manager.send_streaming_card(streaming_card)
        if not card_message_id:
            print("⚠️ 发送流式卡片失败，回退到普通模式")
            self._handle_coco_normal(message_id, chat_id, text, session, project, coco_cwd, global_working_dir)
            return

        update_count = [0]

        def on_chunk(content: str):
            update_count[0] += 1
            streaming_manager.update_content(streaming_card, content)

        final_response = session.send_prompt_streaming(
            text,
            on_chunk=on_chunk,
            cwd=coco_cwd,
            chunk_interval=0.3
        )

        print(f"🎬 流式输出完成: 更新次数={update_count[0]}, 最终长度={len(final_response)}")

        streaming_manager.close_streaming(streaming_card, final_content=final_response)

        if project:
            project.update_coco_snapshot(text, session.message_count)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", final_response[:200])

        self._add_reaction(message_id, EmojiReaction.on_coco_response())

        if card_message_id and project:
            self._register_message_project(card_message_id, project)

    def _show_help(self, message_id: str, chat_id: str):
        is_coco_mode = self._coco_manager.is_in_coco_mode(chat_id)
        current_dir = self._get_working_dir(chat_id)
        project = self._project_manager.get_active_project(chat_id)

        help_result = fmt.format_help(current_dir, is_coco_mode)

        # format_help returns ('post', json_str)，提取 markdown 文本避免 f-string 将 tuple 转为字符串
        if isinstance(help_result, tuple) and len(help_result) == 2:
            try:
                post_data = json.loads(help_result[1])
                lang_data = next(iter(post_data.values()))
                md_parts = []
                for row in lang_data.get("content", []):
                    for elem in row:
                        if elem.get("tag") == "md":
                            md_parts.append(elem.get("text", ""))
                help_md = "\n".join(md_parts)
            except Exception:
                help_md = str(help_result[1])
        else:
            help_md = str(help_result)

        project_help = (
            "\n\n📋 **项目管理命令**\n"
            "• `/projects` - 查看项目看板\n"
            "• `/new 名称 路径` - 创建新项目\n"
            "• `/switch 名称` - 切换项目\n"
            "• `/status` - 查看当前项目状态"
        )

        if project:
            self._reply_message(message_id, f"当前项目: **{project.project_name}**\n\n{help_md}{project_help}")
        else:
            self._reply_message(message_id, f"{help_md}{project_help}")

    def _show_full_help(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode
        
        current_mode = self._mode_manager.get_mode(chat_id)
        current_dir = self._get_working_dir(chat_id)
        
        mode_emoji = {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 Coco 编程模式",
            InteractionMode.CLAUDE: "🔮 Claude 编程模式",
        }
        current_mode_str = mode_emoji.get(current_mode, "🧠 智能模式")
        
        project_info = f"**{project.project_name}** (`{project.root_path}`)" if project else "无"
        
        help_card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📖 GhostAP 使用帮助"},
                "template": "blue"
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"**当前状态**\n• 模式: {current_mode_str}\n• 工作目录: `{current_dir}`\n• 项目: {project_info}"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "markdown",
                        "content": "**🔄 编程模式切换**\n`/coco` - 进入 Coco 编程模式（字节跳动 AI）\n`/claude` - 进入 Claude 编程模式（Anthropic AI）\n`/exit` - 退出当前编程模式\n`/coco_info` - 查看 Coco 会话信息\n`/claude_info` - 查看 Claude 会话信息"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "markdown",
                        "content": "**📂 项目管理**\n`/projects` - 查看所有项目\n`/new <名称> [路径]` - 创建新项目\n`/switch <名称>` - 切换项目\n`/close <名称>` - 关闭项目\n`/status` - 查看当前项目状态"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "markdown",
                        "content": "**🧠 Deep Engine（复杂任务）**\n`/deep <需求>` - 启动 Deep Engine\n`/deep_status` - 查看任务进度\n`/stop_deep` - 停止任务"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "markdown",
                        "content": "**💡 使用提示**\n1. 发送 `/coco` 或 `/claude` 进入编程模式\n2. 在编程模式中发送 `/exit` 或说「退出模式」退出\n3. 智能模式下直接输入 Shell 命令即可执行\n4. 发送 `/help` 或 `/帮助` 查看本帮助"
                    }
                ]
            }
        }
        
        card_content = json.dumps(help_card, ensure_ascii=False)
        self._reply_message(message_id, card_content, msg_type="interactive")

    def _reply_message(self, message_id: str, content, msg_type: str = "text"):
        try:
            client = self._get_api_client()

            if isinstance(content, tuple) and len(content) == 2:
                msg_type = content[0]
                content_str = content[1]
            elif fmt.is_post_format(content):
                msg_type = content[0]
                content_str = content[1]
            elif msg_type == "text":
                content_str = json.dumps({"text": content})
            else:
                content_str = content

            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .content(content_str)
                    .msg_type(msg_type)
                    .build()) \
                .build()

            response = client.im.v1.message.reply(request)
            if not response.success():
                print(f"回复消息失败: {response.code} - {response.msg}")
        except Exception as e:
            print(f"回复消息异常: {e}")

    def _reply_message_with_id(self, message_id: str, content: str, msg_type: str = "text") -> Optional[str]:
        try:
            client = self._get_api_client()

            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type(msg_type)
                    .build()) \
                .build()

            response = client.im.v1.message.reply(request)
            if response.success() and response.data and response.data.message_id:
                return response.data.message_id
            else:
                print(f"回复消息失败: {response.code} - {response.msg}")
                return None
        except Exception as e:
            print(f"回复消息异常: {e}")
            return None

    def send_message(self, chat_id: str, content: str, msg_type: str = "text") -> Optional[str]:
        try:
            client = self._get_api_client()

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content)
                    .msg_type(msg_type)
                    .build()) \
                .build()

            response = client.im.v1.message.create(request)
            if response.success() and response.data and response.data.message_id:
                return response.data.message_id
            else:
                print(f"发送消息失败: {response.code} - {response.msg}")
                return None
        except Exception as e:
            print(f"发送消息异常: {e}")
            return None

    def reply(self, message_id: str, content, msg_type: str = "text", chat_id: Optional[str] = None):
        self._reply_message(message_id, content, msg_type)

    def add_reaction(self, message_id: str, emoji_type: str):
        self._add_reaction(message_id, emoji_type)

    def _handle_reaction_created(self, data):
        pass

    def _handle_chat_entered(self, data):
        pass

    def _handle_message_read(self, data):
        pass

    def _start_deep_engine(self, message_id: str, chat_id: str, requirement: str, project: Optional[ProjectContext] = None):
        if not project:
            working_dir = self._get_working_dir(chat_id)
            try:
                project, is_new = self._project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    print(f"📁 Deep Engine 自动创建项目: {project.project_name} @ {project.root_path}")
            except Exception as e:
                self._reply_message(message_id, f"❌ 创建项目失败: {e}")
                return

        root_path = project.root_path if project else self._get_working_dir(chat_id)

        active_engine = self._deep_engine_manager.get_active_engine(chat_id)
        if active_engine and active_engine.is_running:
            self._reply_message(message_id, "⚠️ 已有 Deep Engine 任务在执行中\n\n发送 `/deep_status` 查看进度\n发送 `/stop_deep` 停止任务")
            return

        self._add_reaction(message_id, EmojiReaction.on_multi_task_start())

        planning_content = self._progress_reporter.format_planning_start(requirement)
        planning_title = self._progress_reporter.get_planning_start_title()
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            title=planning_title,
            content=planning_content,
            engine_name="Coco",
            show_buttons=False,
        )
        self._reply_message(message_id, card_content, msg_type=msg_type)

        engine = self._deep_engine_manager.get_or_create(chat_id, root_path)

        def run_deep_engine():
            try:
                callbacks = self._create_deep_callbacks(message_id, chat_id, project)
                engine.plan_and_execute(requirement, callbacks)
            except Exception as e:
                print(f"Deep Engine 执行异常: {e}")
                import traceback
                traceback.print_exc()
                error_content = self._progress_reporter.format_error(str(e))
                error_title = self._progress_reporter.get_error_title()
                err_msg_type, err_card = CardBuilder.build_deep_card(
                    project=project,
                    title=error_title,
                    content=error_content,
                    engine_name="Coco",
                    show_buttons=False,
                )
                self.send_message(chat_id, err_card, err_msg_type)

        self._executor.submit(run_deep_engine)

    def _create_deep_callbacks(self, message_id: str, chat_id: str, project: Optional[ProjectContext]) -> DeepEngineCallbacks:
        engine_name = "Coco"

        def on_planning_done(deep_project: DeepProject):
            content = self._progress_reporter.format_planning_done(deep_project)
            title = self._progress_reporter.get_planning_done_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                title=title,
                content=content,
                deep_project_id=deep_project.project_id,
                engine_name=engine_name,
                show_buttons=False,
            )
            self.send_message(chat_id, card_content, msg_type)

        def on_task_start(task: DeepTask, current: int, total: int):
            content = self._progress_reporter.format_task_start(task, current, total)
            title = self._progress_reporter.get_task_start_title(current, total)
            progress_bar = self._progress_reporter._make_progress_bar(current - 1, total)
            engine = self._deep_engine_manager.get(chat_id, project.root_path if project else "")
            deep_project_id = engine.project.project_id if engine and engine.project else None
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                title=title,
                content=content,
                progress_bar=progress_bar,
                deep_project_id=deep_project_id,
                is_executing=True,
                engine_name=engine_name,
            )
            self.send_message(chat_id, card_content, msg_type)

        def on_task_done(task: DeepTask, result: ExecutionResult):
            engine = self._deep_engine_manager.get(chat_id, project.root_path if project else "")
            if engine and engine.project:
                current = engine.project.completed_count
                total = engine.project.total_count
                content = self._progress_reporter.format_task_done(task, result, current, total)
                title = self._progress_reporter.get_task_done_title(result.success, current, total)
                progress_bar = self._progress_reporter._make_progress_bar(current, total)
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    deep_project_id=engine.project.project_id,
                    is_executing=True,
                    engine_name=engine_name,
                )
                self.send_message(chat_id, card_content, msg_type)

        def on_project_done(deep_project: DeepProject):
            content = self._progress_reporter.format_project_done(deep_project)
            title = self._progress_reporter.get_project_done_title(deep_project)
            progress_bar = self._progress_reporter._make_progress_bar(
                deep_project.completed_count, deep_project.total_count
            )
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                title=title,
                content=content,
                progress_bar=progress_bar,
                deep_project_id=deep_project.project_id,
                engine_name=engine_name,
            )
            self.send_message(chat_id, card_content, msg_type)
            self._add_reaction(message_id, EmojiReaction.on_multi_task_done())

        def on_error(error: str):
            content = self._progress_reporter.format_error(error)
            title = self._progress_reporter.get_error_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                title=title,
                content=content,
                engine_name=engine_name,
                show_buttons=False,
            )
            self.send_message(chat_id, card_content, msg_type)
            self._add_reaction(message_id, EmojiReaction.on_error())

        return DeepEngineCallbacks(
            on_planning_done=on_planning_done,
            on_task_start=on_task_start,
            on_task_done=on_task_done,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def _show_deep_status(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        root_path = project.root_path if project else self._get_working_dir(chat_id)
        engine = self._deep_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            active_engine = self._deep_engine_manager.get_active_engine(chat_id)
            if active_engine and active_engine.project:
                engine = active_engine
            else:
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    title="📊 当前状态",
                    content="当前没有 Deep Engine 任务\n\n发送 `/deep 你的需求` 开始一个复杂任务",
                    engine_name="Coco",
                    show_buttons=False,
                )
                self._reply_message(message_id, card_content, msg_type=msg_type)
                return

        status_content = self._progress_reporter.format_status(engine.project)
        status_title = self._progress_reporter.get_status_title()
        progress_info = self._progress_reporter.get_progress_info(engine.project)
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            title=status_title,
            content=status_content,
            progress_bar=progress_info["progress_bar"],
            deep_project_id=progress_info["project_id"],
            is_executing=progress_info["is_executing"],
            is_paused=progress_info["is_paused"],
            engine_name="Coco",
        )
        self._reply_message(message_id, card_content, msg_type=msg_type)

    def _pause_deep_engine(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        root_path = project.root_path if project else self._get_working_dir(chat_id)
        engine = self._deep_engine_manager.get(chat_id, root_path)

        if not engine:
            engine = self._deep_engine_manager.get_active_engine(chat_id)

        if engine and engine.is_running:
            engine.pause()
            self._reply_message(message_id, "⏸️ Deep Engine 已暂停")
        else:
            self._reply_message(message_id, "当前没有正在执行的任务")

    def _resume_deep_engine(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        root_path = project.root_path if project else self._get_working_dir(chat_id)
        engine = self._deep_engine_manager.get(chat_id, root_path)

        if not engine:
            engine = self._deep_engine_manager.get_active_engine(chat_id)

        if engine and engine.project and engine.project.status == DeepProjectStatus.PAUSED:
            callbacks = self._create_deep_callbacks(message_id, chat_id, project)
            def run_resume():
                engine.resume(callbacks)
            self._executor.submit(run_resume)
            self._reply_message(message_id, "▶️ Deep Engine 已恢复执行")
        else:
            self._reply_message(message_id, "当前没有可恢复的任务")

    def _stop_deep_engine(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        root_path = project.root_path if project else self._get_working_dir(chat_id)
        engine = self._deep_engine_manager.get(chat_id, root_path)

        if not engine:
            active_engine = self._deep_engine_manager.get_active_engine(chat_id)
            if active_engine:
                engine = active_engine

        if not engine or not engine.is_running:
            self._reply_message(message_id, "📊 当前没有正在执行的 Deep Engine 任务")
            return

        engine.stop()
        self._reply_message(message_id, "🛑 已发送停止信号，任务将在当前步骤完成后停止")

    def start(self):
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._handle_message) \
            .register_p2_im_message_reaction_created_v1(self._handle_reaction_created) \
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._handle_chat_entered) \
            .register_p2_im_message_message_read_v1(self._handle_message_read) \
            .register_p2_card_action_trigger(self._handle_card_action) \
            .build()

        self._client = lark.ws.Client(
            self.settings.app_id,
            self.settings.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG
        )

        self._message_cache.start_cleanup_thread()
        
        print("🔌 正在建立飞书长连接...")
        print("📋 多项目管理已启用")
        self._client.start()
