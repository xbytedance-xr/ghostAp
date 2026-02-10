import json
import logging
import time
import os
import uuid
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse
from typing import Callable, Optional, Any
import threading
from ..config import get_settings
from ..session import CocoSessionManager, ClaudeSessionManager
from ..agent.intent_recognizer import IntentRecognizer, IntentType, IntentResult, TaskStep
from ..project import (
    ProjectManager,
    ProjectContext,
    ProjectStatus,
    MessageProjectMapper,
    MessageLinker,
    ProjectContextManager,
    ContextSourceMode,
    ContextEntryType,
)
from ..card import CardBuilder
from ..card.streaming import StreamingCardManager
from ..deep_engine import DeepEngine, DeepEngineManager, DeepEngineCallbacks, ProgressReporter
from ..deep_engine.models import DeepProject, DeepProjectStatus, DeepTask, ExecutionResult
from ..loop_engine import LoopEngineManager, LoopReporter
from ..tasking import TaskScheduler, TaskSpec, TaskPriority, SYSTEM_QUEUE_SUFFIX
from .message_formatter import FeishuMessageFormatter as fmt
from .emoji import EmojiType, EmojiReaction
from .message_cache import MessageCache
from .image_handler import FeishuImageHandler
from .handler_context import HandlerContext
from .handlers import (
    CocoModeHandler,
    ClaudeModeHandler,
    DeepHandler,
    LoopHandler,
    ProjectHandler,
    SystemHandler,
    DiagnosticsHandler,
)


logger = logging.getLogger(__name__)


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
        self._scheduler = TaskScheduler(
            max_concurrent=self.settings.task_scheduler_max_concurrent,
            per_key_concurrency=self.settings.task_scheduler_per_key_concurrency,
            system_concurrency=10,
            thread_name_prefix="ghost_worker",
        )
        self._working_dirs: dict[str, str] = {}
        self._working_dir_lock = threading.Lock()

        self._project_manager = ProjectManager()
        self._message_mapper = MessageProjectMapper()
        self._message_linker = MessageLinker()

        from ..mode import ModeManager
        self._mode_manager = ModeManager()

        self._streaming_manager: Optional[StreamingCardManager] = None
        self._image_handler: Optional[FeishuImageHandler] = None
        self._pending_image_keys: dict[str, list[str]] = {}
        self._pending_image_only: set[str] = set()  # message_ids that are image-only (no user text)
        self._pending_image_lock = threading.Lock()
        self._enable_streaming = self.settings.streaming_enabled

        self._deep_engine_manager = DeepEngineManager()
        self._progress_reporter = ProgressReporter()
        self._loop_engine_manager = LoopEngineManager()
        self._loop_reporter = LoopReporter()

        self._context_manager = ProjectContextManager()

        # ------------------------------------------------------------------
        # Handler infrastructure
        # ------------------------------------------------------------------
        self._handler_ctx = HandlerContext(
            settings=self.settings,
            api_client_factory=self._get_api_client,
            message_callback=self.message_callback,
            coco_manager=self._coco_manager,
            claude_manager=self._claude_manager,
            intent_recognizer=self._intent_recognizer,
            scheduler=self._scheduler,
            project_manager=self._project_manager,
            message_mapper=self._message_mapper,
            message_linker=self._message_linker,
            mode_manager=self._mode_manager,
            context_manager=self._context_manager,
            deep_engine_manager=self._deep_engine_manager,
            progress_reporter=self._progress_reporter,
            loop_engine_manager=self._loop_engine_manager,
            loop_reporter=self._loop_reporter,
            streaming_manager_factory=self._get_streaming_manager,
            image_handler_factory=self._get_image_handler,
            working_dirs=self._working_dirs,
            working_dir_lock=self._working_dir_lock,
            pending_image_keys=self._pending_image_keys,
            pending_image_lock=self._pending_image_lock,
            enable_streaming=self._enable_streaming,
        )

        # Instantiate handlers
        self._coco_handler = CocoModeHandler(self._handler_ctx)
        self._claude_handler = ClaudeModeHandler(self._handler_ctx)
        self._deep_handler = DeepHandler(self._handler_ctx)
        self._loop_handler = LoopHandler(self._handler_ctx)
        self._project_handler = ProjectHandler(self._handler_ctx)
        self._system_handler = SystemHandler(self._handler_ctx)
        self._diagnostics_handler = DiagnosticsHandler(self._handler_ctx)

        # Wire cross-references
        self._coco_handler._opposite_handler = self._claude_handler
        self._claude_handler._opposite_handler = self._coco_handler
        self._system_handler.coco_handler = self._coco_handler
        self._system_handler.claude_handler = self._claude_handler
        self._system_handler.project_handler = self._project_handler
        self._system_handler.deep_handler = self._deep_handler
        self._system_handler.loop_handler = self._loop_handler
        self._system_handler.diagnostics_handler = self._diagnostics_handler

    def close(self):
        """Best-effort cleanup for background resources."""
        try:
            self._message_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止message_cache清理线程失败: %s", e)

        try:
            self._deep_engine_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理deep_engine_manager失败: %s", e)

        try:
            self._loop_engine_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理loop_engine_manager失败: %s", e)

        try:
            self._scheduler.stop(wait=True, shutdown_executor=True)
        except Exception as e:
            logger.debug("停止scheduler失败: %s", e)

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

    # ==================================================================
    # Handler forwarding dispatch
    # ==================================================================
    # Maps ``client._xxx(...)`` calls to the corresponding handler
    # method.  This replaces 50+ one-liner stubs with a single
    # ``__getattr__`` lookup, keeping backward compatibility with tests
    # that mock ``client._enter_coco_mode`` etc.
    # ------------------------------------------------------------------

    # Dispatch table: _method_name -> (handler_attr, handler_method_name)
    _FORWARDING_MAP: dict[str, tuple[str, str]] = {
        # --- shared base handler helpers (delegate via _coco_handler) ---
        "_add_reaction":             ("_coco_handler", "add_reaction"),
        "_get_working_dir":          ("_coco_handler", "get_working_dir"),
        "_set_working_dir":          ("_coco_handler", "set_working_dir"),
        "_ensure_request_id":        ("_coco_handler", "ensure_request_id"),
        "_format_ref_note":          ("_coco_handler", "format_ref_note"),
        "_register_message_project": ("_coco_handler", "register_message_project"),
        "_reply_message":            ("_coco_handler", "reply_message"),
        "_reply_message_with_id":    ("_coco_handler", "reply_message_with_id"),
        "send_message":              ("_coco_handler", "send_message"),
        "_get_engine_name":          ("_coco_handler", "get_engine_name"),
        "_record_mode_transition":   ("_coco_handler", "record_mode_transition"),
        "_inject_bridge_context":    ("_coco_handler", "inject_bridge_context"),
        # --- Coco mode ---
        "_enter_coco_mode":          ("_coco_handler", "enter_mode"),
        "_exit_coco_mode":           ("_coco_handler", "exit_mode"),
        "_handle_coco_message":      ("_coco_handler", "handle_message"),
        "_handle_coco_response":     ("_coco_handler", "handle_response"),
        "_show_coco_info":           ("_coco_handler", "show_info"),
        "_handle_card_enter_coco":   ("_coco_handler", "handle_card_enter"),
        "_handle_card_exit_coco":    ("_coco_handler", "handle_card_exit"),
        "_handle_card_resume_coco":  ("_coco_handler", "handle_card_resume"),
        "_handle_card_new_coco":     ("_coco_handler", "handle_card_new"),
        # --- Claude mode ---
        "_enter_claude_mode":        ("_claude_handler", "enter_mode"),
        "_exit_claude_mode":         ("_claude_handler", "exit_mode"),
        "_handle_claude_message":    ("_claude_handler", "handle_message"),
        "_handle_claude_response":   ("_claude_handler", "handle_response"),
        "_show_claude_info":         ("_claude_handler", "show_info"),
        "_handle_card_enter_claude": ("_claude_handler", "handle_card_enter"),
        "_handle_card_exit_claude":  ("_claude_handler", "handle_card_exit"),
        "_handle_card_resume_claude":("_claude_handler", "handle_card_resume"),
        "_handle_card_new_claude":   ("_claude_handler", "handle_card_new"),
        # --- Deep Engine ---
        "_handle_deep_command":      ("_deep_handler", "handle_deep_command"),
        "_start_deep_engine":        ("_deep_handler", "start_deep_engine"),
        "_create_deep_callbacks":    ("_deep_handler", "_create_deep_callbacks"),
        "_show_deep_status":         ("_deep_handler", "show_deep_status"),
        "_show_deep_board":          ("_deep_handler", "show_deep_board"),
        "_pause_deep_engine":        ("_deep_handler", "pause_deep_engine"),
        "_resume_deep_engine":       ("_deep_handler", "resume_deep_engine"),
        "_stop_deep_engine":         ("_deep_handler", "stop_deep_engine"),
        "_stop_all_deep_engines":    ("_deep_handler", "stop_all_deep_engines"),
        "_update_deep_context":      ("_deep_handler", "update_deep_context"),
        # --- Loop Engine ---
        "_handle_loop_command":       ("_loop_handler", "handle_loop_command"),
        "_start_loop_engine":        ("_loop_handler", "start_loop_engine"),
        "_show_loop_status":         ("_loop_handler", "show_loop_status"),
        "_pause_loop_engine":        ("_loop_handler", "pause_loop_engine"),
        "_resume_loop_engine":       ("_loop_handler", "resume_loop_engine"),
        "_stop_loop_engine":         ("_loop_handler", "stop_loop_engine"),
        "_update_loop_guidance":     ("_loop_handler", "update_loop_guidance"),
        # --- Project ---
        "_create_project":           ("_project_handler", "create_project"),
        "_show_project_board":       ("_project_handler", "show_project_board"),
        "_show_current_project":     ("_project_handler", "show_current_project"),
        "_show_project_status":      ("_project_handler", "show_project_status"),
        "_preserve_project_context": ("_project_handler", "preserve_project_context"),
        "_restore_project_context":  ("_project_handler", "restore_project_context"),
        "_close_project":            ("_project_handler", "close_project"),
        # --- System ---
        "_show_help":                ("_system_handler", "show_help"),
        "_show_full_help":           ("_system_handler", "show_full_help"),
        "_exit_current_mode":        ("_system_handler", "exit_current_mode"),
        "_submit_shell_command":     ("_system_handler", "submit_shell_command"),
        "_change_directory":         ("_system_handler", "change_directory"),
        "_handle_intercepted_command": ("_system_handler", "handle_intercepted_command"),
        # --- Diagnostics ---
        "_show_task_board":          ("_diagnostics_handler", "show_task_board"),
        "_show_context_diff":        ("_diagnostics_handler", "show_context_diff"),
        "_build_context_diff_report":("_diagnostics_handler", "_build_context_diff_report"),
        "_submit_diff_report":       ("_diagnostics_handler", "_submit_diff_report"),
        "_show_message_trace":       ("_diagnostics_handler", "show_message_trace"),
    }

    def __getattr__(self, name: str):
        fwd = FeishuWSClient._FORWARDING_MAP.get(name)
        if fwd is not None:
            handler_attr, method_name = fwd
            handler = object.__getattribute__(self, handler_attr)
            return getattr(handler, method_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # Thin wrappers that cannot be expressed as simple delegation
    def reply(self, message_id: str, content, msg_type: str = "text", chat_id: Optional[str] = None):
        self._reply_message(message_id, content, msg_type)

    def add_reaction(self, message_id: str, emoji_type: str):
        self._add_reaction(message_id, emoji_type)

    def _switch_project(self, message_id: str, chat_id: str, name: str, auto_enter_coco: bool = True):
        self._project_handler.switch_project(
            message_id, chat_id, name, auto_enter_coco=auto_enter_coco,
            coco_handler=self._coco_handler, claude_handler=self._claude_handler,
        )

    @staticmethod
    def _is_exit_command(text: str) -> bool:
        return SystemHandler.is_exit_command(text)

    @staticmethod
    def _is_deep_command(text: str) -> bool:
        return SystemHandler.is_deep_command(text)

    @staticmethod
    def _is_loop_command(text: str) -> bool:
        return SystemHandler.is_loop_command(text)

    @staticmethod
    def _is_interceptable_command(text: str) -> bool:
        return SystemHandler.is_interceptable_command(text)

    @staticmethod
    def _mode_to_context_source(mode) -> ContextSourceMode:
        from ..mode import InteractionMode
        mapping = {
            InteractionMode.SMART: ContextSourceMode.SMART,
            InteractionMode.COCO: ContextSourceMode.COCO,
            InteractionMode.CLAUDE: ContextSourceMode.CLAUDE,
        }
        return mapping.get(mode, ContextSourceMode.SMART)

    # ==================================================================
    # Core routing — these remain in ws_client.py
    # ==================================================================

    def _resolve_project_from_message(self, message_id: str, chat_id: str, parent_id: Optional[str] = None) -> tuple[Optional[ProjectContext], Optional[str]]:
        auto_enter_mode = None

        if parent_id:
            project_id = self._message_mapper.get_project_id(parent_id)
            if project_id:
                project = self._project_manager.get_project(project_id)
                if project:
                    self._project_manager.set_active_project(chat_id, project_id)
                    logger.info("通过消息引用切换到项目: %s", project.project_name)

                    if project.claude_mode:
                        auto_enter_mode = "claude"
                        logger.info("自动进入 Claude 模式 (回复编程消息)")
                    elif project.coco_mode:
                        auto_enter_mode = "coco"
                        logger.info("自动进入编程模式 (回复编程消息)")

                    return project, auto_enter_mode

        return self._project_manager.get_active_project(chat_id), None

    def _handle_message(self, data: P2ImMessageReceiveV1):
        try:
            msg = data.event.message
            message_id = msg.message_id
            chat_id = msg.chat_id
        except Exception:
            message_id = None
            chat_id = "unknown"

        project_id = None
        try:
            parent_id = getattr(data.event.message, 'parent_id', None)
            root_id = getattr(data.event.message, 'root_id', None)
            for ref in (parent_id, root_id):
                if ref:
                    project_id = self._message_mapper.get_project_id(ref)
                    if project_id:
                        break
        except Exception:
            project_id = None

        if not project_id:
            try:
                active = self._project_manager.get_active_project(chat_id)
                project_id = active.project_id if active else None
            except Exception:
                project_id = None

        is_system = self._is_system_command_message(data)

        request_id = self._ensure_request_id(message_id, chat_id=chat_id, project_id=project_id)

        spec = TaskSpec(
            chat_id=chat_id,
            name="process_message",
            task_type="feishu_message",
            message_id=message_id,
            project_id=project_id,
            origin_message_id=message_id,
            request_id=request_id,
            priority=TaskPriority.HIGH if is_system else TaskPriority.NORMAL,
            is_system_command=is_system,
        )
        handle = self._scheduler.submit(spec, lambda ctx: self._process_message_async(data, task_ctx=ctx))
        try:
            if message_id:
                self._message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(message): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    def _is_system_command_message(self, data: P2ImMessageReceiveV1) -> bool:
        """Check if the message is a system command that should bypass project queue."""
        try:
            message = data.event.message
            content_str = message.content
            if not content_str:
                return False
            import json
            content = json.loads(content_str)
            text = content.get("text", "").strip()
            if text.startswith("@"):
                parts = text.split(None, 1)
                text = parts[1].strip() if len(parts) > 1 else ""
            return self._is_interceptable_command(text)
        except Exception:
            return False

    def _process_message_async(self, data: P2ImMessageReceiveV1, task_ctx=None):
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

            request_id = self._ensure_request_id(message_id, chat_id=chat_id)

            if create_time and self._is_message_expired(int(create_time)):
                logger.debug("跳过过期消息: %s (超过%d秒)", message_id, self.MESSAGE_EXPIRE_SECONDS)
                return

            if self._is_duplicate_message(message_id):
                logger.debug("跳过重复消息: %s", message_id)
                return

            supported_types = {"text", "image", "post"}
            if message_type not in supported_types:
                self._reply_message(message_id, "⚠️ 目前仅支持文本、图片和富文本消息", request_id=request_id)
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

            is_image_only = False  # 纯图片消息（无用户文字）

            if parse_result.image_keys:
                with self._pending_image_lock:
                    self._pending_image_keys[message_id] = parse_result.image_keys

                project, auto_enter_mode = self._resolve_project_from_message(
                    message_id, chat_id, parent_id or root_id
                )

                try:
                    if project:
                        self._message_linker.register_origin(message_id, request_id=request_id, chat_id=chat_id, project_id=project.project_id)
                except Exception as e:
                    logger.debug("register_origin失败(image_msg): message_id=%s, err=%s", message_id, e)

                if task_ctx and project:
                    try:
                        self._scheduler.update_project_id(task_ctx.run_id, project.project_id)
                    except Exception as e:
                        logger.debug("update_project_id失败(image_msg): run_id=%s, err=%s", task_ctx.run_id, e)
                save_dir = FeishuImageHandler.get_image_save_dir(
                    project.root_path if project else None,
                    self._get_working_dir(chat_id),
                )
                download_result = image_handler.download_images(
                    message_id, parse_result.image_keys, save_dir
                )
                if download_result.saved_paths:
                    is_image_only = not text
                    ref_text = FeishuImageHandler.build_image_reference_text(
                        download_result.saved_paths
                    )
                    if text:
                        text += ref_text
                    else:
                        text = "请查看并理解以下图片" + ref_text
                if download_result.failed_keys:
                    logger.warning("部分图片下载失败: %s", download_result.failed_keys)

                if is_image_only:
                    with self._pending_image_lock:
                        self._pending_image_only.add(message_id)
            else:
                project = None
                auto_enter_mode = None

            if not text:
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

            if task_ctx and project:
                try:
                    self._scheduler.update_project_id(task_ctx.run_id, project.project_id)
                except Exception as e:
                    logger.debug("update_project_id失败(text_msg): run_id=%s, err=%s", task_ctx.run_id, e)

            if auto_enter_mode == "claude":
                self._enter_claude_mode(message_id, chat_id, silent=True, project=project)
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                self._handle_claude_message(message_id, chat_id, text, project)
            elif auto_enter_mode == "coco":
                self._enter_coco_mode(message_id, chat_id, silent=True, project=project)
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                self._handle_coco_message(message_id, chat_id, text, project)
            else:
                self._process_with_intent(message_id, chat_id, text, project)

        except Exception as e:
            logger.error("处理消息异常: %s", e, exc_info=True)
        finally:
            with self._pending_image_lock:
                self._pending_image_keys.pop(message_id, None)
                self._pending_image_only.discard(message_id)

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
            logger.debug(
                "卡片回调收到: event_id=%s, event_type=%s, open_message_id=%s, open_chat_id=%s, "
                "action_tag=%s, action_name=%s, value_type=%s, value_preview=%s",
                header.event_id, header.event_type,
                context.open_message_id, context.open_chat_id,
                action.tag, action.name, type(action.value).__name__,
                value_preview,
            )
        except Exception as e:
            logger.warning("卡片回调基础信息解析失败: %s", e)
        try:
            open_message_id = data.event.context.open_message_id
            open_chat_id = data.event.context.open_chat_id
        except Exception:
            open_message_id = None
            open_chat_id = "unknown"

        project_id = None
        try:
            value_raw = data.event.action.value
            if isinstance(value_raw, dict):
                project_id = value_raw.get("project_id")
            elif isinstance(value_raw, str):
                try:
                    parsed = json.loads(value_raw)
                    if isinstance(parsed, dict):
                        project_id = parsed.get("project_id")
                except Exception:
                    project_id = None
        except Exception:
            project_id = None

        if not project_id:
            try:
                active = self._project_manager.get_active_project(open_chat_id)
                project_id = active.project_id if active else None
            except Exception:
                project_id = None

        origin_message_id = None
        try:
            origin_message_id = self._message_linker.resolve_origin(reply_message_id=open_message_id)
        except Exception:
            origin_message_id = None
        origin_message_id = origin_message_id or open_message_id
        request_id = self._ensure_request_id(origin_message_id, chat_id=open_chat_id, project_id=project_id)

        is_system = self._is_system_card_action(data)

        spec = TaskSpec(
            chat_id=open_chat_id,
            name="process_card_action",
            task_type="feishu_card_action",
            message_id=open_message_id,
            project_id=project_id,
            origin_message_id=origin_message_id,
            request_id=request_id,
            priority=TaskPriority.HIGH if is_system else TaskPriority.NORMAL,
            is_system_command=is_system,
        )
        handle = self._scheduler.submit(spec, lambda ctx: self._process_card_action_async(data, task_ctx=ctx))
        try:
            self._message_linker.link_task(origin_message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(card_action): origin=%s, run_id=%s, err=%s", origin_message_id, handle.run_id, e)
        return None

    def _is_system_card_action(self, data: P2CardActionTrigger) -> bool:
        """Check if the card action is a system action that should bypass project queue."""
        try:
            value_raw = data.event.action.value
            if isinstance(value_raw, dict):
                action_type = value_raw.get("action", "")
            elif isinstance(value_raw, str):
                import json
                try:
                    parsed = json.loads(value_raw)
                    action_type = parsed.get("action", "") if isinstance(parsed, dict) else ""
                except Exception:
                    action_type = ""
            else:
                action_type = ""
            system_actions = {
                "show_status", "switch_project", "show_board", "refresh_board",
                "show_detail", "new_project_prompt",
            }
            return action_type in system_actions
        except Exception:
            return False

    def _resolve_deep_target_project(self, chat_id: str, project_id: str, deep_project_id: str) -> Optional[ProjectContext]:
        """Resolve the project for a deep engine card action."""
        target = self._project_manager.get_project(project_id) if project_id else None
        if not target and deep_project_id:
            try:
                engine = self._deep_engine_manager.find_by_deep_project_id(chat_id, deep_project_id)
                if engine:
                    target = self._project_manager.find_project_by_path(engine.root_path)
            except Exception as e:
                logger.debug("resolve_deep_target_project失败: %s", e)
                target = None
        return target

    def _process_card_action_async(self, data: Any, task_ctx=None):
        try:
            start_time = time.perf_counter()
            action = data.event.action
            value_raw = action.value
            operator = data.event.operator
            open_message_id = data.event.context.open_message_id
            open_chat_id = data.event.context.open_chat_id
            logger.debug(
                "卡片回调上下文: operator_open_id=%s, operator_user_id=%s, value_raw_type=%s",
                getattr(operator, 'open_id', None),
                getattr(operator, 'user_id', None),
                type(value_raw).__name__,
            )

            if isinstance(value_raw, dict):
                value = value_raw
            elif isinstance(value_raw, str):
                try:
                    value = json.loads(value_raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("卡片 value 解析失败: value_raw=%s", value_raw[:500])
                    value = {"action": value_raw}
            else:
                value = {"action": str(value_raw)}

            action_type = value.get("action", "")
            project_id = value.get("project_id", "")

            if task_ctx and project_id:
                try:
                    self._scheduler.update_project_id(task_ctx.run_id, project_id)
                except Exception as e:
                    logger.debug("update_project_id失败(card_action): run_id=%s, err=%s", task_ctx.run_id, e)

            logger.info(
                "卡片按钮点击: action=%s, project_id=%s, value_keys=%s",
                action_type, project_id, list(value.keys()),
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
                    content = f"继续在 **{project.project_name}** 项目中开发\n\n📂 项目目录: `{project.root_path}`\n\n直接发送命令或消息即可"
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
                    self._submit_shell_command(open_message_id, open_chat_id, "ls -la", project.root_path, project)
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
            elif action_type in ("deep_pause", "deep_resume", "deep_stop"):
                target_project = self._resolve_deep_target_project(
                    open_chat_id, project_id, value.get("deep_project_id", "")
                )
                deep_actions = {
                    "deep_pause":  self._pause_deep_engine,
                    "deep_resume": self._resume_deep_engine,
                    "deep_stop":   self._stop_deep_engine,
                }
                deep_actions[action_type](open_message_id, open_chat_id, project=target_project)
            elif action_type == "new_project_prompt":
                self._reply_message(open_message_id, "📝 创建新项目\n\n请发送: `/new 项目名 路径`\n\n例如: `/new myApp ~/workspace/myApp`")
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            logger.debug("卡片回调处理耗时: %dms", elapsed_ms)

        except Exception as e:
            logger.error("处理卡片回调异常: %s", e, exc_info=True)

    def _process_with_intent(self, message_id: str, chat_id: str, text: str, project: Optional[ProjectContext] = None):
        from ..mode import InteractionMode

        current_mode = self._mode_manager.get_mode(chat_id)
        is_in_programming = current_mode in (InteractionMode.COCO, InteractionMode.CLAUDE)

        # Control-plane commands: handle consistently in all modes
        if self._is_deep_command(text):
            self._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_deep_command(message_id, chat_id, text, project)
            return

        if self._is_loop_command(text):
            self._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_loop_command(message_id, chat_id, text, project)
            return

        if self._is_interceptable_command(text):
            self._handle_intercepted_command(message_id, chat_id, text, project)
            return

        # Programming mode (Coco / Claude): exit or forward to active session
        if is_in_programming:
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._exit_current_mode(message_id, chat_id, project=project)
                return

            self._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            if current_mode == InteractionMode.COCO:
                self._handle_coco_message(message_id, chat_id, text, project)
            else:
                self._handle_claude_message(message_id, chat_id, text, project)
            return

        # SMART mode: image-only messages bypass intent recognition
        with self._pending_image_lock:
            is_image_only = message_id in self._pending_image_only
        if is_image_only:
            self._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_coco_message(message_id, chat_id, text, project)
            return

        # SMART mode: intent recognition
        self._add_reaction(message_id, EmojiReaction.on_smart_mode())
        self._add_reaction(message_id, EmojiReaction.on_processing())

        try:
            intent_result = self._intent_recognizer.recognize(text, current_mode.value)
        except Exception as e:
            logger.error("意图识别异常: %s", e)
            working_dir = self._get_working_dir(chat_id)
            self._submit_shell_command(message_id, chat_id, text, working_dir, project)
            return

        logger.info("意图识别: %s (置信度: %.2f, 任务数: %d)", intent_result.primary_intent.value, intent_result.confidence, len(intent_result.tasks))

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
            arg = (data.get("arg") or "").strip().lower()
            if arg in ("all", "-a", "--all"):
                self._show_deep_board(message_id, chat_id)
            else:
                self._show_deep_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_DEEP:
            arg = (data.get("arg") or "").strip().lower()
            if arg in ("all", "-a", "--all"):
                self._stop_all_deep_engines(message_id, chat_id)
            else:
                self._stop_deep_engine(message_id, chat_id, project)

        elif intent == IntentType.DEEP_UPDATE:
            update_message = data.get("message")
            if update_message:
                self._update_deep_context(message_id, chat_id, update_message, project)
            else:
                self._reply_message(message_id, "📝 请提供上下文信息\n\n用法: `/deep_update <上下文描述>`")

        elif intent == IntentType.ENTER_LOOP:
            requirement = data.get("requirement") or original_text
            self._start_loop_engine(message_id, chat_id, requirement, project)

        elif intent == IntentType.LOOP_STATUS:
            self._show_loop_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_LOOP:
            self._stop_loop_engine(message_id, chat_id, project)

        elif intent == IntentType.LOOP_PAUSE:
            self._pause_loop_engine(message_id, chat_id, project)

        elif intent == IntentType.LOOP_RESUME:
            self._resume_loop_engine(message_id, chat_id, project)

        elif intent == IntentType.LOOP_GUIDE:
            guide_message = data.get("message")
            if guide_message:
                self._update_loop_guidance(message_id, chat_id, guide_message, project)
            else:
                self._reply_message(message_id, "📝 请提供引导信息\n\n用法: `/loop_guide <引导描述>`")

        elif intent == IntentType.SHELL_COMMAND:
            working_dir = self._get_working_dir(chat_id)
            cmd = data.get("command") or original_text
            self._submit_shell_command(message_id, chat_id, cmd, working_dir, project)

            if project:
                project.add_conversation("user", cmd, message_id)
                self._context_manager.update_context(project.project_id, conversation={"role": "user", "content": cmd, "source_mode": "shell", "message_id": message_id})

        elif intent == IntentType.UNKNOWN:
            self._reply_message(message_id, fmt.format_unknown_intent())

    def _execute_task_step(self, message_id: str, chat_id: str, task: TaskStep, step_num: int, total_steps: int, project: Optional[ProjectContext] = None) -> bool:
        intent = task.intent
        data = task.data
        desc = task.description or self._get_task_description(task)

        logger.info("执行步骤 %d/%d: %s", step_num, total_steps, desc)

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
            logger.error("执行步骤 %d 异常: %s", step_num, e)
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
            return "执行命令"
        else:
            return "未知操作"

    # ==================================================================
    # Event stubs (no-op)
    # ==================================================================
    def _handle_reaction_created(self, data):
        pass

    def _handle_chat_entered(self, data):
        pass

    def _handle_message_read(self, data):
        pass

    # ==================================================================
    # WebSocket lifecycle
    # ==================================================================
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

        logger.info("正在建立飞书长连接...")
        logger.info("多项目管理已启用")
        self._client.start()
