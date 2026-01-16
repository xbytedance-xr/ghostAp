import json
import time
import os
from collections import OrderedDict
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from typing import Callable, Optional
import threading
from concurrent.futures import ThreadPoolExecutor
from ..config import get_settings
from ..coco.session import CocoSessionManager
from ..agent.intent_recognizer import IntentRecognizer, IntentType, IntentResult, TaskStep
from .message_formatter import FeishuMessageFormatter as fmt


class EmojiType:
    OK = "OK"
    THUMBSUP = "THUMBSUP"
    DONE = "DONE"
    TYPING = "Typing"
    SMART = "SMART"
    CROSS_MARK = "CrossMark"
    CHECK_MARK = "CheckMark"
    THINKING = "THINKING"
    MUSCLE = "MUSCLE"
    FIRE = "Fire"
    LGTM = "LGTM"
    ROCKET = "Rocket"


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

            if create_time and self._is_message_expired(int(create_time)):
                print(f"⏭️ 跳过过期消息: {message_id} (超过{self.MESSAGE_EXPIRE_SECONDS}秒)")
                return

            if self._is_duplicate_message(message_id):
                print(f"⏭️ 跳过重复消息: {message_id}")
                return

            self._add_reaction(message_id, EmojiType.OK)

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

            self._process_with_intent(message_id, chat_id, text)

        except Exception as e:
            print(f"处理消息异常: {e}")

    def _process_with_intent(self, message_id: str, chat_id: str, text: str):
        is_in_coco = self._coco_manager.is_in_coco_mode(chat_id)

        try:
            intent_result = self._intent_recognizer.recognize(text, is_in_coco)
        except Exception as e:
            print(f"意图识别异常: {e}")
            if is_in_coco:
                self._handle_coco_message(message_id, chat_id, text)
            else:
                working_dir = self._get_working_dir(chat_id)
                self.message_callback(message_id, chat_id, text, working_dir)
            return

        print(f"🧠 意图识别: {intent_result.primary_intent.value} (置信度: {intent_result.confidence:.2f}, 任务数: {len(intent_result.tasks)})")

        if intent_result.is_multi_task:
            self._execute_multi_tasks(message_id, chat_id, intent_result)
        else:
            self._execute_single_task(message_id, chat_id, intent_result.tasks[0] if intent_result.tasks else None, text)

    def _execute_multi_tasks(self, message_id: str, chat_id: str, intent_result: IntentResult):
        tasks = intent_result.tasks

        task_list = [{"description": task.description or self._get_task_description(task)} for task in tasks]
        plan_msg = fmt.format_multi_task_plan(task_list)
        self._reply_message(message_id, plan_msg)

        self._add_reaction(message_id, EmojiType.ROCKET)

        for i, task in enumerate(tasks, 1):
            success = self._execute_task_step(message_id, chat_id, task, step_num=i, total_steps=len(tasks))

            if task.intent == IntentType.ENTER_COCO:
                break

            if not success:
                self._reply_message(message_id, f"⚠️ 步骤 {i} 执行失败，后续步骤已取消")
                break

        self._add_reaction(message_id, EmojiType.DONE)

    def _execute_single_task(self, message_id: str, chat_id: str, task: Optional[TaskStep], original_text: str):
        if not task:
            self._reply_message(message_id, "🤔 无法理解你的意图")
            return

        intent = task.intent
        data = task.data

        if intent == IntentType.ENTER_COCO:
            self._enter_coco_mode(message_id, chat_id)

        elif intent == IntentType.EXIT_COCO:
            self._exit_coco_mode(message_id, chat_id)

        elif intent == IntentType.CHANGE_DIR:
            path = data.get("path", "")
            self._change_directory(message_id, chat_id, path)

        elif intent == IntentType.COCO_MESSAGE:
            if data.get("command") == "info":
                self._show_coco_info(message_id, chat_id)
            else:
                self._handle_coco_message(message_id, chat_id, original_text)

        elif intent == IntentType.SHELL_COMMAND:
            working_dir = self._get_working_dir(chat_id)
            cmd = data.get("command") or original_text
            self.message_callback(message_id, chat_id, cmd, working_dir)

        elif intent == IntentType.UNKNOWN:
            if self._coco_manager.is_in_coco_mode(chat_id):
                self._handle_coco_message(message_id, chat_id, original_text)
            else:
                self._reply_message(message_id, fmt.format_unknown_intent())

    def _execute_task_step(self, message_id: str, chat_id: str, task: TaskStep, step_num: int, total_steps: int) -> bool:
        intent = task.intent
        data = task.data
        desc = task.description or self._get_task_description(task)

        print(f"📌 执行步骤 {step_num}/{total_steps}: {desc}")

        try:
            if intent == IntentType.ENTER_COCO:
                self._enter_coco_mode(message_id, chat_id, silent=True)
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
        elif intent == IntentType.SHELL_COMMAND:
            return f"执行命令"
        else:
            return "未知操作"

    def _enter_coco_mode(self, message_id: str, chat_id: str, silent: bool = False):
        if self._coco_manager.is_in_coco_mode(chat_id):
            if not silent:
                info = self._coco_manager.get_session_info(chat_id)
                self._reply_message(
                    message_id,
                    fmt.format_warning(f"已经在 Coco 模式中\n\n{info}\n\n说「退出」或发送 /end_coco 退出")
                )
        else:
            self._add_reaction(message_id, EmojiType.SMART)
            self._coco_manager.start_session(chat_id)
            if not silent:
                self._reply_message(message_id, fmt.format_coco_enter())

    def _exit_coco_mode(self, message_id: str, chat_id: str):
        if self._coco_manager.end_session(chat_id):
            self._add_reaction(message_id, EmojiType.DONE)
            self._reply_message(message_id, fmt.format_coco_exit())
        else:
            self._reply_message(message_id, fmt.format_warning("当前不在 Coco 模式中"))

    def _show_coco_info(self, message_id: str, chat_id: str):
        info = self._coco_manager.get_session_info(chat_id)
        if info:
            self._reply_message(message_id, info)
        else:
            self._reply_message(message_id, fmt.format_warning("当前不在 Coco 模式中"))

    def _change_directory(self, message_id: str, chat_id: str, path: str):
        if not path:
            current_dir = self._get_working_dir(chat_id)
            self._add_reaction(message_id, EmojiType.CHECK_MARK)
            self._reply_message(message_id, fmt.format_current_dir(current_dir))
            return

        success, result = self._set_working_dir(chat_id, path)
        if success:
            self._add_reaction(message_id, EmojiType.CHECK_MARK)
            self._reply_message(message_id, fmt.format_dir_change(result, True))
        else:
            self._add_reaction(message_id, EmojiType.CROSS_MARK)
            self._reply_message(message_id, fmt.format_error(result))

    def _handle_coco_message(self, message_id: str, chat_id: str, text: str):
        session = self._coco_manager.get_session(chat_id)
        if not session:
            self._reply_message(message_id, fmt.format_warning("Coco 会话已过期，请说「帮我写代码」重新开始"))
            return

        self._add_reaction(message_id, EmojiType.TYPING)

        working_dir = self._get_working_dir(chat_id)
        response = session.send_prompt(text, cwd=working_dir)

        self._add_reaction(message_id, EmojiType.DONE)
        self._reply_message(message_id, fmt.format_coco_response(response))

    def _show_help(self, message_id: str, chat_id: str):
        is_coco_mode = self._coco_manager.is_in_coco_mode(chat_id)
        current_dir = self._get_working_dir(chat_id)
        self._reply_message(message_id, fmt.format_help(current_dir, is_coco_mode))

    def _reply_message(self, message_id: str, content, msg_type: str = "text"):
        try:
            client = self._get_api_client()

            if fmt.is_post_format(content):
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

    def reply(self, message_id: str, content, msg_type: str = "text"):
        self._reply_message(message_id, content, msg_type)

    def add_reaction(self, message_id: str, emoji_type: str):
        self._add_reaction(message_id, emoji_type)

    def _handle_reaction_created(self, data):
        pass

    def start(self):
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._handle_message) \
            .register_p2_im_message_reaction_created_v1(self._handle_reaction_created) \
            .build()

        self._client = lark.ws.Client(
            self.settings.app_id,
            self.settings.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG
        )

        print("🔌 正在建立飞书长连接...")
        self._client.start()
