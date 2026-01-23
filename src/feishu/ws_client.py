import json
import time
import os
from collections import OrderedDict
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse
from typing import Callable, Optional, Any
import threading
from concurrent.futures import ThreadPoolExecutor
from ..config import get_settings
from ..coco.session import CocoSessionManager
from ..agent.intent_recognizer import IntentRecognizer, IntentType, IntentResult, TaskStep
from ..project import ProjectManager, ProjectContext, ProjectStatus, MessageProjectMapper
from ..card import CardBuilder
from ..card.streaming import StreamingCardManager
from .message_formatter import FeishuMessageFormatter as fmt


class EmojiType:
    GET = "OnIt"
    TYPING = "Typing"
    THINKING = "THINKING"
    ONE_SEC = "OneSec"
    
    DONE = "DONE"
    CHECK_MARK = "CheckMark"
    LGTM = "LGTM"
    THUMBSUP = "THUMBSUP"
    OK = "OK"
    AWESOME = "AWESOME"
    
    CROSS_MARK = "CrossMark"
    ERROR = "ERROR"
    SOB = "SOB"
    WRONG = "WRONGED"
    
    SMART = "SMART"
    ROCKET = "Rocket"
    FIRE = "Fire"
    MUSCLE = "MUSCLE"
    YEAH = "YEAH"
    PARTY = "PARTY"
    
    SALUTE = "SALUTE"
    HIGHFIVE = "HIGHFIVE"
    WAVE = "WAVE"
    CLAP = "CLAP"
    
    FLASH = "StatusFlashOfInspiration"
    READING = "StatusReading"
    BUSY = "BusyStatus"


class EmojiReaction:
    @staticmethod
    def on_message_received() -> str:
        return EmojiType.GET
    
    @staticmethod
    def on_processing() -> str:
        return EmojiType.TYPING
    
    @staticmethod
    def on_thinking() -> str:
        return EmojiType.THINKING
    
    @staticmethod
    def on_success() -> str:
        return EmojiType.DONE
    
    @staticmethod
    def on_error() -> str:
        return EmojiType.SOB
    
    @staticmethod
    def on_coco_enter() -> str:
        return EmojiType.SMART
    
    @staticmethod
    def on_coco_exit() -> str:
        return EmojiType.WAVE
    
    @staticmethod
    def on_coco_response() -> str:
        return EmojiType.LGTM
    
    @staticmethod
    def on_multi_task_start() -> str:
        return EmojiType.ROCKET
    
    @staticmethod
    def on_multi_task_done() -> str:
        return EmojiType.PARTY
    
    @staticmethod
    def on_project_created() -> str:
        return EmojiType.FLASH
    
    @staticmethod
    def on_project_switched() -> str:
        return EmojiType.HIGHFIVE
    
    @staticmethod
    def on_dir_changed() -> str:
        return EmojiType.CHECK_MARK
    
    @staticmethod
    def on_shell_executed() -> str:
        return EmojiType.DONE
    
    @staticmethod
    def on_blocked() -> str:
        return EmojiType.CROSS_MARK

    @staticmethod
    def on_smart_mode() -> str:
        return EmojiType.OK

    @staticmethod
    def on_coco_mode() -> str:
        return EmojiType.GET


class FeishuWSClient:
    MESSAGE_EXPIRE_SECONDS = 30

    def __init__(self, message_callback: Callable[[str, str, str, Optional[str]], None]):
        self.settings = get_settings()
        self.message_callback = message_callback
        self._client: Optional[lark.ws.Client] = None
        self._api_client: Optional[lark.Client] = None
        self._coco_manager = CocoSessionManager()
        self._intent_recognizer = IntentRecognizer()
        self._processed_messages: OrderedDict[str, float] = OrderedDict()
        self._message_cache_ttl = 300
        self._message_cache_max_size = 1000
        self._executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ghost_worker")
        self._lock = threading.Lock()
        self._working_dirs: dict[str, str] = {}

        self._project_manager = ProjectManager()
        self._message_mapper = MessageProjectMapper()
        
        from ..mode import ModeManager
        self._mode_manager = ModeManager()
        
        self._streaming_manager: Optional[StreamingCardManager] = None
        self._enable_streaming = self.settings.streaming_enabled

    def _is_message_expired(self, create_time: int) -> bool:
        if not create_time:
            return False
        current_time = int(time.time() * 1000)
        message_age_ms = current_time - create_time
        return message_age_ms > self.MESSAGE_EXPIRE_SECONDS * 1000

    def _is_duplicate_message(self, message_id: str) -> bool:
        current_time = time.time()

        with self._lock:
            while self._processed_messages:
                oldest_id, timestamp = next(iter(self._processed_messages.items()))
                if current_time - timestamp > self._message_cache_ttl:
                    self._processed_messages.pop(oldest_id)
                else:
                    break

            if message_id in self._processed_messages:
                return True

            self._processed_messages[message_id] = current_time

            while len(self._processed_messages) > self._message_cache_max_size:
                self._processed_messages.popitem(last=False)

            return False

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

    def _resolve_project_from_message(self, message_id: str, chat_id: str, parent_id: Optional[str] = None) -> tuple[Optional[ProjectContext], bool]:
        auto_enter_coco = False
        
        if parent_id:
            project_id = self._message_mapper.get_project_id(parent_id)
            if project_id:
                project = self._project_manager.get_project(project_id)
                if project:
                    self._project_manager.set_active_project(chat_id, project_id)
                    print(f"📎 通过消息引用切换到项目: {project.project_name}")
                    
                    if project.coco_mode:
                        auto_enter_coco = True
                        print(f"🤖 自动进入编程模式 (回复编程消息)")
                    
                    return project, auto_enter_coco

        return self._project_manager.get_active_project(chat_id), False

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

            if message_type != "text":
                self._reply_message(message_id, "⚠️ 目前仅支持文本消息")
                return

            try:
                content_dict = json.loads(content_str)
                text = content_dict.get("text", "")
            except json.JSONDecodeError:
                text = content_str

            text = text.strip()
            if text.startswith("@"):
                parts = text.split(None, 1)
                if len(parts) > 1:
                    text = parts[1].strip()
                else:
                    text = ""

            if not text:
                self._show_help(message_id, chat_id)
                return

            project, auto_enter_coco = self._resolve_project_from_message(message_id, chat_id, parent_id or root_id)

            if auto_enter_coco:
                from ..mode import InteractionMode
                self._mode_manager.enter_coco_mode(chat_id, auto=True)
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                self._handle_coco_message(message_id, chat_id, text, project)
            else:
                self._process_with_intent(message_id, chat_id, text, project)

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
            f"• Coco 模式: {'🤖 开启' if project.coco_mode else '关闭'}"
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

        global_working_dir = self._get_working_dir(chat_id)
        content = (
            f"• 状态: {project.get_status_emoji()} {project.status.value}\n"
            f"• 📂 项目目录: `{project.root_path}`\n"
            f"• 📁 工作目录: `{global_working_dir}`\n"
            f"• 最后活跃: {CardBuilder._format_time_ago(project.last_active)}"
            f"{coco_info}"
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

        self._enter_coco_mode(message_id, chat_id)

    def _handle_card_exit_coco(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self._project_manager.get_project(project_id)
            if project and project.coco_mode:
                project.set_coco_mode(False)

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

        self._enter_coco_mode(message_id, chat_id)

    def _process_with_intent(self, message_id: str, chat_id: str, text: str, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode
        
        current_mode = self._mode_manager.get_mode(chat_id)
        is_in_coco = current_mode == InteractionMode.COCO
        
        if is_in_coco:
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._exit_current_mode(message_id, chat_id, project=project)
                return
            self._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_coco_message(message_id, chat_id, text, project)
            return

        self._add_reaction(message_id, EmojiReaction.on_smart_mode())
        self._add_reaction(message_id, EmojiReaction.on_processing())

        try:
            intent_result = self._intent_recognizer.recognize(text, is_in_coco)
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

        if project and session:
            project.update_coco_snapshot(
                query=session.session_id,
                query_count=session.message_count
            )
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
        exit_commands = {"/exit", "/quit", "/end_coco", "/exit_coco"}
        exit_keywords = {"退出模式", "退出编程模式", "退出编程", "结束编程"}
        
        if text_lower in exit_commands:
            return True
        
        return any(kw in text_lower for kw in exit_keywords)

    def _exit_current_mode(self, message_id: str, chat_id: str, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode
        
        current_mode = self._mode_manager.get_mode(chat_id)
        
        if current_mode == InteractionMode.COCO:
            self._exit_coco_mode(message_id, chat_id, project)
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
            header = f"🤖 Coco · {project.project_name}"
            footer = (
                f"📂 项目目录: {project.root_path}\n"
                f"📁 工作目录: {global_working_dir}"
            )
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, header, response, show_buttons=True, footer=footer
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

        print(f"🎬 开始流式输出: project={project_name}, path={project_path}")

        streaming_card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project_name,
            project_path=project_path,
            project_id=project_id,
            initial_content="🤔 正在思考...",
            is_coco_mode=True,
            reply_to_message_id=message_id,
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

        help_text = fmt.format_help(current_dir, is_coco_mode)

        project_help = (
            "\n\n📋 **项目管理命令**\n"
            "• `/projects` - 查看项目看板\n"
            "• `/new 名称 路径` - 创建新项目\n"
            "• `/switch 名称` - 切换项目\n"
            "• `/status` - 查看当前项目状态"
        )

        if project:
            self._reply_message(message_id, f"当前项目: **{project.project_name}**\n\n{help_text}{project_help}")
        else:
            self._reply_message(message_id, f"{help_text}{project_help}")

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
        """回复消息，并复制一份重新发送"""
        # 第一次回复（原回复）
        self._reply_message(message_id, content, msg_type)
        
        # 复制内容并重新发送
        import time
        time.sleep(1)  # 等待1秒避免消息发送过快
        
        # 解析内容，准备重新发送
        if isinstance(content, tuple) and len(content) == 2:
            # 如果是元组格式 (msg_type, content_str)
            content_to_resend = content[1]
        elif hasattr(content, '__contains__') and 'text' in str(content):
            # 如果是JSON字符串
            try:
                import json
                content_dict = json.loads(content) if isinstance(content, str) else content
                text_content = content_dict.get('text', '')
                content_to_resend = json.dumps({'text': f'【复制】{text_content}'})
            except:
                content_to_resend = str(content)
        else:
            # 其他情况直接转换为字符串
            content_to_resend = str(content)
        
        # 重新发送（作为新消息而不是回复）
        if chat_id:
            try:
                print(f"🔄 复制消息并重新发送: {content_to_resend[:50]}...")
                self.send_message(chat_id, content_to_resend, msg_type)
            except Exception as e:
                print(f"重新发送消息失败: {e}")
        else:
            print("⚠️ 无法重新发送消息：缺少 chat_id")

    def add_reaction(self, message_id: str, emoji_type: str):
        self._add_reaction(message_id, emoji_type)

    def _handle_reaction_created(self, data):
        pass

    def _handle_chat_entered(self, data):
        pass

    def _handle_message_read(self, data):
        pass

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

        print("🔌 正在建立飞书长连接...")
        print("📋 多项目管理已启用")
        self._client.start()
