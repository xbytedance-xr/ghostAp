"""Feishu WebSocket 客户端（核心路由枢纽）。

职责概览：
- 接收飞书 WS 事件（消息、卡片动作、反应等）并做基础校验/去重。
- 将用户消息路由到不同 handler（SMART/COCO/CLAUDE/SHELL/TTADK 以及 Deep/Loop/Spec 引擎）。
- 通过 `TaskScheduler` 提供：按项目串行、全局并发限制、系统命令快通道、背压与熔断。

关键设计点：
- `_FORWARDING_MAP` + `__getattr__`：把不同 mode 的实现解耦到 handlers 中，同时保持 ws_client 的调用面稳定。
- 兼容性：部分 lark-oapi 版本不包含完整的 callback model 类型；这里对仅用于类型标注的符号做了降级处理。
"""

import asyncio
from collections import deque
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Optional

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws import client as lark_ws_client_impl
from lark_oapi.ws.const import HEADER_TYPE
from lark_oapi.ws.enum import MessageType

# NOTE: lark-oapi 的 event callback models 在不同版本中并不完整。
# 本项目仅将 P2ImMessageReceiveV1 用于类型标注；运行时缺失不应导致 import 失败。
try:  # pragma: no cover
    from lark_oapi.event.callback.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1  # type: ignore
except Exception:  # pragma: no cover
    P2ImMessageReceiveV1 = Any  # type: ignore

from ..acp.manager import ACPSessionManager
from ..agent.intent_recognizer import IntentRecognizer, IntentResult, IntentType, TaskStep
from ..card import CardBuilder
from ..card.streaming import StreamingCardManager
from ..config import get_settings
from ..deep_engine import DeepEngineManager, ProgressReporter
from ..loop_engine import LoopEngineManager, LoopReporter
from ..project import (
    ContextSourceMode,
    MessageLinker,
    MessageProjectMapper,
    ProjectContext,
    ProjectContextManager,
    ProjectManager,
)
from ..spec_engine import SpecEngineManager, SpecReporter
from ..thread import ThreadContextManager, get_current_thread_id, get_thread_manager
from ..tasking import TaskEvent, TaskPriority, TaskScheduler, TaskSpec, TaskStatus
from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException
from ..utils.errors import log_exception
from ..utils.rate_limit import RateLimiter, RateLimitExceededException
from ..utils.trace import TraceContext, configure_logging_with_trace
from .action_dispatcher import ActionDispatcher
from .emoji import EmojiReaction
from .handler_context import HandlerContext
from .handlers import (
    ClaudeModeHandler,
    CocoModeHandler,
    AidenModeHandler,
    CodexModeHandler,
    GeminiModeHandler,
    DeepHandler,
    DiagnosticsHandler,
    LoopHandler,
    ProjectHandler,
    SpecHandler,
    SystemHandler,
    TTADKModeHandler,
)
from .image_handler import FeishuImageHandler
from .message_cache import MessageCache
from .message_formatter import FeishuMessageFormatter as fmt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingExit:
    chat_id: str
    project_id: Optional[str]
    message_id: str
    requested_at: float


def _frame_header_value(frame: Any, key: str) -> Optional[str]:
    for header in getattr(frame, "headers", []) or []:
        if getattr(header, "key", None) == key:
            return getattr(header, "value", None)
    return None


class _ObservedLarkWSClient(lark.ws.Client):
    """Wrap lark-oapi WS client to expose connection activity hooks.

    lark-oapi only reconnects after explicit read/write failures. If the socket
    becomes half-open, its recv loop can stay blocked forever and the service
    stops receiving new Feishu events without emitting any error. We observe
    connect/data/pong/disconnect to drive an external watchdog.
    """

    def __init__(self, *args, on_activity: Callable[[str], None], **kwargs):
        super().__init__(*args, **kwargs)
        self._on_activity = on_activity

    async def _connect(self) -> None:
        await super()._connect()
        self._on_activity("connected")

    async def _disconnect(self):
        try:
            return await super()._disconnect()
        finally:
            self._on_activity("disconnected")

    async def _handle_control_frame(self, frame):
        message_type = _frame_header_value(frame, HEADER_TYPE)
        if message_type == MessageType.PONG.value:
            self._on_activity("pong")
        elif message_type == MessageType.PING.value:
            self._on_activity("ping")
        else:
            self._on_activity("control")
        return await super()._handle_control_frame(frame)

    async def _handle_data_frame(self, frame):
        self._on_activity("data")
        return await super()._handle_data_frame(frame)


_READONLY_CARD_ACTIONS = {
    "deep_expand", "deep_collapse", "deep_mode_full", "deep_mode_compact", "deep_expand_ac", "deep_collapse_ac",
    "loop_expand", "loop_collapse", "loop_mode_full", "loop_mode_compact", "loop_expand_ac", "loop_collapse_ac",
    "spec_expand", "spec_collapse", "spec_mode_full", "spec_mode_compact", "spec_expand_ac", "spec_collapse_ac",
}


class FeishuWSClient:
    """Feishu WS Client 的服务端运行态。

    该类面向"长连接服务"场景：
    - 内部会初始化 scheduler / handler / cache，并在 `start()` 后进入事件循环。
    - `close()` 提供 best-effort 资源回收（线程/缓存/调度器等）。
    """

    MESSAGE_EXPIRE_SECONDS = 30

    def __init__(self, message_callback: Callable[[str, str, str, Optional[str]], None]):
        self.settings = get_settings()
        self.message_callback = message_callback
        self._client: Optional[lark.ws.Client] = None
        self._api_client: Optional[lark.Client] = None
        self._coco_manager = ACPSessionManager("coco", session_timeout=self.settings.coco_session_timeout, keepalive_interval=self.settings.acp_keepalive_interval, idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s)
        self._claude_manager = ACPSessionManager("claude", session_timeout=self.settings.claude_session_timeout, keepalive_interval=self.settings.acp_keepalive_interval, idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s)
        self._aiden_manager = ACPSessionManager("aiden", session_timeout=self.settings.coco_session_timeout, keepalive_interval=self.settings.acp_keepalive_interval, idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s)
        self._codex_manager = ACPSessionManager("codex", session_timeout=self.settings.coco_session_timeout, keepalive_interval=self.settings.acp_keepalive_interval, idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s)
        self._gemini_manager = ACPSessionManager("gemini", session_timeout=self.settings.coco_session_timeout, keepalive_interval=self.settings.acp_keepalive_interval, idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s)
        self._ttadk_manager = ACPSessionManager("ttadk", session_timeout=self.settings.coco_session_timeout, keepalive_interval=self.settings.acp_keepalive_interval, idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s)
        self._intent_recognizer = IntentRecognizer()
        self._message_cache = MessageCache(ttl=300, max_size=1000, cleanup_interval=60)
        self._card_event_cache = MessageCache(ttl=300, max_size=1000, cleanup_interval=60)
        # Card action dedupe (user rapid clicks): short TTL, per-action key.
        self._card_action_dedup_cache = MessageCache(ttl=1, max_size=5000, cleanup_interval=30)
        self._scheduler = TaskScheduler(
            max_concurrent=self.settings.task_scheduler_max_concurrent,
            per_key_concurrency=self.settings.task_scheduler_per_key_concurrency,
            system_concurrency=10,
            thread_name_prefix="ghost_worker",
        )
        # Spec Engine limits: e.g. 50 calls per second, max 100 capacity
        self._scheduler.register_policy(
            "spec_command",
            rate_limiter=RateLimiter(capacity=100, fill_rate=50.0),
            circuit_breaker=CircuitBreaker(failure_threshold=10, recovery_timeout=5.0),
        )
        self._working_dirs: dict[str, str] = {}
        self._working_dir_lock = threading.Lock()

        # ------------------------------------------------------------------
        # Control-plane bookkeeping (deferred /exit)
        # ------------------------------------------------------------------
        self._pending_exit_lock = threading.Lock()
        self._pending_exits: dict[str, _PendingExit] = {}  # key -> pending exit
        self._control_plane_event_q: Deque[str] = deque()
        self._control_plane_event_lock = threading.Lock()
        self._control_plane_wakeup = threading.Event()
        self._control_plane_stop = threading.Event()
        # IMPORTANT: scheduler listeners are invoked under scheduler locks;
        # the listener MUST be non-blocking and must not call scheduler APIs.
        self._scheduler.add_listener(self._on_scheduler_event)
        self._control_plane_thread = threading.Thread(
            target=self._control_plane_loop,
            name="control_plane",
            daemon=True,
        )
        self._control_plane_thread.start()

        # System command gate: while /help or /exit is being handled, card actions are blocked.
        self._system_cmd_gate_lock = threading.Lock()
        self._system_cmd_inflight_by_chat: dict[str, int] = {}

        self._project_manager = ProjectManager()
        self._message_mapper = MessageProjectMapper()
        self._message_linker = MessageLinker()

        from ..mode import ModeManager

        self._mode_manager = ModeManager()
        self._thread_manager = get_thread_manager()
        self._thread_manager._on_evict = self._on_thread_evicted

        self._streaming_manager: Optional[StreamingCardManager] = None
        self._image_handler: Optional[FeishuImageHandler] = None
        self._pending_image_keys: dict[str, list[str]] = {}
        self._pending_image_only: set[str] = set()  # message_ids that are image-only (no user text)
        self._pending_image_lock = threading.Lock()
        self._enable_streaming = self.settings.streaming_enabled

        self._ws_health_lock = threading.Lock()
        self._ws_last_connect_at = 0.0
        self._ws_last_frame_at = 0.0
        self._ws_last_pong_at = 0.0
        self._ws_reconnect_requested_at = 0.0
        self._ws_watchdog_stop = threading.Event()
        self._ws_watchdog_thread: Optional[threading.Thread] = None

        self._deep_engine_manager = DeepEngineManager()
        self._progress_reporter = ProgressReporter()
        self._loop_engine_manager = LoopEngineManager()
        self._loop_reporter = LoopReporter()
        self._spec_engine_manager = SpecEngineManager()
        self._spec_reporter = SpecReporter()

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
            aiden_manager=self._aiden_manager,
            codex_manager=self._codex_manager,
            gemini_manager=self._gemini_manager,
            ttadk_manager=self._ttadk_manager,
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
            spec_engine_manager=self._spec_engine_manager,
            spec_reporter=self._spec_reporter,
            thread_manager=self._thread_manager,
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
        self._aiden_handler = AidenModeHandler(self._handler_ctx)
        self._codex_handler = CodexModeHandler(self._handler_ctx)
        self._gemini_handler = GeminiModeHandler(self._handler_ctx)
        self._ttadk_handler = TTADKModeHandler(self._handler_ctx)
        self._deep_handler = DeepHandler(self._handler_ctx)
        self._loop_handler = LoopHandler(self._handler_ctx)
        self._spec_handler = SpecHandler(self._handler_ctx)
        self._project_handler = ProjectHandler(self._handler_ctx)
        self._system_handler = SystemHandler(self._handler_ctx)
        self._diagnostics_handler = DiagnosticsHandler(self._handler_ctx)

        # ------------------------------------------------------------------
        # Wire cross-references (automated via handler registry)
        # ------------------------------------------------------------------
        # All programming mode handlers that need peer references
        self._all_mode_handlers: list[ProgrammingModeHandler] = [
            self._coco_handler,
            self._claude_handler,
            self._aiden_handler,
            self._codex_handler,
            self._gemini_handler,
            self._ttadk_handler,
        ]

        # Auto-inject peer references for all mode handlers
        for h in self._all_mode_handlers:
            h._opposite_handler = self._coco_handler  # backward compat
            for peer in self._all_mode_handlers:
                peer_name = f"_{peer.__class__.__name__.replace('ModeHandler', '').lower()}_handler"
                setattr(h, peer_name, peer)

        # System handler needs references to all handlers
        self._system_handler.coco_handler = self._coco_handler
        self._system_handler.claude_handler = self._claude_handler
        self._system_handler.aiden_handler = self._aiden_handler
        self._system_handler.codex_handler = self._codex_handler
        self._system_handler.gemini_handler = self._gemini_handler
        self._system_handler.ttadk_handler = self._ttadk_handler
        self._system_handler.project_handler = self._project_handler
        self._system_handler.deep_handler = self._deep_handler
        self._system_handler.loop_handler = self._loop_handler
        self._system_handler.spec_handler = self._spec_handler
        self._system_handler.diagnostics_handler = self._diagnostics_handler

        # Bind forwarding methods directly on instance (replaces __getattr__ dispatch)
        for attr_name, (handler_attr, method_name) in self._FORWARDING_MAP.items():
            handler = getattr(self, handler_attr)
            setattr(self, attr_name, getattr(handler, method_name))

        # --- Action Dispatcher ---
        self._action_dispatcher = ActionDispatcher()
        self._init_action_registry()

        # Configure trace logging
        configure_logging_with_trace()

    def _register_action(self, handler: Callable, exact: Optional[str] = None, prefix: Optional[str] = None):
        """Register a card action handler."""
        self._action_dispatcher.register(handler, exact, prefix)

    def _init_action_registry(self):
        """Initialize all card action handlers."""
        self._register_programming_mode_actions()

        # Project
        self._register_action(
            lambda mid, cid, pid, val: self._show_project_status(
                mid, cid, self._project_manager.get_project(pid) if pid else None
            ),
            exact="show_status",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._show_project_board(mid, cid, origin_message_id=mid), exact="switch_project"
        )
        self._register_action(
            lambda mid, cid, pid, val: self._show_project_board(mid, cid, origin_message_id=mid), exact="show_board"
        )
        self._register_action(
            lambda mid, cid, pid, val: self._show_project_board(mid, cid, origin_message_id=mid), exact="refresh_board"
        )
        self._register_action(
            lambda mid, cid, pid, val: self._show_project_board(
                mid, cid, origin_message_id=mid, page=val.get("page", 1)
            ),
            exact="switch_board_page",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._show_project_status(
                mid, cid, self._project_manager.get_project(pid) if pid else None, origin_message_id=mid
            ),
            exact="show_detail",
        )

        def _handle_switch_to(mid, cid, pid, val):
            if pid:
                project = self._project_manager.get_project(pid)
                if project:
                    self._switch_project(mid, cid, project.project_name)

        self._register_action(_handle_switch_to, exact="switch_to")

        def _handle_continue_dev(mid, cid, pid, val):
            project = self._project_manager.get_project(pid) if pid else None
            if project:
                self._project_manager.set_active_project(cid, pid)
                content = f"继续在 **{project.project_name}** 项目中开发\n\n📂 项目目录: `{project.root_path}`\n\n直接发送命令或消息即可"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, "继续开发", content, show_buttons=True
                )
                response_id = self._reply_message_with_id(mid, card_content, msg_type)
                if response_id:
                    self._register_message_project(response_id, project)

        self._register_action(_handle_continue_dev, exact="continue_dev")

        def _handle_list_files(mid, cid, pid, val):
            project = self._project_manager.get_project(pid) if pid else None
            if project:
                self._project_manager.set_active_project(cid, pid)
                self._submit_shell_command(mid, cid, "ls -la", project.root_path, project)

        self._register_action(_handle_list_files, exact="list_files")

        self._register_action(
            lambda mid, cid, pid, val: self._reply_message(
                mid, "📝 创建新项目\n\n请发送: `/new 项目名 路径`\n\n例如: `/new myApp ~/workspace/myApp`"
            ),
            exact="new_project_prompt",
        )

        self._register_action(
            lambda mid, cid, pid, val: self._handle_select_ttadk_tool(
                mid, cid, val.get("_option") or val.get("tool_name", ""), pid
            ),
            exact="select_ttadk_tool",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._handle_toggle_ttadk_yolo(
                mid,
                cid,
                bool(val.get("enabled")),
                val.get("view", "tool_select"),
                val.get("tool_name", ""),
                pid,
            ),
            exact="toggle_ttadk_yolo",
        )

    def _register_programming_mode_actions(self):
        """Register enter/exit/resume/new actions for all programming modes."""
        mode_names = ("coco", "claude", "aiden", "codex", "gemini", "ttadk")
        for mode in mode_names:
            enter = getattr(self, f"_handle_card_enter_{mode}")
            exit_ = getattr(self, f"_handle_card_exit_{mode}")
            resume = getattr(self, f"_handle_card_resume_{mode}")
            new = getattr(self, f"_handle_card_new_{mode}")

            self._register_action(enter, exact=f"enter_{mode}")
            self._register_action(exit_, exact=f"exit_{mode}")
            self._register_action(
                lambda mid, cid, pid, val, _resume=resume: _resume(mid, cid, pid, val.get("session_id", "")),
                exact=f"resume_{mode}",
            )
            self._register_action(new, exact=f"new_{mode}")
        self._register_action(
            lambda mid, cid, pid, val: self._handle_select_ttadk_model(
                mid,
                cid,
                val.get("tool_name", ""),
                val.get("_option") or val.get("model_name", ""),
                self._project_manager.get_project(pid) if pid else None,
            ),
            exact="select_ttadk_model",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._handle_refresh_ttadk_models(mid, cid, val.get("tool_name", ""), pid),
            exact="refresh_ttadk_models",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._handle_ttadk_command(
                mid, cid, self._project_manager.get_project(pid) if pid else None, True
            ),
            exact="show_ttadk_menu",
        )

        # ACP
        self._register_action(
            lambda mid, cid, pid, val: self._handle_acp_command(
                mid, cid, self._project_manager.get_project(pid) if pid else None
            ),
            exact="show_acp_menu",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._handle_select_acp_tool(mid, cid, val.get("tool_name", ""), pid),
            exact="select_acp_tool",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._handle_select_acp_model(
                mid,
                cid,
                val.get("tool_name", ""),
                val.get("model_name", ""),
                self._project_manager.get_project(pid) if pid else None,
            ),
            exact="select_acp_model",
        )
        self._register_action(
            lambda mid, cid, pid, val: self._handle_refresh_acp_models(mid, cid, val.get("tool_name", ""), pid),
            exact="refresh_acp_models",
        )

        # System
        self._register_action(
            lambda mid, cid, pid, val: self._show_full_help(
                mid, cid, self._project_manager.get_project(pid) if pid else None
            ),
            exact="show_help_menu",
        )
        self._register_action(lambda mid, cid, pid, val: self._handle_deep_prompt(mid, cid), exact="enter_deep_prompt")
        self._register_action(
            lambda mid, cid, pid, val: self._handle_help_category(
                mid,
                cid,
                val.get("category", "main"),
                self._project_manager.get_project(pid) if pid else None,
                origin_message_id=mid,
            ),
            exact="help_category",
        )

        # Streaming
        def _handle_load_more(mid, cid, pid, val):
            msg_id = (val.get("message_id") or "").strip() or mid
            manager = self._get_streaming_manager()
            manager.increase_pagination(msg_id)

        self._register_action(_handle_load_more, exact="load_more")

        def _handle_load_prev(mid, cid, pid, val):
            msg_id = (val.get("message_id") or "").strip() or mid
            manager = self._get_streaming_manager()
            manager.decrease_pagination(msg_id)

        self._register_action(_handle_load_prev, exact="load_prev")

        # Deep Engine
        self._register_action(
            lambda mid, cid, pid, val: self._show_deep_status(
                mid, cid, self._project_manager.get_project(pid) if pid else None, origin_message_id=mid
            ),
            exact="show_deep_status",
        )
        self._register_action(
            lambda mid, cid, pid, val, type=None: self._deep_handler.handle_card_action(mid, cid, type, val),
            prefix="deep_",
        )

        # Loop Engine
        self._register_action(
            lambda mid, cid, pid, val, type=None: self._loop_handler.handle_card_action(mid, cid, type, val),
            prefix="loop_",
        )

        # Spec Engine
        self._register_action(
            lambda mid, cid, pid, val, type=None: self._spec_handler.handle_card_action(mid, cid, type, val),
            prefix="spec_",
        )

    def _record_ws_activity(self, kind: str) -> None:
        now = time.time()
        with self._ws_health_lock:
            if kind == "connected":
                self._ws_last_connect_at = now
                self._ws_last_frame_at = now
                self._ws_last_pong_at = now
                self._ws_reconnect_requested_at = 0.0
                return
            if kind in {"pong", "ping", "control", "data"}:
                self._ws_last_frame_at = now
                if kind == "pong":
                    self._ws_last_pong_at = now
                return
            if kind == "disconnected" and self._ws_reconnect_requested_at <= 0.0:
                self._ws_reconnect_requested_at = now
                logger.warning("WS断连，已触发重连请求: ts=%.3f", now)
                logger.warning("[METRIC] ws_disconnect")

    def _get_ws_watchdog_interval(self) -> float:
        value = getattr(self.settings, "feishu_ws_watchdog_interval", 15.0)
        try:
            return max(1.0, float(value))
        except Exception:
            return 15.0

    def _get_ws_stale_timeout(self) -> float:
        configured = getattr(self.settings, "feishu_ws_stale_timeout", 300.0)
        try:
            configured_timeout = max(60.0, float(configured))
        except Exception:
            configured_timeout = 300.0

        ping_interval = 120.0
        client = self._client
        if client is not None:
            try:
                ping_interval = max(1.0, float(getattr(client, "_ping_interval", 120.0) or 120.0))
            except Exception:
                ping_interval = 120.0

        grace = getattr(self.settings, "feishu_ws_stale_grace_seconds", 30.0)
        try:
            grace_seconds = max(5.0, float(grace))
        except Exception:
            grace_seconds = 30.0

        return max(configured_timeout, ping_interval * 2 + grace_seconds)

    def _trigger_ws_disconnect(self, *, reason: str) -> bool:
        client = self._client
        if client is None or getattr(client, "_conn", None) is None:
            return False

        try:
            fut = asyncio.run_coroutine_threadsafe(client._disconnect(), lark_ws_client_impl.loop)
            fut.result(timeout=5)
            logger.warning("飞书长连接 watchdog 已触发重连: %s", reason)
            return True
        except Exception as e:
            logger.warning("飞书长连接 watchdog 触发重连失败: reason=%s err=%s", reason, e)
            return False

    def _check_ws_health_once(self, now: Optional[float] = None) -> bool:
        client = self._client
        if client is None or getattr(client, "_conn", None) is None:
            return False

        current_time = now if now is not None else time.time()
        stale_timeout = self._get_ws_stale_timeout()

        with self._ws_health_lock:
            last_seen = max(self._ws_last_pong_at, self._ws_last_frame_at, self._ws_last_connect_at)
            if last_seen <= 0.0:
                return False
            idle_for = current_time - last_seen
            if idle_for <= stale_timeout:
                return False

            requested_at = self._ws_reconnect_requested_at
            if requested_at > 0.0 and (current_time - requested_at) < 30.0:
                return False

            self._ws_reconnect_requested_at = current_time

        return self._trigger_ws_disconnect(reason=f"idle_for={idle_for:.1f}s > timeout={stale_timeout:.1f}s")

    def _ws_watchdog_loop(self) -> None:
        interval = self._get_ws_watchdog_interval()
        while not self._ws_watchdog_stop.wait(interval):
            try:
                self._check_ws_health_once()
            except Exception as e:
                logger.debug("飞书长连接 watchdog 检查失败: %s", e)

    def _start_ws_watchdog(self) -> None:
        if self._ws_watchdog_thread and self._ws_watchdog_thread.is_alive():
            return
        self._ws_watchdog_stop.clear()
        self._ws_watchdog_thread = threading.Thread(
            target=self._ws_watchdog_loop,
            name="feishu_ws_watchdog",
            daemon=True,
        )
        self._ws_watchdog_thread.start()

    def _stop_ws_watchdog(self) -> None:
        self._ws_watchdog_stop.set()
        if self._ws_watchdog_thread and self._ws_watchdog_thread.is_alive():
            self._ws_watchdog_thread.join(timeout=2)
        self._ws_watchdog_thread = None

    # ==================================================================
    # Control plane: deferred /exit (never block scheduler listener)
    # ==================================================================

    @staticmethod
    def _pending_exit_key(chat_id: str, project_id: Optional[str]) -> str:
        return f"{chat_id}:{project_id or 'DEFAULT'}"

    def _on_scheduler_event(self, ev: TaskEvent) -> None:
        """TaskScheduler listener (MUST be non-blocking).

        NOTE: scheduler invokes listeners under its internal locks.
        This callback must not call back into scheduler APIs.
        """

        try:
            # 1) System command gate state
            if ev.task_type in {"system_help", "system_exit"}:
                with self._system_cmd_gate_lock:
                    cur = int(self._system_cmd_inflight_by_chat.get(ev.chat_id, 0) or 0)
                    if ev.status == TaskStatus.RUNNING:
                        self._system_cmd_inflight_by_chat[ev.chat_id] = cur + 1
                    elif ev.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
                        nxt = max(0, cur - 1)
                        if nxt <= 0:
                            self._system_cmd_inflight_by_chat.pop(ev.chat_id, None)
                        else:
                            self._system_cmd_inflight_by_chat[ev.chat_id] = nxt

            # 2) Deferred exit processing wakeup
            if ev.status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
                return
            key = self._pending_exit_key(ev.chat_id, ev.project_id)
            with self._control_plane_event_lock:
                self._control_plane_event_q.append(key)
            self._control_plane_wakeup.set()
        except Exception:
            # best-effort only
            return

    def _control_plane_loop(self) -> None:
        """后台线程：处理 deferred /exit 的收敛与触发。"""

        while not self._control_plane_stop.is_set():
            self._control_plane_wakeup.wait(timeout=0.2)
            if self._control_plane_stop.is_set():
                return

            keys: set[str] = set()
            with self._control_plane_event_lock:
                while self._control_plane_event_q:
                    keys.add(self._control_plane_event_q.popleft())
                self._control_plane_wakeup.clear()

            for key in keys:
                try:
                    self._maybe_finalize_deferred_exit(key)
                except Exception:
                    continue

    def _maybe_finalize_deferred_exit(self, key: str) -> None:
        """若对应 chat/project 下已无运行中的非系统任务，则触发 deferred /exit。"""

        with self._pending_exit_lock:
            pending = self._pending_exits.get(key)
        if not pending:
            return

        # Check if any non-system task is still running under the same scope.
        project_id = pending.project_id
        tasks = self._scheduler.list_tasks(chat_id=pending.chat_id, project_id=project_id, include_done=False, limit=200)
        has_running_non_system = any(
            (st.status == TaskStatus.RUNNING) and (not bool(getattr(st.spec, "is_system_command", False)))
            for st in tasks
        )
        if has_running_non_system:
            return

        with self._pending_exit_lock:
            pending = self._pending_exits.pop(key, None)
        if not pending:
            return

        def _do_exit(_ctx):
            proj = None
            try:
                if pending.project_id:
                    proj = self._project_manager.get_project(pending.project_id)
                if proj is None:
                    proj = self._project_manager.get_active_project(pending.chat_id)
            except Exception:
                proj = None
            self._exit_current_mode(pending.message_id, pending.chat_id, project=proj)
            return True

        spec = TaskSpec(
            chat_id=pending.chat_id,
            name="deferred_exit",
            task_type="system_exit",
            message_id=pending.message_id,
            project_id=pending.project_id,
            origin_message_id=pending.message_id,
            priority=TaskPriority.HIGH,
            is_system_command=True,
        )
        self._scheduler.submit(spec, _do_exit)

    def _request_deferred_exit(
        self,
        *,
        message_id: str,
        chat_id: str,
        project_id: Optional[str],
    ) -> None:
        key = self._pending_exit_key(chat_id, project_id)
        with self._pending_exit_lock:
            self._pending_exits[key] = _PendingExit(
                chat_id=chat_id,
                project_id=project_id,
                message_id=message_id,
                requested_at=time.time(),
            )

    def _should_defer_exit(self, *, chat_id: str, project_id: Optional[str]) -> bool:
        tasks = self._scheduler.list_tasks(chat_id=chat_id, project_id=project_id, include_done=False, limit=200)
        return any(
            (st.status == TaskStatus.RUNNING) and (not bool(getattr(st.spec, "is_system_command", False)))
            for st in tasks
        )

    def close(self):
        """Best-effort cleanup for background resources."""

        self._stop_ws_watchdog()

        try:
            self._control_plane_stop.set()
            self._control_plane_wakeup.set()
            if self._control_plane_thread and self._control_plane_thread.is_alive():
                self._control_plane_thread.join(timeout=2)
        except Exception:
            pass

        def _wait_engines_stopped(engines: list[Any], timeout_s: float = 5.0, interval_s: float = 0.05) -> None:
            deadline = time.time() + max(0.1, timeout_s)
            while time.time() < deadline:
                any_running = False
                for e in engines:
                    try:
                        if e and getattr(e, "is_running", False):
                            any_running = True
                            break
                    except Exception:
                        continue
                if not any_running:
                    return
                time.sleep(interval_s)

        # 1) Stop long-running engines first (they may hold ACP subprocesses)
        deep_engines: list[Any] = []
        loop_engines: list[Any] = []
        spec_engines: list[Any] = []

        try:
            deep_engines = list(self._deep_engine_manager.list_engines())
            for engine in deep_engines:
                try:
                    if engine and getattr(engine, "is_running", False):
                        engine.stop()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            loop_engines = list(self._loop_engine_manager.list_engines())
            for engine in loop_engines:
                try:
                    if engine and getattr(engine, "is_running", False):
                        engine.stop()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            spec_engines = list(self._spec_engine_manager.list_engines())
            for engine in spec_engines:
                try:
                    if engine and getattr(engine, "is_running", False):
                        engine.stop()
                except Exception:
                    pass
        except Exception:
            pass

        # Give running engines a short grace period to exit run loops before hard cleanup.
        _wait_engines_stopped(deep_engines)
        _wait_engines_stopped(loop_engines)
        _wait_engines_stopped(spec_engines)

        try:
            self._message_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止message_cache清理线程失败: %s", e)

        try:
            self._card_event_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止card_event_cache清理线程失败: %s", e)

        try:
            self._card_action_dedup_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止card_action_dedup_cache清理线程失败: %s", e)

        # 2) Close per-chat programming sessions (kills ACP agent subprocesses)
        try:
            self._coco_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理coco_session_manager失败: %s", e)

        try:
            self._claude_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理claude_session_manager失败: %s", e)

        try:
            self._ttadk_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理ttadk_session_manager失败: %s", e)

        try:
            self._aiden_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理aiden_session_manager失败: %s", e)

        try:
            self._codex_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理codex_session_manager失败: %s", e)

        try:
            self._gemini_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理gemini_session_manager失败: %s", e)

        try:
            self._deep_engine_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理deep_engine_manager失败: %s", e)

        try:
            self._loop_engine_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理loop_engine_manager失败: %s", e)

        try:
            self._spec_engine_manager.cleanup_all()
        except Exception as e:
            logger.debug("清理spec_engine_manager失败: %s", e)

        try:
            self._thread_manager.close()
        except Exception as e:
            logger.debug("清理thread_manager失败: %s", e)

        try:
            self._scheduler.stop(wait=True, shutdown_executor=True)
        except Exception as e:
            logger.debug("停止scheduler失败: %s", e)

    def _on_thread_evicted(self, ctx) -> None:
        for mgr in (
            self._coco_manager,
            self._claude_manager,
            self._aiden_manager,
            self._codex_manager,
            self._gemini_manager,
            self._ttadk_manager,
        ):
            try:
                mgr.end_session(ctx.chat_id, project_id=ctx.project_id, thread_id=ctx.thread_root_id)
            except Exception:
                pass

    def _is_message_expired(self, create_time: int) -> bool:
        """判断消息是否过期。

        飞书历史消息可能会被 WS 重放；这里通过 `create_time` 过滤掉过旧消息，
        避免触发重复执行（尤其是 shell/编程任务）。
        """
        if not create_time:
            return False
        current_time = int(time.time() * 1000)
        message_age_ms = current_time - create_time
        return message_age_ms > self.MESSAGE_EXPIRE_SECONDS * 1000

    def _is_duplicate_message(self, message_id: str) -> bool:
        """消息去重：基于 `MessageCache` 判断是否重复处理。"""
        return self._message_cache.is_duplicate(message_id)

    def _get_api_client(self) -> lark.Client:
        """延迟构造 `lark_oapi.Client`（用于调用消息/卡片 API）。"""
        if self._api_client is None:
            self._api_client = (
                lark.Client.builder()
                .app_id(self.settings.app_id)
                .app_secret(self.settings.app_secret)
                .log_level(lark.LogLevel.INFO)
                .build()
            )
        return self._api_client

    def _get_streaming_manager(self) -> StreamingCardManager:
        """获取/创建流式卡片管理器（用于增量 patch 卡片）。"""
        if self._streaming_manager is None:
            self._streaming_manager = StreamingCardManager(self._get_api_client())
        return self._streaming_manager

    def _get_image_handler(self) -> FeishuImageHandler:
        """获取/创建图片处理器（解析 + 下载 + 生成引用文本）。"""
        if self._image_handler is None:
            self._image_handler = FeishuImageHandler(self._get_api_client, self.settings)
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
        "_add_reaction": ("_coco_handler", "add_reaction"),
        "_get_working_dir": ("_coco_handler", "get_working_dir"),
        "_set_working_dir": ("_coco_handler", "set_working_dir"),
        "_ensure_request_id": ("_coco_handler", "ensure_request_id"),
        "_format_ref_note": ("_coco_handler", "format_ref_note"),
        "_register_message_project": ("_coco_handler", "register_message_project"),
        "_reply_message": ("_coco_handler", "reply_message"),
        "_reply_message_with_id": ("_coco_handler", "reply_message_with_id"),
        "send_message": ("_coco_handler", "send_message"),
        "_get_engine_name": ("_coco_handler", "get_engine_name"),
        "_record_mode_transition": ("_coco_handler", "record_mode_transition"),
        "_inject_bridge_context": ("_coco_handler", "inject_bridge_context"),
        # --- Coco mode ---
        "_enter_coco_mode": ("_coco_handler", "enter_mode"),
        "_exit_coco_mode": ("_coco_handler", "exit_mode"),
        "_handle_coco_message": ("_coco_handler", "handle_message"),
        "_handle_coco_response": ("_coco_handler", "handle_response"),
        "_show_coco_info": ("_coco_handler", "show_info"),
        "_handle_card_enter_coco": ("_coco_handler", "handle_card_enter"),
        "_handle_card_exit_coco": ("_coco_handler", "handle_card_exit"),
        "_handle_card_resume_coco": ("_coco_handler", "handle_card_resume"),
        "_handle_card_new_coco": ("_coco_handler", "handle_card_new"),
        # --- Claude mode ---
        "_enter_claude_mode": ("_claude_handler", "enter_mode"),
        "_exit_claude_mode": ("_claude_handler", "exit_mode"),
        "_handle_claude_message": ("_claude_handler", "handle_message"),
        "_handle_claude_response": ("_claude_handler", "handle_response"),
        "_show_claude_info": ("_claude_handler", "show_info"),
        "_handle_card_enter_claude": ("_claude_handler", "handle_card_enter"),
        "_handle_card_exit_claude": ("_claude_handler", "handle_card_exit"),
        "_handle_card_resume_claude": ("_claude_handler", "handle_card_resume"),
        "_handle_card_new_claude": ("_claude_handler", "handle_card_new"),
        # --- Aiden mode ---
        "_enter_aiden_mode": ("_aiden_handler", "enter_mode"),
        "_exit_aiden_mode": ("_aiden_handler", "exit_mode"),
        "_handle_aiden_message": ("_aiden_handler", "handle_message"),
        "_handle_aiden_response": ("_aiden_handler", "handle_response"),
        "_show_aiden_info": ("_aiden_handler", "show_info"),
        "_handle_card_enter_aiden": ("_aiden_handler", "handle_card_enter"),
        "_handle_card_exit_aiden": ("_aiden_handler", "handle_card_exit"),
        "_handle_card_resume_aiden": ("_aiden_handler", "handle_card_resume"),
        "_handle_card_new_aiden": ("_aiden_handler", "handle_card_new"),
        # --- Gemini mode ---
        "_enter_gemini_mode": ("_gemini_handler", "enter_mode"),
        "_exit_gemini_mode": ("_gemini_handler", "exit_mode"),
        "_handle_gemini_message": ("_gemini_handler", "handle_message"),
        "_handle_gemini_response": ("_gemini_handler", "handle_response"),
        "_show_gemini_info": ("_gemini_handler", "show_info"),
        "_handle_card_enter_gemini": ("_gemini_handler", "handle_card_enter"),
        "_handle_card_exit_gemini": ("_gemini_handler", "handle_card_exit"),
        "_handle_card_resume_gemini": ("_gemini_handler", "handle_card_resume"),
        "_handle_card_new_gemini": ("_gemini_handler", "handle_card_new"),
        # --- Codex mode ---
        "_enter_codex_mode": ("_codex_handler", "enter_mode"),
        "_exit_codex_mode": ("_codex_handler", "exit_mode"),
        "_handle_codex_message": ("_codex_handler", "handle_message"),
        "_handle_codex_response": ("_codex_handler", "handle_response"),
        "_show_codex_info": ("_codex_handler", "show_info"),
        "_handle_card_enter_codex": ("_codex_handler", "handle_card_enter"),
        "_handle_card_exit_codex": ("_codex_handler", "handle_card_exit"),
        "_handle_card_resume_codex": ("_codex_handler", "handle_card_resume"),
        "_handle_card_new_codex": ("_codex_handler", "handle_card_new"),
        # --- TTADK mode ---
        "_enter_ttadk_mode": ("_ttadk_handler", "enter_mode"),
        "_exit_ttadk_mode": ("_ttadk_handler", "exit_mode"),
        "_handle_ttadk_message": ("_ttadk_handler", "handle_message"),
        "_handle_ttadk_response": ("_ttadk_handler", "handle_response"),
        "_show_ttadk_info": ("_ttadk_handler", "show_info"),
        "_handle_card_enter_ttadk": ("_ttadk_handler", "handle_card_enter"),
        "_handle_card_exit_ttadk": ("_ttadk_handler", "handle_card_exit"),
        "_handle_card_resume_ttadk": ("_ttadk_handler", "handle_card_resume"),
        "_handle_card_new_ttadk": ("_ttadk_handler", "handle_card_new"),
        "_handle_ttadk_command": ("_system_handler", "handle_ttadk_command"),
        "_handle_select_ttadk_tool": ("_system_handler", "handle_select_ttadk_tool"),
        "_handle_select_ttadk_model": ("_system_handler", "handle_select_ttadk_model"),
        "_handle_refresh_ttadk_models": ("_system_handler", "handle_refresh_ttadk_models"),
        "_handle_toggle_ttadk_yolo": ("_system_handler", "handle_toggle_ttadk_yolo"),
        "_handle_acp_command": ("_system_handler", "handle_acp_command"),
        "_handle_select_acp_tool": ("_system_handler", "handle_select_acp_tool"),
        "_handle_select_acp_model": ("_system_handler", "handle_select_acp_model"),
        "_handle_refresh_acp_models": ("_system_handler", "handle_refresh_acp_models"),
        "_handle_help_category": ("_system_handler", "handle_help_category"),
        "_handle_deep_prompt": ("_system_handler", "handle_deep_prompt"),
        # --- Deep Engine ---
        "_handle_deep_command": ("_deep_handler", "handle_deep_command"),
        "_start_deep_engine": ("_deep_handler", "start_deep_engine"),
        "_create_deep_callbacks": ("_deep_handler", "_create_deep_callbacks"),
        "_show_deep_status": ("_deep_handler", "show_deep_status"),
        "_show_deep_board": ("_deep_handler", "show_deep_board"),
        "_pause_deep_engine": ("_deep_handler", "pause_deep_engine"),
        "_resume_deep_engine": ("_deep_handler", "resume_deep_engine"),
        "_stop_deep_engine": ("_deep_handler", "stop_deep_engine"),
        "_stop_all_deep_engines": ("_deep_handler", "stop_all_deep_engines"),
        "_update_deep_context": ("_deep_handler", "update_deep_context"),
        "_toggle_deep_log": ("_deep_handler", "toggle_deep_log"),
        "_switch_deep_card_mode": ("_deep_handler", "switch_deep_card_mode"),
        # --- Loop Engine ---
        "_handle_loop_command": ("_loop_handler", "handle_loop_command"),
        "_start_loop_engine": ("_loop_handler", "start_loop_engine"),
        "_show_loop_status": ("_loop_handler", "show_loop_status"),
        "_pause_loop_engine": ("_loop_handler", "pause_loop_engine"),
        "_resume_loop_engine": ("_loop_handler", "resume_loop_engine"),
        "_stop_loop_engine": ("_loop_handler", "stop_loop_engine"),
        "_update_loop_guidance": ("_loop_handler", "update_loop_guidance"),
        "_toggle_loop_log": ("_loop_handler", "toggle_loop_log"),
        "_switch_loop_card_mode": ("_loop_handler", "switch_loop_card_mode"),
        # --- Spec Engine ---
        "_handle_spec_command": ("_spec_handler", "handle_spec_command"),
        "_start_spec_engine": ("_spec_handler", "start_spec_engine"),
        "_show_spec_status": ("_spec_handler", "show_spec_status"),
        "_pause_spec_engine": ("_spec_handler", "pause_spec_engine"),
        "_resume_spec_engine": ("_spec_handler", "resume_spec_engine"),
        "_stop_spec_engine": ("_spec_handler", "stop_spec_engine"),
        "_update_spec_guidance": ("_spec_handler", "update_spec_guidance"),
        "_toggle_spec_log": ("_spec_handler", "toggle_spec_log"),
        "_switch_spec_card_mode": ("_spec_handler", "switch_spec_card_mode"),
        "_toggle_spec_ac": ("_spec_handler", "toggle_spec_ac"),
        # --- Project ---
        "_create_project": ("_project_handler", "create_project"),
        "_show_project_board": ("_project_handler", "show_project_board"),
        "_show_current_project": ("_project_handler", "show_current_project"),
        "_show_project_status": ("_project_handler", "show_project_status"),
        "_preserve_project_context": ("_project_handler", "preserve_project_context"),
        "_restore_project_context": ("_project_handler", "restore_project_context"),
        "_close_project": ("_project_handler", "close_project"),
        # --- System ---
        "_show_help": ("_system_handler", "show_help"),
        "_show_full_help": ("_system_handler", "show_full_help"),
        "_exit_current_mode": ("_system_handler", "exit_current_mode"),
        "_submit_shell_command": ("_system_handler", "submit_shell_command"),
        "_change_directory": ("_system_handler", "change_directory"),
        "_handle_intercepted_command": ("_system_handler", "handle_intercepted_command"),
        # --- Diagnostics ---
        "_show_task_board": ("_diagnostics_handler", "show_task_board"),
        "_show_context_diff": ("_diagnostics_handler", "show_context_diff"),
        "_build_context_diff_report": ("_diagnostics_handler", "_build_context_diff_report"),
        "_submit_diff_report": ("_diagnostics_handler", "_submit_diff_report"),
        "_show_message_trace": ("_diagnostics_handler", "show_message_trace"),
    }

    def __getattr__(self, name: str):
        # Fallback for any attribute not found — all forwarding methods are
        # bound eagerly in __init__ via setattr, so this only fires for
        # genuinely missing attributes.
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    # Thin wrappers that cannot be expressed as simple delegation
    def reply(self, message_id: str, content, msg_type: str = "text", chat_id: Optional[str] = None):
        """轻量回复封装：兼容旧调用路径，实际委托到 handler 的 `_reply_message`。"""
        self._reply_message(message_id, content, msg_type)

    def add_reaction(self, message_id: str, emoji_type: str):
        """轻量表情反馈封装：委托到 handler 的 `add_reaction`。"""
        self._add_reaction(message_id, emoji_type)

    def _switch_project(self, message_id: str, chat_id: str, name: str, auto_enter_coco: bool = True):
        """切换当前 chat 的 active project，并可选自动进入 Coco 模式。"""
        self._project_handler.switch_project(
            message_id,
            chat_id,
            name,
            auto_enter_coco=auto_enter_coco,
            coco_handler=self._coco_handler,
            claude_handler=self._claude_handler,
        )

    @staticmethod
    def _is_exit_command(text: str) -> bool:
        """判断是否为“退出当前编程模式”的命令（跨模式一致）。"""
        return SystemHandler.is_exit_command(text)

    @staticmethod
    def _is_deep_command(text: str) -> bool:
        """判断是否为 Deep Engine 命令。"""
        return SystemHandler.is_deep_command(text)

    @staticmethod
    def _is_loop_command(text: str) -> bool:
        """判断是否为 Loop Engine 命令。"""
        return SystemHandler.is_loop_command(text)

    @staticmethod
    def _is_spec_command(text: str) -> bool:
        """判断是否为 Spec Engine 命令。"""
        return SystemHandler.is_spec_command(text)

    @staticmethod
    def _is_interceptable_command(text: str) -> bool:
        """判断是否为需要系统层拦截的命令（帮助/项目/状态等）。"""
        return SystemHandler.is_interceptable_command(text)

    @staticmethod
    def _mode_to_context_source(mode) -> ContextSourceMode:
        """将 `InteractionMode` 映射到 `ContextSourceMode`（用于统一上下文记录）。"""
        from ..mode import InteractionMode

        mapping = {
            InteractionMode.SMART: ContextSourceMode.SMART,
            InteractionMode.COCO: ContextSourceMode.COCO,
            InteractionMode.CLAUDE: ContextSourceMode.CLAUDE,
            InteractionMode.AIDEN: ContextSourceMode.AIDEN,
            InteractionMode.CODEX: ContextSourceMode.CODEX,
            InteractionMode.GEMINI: ContextSourceMode.GEMINI,
            InteractionMode.TTADK: ContextSourceMode.TTADK,
        }
        return mapping.get(mode, ContextSourceMode.SMART)

    # ==================================================================
    # Core routing — these remain in ws_client.py
    # ==================================================================

    def _resolve_project_from_message(
        self, message_id: str, chat_id: str, parent_id: Optional[str] = None
    ) -> tuple[Optional[ProjectContext], Optional[str]]:
        """根据消息引用（parent/root）解析项目上下文。

        返回：
        - `project`: 最终解析到的 ProjectContext（或当前 active project）。
        - `auto_enter_mode`: 若该消息是回复某个编程会话/项目卡片，允许自动进入对应编程模式。
        """
        auto_enter_mode = None

        if parent_id:
            project_id = self._message_mapper.get_project_id(parent_id)
            if project_id:
                project = self._project_manager.get_project(project_id)
                if project:
                    self._project_manager.set_active_project(chat_id, project_id)
                    logger.info("通过消息引用切换到项目: %s", project.project_name)

                    if project.ttadk_mode:
                        auto_enter_mode = "ttadk"
                    elif project.gemini_mode:
                        auto_enter_mode = "gemini"
                    elif project.codex_mode:
                        auto_enter_mode = "codex"
                    elif project.aiden_mode:
                        auto_enter_mode = "aiden"
                    elif project.claude_mode:
                        auto_enter_mode = "claude"
                    elif project.coco_mode:
                        auto_enter_mode = "coco"

                    if auto_enter_mode:
                        logger.info("自动进入 %s 模式 (回复编程消息)", auto_enter_mode)

                    return project, auto_enter_mode

        return self._project_manager.get_active_project(chat_id), None

    def _handle_message(self, data: P2ImMessageReceiveV1):
        """飞书消息事件入口：只做轻量前置判断，然后交给 scheduler 异步处理。"""
        try:
            msg = data.event.message
            message_id = msg.message_id
            chat_id = msg.chat_id
        except Exception:
            message_id = None
            chat_id = "unknown"

        project_id = None
        thread_root_id = None
        try:
            parent_id = getattr(data.event.message, "parent_id", None)
            root_id = getattr(data.event.message, "root_id", None)
            thread_root_id = root_id
            thread_ctx = None

            if root_id and self.settings.thread_programming_enabled:
                thread_ctx = self._thread_manager.get(root_id)
                if thread_ctx:
                    project_id = thread_ctx.project_id
                    thread_root_id = thread_ctx.thread_root_id
                    logger.debug(
                        "[Thread] _handle_message hit: msg_root=%s canonical=%s mode=%s",
                        root_id[:12] if root_id else "N", thread_ctx.thread_root_id[:12], thread_ctx.mode,
                    )
                else:
                    logger.debug("[Thread] _handle_message miss: msg_root=%s", root_id[:12] if root_id else "N")

            if not project_id and self.settings.thread_programming_enabled and not thread_ctx:
                chat_fallbacks = self._thread_manager.get_by_chat(chat_id)
                if chat_fallbacks:
                    _fb = chat_fallbacks[0]
                    if _fb.mode and _fb.mode != "smart":
                        project_id = _fb.project_id
                        thread_root_id = _fb.thread_root_id
                        logger.debug(
                            "[Thread] _handle_message chat-fallback: chat=%s canonical=%s mode=%s",
                            chat_id[:12], _fb.thread_root_id[:12], _fb.mode,
                        )

            if not project_id:
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

        text = self._extract_text_from_message(data)
        is_system = self._is_system_command_message(data)
        is_shell_fast = False if is_system else self._is_likely_shell_command_message(data)
        is_spec = self._is_spec_command(text) if text else False

        # For likely shell commands, route to a separate shell queue so they
        # don't block behind long-running programming tasks on the project queue.
        shell_queue_key = None
        if is_shell_fast:
            queue_suffix = project_id or "default"
            if thread_root_id:
                queue_suffix = f"{queue_suffix}:t:{thread_root_id}"
            shell_queue_key = f"{chat_id}:shell:{queue_suffix}"

        control_queue_key = self._build_control_queue_key(chat_id=chat_id, project_id=project_id, text=text)
        queue_key = shell_queue_key or control_queue_key
        if not queue_key and thread_root_id and self.settings.thread_programming_enabled:
            queue_suffix = project_id or "default"
            queue_key = f"{chat_id}:{queue_suffix}:t:{thread_root_id}"

        request_id = self._ensure_request_id(message_id, chat_id=chat_id, project_id=project_id)

        with TraceContext(request_id):
            task_type = "spec_command" if is_spec else "feishu_message"
            if is_system:
                tl = (text or "").strip().lower()
                if tl in {"/help", "/帮助"}:
                    task_type = "system_help"
                elif tl in {"/exit", "/quit"}:
                    task_type = "system_exit"
            spec = TaskSpec(
                chat_id=chat_id,
                name="process_message",
                task_type=task_type,
                message_id=message_id,
                project_id=project_id,
                origin_message_id=message_id,
                request_id=request_id,
                priority=TaskPriority.HIGH if is_system else TaskPriority.NORMAL,
                is_system_command=is_system,
                queue_key=queue_key,
            )
            try:
                handle = self._scheduler.submit(
                    spec,
                    lambda ctx, _sf=is_shell_fast: self._process_message_async(
                        data, task_ctx=ctx, shell_fast_tracked=_sf
                    ),
                )
            except (RateLimitExceededException, CircuitBreakerOpenException) as e:
                logger.warning(f"Backpressure applied: {e}")
                if is_spec:
                    self._reply_message(message_id, "⚠️ 系统繁忙 (Spec 模式)，请稍后再试。")
                else:
                    self._reply_message(message_id, "⚠️ 当前服务繁忙，请稍后再试。")
                return
            try:
                if message_id:
                    self._message_linker.link_task(message_id, handle.run_id)
            except Exception as e:
                logger.debug("link_task失败(message): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    def _is_system_command_message(self, data: P2ImMessageReceiveV1) -> bool:
        """Check if the message is a system command that should bypass project queue.

        All slash commands (``/xxx``) are system commands: they should never
        block behind long-running Coco/Claude programming tasks on the project
        queue.  This includes ``/stop_deep``, ``/exit``, ``/loop_status``, etc.
        """
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
            if not text:
                return False
            # All /command messages are system commands
            if text.startswith("/"):
                return True
            # Also detect exit keywords (Chinese)
            return self._is_exit_command(text)
        except Exception:
            return False

    def _extract_text_from_message(self, data: P2ImMessageReceiveV1) -> str:
        """Extract plain text from a Feishu message event (for early routing)."""
        try:
            content_str = data.event.message.content
            if not content_str:
                return ""
            content = json.loads(content_str)
            text = content.get("text", "").strip()
            if text.startswith("@"):
                parts = text.split(None, 1)
                text = parts[1].strip() if len(parts) > 1 else ""
            return text
        except Exception:
            return ""

    @staticmethod
    def _is_programming_entry_command(text: str) -> bool:
        """是否为编程模式初始化命令（用于与 /spec 串行化控制面执行）。"""
        text_lower = (text or "").strip().lower()
        return text_lower in {
            "/coco",
            "/enter_coco",
            "/claude",
            "/enter_claude",
            "/aiden",
            "/enter_aiden",
            "/codex",
            "/enter_codex",
            "/gemini",
            "/enter_gemini",
            "/ttadk",
            "/acp",
        }

    def _build_control_queue_key(self, *, chat_id: str, project_id: Optional[str], text: str) -> Optional[str]:
        """为编程初始化与 spec 命令构造串行控制队列 key。"""
        normalized = (text or "").strip()
        if not normalized:
            return None
        if not (self._is_spec_command(normalized) or self._is_programming_entry_command(normalized)):
            return None
        queue_suffix = project_id or "default"
        return f"{chat_id}:control:{queue_suffix}"

    def _is_likely_shell_command_message(self, data: P2ImMessageReceiveV1) -> bool:
        """Check if the message looks like a shell command for early routing."""
        text = self._extract_text_from_message(data)
        return SystemHandler.is_likely_shell_command(text) if text else False

    def _process_message_async(self, data: P2ImMessageReceiveV1, task_ctx=None, shell_fast_tracked: bool = False):
        """消息处理主逻辑（运行在 scheduler 线程池中）。

        大致流程：校验 → 解析文本/图片 → 解析项目上下文 → 路由到对应模式/引擎。
        """
        from ..thread import set_current_thread_id

        try:
            event = data.event
            message = event.message
            message_id = message.message_id
            chat_id = message.chat_id
            request_id = self._ensure_request_id(message_id, chat_id=chat_id)

            root_id = getattr(message, "root_id", None)
            if root_id and self.settings.thread_programming_enabled:
                thread_ctx = self._thread_manager.get(root_id)
                if thread_ctx:
                    set_current_thread_id(thread_ctx.thread_root_id)
                    logger.debug(
                        "[Thread] _process_async hit: msg_root=%s canonical=%s",
                        root_id[:12], thread_ctx.thread_root_id[:12],
                    )

            # 1. Validation
            if not self._validate_message(message, request_id):
                return

            # 2. Parse Content
            image_handler = self._get_image_handler()
            parse_result = image_handler.parse_message(message.message_type, message.content)
            text = self._clean_at_text(parse_result.text)

            # 3. Handle Images (if any)
            is_image_only = False
            if parse_result.image_keys:
                project, auto_enter_mode, text, is_image_only = self._handle_image_content(
                    message, parse_result.image_keys, text, request_id, task_ctx
                )
            else:
                # 4. Resolve Context (if no images to drive it)
                project, auto_enter_mode = self._resolve_message_context(message)

            # 4b. Safety net: if auto_enter_mode is still None but we are in a
            # registered thread, force-set mode from thread context so that the
            # message never accidentally falls through to SMART / intent recognition.
            if not auto_enter_mode:
                _root = getattr(message, "root_id", None)
                if _root and self.settings.thread_programming_enabled:
                    _tctx = self._thread_manager.get(_root)
                    if _tctx and _tctx.mode and _tctx.mode != "smart":
                        auto_enter_mode = _tctx.mode
                        set_current_thread_id(_tctx.thread_root_id)
                        if not project:
                            project = self._project_manager.get_project(_tctx.project_id) or self._project_manager.get_active_project(chat_id)
                        logger.info(
                            "[Thread] Safety-net resolved mode: root=%s canonical=%s mode=%s",
                            _root[:12], _tctx.thread_root_id[:12], auto_enter_mode,
                        )
                if not auto_enter_mode and self.settings.thread_programming_enabled:
                    _chat_ctxs = self._thread_manager.get_by_chat(chat_id)
                    if _chat_ctxs:
                        _best = _chat_ctxs[0]
                        if _best.mode and _best.mode != "smart":
                            auto_enter_mode = _best.mode
                            set_current_thread_id(_best.thread_root_id)
                            if not project:
                                project = self._project_manager.get_project(_best.project_id) or self._project_manager.get_active_project(chat_id)
                            logger.info(
                                "[Thread] Safety-net fallback by chat: chat=%s canonical=%s mode=%s",
                                chat_id[:12], _best.thread_root_id[:12], auto_enter_mode,
                            )

            # 5. Handle Context Updates (Task Scheduler)
            if task_ctx and project:
                self._update_task_project(task_ctx, project.project_id)

            # 6. Dispatch Logic
            if not text and not is_image_only:
                # Special case: handle empty text (e.g. unsupported content that parsed to empty)
                # But wait, if image_keys exist, text might be empty but valid (image only).
                # _handle_image_content handles text augmentation.
                # If we are here and text is empty, check if we should show help or dispatch to mode.
                self._dispatch_empty_text(message_id, chat_id, project, task_ctx)
                return

            self._dispatch_message_logic(
                message_id,
                chat_id,
                text,
                project,
                auto_enter_mode,
                is_image_only=is_image_only,
                shell_fast_tracked=shell_fast_tracked,
            )

        except asyncio.TimeoutError:
            logger.warning("处理消息超时 (asyncio.TimeoutError)")
        except Exception as e:
            logger.error("处理消息异常: %s", e, exc_info=True)
            try:
                self._reply_message(message_id, "❌ 处理消息时发生内部错误，请稍后重试")
            except Exception:
                pass
        finally:
            set_current_thread_id(None)
            with self._pending_image_lock:
                self._pending_image_keys.pop(message_id, None)
                self._pending_image_only.discard(message_id)

    def _validate_message(self, message, request_id: str) -> bool:
        """校验消息是否需要处理（过期/重复/类型不支持等）。"""
        if message.create_time and self._is_message_expired(int(message.create_time)):
            logger.debug("跳过过期消息: %s", message.message_id)
            return False

        if self._is_duplicate_message(message.message_id):
            logger.debug("跳过重复消息: %s", message.message_id)
            return False

        supported_types = {"text", "image", "post"}
        if message.message_type not in supported_types:
            self._reply_message(message.message_id, "⚠️ 目前仅支持文本、图片和富文本消息", request_id=request_id)
            return False
        return True

    def _clean_at_text(self, text: str) -> str:
        """移除 '@机器人' 前缀，得到用户真实输入文本。"""
        text = text.strip()
        if text.startswith("@"):
            parts = text.split(None, 1)
            if len(parts) > 1:
                return parts[1].strip()
            return ""
        return text

    def _handle_image_content(self, message, image_keys, text, request_id, task_ctx):
        """处理图片消息：下载并把图片引用文本拼接回 prompt。

        返回 `(project, auto_enter_mode, text, is_image_only)`。
        """
        message_id = message.message_id
        chat_id = message.chat_id
        parent_id = getattr(message, "parent_id", None)
        root_id = getattr(message, "root_id", None)

        with self._pending_image_lock:
            self._pending_image_keys[message_id] = image_keys

        project, auto_enter_mode = self._resolve_message_context(message)

        try:
            if project:
                self._message_linker.register_origin(
                    message_id, request_id=request_id, chat_id=chat_id, project_id=project.project_id
                )
        except Exception as e:
            logger.debug("register_origin失败(image_msg): message_id=%s, err=%s", message_id, e)

        if task_ctx and project:
            self._update_task_project(task_ctx, project.project_id)

        save_dir = FeishuImageHandler.get_image_save_dir(
            project.root_path if project else None,
            self._get_working_dir(chat_id),
        )

        image_handler = self._get_image_handler()
        download_result = image_handler.download_images(message_id, image_keys, save_dir)

        is_image_only = False
        if download_result.saved_paths:
            is_image_only = not text
            ref_text = FeishuImageHandler.build_image_reference_text(download_result.saved_paths)
            if text:
                text += ref_text
            else:
                text = "请查看并理解以下图片" + ref_text

        if download_result.failed_keys:
            logger.warning("部分图片下载失败: %s", download_result.failed_keys)

        if is_image_only:
            with self._pending_image_lock:
                self._pending_image_only.add(message_id)

        return project, auto_enter_mode, text, is_image_only

    def _resolve_message_context(self, message):
        """从 message 的 parent/root 引用恢复项目上下文。

        优先级：话题上下文 > 消息引用上下文 > 当前 active project。
        """
        message_id = message.message_id
        chat_id = message.chat_id
        parent_id = getattr(message, "parent_id", None)
        root_id = getattr(message, "root_id", None)

        if root_id and self.settings.thread_programming_enabled:
            thread_ctx = self._thread_manager.get(root_id)
            if thread_ctx:
                from ..thread import set_current_thread_id
                set_current_thread_id(thread_ctx.thread_root_id)
                auto_enter_mode = thread_ctx.mode if thread_ctx.mode != "smart" else None
                project = self._project_manager.get_project(thread_ctx.project_id)
                if not project:
                    project = self._project_manager.get_active_project(chat_id)
                logger.info(
                    "[Thread] Resolved context: msg_root=%s canonical=%s project=%s mode=%s project_found=%s",
                    root_id[:12], thread_ctx.thread_root_id[:12], thread_ctx.project_id, thread_ctx.mode, project is not None,
                )
                return project, auto_enter_mode

        if self.settings.thread_programming_enabled:
            chat_ctxs = self._thread_manager.get_by_chat(chat_id)
            if chat_ctxs:
                best_ctx = chat_ctxs[0]
                if best_ctx.mode and best_ctx.mode != "smart":
                    from ..thread import set_current_thread_id
                    set_current_thread_id(best_ctx.thread_root_id)
                    auto_enter_mode = best_ctx.mode
                    project = self._project_manager.get_project(best_ctx.project_id)
                    if not project:
                        project = self._project_manager.get_active_project(chat_id)
                    logger.info(
                        "[Thread] Fallback by chat: chat=%s canonical=%s project=%s mode=%s",
                        chat_id[:12], best_ctx.thread_root_id[:12], best_ctx.project_id, best_ctx.mode,
                    )
                    return project, auto_enter_mode

        return self._resolve_project_from_message(message_id, chat_id, parent_id or root_id)

    def _get_effective_mode(self, chat_id: str, project_id: Optional[str] = None):
        from ..mode import InteractionMode
        thread_id = get_current_thread_id()
        if thread_id:
            thread_ctx = self._thread_manager.get(thread_id)
            if thread_ctx and thread_ctx.mode != "smart":
                return InteractionMode(thread_ctx.mode), True
        return self._mode_manager.get_mode(chat_id, project_id=project_id), self._mode_manager.is_programming_mode(chat_id, project_id=project_id)

    def _get_mode_handler(self, mode):
        from ..mode import InteractionMode
        _map = {
            InteractionMode.COCO: self._coco_handler,
            InteractionMode.CLAUDE: self._claude_handler,
            InteractionMode.AIDEN: self._aiden_handler,
            InteractionMode.CODEX: self._codex_handler,
            InteractionMode.GEMINI: self._gemini_handler,
            InteractionMode.TTADK: self._ttadk_handler,
        }
        return _map.get(mode)

    def _is_one_shot_pending(self, chat_id, project_id, current_mode):
        if get_current_thread_id():
            return False, None
        if not self.settings.thread_programming_enabled:
            return False, None
        handler = self._get_mode_handler(current_mode)
        if not handler:
            return False, None
        mgr = handler._get_session_manager()
        session = mgr.get_session(chat_id, project_id=project_id, thread_id=None)
        if session:
            return False, None
        return True, handler

    def _find_active_thread(self, chat_id):
        if not self.settings.thread_programming_enabled:
            return None
        contexts = self._thread_manager.get_by_chat(chat_id)
        for ctx in contexts:
            if ctx.mode and ctx.mode != "smart":
                return ctx
        return None

    def _dispatch_to_thread(self, message_id, chat_id, text, project, current_mode, handler):
        from ..thread import set_current_thread_id, get_thread_manager

        self._add_reaction(message_id, EmojiReaction.on_coco_mode())
        self._add_reaction(message_id, EmojiReaction.on_processing())

        project_id = project.project_id if project else None

        old_thread = self._find_active_thread(chat_id)
        if old_thread:
            try:
                from ..mode import InteractionMode as _IM
                old_handler = self._get_mode_handler(_IM(old_thread.mode))
                if old_handler:
                    old_session_mgr = old_handler._get_session_manager()
                    old_session_mgr.end_session(chat_id, project_id=old_thread.project_id, thread_id=old_thread.thread_root_id)
                self._thread_manager.remove(old_thread.thread_root_id)
                logger.info("[Thread] Closed old thread %s before creating new one", old_thread.thread_root_id[:12])
            except Exception as e:
                logger.warning("[Thread] Failed to close old thread: %s", e)

        mode_name = handler.mode_name
        content = (
            f"{handler.mode_emoji} 正在创建编程话题…\n\n"
            f"你的需求将在话题中由 {mode_name} 处理"
        )
        if project:
            msg_type, card_content = CardBuilder.build_project_response_card(
                project,
                f"{handler.mode_emoji} {mode_name} 编程话题",
                content,
                show_buttons=False,
                footer=f"📂 项目目录: {project.root_path}",
            )
            reply_id = handler.reply_message_with_id(
                message_id, card_content, msg_type, reply_in_thread=True,
            )
            if reply_id:
                handler.register_message_project(reply_id, project)
        else:
            reply_id = handler.reply_message_with_id(
                message_id, content, "text", reply_in_thread=True,
            )

        if not reply_id:
            self._reply_message(message_id, "⚠️ 创建编程话题失败，请重试")
            return

        thread_root_id = reply_id
        alias_keys = [message_id] if message_id != reply_id else []

        try:
            set_current_thread_id(thread_root_id)

            handler.enter_mode(
                thread_root_id, chat_id, silent=True, project=project, thread_id=thread_root_id,
            )

            if not project:
                project = self._project_manager.get_active_project(chat_id)
                project_id = project.project_id if project else None

            session = handler._get_session_manager().get_session(
                chat_id, project_id=project_id, thread_id=thread_root_id,
            )
            if session:
                self._mode_manager.exit_to_smart(chat_id, project_id=project_id)
                if project:
                    handler._set_mode_on_project(project, False)
                handler._register_thread_context(thread_root_id, chat_id, project, session, alias_keys=alias_keys)
                handler.handle_message(message_id, chat_id, text, project)
            else:
                self._mode_manager.exit_to_smart(chat_id, project_id=project_id)
                if project:
                    handler._set_mode_on_project(project, False)
                self._reply_message(
                    message_id,
                    f"⚠️ {mode_name} 会话启动失败，已退回智能模式，请重新发送 /{mode_name.lower()} 重试",
                )
        except Exception as e:
            log_exception(logger, f"{mode_name} 话题执行异常", e)
            self._mode_manager.exit_to_smart(chat_id, project_id=project_id)
            if project:
                handler._set_mode_on_project(project, False)
        finally:
            set_current_thread_id(None)

    def _update_task_project(self, task_ctx, project_id):
        """将调度任务与 project_id 关联（便于任务看板/诊断）。"""
        try:
            self._scheduler.update_project_id(task_ctx.run_id, project_id)
        except Exception as e:
            logger.debug("update_project_id失败: run_id=%s, err=%s", task_ctx.run_id, e)

    def _dispatch_empty_text(self, message_id, chat_id, project, task_ctx):
        """处理“文本为空”的情况：在编程模式下仍转发（保持会话），否则展示帮助。"""
        from ..mode import InteractionMode

        _pid = project.project_id if project else None
        if not _pid and task_ctx and task_ctx.spec.project_id:
            _pid = task_ctx.spec.project_id

        current_mode, _is_prog = self._get_effective_mode(chat_id, project_id=_pid)
        if current_mode in {
            InteractionMode.COCO,
            InteractionMode.CLAUDE,
            InteractionMode.AIDEN,
            InteractionMode.CODEX,
            InteractionMode.GEMINI,
            InteractionMode.TTADK,
        }:
            if not get_current_thread_id() and self.settings.thread_programming_enabled:
                pending, handler = self._is_one_shot_pending(chat_id, _pid, current_mode)
                if pending:
                    self._reply_message(
                        message_id,
                        f"📝 当前已开启{handler.mode_name}编程模式\n\n请发送你的编程需求，将自动创建编程话题",
                    )
                    return

            if project is None:
                project = self._project_manager.get_active_project(chat_id)

            if current_mode == InteractionMode.COCO:
                self._handle_coco_message(message_id, chat_id, "", project)
            elif current_mode == InteractionMode.CLAUDE:
                self._handle_claude_message(message_id, chat_id, "", project)
            elif current_mode == InteractionMode.AIDEN:
                self._handle_aiden_message(message_id, chat_id, "", project)
            elif current_mode == InteractionMode.CODEX:
                self._handle_codex_message(message_id, chat_id, "", project)
            elif current_mode == InteractionMode.GEMINI:
                self._handle_gemini_message(message_id, chat_id, "", project)
            else:
                self._ttadk_handler.handle_message(message_id, chat_id, "", project)
        else:
            self._show_help(message_id, chat_id)

    def _dispatch_message_logic(
        self, message_id, chat_id, text, project, auto_enter_mode, is_image_only=False, shell_fast_tracked=False
    ):
        """根据 auto-enter 与当前模式，将消息路由到对应编程模式或 SMART 处理路径。"""
        if auto_enter_mode:
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                _pid = project.project_id if project else None
                if self._should_defer_exit(chat_id=chat_id, project_id=_pid):
                    self._request_deferred_exit(message_id=message_id, chat_id=chat_id, project_id=_pid)
                    self._reply_message(message_id, "✅ 已收到 /exit，将在当前任务完成后退出（不中断执行）")
                    return
                self._exit_current_mode(message_id, chat_id, project=project)
                return
            if self._is_programming_entry_command(text):
                self._reply_message(
                    message_id,
                    f"💡 当前话题已在编程模式中，直接发送你的需求即可\n\n如需切换工具，请在主对话中发送对应命令创建新话题",
                )
                return
            if self._is_deep_command(text) or self._is_loop_command(text) or self._is_spec_command(text):
                self._reply_message(
                    message_id,
                    "⚠️ Deep/Loop/Spec 模式暂不支持在话题中使用，请在主对话中发送对应命令",
                )
                return
        if auto_enter_mode and auto_enter_mode in {"coco", "claude", "aiden", "codex", "gemini", "ttadk"}:
            from ..mode import InteractionMode
            handler = self._get_mode_handler(InteractionMode(auto_enter_mode))
            if handler:
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                handler.handle_message(message_id, chat_id, text, project)
            else:
                self._process_with_intent(message_id, chat_id, text, project, shell_fast_tracked=shell_fast_tracked)
        else:
            self._process_with_intent(message_id, chat_id, text, project, shell_fast_tracked=shell_fast_tracked)

    def _handle_card_action(self, data: P2CardActionTrigger) -> Optional[P2CardActionTriggerResponse]:
        """飞书卡片回调入口：做去重 + 任务入队（system action 走快通道）。"""
        try:
            header = data.header
            event_id = header.event_id
            if self._card_event_cache.is_duplicate(event_id):
                logger.warning("跳过重复卡片回调事件: %s", event_id)
                return None

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
                header.event_id,
                header.event_type,
                context.open_message_id,
                context.open_chat_id,
                action.tag,
                action.name,
                type(action.value).__name__,
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

        action_type_preview = ""
        try:
            value_raw = data.event.action.value
            if isinstance(value_raw, dict):
                action_type_preview = str(value_raw.get("action", "") or "")
            elif isinstance(value_raw, str):
                try:
                    parsed = json.loads(value_raw)
                    if isinstance(parsed, dict):
                        action_type_preview = str(parsed.get("action", "") or "")
                    else:
                        action_type_preview = ""
                except Exception:
                    action_type_preview = ""
            else:
                action_type_preview = ""
        except Exception:
            action_type_preview = ""

        try:
            with self._system_cmd_gate_lock:
                inflight = int(self._system_cmd_inflight_by_chat.get(open_chat_id, 0) or 0)
            if inflight > 0 and action_type_preview not in _READONLY_CARD_ACTIONS:
                if open_message_id:
                    self._reply_message(open_message_id, "⏳ 系统指令处理中，按钮暂不可用，请稍后重试")
                return None
        except Exception:
            pass

        operator_id = ""
        try:
            operator = data.event.operator
            operator_id = (
                getattr(operator, "open_id", None)
                or getattr(operator, "user_id", None)
                or getattr(operator, "union_id", None)
                or ""
            )
        except Exception:
            operator_id = ""

        if open_message_id and action_type_preview:
            dedupe_key = f"{open_chat_id}:{open_message_id}:{operator_id}:{action_type_preview}"
            try:
                if self._card_action_dedup_cache.is_duplicate(dedupe_key):
                    return None

                # Immediate feedback: prefer patching streaming card (non-spam), fallback to reply.
                try:
                    manager = self._get_streaming_manager()
                    card = manager.get_card(open_message_id)
                    if card:
                        manager.set_sticky_message(open_message_id, "已收到操作，正在处理…", duration=2.5)
                except Exception:
                    pass
            except Exception:
                # best-effort only
                pass

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

        with TraceContext(request_id):
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
                logger.debug(
                    "link_task失败(card_action): origin=%s, run_id=%s, err=%s", origin_message_id, handle.run_id, e
                )
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
                "show_status",
                "switch_project",
                "show_board",
                "refresh_board",
                "show_detail",
                "new_project_prompt",
                "select_ttadk_tool",
                "select_ttadk_model",
                "refresh_ttadk_models",
                "select_acp_tool",
                "select_acp_model",
                "refresh_acp_models",
                "load_more",
                "load_prev",
                "show_ttadk_menu",
                "show_acp_menu",
                "show_help_menu",
                "enter_deep_prompt",
                "help_category",
                "deep_pause",
                "deep_stop",
                "deep_resume",
                "loop_pause",
                "loop_stop",
                "loop_resume",
                "spec_pause",
                "spec_stop",
                "spec_resume",
            }
            return action_type in system_actions
        except Exception:
            return False

    def _process_card_action_async(self, data: Any, task_ctx=None):
        """卡片动作处理逻辑（第二阶段实现）。

        该方法会把 `action.value` normalize 为 dict，提取 `action/project_id`，并通过
        `ActionDispatcher` 做 exact/prefix 路由。
        """
        from ..thread import set_current_thread_id

        try:
            start_time = time.perf_counter()
            action = data.event.action
            value_raw = action.value
            operator = data.event.operator
            open_message_id = data.event.context.open_message_id
            open_chat_id = data.event.context.open_chat_id
            logger.debug(
                "卡片回调上下文: operator_open_id=%s, operator_user_id=%s, value_raw_type=%s",
                getattr(operator, "open_id", None),
                getattr(operator, "user_id", None),
                type(value_raw).__name__,
            )

            if isinstance(value_raw, dict):
                value = dict(value_raw)
            elif isinstance(value_raw, str):
                try:
                    value = json.loads(value_raw)
                    if not isinstance(value, dict):
                        value = {"action": value_raw}
                except (json.JSONDecodeError, TypeError):
                    logger.warning("卡片 value 解析失败: value_raw=%s", value_raw[:500])
                    value = {"action": value_raw}
            else:
                value = {"action": str(value_raw)}

            # --- 注入交互组件的额外返回值 ---
            try:
                if getattr(action, "option", None) is not None:
                    value["_option"] = action.option
                if getattr(action, "options", None) is not None:
                    value["_options"] = action.options
                if getattr(action, "form_value", None) is not None:
                    value["_form_value"] = action.form_value
                if getattr(action, "input_value", None) is not None:
                    value["_input_value"] = action.input_value
            except Exception:
                pass

            action_type = value.get("action", "")
            project_id = value.get("project_id", "")

            card_thread_id = value.get("thread_root_id")
            if card_thread_id and self.settings.thread_programming_enabled:
                thread_ctx = self._thread_manager.get(card_thread_id)
                if thread_ctx:
                    set_current_thread_id(card_thread_id)

            if task_ctx and project_id:
                try:
                    self._scheduler.update_project_id(task_ctx.run_id, project_id)
                except Exception as e:
                    logger.debug("update_project_id失败(card_action): run_id=%s, err=%s", task_ctx.run_id, e)

            logger.info(
                "卡片按钮点击: action=%s, project_id=%s, value_keys=%s",
                action_type,
                project_id,
                list(value.keys()),
            )

            # --- Dispatch via ActionDispatcher ---
            matched = self._action_dispatcher.dispatch(action_type, open_message_id, open_chat_id, project_id, value)

            if not matched:
                logger.debug("未注册的卡片动作: %s", action_type)

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            logger.debug("卡片回调处理耗时: %dms", elapsed_ms)

        except asyncio.TimeoutError:
            logger.warning("处理卡片动作超时")
        except Exception as e:
            logger.error("处理卡片动作异常: %s", e, exc_info=True)
            # 发送错误提示给用户
            _mid = locals().get("open_message_id", "unknown")
            _cid = locals().get("open_chat_id", "unknown")
            _action = locals().get("action_type", "unknown")
            try:
                if _mid != "unknown":
                    if str(_action).startswith("ttadk") or _action in {
                        "show_ttadk_menu",
                        "select_ttadk_tool",
                        "select_ttadk_model",
                        "refresh_ttadk_models",
                        "toggle_ttadk_yolo",
                    }:
                        try:
                            from ..card import CardBuilder

                            project_id = locals().get("project_id")
                            msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(
                                "操作未完成",
                                project_id=project_id or None,
                            )
                            self._reply_message(_mid, card_content, msg_type=msg_type)
                        except Exception:
                            self._reply_message(_mid, "⚠️ 操作未完成，请稍后重试或发送 /ttadk 重新进入")
                    else:
                        self._reply_message(_mid, f"❌ 操作失败 ({_action}): {e}")
            except Exception:
                pass
        finally:
            set_current_thread_id(None)

    def _process_with_intent(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional[ProjectContext] = None,
        *,
        shell_fast_tracked: bool = False,
    ):
        """SMART 模式下的主路由：控制命令优先，其次进入意图识别/多任务执行。"""
        from ..mode import InteractionMode

        _pid = project.project_id if project else None
        current_mode, is_in_programming = self._get_effective_mode(chat_id, project_id=_pid)

        # Control-plane commands: handle consistently in all modes
        if self._is_deep_command(text):
            if get_current_thread_id():
                self._reply_message(message_id, "⚠️ Deep 模式暂不支持在话题中使用，请在主对话中发送 /deep 命令")
                return
            self._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_deep_command(message_id, chat_id, text, project)
            return

        if self._is_loop_command(text):
            if get_current_thread_id():
                self._reply_message(message_id, "⚠️ Loop 模式暂不支持在话题中使用，请在主对话中发送 /loop 命令")
                return
            self._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_loop_command(message_id, chat_id, text, project)
            return

        if self._is_spec_command(text):
            if get_current_thread_id():
                self._reply_message(message_id, "⚠️ Spec 模式暂不支持在话题中使用，请在主对话中发送 /spec 命令")
                return
            self._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            self._handle_spec_command(message_id, chat_id, text, project)
            return

        if self._is_interceptable_command(text):
            self._handle_intercepted_command(message_id, chat_id, text, project)
            return

        # Programming mode (Coco / Claude / TTADK): exit or forward to active session
        if is_in_programming:
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                if self._should_defer_exit(chat_id=chat_id, project_id=_pid):
                    self._request_deferred_exit(message_id=message_id, chat_id=chat_id, project_id=_pid)
                    self._reply_message(message_id, "✅ 已收到 /exit，将在当前任务完成后退出（不中断执行）")
                    return
                self._exit_current_mode(message_id, chat_id, project=project)
                return

            if not get_current_thread_id() and self.settings.thread_programming_enabled:
                pending, handler = self._is_one_shot_pending(chat_id, _pid, current_mode)
                if pending:
                    if not shell_fast_tracked:
                        self._dispatch_to_thread(message_id, chat_id, text, project, current_mode, handler)
                        return
                    is_in_programming = False

            self._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self._add_reaction(message_id, EmojiReaction.on_processing())
            if current_mode == InteractionMode.COCO:
                self._handle_coco_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.CLAUDE:
                self._handle_claude_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.AIDEN:
                self._handle_aiden_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.CODEX:
                self._handle_codex_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.GEMINI:
                self._handle_gemini_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.TTADK:
                self._ttadk_handler.handle_message(message_id, chat_id, text, project)
            else:
                self._show_help(message_id, chat_id)
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

        logger.info(
            "意图识别: %s (置信度: %.2f, 任务数: %d)",
            intent_result.primary_intent.value,
            intent_result.confidence,
            len(intent_result.tasks),
        )

        if intent_result.is_multi_task:
            self._execute_multi_tasks(message_id, chat_id, intent_result, project)
        else:
            self._execute_single_task(
                message_id,
                chat_id,
                intent_result.tasks[0] if intent_result.tasks else None,
                text,
                project,
                shell_fast_tracked=shell_fast_tracked,
            )

    def _execute_multi_tasks(
        self, message_id: str, chat_id: str, intent_result: IntentResult, project: Optional[ProjectContext] = None
    ):
        """执行多任务计划（逐步执行；遇到失败停止后续步骤）。"""
        tasks = intent_result.tasks

        task_list = [{"description": task.description or self._get_task_description(task)} for task in tasks]
        plan_msg = fmt.format_multi_task_plan(task_list)
        self._reply_message(message_id, plan_msg)

        self._add_reaction(message_id, EmojiReaction.on_multi_task_start())

        all_success = True
        for i, task in enumerate(tasks, 1):
            success = self._execute_task_step(
                message_id, chat_id, task, step_num=i, total_steps=len(tasks), project=project
            )

            if task.intent in {
                IntentType.ENTER_COCO,
                IntentType.ENTER_CLAUDE,
                IntentType.ENTER_AIDEN,
                IntentType.ENTER_CODEX,
                IntentType.ENTER_GEMINI,
                IntentType.TTADK_MESSAGE,
            }:
                break

            if not success:
                all_success = False
                self._reply_message(message_id, f"⚠️ 步骤 {i} 执行失败，后续步骤已取消")
                break

        if all_success:
            self._add_reaction(message_id, EmojiReaction.on_multi_task_done())
        else:
            self._add_reaction(message_id, EmojiReaction.on_error())

    def _execute_single_task(
        self,
        message_id: str,
        chat_id: str,
        task: Optional[TaskStep],
        original_text: str,
        project: Optional[ProjectContext] = None,
        *,
        shell_fast_tracked: bool = False,
    ):
        """执行单一任务步骤（模式切换/系统命令/引擎命令/执行 shell 等）。"""
        if not task:
            if self.settings.thread_programming_enabled and not get_current_thread_id():
                active_thread = self._find_active_thread(chat_id)
                if active_thread:
                    mode_display = active_thread.mode.upper() if active_thread.mode else "编程"
                    self._reply_message(
                        message_id,
                        f"💡 你有一个活跃的 {mode_display} 编程话题正在进行中\n\n"
                        "请在话题中回复继续对话\n"
                        "如需新建编程环境，请先发送对应的编程模式命令（如 /coco）",
                    )
                    return
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

        elif intent == IntentType.ENTER_AIDEN:
            self._enter_aiden_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_AIDEN:
            self._exit_aiden_mode(message_id, chat_id, project=project)

        elif intent == IntentType.AIDEN_MESSAGE:
            if data.get("command") == "info":
                self._show_aiden_info(message_id, chat_id, project)
            else:
                self._handle_aiden_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.ENTER_CODEX:
            self._enter_codex_mode(message_id, chat_id, project=project)

        elif intent == IntentType.ENTER_GEMINI:
            self._enter_gemini_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_CODEX:
            self._exit_codex_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_GEMINI:
            self._exit_gemini_mode(message_id, chat_id, project=project)

        elif intent == IntentType.CODEX_MESSAGE:
            if data.get("command") == "info":
                self._show_codex_info(message_id, chat_id, project)
            else:
                self._handle_codex_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.GEMINI_MESSAGE:
            if data.get("command") == "info":
                self._show_gemini_info(message_id, chat_id, project)
            else:
                self._handle_gemini_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.TTADK_MESSAGE:
            from ..mode import InteractionMode

            if data.get("command") == "info":
                self._show_ttadk_info(message_id, chat_id, project)
            elif str(original_text or "").strip().lower() in {"/ttadk", "/enter_ttadk"}:
                self._handle_ttadk_command(message_id, chat_id, project)
            else:
                _pid = project.project_id if project else None
                mode = self._mode_manager.get_mode(chat_id, project_id=_pid)
                if mode != InteractionMode.TTADK:
                    self._enter_ttadk_mode(message_id, chat_id, project=project)
                self._ttadk_handler.handle_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.SHOW_HELP:
            self._show_full_help(message_id, chat_id, project)

        elif intent == IntentType.SHOW_TOOLS:
            self._system_handler.show_tools_list(message_id, chat_id, project)

        elif intent == IntentType.TOOLS_STATUS:
            self._system_handler.show_tools_status(message_id, chat_id, project)

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

        elif intent == IntentType.ENTER_SPEC:
            requirement = data.get("requirement") or original_text
            self._start_spec_engine(message_id, chat_id, requirement, project)

        elif intent == IntentType.SPEC_STATUS:
            self._show_spec_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_SPEC:
            self._stop_spec_engine(message_id, chat_id, project)

        elif intent == IntentType.SPEC_PAUSE:
            self._pause_spec_engine(message_id, chat_id, project)

        elif intent == IntentType.SPEC_RESUME:
            self._resume_spec_engine(message_id, chat_id, project)

        elif intent == IntentType.SPEC_GUIDE:
            guide_message = data.get("message")
            if guide_message:
                self._update_spec_guidance(message_id, chat_id, guide_message, project)
            else:
                self._reply_message(message_id, "📝 请提供引导信息\n\n用法: `/spec_guide <引导描述>`")

        elif intent == IntentType.SHELL_COMMAND:
            working_dir = self._get_working_dir(chat_id)
            cmd = data.get("command") or original_text
            if shell_fast_tracked:
                # Already on shell queue — execute directly to avoid nested-task deadlock
                self._system_handler.execute_shell_and_reply(message_id, chat_id, cmd, working_dir, project)
            else:
                self._submit_shell_command(message_id, chat_id, cmd, working_dir, project)

            if project:
                project.add_conversation("user", cmd, message_id)
                self._context_manager.update_context(
                    project.project_id,
                    conversation={"role": "user", "content": cmd, "source_mode": "shell", "message_id": message_id},
                )

        elif intent == IntentType.UNKNOWN:
            self._reply_message(message_id, fmt.format_unknown_intent())

    def _execute_task_step(
        self,
        message_id: str,
        chat_id: str,
        task: TaskStep,
        step_num: int,
        total_steps: int,
        project: Optional[ProjectContext] = None,
    ) -> bool:
        """执行一个 TaskStep，并返回是否成功。

        主要用于 multi-task 场景的逐步执行。
        """
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
                    project_id=project_id, project_name=name, root_path=path, chat_id=chat_id
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
                    self._system_handler.execute_shell_and_reply(message_id, chat_id, cmd, working_dir, project)
                return True
            elif intent == IntentType.TTADK_MESSAGE:
                self._enter_ttadk_mode(message_id, chat_id, silent=True, project=project)
                self._reply_message(message_id, f"✅ 步骤 {step_num}: 已进入 TTADK 模式")
                return True

            else:
                return False

        except Exception as e:
            logger.error("执行步骤 %d 异常: %s", step_num, e)
            return False

    def _get_task_description(self, task: TaskStep) -> str:
        """为 TaskStep 生成可读描述（用于多任务计划展示）。"""
        intent = task.intent
        data = task.data

        if intent == IntentType.ENTER_COCO:
            return "进入 Coco 编程模式"
        elif intent == IntentType.ENTER_CLAUDE:
            return "进入 Claude 编程模式"
        elif intent == IntentType.ENTER_AIDEN:
            return "进入 Aiden 编程模式"
        elif intent == IntentType.ENTER_CODEX:
            return "进入 Codex 编程模式"
        elif intent == IntentType.ENTER_GEMINI:
            return "进入 Gemini 编程模式"
        elif intent == IntentType.TTADK_MESSAGE:
            return "进入 TTADK 编程模式"
        elif intent == IntentType.EXIT_COCO:
            return "退出 Coco 模式"
        elif intent == IntentType.EXIT_CLAUDE:
            return "退出 Claude 模式"
        elif intent == IntentType.EXIT_AIDEN:
            return "退出 Aiden 模式"
        elif intent == IntentType.EXIT_CODEX:
            return "退出 Codex 模式"
        elif intent == IntentType.EXIT_GEMINI:
            return "退出 Gemini 模式"
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
        """飞书 reaction 事件回调（当前无需处理，保留占位）。"""
        pass

    def _handle_chat_entered(self, data):
        """飞书 chat entered 事件回调（当前无需处理，保留占位）。"""
        pass

    def _handle_message_read(self, data):
        """飞书 message read 事件回调（当前无需处理，保留占位）。"""
        pass

    # ==================================================================
    # WebSocket lifecycle
    # ==================================================================
    def start(self):
        """启动 WS 长连接并进入重连循环。

        注意：该方法是阻塞的；通常在主线程调用。
        """
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .register_p2_im_message_reaction_created_v1(self._handle_reaction_created)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._handle_chat_entered)
            .register_p2_im_message_message_read_v1(self._handle_message_read)
            .register_p2_card_action_trigger(self._handle_card_action)
            .build()
        )

        self._client = _ObservedLarkWSClient(
            self.settings.app_id,
            self.settings.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG,
            on_activity=self._record_ws_activity,
        )

        self._message_cache.start_cleanup_thread()
        self._card_event_cache.start_cleanup_thread()
        self._start_ws_watchdog()

        logger.info("正在建立飞书长连接...")
        logger.info("多项目管理已启用")
        self._client.start()
