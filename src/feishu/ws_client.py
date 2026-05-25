"""Feishu WebSocket 客户端（核心路由枢纽）。

职责概览：
- 接收飞书 WS 事件（消息、卡片动作、反应等）并做基础校验/去重。
- 将用户消息路由到不同 handler（SMART/COCO/CLAUDE/SHELL/TTADK 以及 Deep/Spec 引擎）。
- 通过 `TaskScheduler` 提供：按项目串行、全局并发限制、系统命令快通道、背压与熔断。

关键设计点：
- `_FORWARDING_MAP` + `__getattr__`：把不同 mode 的实现解耦到 handlers 中，同时保持 ws_client 的调用面稳定。
- 兼容性：部分 lark-oapi 版本不包含完整的 callback model 类型；这里对仅用于类型标注的符号做了降级处理。
"""

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

# NOTE: lark-oapi 的 event callback models 在不同版本中并不完整。
# 本项目仅将 P2ImMessageReceiveV1 用于类型标注；运行时缺失不应导致 import 失败。
try:  # pragma: no cover
    from lark_oapi.event.callback.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1  # type: ignore
except (ImportError, AttributeError):  # pragma: no cover
    P2ImMessageReceiveV1 = Any  # type: ignore

from ..acp.manager import ACPSessionManager
from ..acp.telemetry import build_idle_health_config_for_manager
from ..agent.intent_recognizer import IntentRecognizer, IntentResult, TaskStep
from ..card.ui_text import UI_TEXT
from ..config import get_settings
from ..deep_engine import DeepEngineManager, ProgressReporter
from ..project import (
    ContextSourceMode,
    MessageLinker,
    MessageProjectMapper,
    ProjectContext,
    ProjectContextManager,
    ProjectManager,
)
from ..slock_engine import SlockEngineManager
from ..spec_engine import SpecEngineManager, SpecReporter
from ..tasking import TaskPriority, TaskScheduler, TaskSpec
from ..thread import get_current_thread_id, get_thread_manager
from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException
from ..utils.errors import get_error_detail
from ..utils.rate_limit import RateLimiter, RateLimitExceededException
from ..utils.trace import TraceContext, configure_logging_with_trace
from ..worktree_engine.manager import WorktreeManager
from .action_dispatcher import ActionDispatcher
from .emoji import EmojiReaction
from .handler_context import HandlerContext
from .handlers import (
    AidenModeHandler,
    ClaudeModeHandler,
    CocoModeHandler,
    CodexModeHandler,
    DeepHandler,
    DiagnosticsHandler,
    GeminiModeHandler,
    ProjectHandler,
    SlockHandler,
    SpecHandler,
    SystemHandler,
    TTADKModeHandler,
)
from .handlers.worktree import WorktreeHandler
from .image_handler import FeishuImageHandler
from .message_cache import MessageCache
from .renderers.deep_renderer import DeepRenderer
from .renderers.spec_renderer import SpecRenderer
from .renderers.worktree_renderer import WorktreeRenderer
from .slash_command_parser import CommandMatch, SlashCommandParser
from .ws_card_action_handler import CardActionInspector, classify_card_action_error
from .ws_event_router import MessageIngressGuard, classify_ws_error
from .ws_health import WSHealthMonitor
from .ws_lifecycle import ObservedLarkWSClient
from .ws_resource_manager import EngineResourceGroup

logger = logging.getLogger(__name__)

# Sentinel used to distinguish "caller didn't provide command_match" from
# "caller provided command_match=None". This ensures request-scoped SSOT:
# parse exactly once at WS ingress, then thread the result through.
_COMMAND_MATCH_MISSING: object = object()


_READONLY_CARD_ACTIONS = {
    "deep_expand", "deep_collapse", "deep_mode_full", "deep_mode_compact", "deep_expand_ac", "deep_collapse_ac",
    "spec_expand", "spec_collapse", "spec_mode_full", "spec_mode_compact", "spec_expand_ac", "spec_collapse_ac",
}


class FeishuWSClient:
    """Feishu WS Client 的服务端运行态。

    该类面向"长连接服务"场景：
    - 内部会初始化 scheduler / handler / cache，并在 `start()` 后进入事件循环。
    - `close()` 提供 best-effort 资源回收（线程/缓存/调度器等）。
    """

    def __init__(self, message_callback: Callable[[str, str, str, Optional[str]], None]):
        self.settings = get_settings()
        self.message_callback = message_callback
        self._client: Optional[lark.ws.Client] = None
        self._closed = False
        self._api_client: Optional[lark.Client] = None

        # ACPSessionManager: IdleHealth 相关协作者统一通过 IdleHealthConfig 注入，
        # 避免在构造函数中直接依赖具体 Telemetry/Service 实现。
        idle_health_cfg = build_idle_health_config_for_manager()

        self._coco_manager = ACPSessionManager(
            "coco",
            session_timeout=self.settings.coco_session_timeout,
            keepalive_interval=self.settings.acp_keepalive_interval,
            idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s,
            idle_health_config=idle_health_cfg,
        )
        self._claude_manager = ACPSessionManager(
            "claude",
            session_timeout=self.settings.claude_session_timeout,
            keepalive_interval=self.settings.acp_keepalive_interval,
            idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s,
            idle_health_config=idle_health_cfg,
        )
        self._aiden_manager = ACPSessionManager(
            "aiden",
            session_timeout=self.settings.coco_session_timeout,
            keepalive_interval=self.settings.acp_keepalive_interval,
            idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s,
            idle_health_config=idle_health_cfg,
        )
        self._codex_manager = ACPSessionManager(
            "codex",
            session_timeout=self.settings.coco_session_timeout,
            keepalive_interval=self.settings.acp_keepalive_interval,
            idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s,
            idle_health_config=idle_health_cfg,
        )
        self._gemini_manager = ACPSessionManager(
            "gemini",
            session_timeout=self.settings.coco_session_timeout,
            keepalive_interval=self.settings.acp_keepalive_interval,
            idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s,
            idle_health_config=idle_health_cfg,
        )
        self._ttadk_manager = ACPSessionManager(
            "ttadk",
            session_timeout=self.settings.coco_session_timeout,
            keepalive_interval=self.settings.acp_keepalive_interval,
            idle_healthcheck_s=self.settings.acp_session_idle_healthcheck_s,
            idle_health_config=idle_health_cfg,
        )
        self._intent_recognizer = IntentRecognizer()
        self._message_cache = MessageCache(ttl=self.settings.message_cache_ttl, max_size=self.settings.message_cache_max_size, cleanup_interval=60)
        self._message_ingress_guard = MessageIngressGuard(
            message_cache=self._message_cache,
            message_expire_seconds=self.settings.message_expire_seconds,
        )
        self._card_event_cache = MessageCache(ttl=self.settings.message_cache_ttl, max_size=self.settings.message_cache_max_size, cleanup_interval=60)
        # Card action dedupe (user rapid clicks): short TTL, per-action key.
        self._card_action_dedup_cache = MessageCache(ttl=self.settings.card.action_dedup_ttl, max_size=self.settings.card.action_dedup_max_size, cleanup_interval=30)
        # Chat lock gate: initialized after handler_ctx is available (see below).
        self._chat_lock_gate = None  # type: ignore[assignment]
        self._scheduler = TaskScheduler(
            max_concurrent=self.settings.task_scheduler_max_concurrent,
            per_key_concurrency=self.settings.task_scheduler_per_key_concurrency,
            system_concurrency=self.settings.system_command_concurrency,
            thread_name_prefix="ghost_worker",
        )
        # Spec Engine limits: e.g. 50 calls per second, max 100 capacity
        self._scheduler.register_policy(
            "spec_command",
            rate_limiter=RateLimiter(capacity=self.settings.spec_rate_limit_capacity, fill_rate=self.settings.spec_rate_limit_fill_rate),
            circuit_breaker=CircuitBreaker(failure_threshold=self.settings.spec_circuit_breaker_threshold, recovery_timeout=self.settings.spec_circuit_breaker_recovery),
        )
        self._WORKING_DIRS_MAX_SIZE = 500
        self._working_dirs: OrderedDict[str, str] = OrderedDict()
        self._working_dir_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        self._project_manager = ProjectManager()
        self._project_manager.on_eviction = self._on_project_evicted
        self._message_mapper = MessageProjectMapper()
        self._message_linker = MessageLinker()

        from ..mode import ModeManager

        self._mode_manager = ModeManager()
        # Inject mode_manager into project_manager so LRU eviction
        # automatically cleans up stale _project_modes entries (AC-R01).
        self._project_manager.mode_manager = self._mode_manager
        self._thread_manager = get_thread_manager()
        self._thread_manager._on_evict = self._on_thread_evicted

        self._image_handler: Optional[FeishuImageHandler] = None
        self._pending_image_keys: dict[str, list[str]] = {}
        self._pending_image_only: set[str] = set()  # message_ids that are image-only (no user text)
        self._pending_image_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._enable_streaming = self.settings.streaming_enabled

        self._ws_health_monitor = WSHealthMonitor(self, self.settings)

        self._deep_engine_manager = DeepEngineManager()
        self._progress_reporter = ProgressReporter()
        self._spec_engine_manager = SpecEngineManager()
        self._spec_reporter = SpecReporter()
        self._slock_engine_manager = SlockEngineManager()

        self._context_manager = ProjectContextManager()

        # Initialize lock managers before HandlerContext construction
        _repo_lock_mgr = None
        try:
            from ..repo_lock import get_repo_lock_manager
            _repo_lock_mgr = get_repo_lock_manager()
        except Exception:
            logger.warning("RepoLockManager initialization failed", exc_info=True)

        _chat_lock_mgr = None
        try:
            from ..chat_lock import get_chat_lock_manager
            _chat_lock_mgr = get_chat_lock_manager()
        except Exception:
            logger.warning("ChatLockManager initialization failed", exc_info=True)

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
            spec_engine_manager=self._spec_engine_manager,
            spec_reporter=self._spec_reporter,
            slock_engine_manager=self._slock_engine_manager,
            thread_manager=self._thread_manager,

            image_handler_factory=self._get_image_handler,
            working_dirs=self._working_dirs,
            working_dir_lock=self._working_dir_lock,
            pending_image_keys=self._pending_image_keys,
            pending_image_lock=self._pending_image_lock,
            enable_streaming=self._enable_streaming,
            repo_lock_manager=_repo_lock_mgr,
            chat_lock_manager=_chat_lock_mgr,
        )

        # Instantiate handlers (temp locals for registry population)
        coco_handler = CocoModeHandler(self._handler_ctx)
        claude_handler = ClaudeModeHandler(self._handler_ctx)
        aiden_handler = AidenModeHandler(self._handler_ctx)
        codex_handler = CodexModeHandler(self._handler_ctx)
        gemini_handler = GeminiModeHandler(self._handler_ctx)
        ttadk_handler = TTADKModeHandler(self._handler_ctx)
        deep_handler = DeepHandler(self._handler_ctx)
        deep_handler.renderer = DeepRenderer(deep_handler)
        spec_handler = SpecHandler(self._handler_ctx)
        spec_handler.renderer = SpecRenderer(spec_handler)
        project_handler = ProjectHandler(self._handler_ctx)
        system_handler = SystemHandler(self._handler_ctx)
        worktree_handler = WorktreeHandler(self._handler_ctx)
        worktree_handler._renderer = WorktreeRenderer(worktree_handler)
        diagnostics_handler = DiagnosticsHandler(self._handler_ctx)
        slock_handler = SlockHandler(self._handler_ctx)

        # ------------------------------------------------------------------
        # Populate registry containers in context
        # ------------------------------------------------------------------
        # Bind handlers directly on instance for backward compatibility (especially for tests)
        self._coco_handler = coco_handler
        self._claude_handler = claude_handler
        self._aiden_handler = aiden_handler
        self._codex_handler = codex_handler
        self._gemini_handler = gemini_handler
        self._ttadk_handler = ttadk_handler
        self._deep_handler = deep_handler
        self._spec_handler = spec_handler
        self._project_handler = project_handler
        self._system_handler = system_handler
        self._worktree_handler = worktree_handler
        self._diagnostics_handler = diagnostics_handler
        self._slock_handler = slock_handler

        self._handler_ctx.managers.update({
            "coco": self._coco_manager,
            "claude": self._claude_manager,
            "aiden": self._aiden_manager,
            "codex": self._codex_manager,
            "gemini": self._gemini_manager,
            "ttadk": self._ttadk_manager,
        })
        self._handler_ctx.handlers.update({
            "coco": coco_handler,
            "claude": claude_handler,
            "aiden": aiden_handler,
            "codex": codex_handler,
            "gemini": gemini_handler,
            "ttadk": ttadk_handler,
            "deep": deep_handler,
            "spec": spec_handler,
            "project": project_handler,
            "system": system_handler,
            "worktree": worktree_handler,
            "diagnostics": diagnostics_handler,
            "slock": slock_handler,
        })

        # Subscribe to hard-timeout reclaim events on RepoLockManager
        # (fire-and-forget notification to the displaced lock holder chat).
        repo_lock_mgr = self._handler_ctx.repo_lock_manager
        if repo_lock_mgr is not None:
            _send_card = system_handler.send_card_to_chat  # narrow reference

            def _notify_hard_timeout_reclaim(root_path: str, holder_chat_id: str) -> None:
                try:
                    from pathlib import Path as _Path

                    from ..card.builders.lock import build_lock_reclaim_notify_card
                    repo_name = _Path(root_path).name or root_path
                    _send_card(
                        holder_chat_id,
                        build_lock_reclaim_notify_card(
                            repo_name, reason="hard_timeout",
                            hard_timeout_seconds=getattr(self.settings, "repo_lock_hard_timeout", None),
                        ),
                    )
                except Exception as _exc:
                    logger.warning(
                        "Failed to notify hard-timeout reclaim to chat=%s: %s",
                        holder_chat_id[:12], _exc,
                    )

            repo_lock_mgr.on_reclaim.subscribe(_notify_hard_timeout_reclaim)

            # Subscribe to lock release events — notify previously blocked chats.
            def _notify_lock_released(root_path: str, blocked_chat_ids: set) -> None:
                try:
                    import json as _json
                    from pathlib import Path as _Path

                    from ..card.builders.lock_common import _compute_command_sig
                    from ..card.ui_text import UI_TEXT
                    repo_name = _Path(root_path).name or root_path
                    _text = UI_TEXT["repo_lock_released_notify"].format(repo_name=repo_name)
                    _btn = {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📊 查看状态"},
                        "type": "default",
                        "value": {"action": "retry_command", "_t": "/status", "_s": _compute_command_sig("/status")},
                    }
                    _card = _json.dumps({
                        "config": {"wide_screen_mode": True},
                        "elements": [
                            {"tag": "markdown", "content": _text},
                            {
                                "tag": "column_set",
                                "flex_mode": "none",
                                "background_style": "default",
                                "columns": [
                                    {
                                        "tag": "column",
                                        "width": "weighted",
                                        "weight": 1,
                                        "elements": [_btn],
                                    }
                                ],
                            },
                        ],
                    }, ensure_ascii=False)
                    for _cid in blocked_chat_ids:
                        try:
                            _send_card(_cid, _card)
                        except Exception as _inner:
                            logger.debug("Failed to notify release to chat=%s: %s", _cid[:12], _inner)
                except Exception as _exc:
                    logger.warning("Failed to send lock release notifications: %s", _exc)

            repo_lock_mgr.on_release.subscribe(_notify_lock_released)

        # Initialize ChatLockGate (ingress-level chat-lock interception).
        from .chat_lock_gate import ChatLockGate
        _clm = getattr(self._handler_ctx, "chat_lock_manager", None)
        _lock_dedup = MessageCache(ttl=30, max_size=10_000, cleanup_interval=60)
        self._chat_lock_gate = ChatLockGate(_clm, _lock_dedup, host=self)

        # Bind forwarding methods directly on instance (replaces __getattr__ dispatch)
        from .router import bind_forwarding_methods
        bind_forwarding_methods(self, self._handler_ctx)

        # ------------------------------------------------------------------
        # Control-plane (deferred /exit, system command gate)
        # ------------------------------------------------------------------
        from .control_plane import ControlPlane
        self._control_plane = ControlPlane(
            scheduler=self._scheduler,
            project_manager=self._project_manager,
            exit_handler_fn=lambda *a, **kw: self._exit_current_mode(*a, **kw),
        )
        self._scheduler.add_listener(self._control_plane.on_scheduler_event)
        # Backward-compat aliases for tests
        self._system_cmd_gate_lock = self._control_plane._system_cmd_gate_lock
        self._system_cmd_inflight_by_chat = self._control_plane._system_cmd_inflight_by_chat

        # --- Message Dispatcher ---
        from .dispatcher import MessageDispatcher
        self._message_dispatcher = MessageDispatcher(self)

        # --- Action Dispatcher ---
        from .action_registry import init_action_registry
        self._action_dispatcher = ActionDispatcher()
        init_action_registry(self)

        # Configure trace logging
        configure_logging_with_trace()

    def _register_action(self, handler: Callable, exact: Optional[str] = None, prefix: Optional[str] = None):
        """Register a card action handler."""
        self._action_dispatcher.register(handler, exact, prefix)

    def close(self):
        """Best-effort cleanup for background resources."""
        self._closed = True

        self._ws_health_monitor.stop_watchdog()

        # Stop chat lock gate dedup cache cleanup
        try:
            self._chat_lock_gate.close()
        except Exception:
            logger.debug("failed to close chat_lock_gate", exc_info=True)

        try:
            self._control_plane.stop()
        except Exception:
            logger.debug("failed to stop control_plane", exc_info=True)

        # 1) Stop long-running engines first (they may hold ACP subprocesses)
        deep_resources = EngineResourceGroup("deep_engine", self._deep_engine_manager)
        spec_resources = EngineResourceGroup("spec_engine", self._spec_engine_manager)
        deep_engines = deep_resources.stop_running_engines()
        spec_engines = spec_resources.stop_running_engines()

        # Give running engines a short grace period to exit run loops before hard cleanup.
        EngineResourceGroup.wait_stopped(deep_engines)
        EngineResourceGroup.wait_stopped(spec_engines)

        try:
            self._message_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止message_cache清理线程失败: %s", get_error_detail(e))

        try:
            self._card_event_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止card_event_cache清理线程失败: %s", get_error_detail(e))

        try:
            self._card_action_dedup_cache.stop_cleanup_thread()
        except Exception as e:
            logger.debug("停止card_action_dedup_cache清理线程失败: %s", get_error_detail(e))

        # 2) Close per-chat programming sessions (kills ACP agent subprocesses)
        for name, mgr in self._handler_ctx.managers.items():
            try:
                mgr.cleanup_all()
            except Exception as e:
                logger.debug("清理%s_session_manager失败: %s", name, get_error_detail(e))

        deep_resources.cleanup_all()
        spec_resources.cleanup_all()

        try:
            self._thread_manager.close()
        except Exception as e:
            logger.debug("清理thread_manager失败: %s", get_error_detail(e))

        try:
            self._scheduler.stop(wait=True, shutdown_executor=True)
        except Exception as e:
            logger.debug("停止scheduler失败: %s", get_error_detail(e))

        # Best-effort shutdown lock-manager daemon threads so non-Application
        # callers (e.g. tests) do not leak background threads.
        try:
            from ..chat_lock import shutdown_if_active as _chat_sd
            _chat_sd()
        except Exception:
            logger.debug("ChatLockManager shutdown in close() skipped", exc_info=True)
        try:
            from ..repo_lock import shutdown_if_active as _repo_sd
            _repo_sd()
        except Exception:
            logger.debug("RepoLockManager shutdown in close() skipped", exc_info=True)

    def _on_thread_evicted(self, ctx) -> None:
        for mgr in self._handler_ctx.managers.values():
            try:
                mgr.end_session(ctx.chat_id, project_id=ctx.project_id, thread_id=ctx.thread_root_id)
            except Exception:
                logger.debug("failed to end ACP session during cleanup", exc_info=True)

    def _on_project_evicted(self, evicted_chat_id: str, project_name: str, project_id: str) -> None:
        """Notify a chat that its project binding was evicted due to LRU capacity.

        Convergence: cleans up ACP sessions for the evicted project, then sends
        a rebind notification card.  Both run in a daemon thread to avoid blocking
        ProjectManager's critical section (which holds _lock when calling
        this callback).
        """
        def _send_notification():
            # Phase 1: Clean up ACP sessions for the evicted project.
            for mgr in self._handler_ctx.managers.values():
                try:
                    mgr.end_session(evicted_chat_id, project_id=project_id)
                except Exception:
                    logger.debug("failed to end session for evicted project", exc_info=True)

            # Phase 2: Send rebind notification card.
            try:
                from ..card.builders.project import ProjectBuilder
                content = UI_TEXT["eviction_notify_body"].format(name=project_name)
                buttons = [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": UI_TEXT["eviction_notify_btn_rebind"]},
                        "type": "primary",
                        "value": {"action": "show_board"},
                    }
                ]
                msg_type, card_json = ProjectBuilder.build_project_response_card(
                    project=None,
                    title=UI_TEXT["eviction_notify_title"],
                    content=content,
                    show_buttons=False,
                    extra_buttons=buttons,
                )
                self.reply(evicted_chat_id, card_json, msg_type=msg_type, chat_id=evicted_chat_id)
            except Exception as send_err:
                # Fallback to plain text
                try:
                    msg = UI_TEXT["ws_project_eviction_notify"].format(name=project_name)
                    self.reply(evicted_chat_id, msg, msg_type="text", chat_id=evicted_chat_id)
                except Exception:
                    logger.debug("failed to send eviction fallback notification", exc_info=True)
                logger.warning("Failed to send LRU eviction notification to %s: %s", evicted_chat_id[:12], send_err)

        threading.Thread(target=_send_notification, daemon=True).start()

    def _is_message_expired(self, create_time: int) -> bool:
        """判断消息是否过期。

        飞书历史消息可能会被 WS 重放；这里通过 `create_time` 过滤掉过旧消息，
        避免触发重复执行（尤其是 shell/编程任务）。
        """
        return self._message_ingress_guard.is_message_expired(create_time)

    def _is_duplicate_message(self, message_id: str) -> bool:
        """消息去重：基于 `MessageCache` 判断是否重复处理。"""
        return self._message_ingress_guard.is_duplicate_message(message_id)

    def _get_api_client(self) -> lark.Client:
        """延迟构造 `lark_oapi.Client`（用于调用消息/卡片 API）。"""
        if self._api_client is None:
            self._api_client = (
                lark.Client.builder()
                .app_id(self.settings.app_id)
                .app_secret(self.settings.app_secret)
                .log_level(lark.LogLevel.INFO)
                .timeout(30)  # 30s timeout for all API calls (card delivery protection)
                .build()
            )
        return self._api_client



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

    def reply(self, message_id: str, content, msg_type: str = "text", chat_id: Optional[str] = None):
        """轻量回复封装：兼容旧调用路径，按 msg_type 委托到对应的新 API。"""
        if chat_id is not None:
            logger.warning("chat_id 参数已废弃且不再生效，请移除该参数")
        if msg_type == "text":
            self._reply_text(message_id, content)
        else:
            self._reply_card(message_id, content)

    def add_reaction(self, message_id: str, emoji_type: str):
        """轻量表情反馈封装：委托到 handler 的 `add_reaction`。"""
        self._add_reaction(message_id, emoji_type)

    def send_lock_conflict_card(
        self, e, message_id: str, command_text: str, *, retry_count: int = 0,
    ) -> None:
        """Public facade: send a repo-lock conflict card via the system handler.

        Delegates to ``SystemHandler.send_lock_conflict_card`` obtained via
        ``_get_handler("system")``, consistent with other handler access
        patterns (e.g. ``_switch_project``).
        """
        handler = self._get_handler("system")
        if handler:
            handler.send_lock_conflict_card(e, message_id, command_text, retry_count=retry_count)
        else:
            from .handlers.lock_helper import logger as _lock_logger
            _lock_logger.warning("send_lock_conflict_card: _system_handler unavailable, cannot notify user")
            # Fallback: send plain text notification
            self._reply_text(message_id, f"🔒 {str(e) or 'lock conflict'}")

    def _get_handler(self, key: str) -> Any:
        return self._handler_ctx.handlers.get(key)

    def _switch_project(self, message_id: str, chat_id: str, name: str, auto_enter_coco: bool = True):
        """切换当前 chat 的 active project，并可选自动进入 Coco 模式。"""
        project_handler = self._get_handler("project")
        if project_handler:
            project_handler.switch_project(
                message_id,
                chat_id,
                name,
                auto_enter_coco=auto_enter_coco,
                coco_handler=self._get_handler("coco"),
                claude_handler=self._get_handler("claude"),
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
    def _is_spec_command(text: str) -> bool:
        """判断是否为 Spec Engine 命令。"""
        return SystemHandler.is_spec_command(text)

    def _is_slock_command(self, text: str, chat_id: str = "") -> "bool | str":
        """判断是否为 Slock Engine 命令。"""
        from ..slock_engine.slash_commands import is_slock_command
        manager = getattr(self, '_slock_engine_manager', None)
        return is_slock_command(text, chat_id=chat_id, manager=manager)

    def _is_slock_active(self, chat_id: str) -> bool:
        """Check if a chat has an active slock engine."""
        manager = getattr(self, '_slock_engine_manager', None)
        if manager is None:
            return False
        return manager.is_slock_active(chat_id)

    def _is_slock_managed_chat(self, chat_id: str) -> bool:
        """Check if a chat is registered as managed by the slock engine."""
        manager = getattr(self, '_slock_engine_manager', None)
        if manager is None:
            return False
        return manager.is_managed_chat(chat_id)

    # ------------------------------------------------------------------
    # Passive mode auto-activate helpers
    # ------------------------------------------------------------------

    _chat_locks: dict[str, threading.Lock] = {}
    _chat_locks_meta: dict[str, float] = {}  # chat_id → last_used timestamp
    _chat_locks_guard = threading.Lock()

    def _get_chat_lock(self, chat_id: str) -> threading.Lock:
        """Get or create a per-chat activation lock."""
        with self._chat_locks_guard:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = threading.Lock()
            self._chat_locks_meta[chat_id] = time.time()
            return self._chat_locks[chat_id]

    @classmethod
    def _gc_chat_locks(cls, max_age: float = 300.0) -> int:
        """Remove chat locks unused for more than max_age seconds."""
        now = time.time()
        removed = 0
        with cls._chat_locks_guard:
            stale = [
                cid for cid, ts in cls._chat_locks_meta.items()
                if now - ts > max_age
            ]
            for cid in stale:
                # Only remove if lock is not currently held
                lock = cls._chat_locks.get(cid)
                if lock and not lock.locked():
                    del cls._chat_locks[cid]
                    del cls._chat_locks_meta[cid]
                    removed += 1
        return removed

    def _should_auto_activate_slock(self, chat_id: str, text: str, *, chat_type: str = "group") -> bool:
        """Check if message should trigger slock auto-activation.

        Short-circuits for already-managed chats to avoid redundant classification
        overhead. Only performs task classification for unmanaged group chats.

        Returns True if:
        - Chat is already managed by slock (short-circuit), or
        - Chat is an unmanaged group AND text is classified as a task
        """
        if chat_type != "group":
            return False
        # Short-circuit: already managed chats don't need classification
        if self._is_slock_managed_chat(chat_id):
            return True
        from src.slock_engine.task_classifier import TaskClassifier

        return TaskClassifier.is_task(text)

    def _auto_activate_slock(
        self, chat_id: str, text: str, project: "Optional[ProjectContext]" = None
    ) -> tuple[bool, str]:
        """Auto-activate slock for an unmanaged chat on first valid task message.

        Returns a tuple of (success, reason):
        - success: True if activation succeeded, False if denied/failed.
        - reason: A string indicating the result. One of:
            - ACTIVATION_ALLOWED: activation succeeded
            - ACTIVATION_DENIED_RATE_LIMIT: rate limit exceeded
            - ACTIVATION_DENIED_ADMIN_REQUIRED: admin-only policy
            - ACTIVATION_DENIED_NOT_WHITELISTED: not in whitelist
            - "error": activation failed with exception

        Idempotent: if the chat becomes managed between check and call, the
        slock handler's activate_slock will detect the existing engine and
        short-circuit. Uses _auto_activate_lock to prevent concurrent bootstrap
        for the same chat.

        Guarded by ActivationGuard for permission and rate-limit checks.
        """
        from src.slock_engine.activation_guard import (
            ACTIVATION_ALLOWED,
            get_activation_guard,
        )

        # Permission and rate-limit gate
        guard = get_activation_guard()
        from src.thread.manager import get_current_sender_id
        sender_id = get_current_sender_id() or ""
        allowed, reason = guard.can_auto_activate(sender_id, chat_id, self.settings)
        if not allowed:
            logger.debug(
                "Auto-activate blocked by guard for user=%s chat=%s: reason=%s",
                sender_id, chat_id, reason,
            )
            return False, reason

        chat_lock = self._get_chat_lock(chat_id)
        with chat_lock:
            # Double-check after acquiring lock
            if self._is_slock_managed_chat(chat_id):
                return True, ACTIVATION_ALLOWED
            try:
                # Use a synthetic message_id since passive activation has no
                # user-initiated command message to reply to.
                # skip_guard_check=True: guard was already checked above to avoid
                # double consumption of rate-limit budget.
                synthetic_msg_id = f"passive-activate-{chat_id}"
                success = self._slock_handler.activate_slock(
                    message_id=synthetic_msg_id,
                    chat_id=chat_id,
                    requirement=text,
                    project=project,
                    skip_guard_check=True,
                )
                return success, ACTIVATION_ALLOWED if success else "error"
            except Exception:
                logger.warning(
                    "Failed to auto-activate slock for chat %s", chat_id, exc_info=True
                )
                # Send user-friendly card notification
                try:
                    from src.slock_engine.card_templates.common import build_error_state_card
                    card = build_error_state_card(
                        title="任务暂时无法自动处理",
                        error_msg="你的消息暂时无法被自动分配，正在通过其他方式处理。请稍后重试，或直接描述你的需求让系统尝试其他方式处理。",
                    )
                    self._system_handler.send_card_to_chat(
                        chat_id,
                        json.dumps(card, ensure_ascii=False),
                    )
                except Exception as card_err:
                    logger.warning(
                        "Failed to send activation failure card to chat %s: %s",
                        chat_id, card_err,
                    )
                return False, "error"

    @staticmethod
    def _is_interceptable_command_match(command_match: CommandMatch | None) -> bool:
        """SSOT variant: decide based on request-scoped CommandMatch."""
        return SystemHandler.is_interceptable_command_match(command_match)

    def _is_worktree_awaiting_goal(self, project: "ProjectContext") -> bool:
        """Return True when worktree journey is awaiting a goal.

        具体判定逻辑下沉到 ``WorktreeManager.is_awaiting_goal``，避免在
        WS 层拼装布尔条件，统一依赖 WorktreeRuntimeState / journey 状态机。
        """

        if not getattr(project, "project_id", None):
            return WorktreeManager.is_awaiting_goal(getattr(project, "worktree_state", None))
        try:
            state = self._worktree_handler._worktree_manager().get_state(project)
        except Exception:
            state = getattr(project, "worktree_state", None)
        return WorktreeManager.is_awaiting_goal(state)

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
            if not isinstance(project_id, str):
                project_id = None
            if project_id:
                project = self._project_manager.get_project_for_chat(project_id, chat_id)
                if project:
                    self._project_manager.set_active_project(chat_id, project_id)
                    logger.info("通过消息引用切换到项目: %s", project.project_name)

                    # Resolve mode from ModeManager (single source of truth).
                    _proj_mode = self._mode_manager.get_mode(chat_id, project_id=project_id)
                    if _proj_mode.value in {"coco", "claude", "aiden", "codex", "gemini", "ttadk"}:
                        auto_enter_mode = _proj_mode.value

                    if auto_enter_mode:
                        logger.info("自动进入 %s 模式 (回复编程消息)", auto_enter_mode)

                    return project, auto_enter_mode

        bound_project = self._project_manager.find_by_bound_chat_id(chat_id)
        if bound_project is not None:
            return bound_project, None

        return self._project_manager.get_active_project(chat_id), None

    def _handle_message(self, data: P2ImMessageReceiveV1):
        """飞书消息事件入口：只做轻量前置判断，然后交给 scheduler 异步处理。"""
        try:
            msg = data.event.message
            message_id = msg.message_id
            chat_id = msg.chat_id
        except (AttributeError, TypeError):
            message_id = None
            chat_id = "unknown"

        # Extract chat_type for p2p privilege detection
        chat_type = getattr(getattr(data.event, "message", None), "chat_type", None)
        is_p2p = chat_type == "p2p"

        # Extract sender_id for explicit passing via TaskSpec
        _raw_sender = getattr(
            getattr(getattr(data.event, "sender", None), "sender_id", None),
            "open_id", None,
        )
        _sender_id = _raw_sender if isinstance(_raw_sender, str) else ""

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

            if not project_id:
                for ref in (parent_id, root_id):
                    if ref:
                        project_id = self._message_mapper.get_project_id(ref)
                        if not isinstance(project_id, str):
                            project_id = None
                        if project_id:
                            break
        except (AttributeError, KeyError, TypeError):
            project_id = None

        if not project_id:
            try:
                active = self._project_manager.get_active_project(chat_id)
                project_id = active.project_id if active else None
                if not isinstance(project_id, str):
                    project_id = None
            except (AttributeError, KeyError):
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
                is_p2p=is_p2p,
                sender_id=_sender_id,
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
                logger.warning(f"Backpressure applied: {get_error_detail(e)}")
                if is_spec:
                    self._reply_text(message_id, UI_TEXT["ws_backpressure_spec"])
                else:
                    self._reply_text(message_id, UI_TEXT["ws_backpressure_generic"])
                return
            try:
                if message_id:
                    self._message_linker.link_task(message_id, handle.run_id)
            except (KeyError, AttributeError, RuntimeError) as e:
                logger.debug("link_task失败(message): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, get_error_detail(e))

    def _is_system_command_message(self, data: P2ImMessageReceiveV1) -> bool:
        """Check if the message is a system command that should bypass project queue.

        All slash commands (``/xxx``) are system commands: they should never
        block behind long-running Coco/Claude programming tasks on the project
        queue.  This includes ``/stop_deep``, ``/exit``, ``/spec_status``, etc.
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
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
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
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
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
        from ..thread import set_current_is_p2p, set_current_sender_id, set_current_sender_name, set_current_thread_id

        try:
            event = data.event
            message = event.message
            message_id = message.message_id
            chat_id = message.chat_id
            request_id = self._ensure_request_id(message_id, chat_id=chat_id)

            # sender_id is carried in task_ctx.spec (set at submit time);
            # fall back to event extraction only when task_ctx is unavailable.
            _sender_id = (
                task_ctx.spec.sender_id
                if task_ctx and hasattr(task_ctx, "spec") and task_ctx.spec.sender_id
                else (
                    getattr(
                        getattr(getattr(event, "sender", None), "sender_id", None),
                        "open_id", None,
                    ) or ""
                )
            )
            # Propagate to thread-local so downstream handlers (e.g. /lock) can access it.
            set_current_sender_id(_sender_id)
            # Resolve display name via cached Feishu contact API lookup;
            # falls back to truncated sender_id if unavailable.
            from .user_cache import resolve_display_name
            _display_name = resolve_display_name(_sender_id, self._get_api_client) if _sender_id else ""
            set_current_sender_name(_display_name or (_sender_id[:8] if _sender_id else ""))
            _is_p2p = task_ctx.spec.is_p2p if task_ctx and hasattr(task_ctx, "spec") else False
            set_current_is_p2p(_is_p2p)
            chat_type = "p2p" if _is_p2p else "group"

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

            # 2b. Slash parsing is request-scoped: parse once and reuse.
            # This match becomes the single source of truth for downstream
            # slash consumers (gate/system/worktree).
            try:
                command_match = SlashCommandParser.parse(text)
            except Exception:
                command_match = None

            # 2c. Chat lock interception (fail-close: non-admin blocked on exception).
            # Use the request-scoped CommandMatch instead of re-parsing raw text.
            if self._chat_lock_gate.check(
                chat_id,
                _sender_id,
                message_id,
                command_match=command_match,
            ):
                return

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
                            project = self._project_manager.get_project_for_chat(_tctx.project_id, chat_id) or self._project_manager.get_active_project(chat_id)
                        logger.info(
                            "[Thread] Safety-net resolved mode: root=%s canonical=%s mode=%s",
                            _root[:12], _tctx.thread_root_id[:12], auto_enter_mode,
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
                _root_id = getattr(message, "root_id", None)
                self._dispatch_empty_text(message_id, chat_id, project, task_ctx, root_id=_root_id)
                return

            self._dispatch_message_logic(
                message_id,
                chat_id,
                text,
                project,
                auto_enter_mode,
                command_match=command_match,
                is_image_only=is_image_only,
                shell_fast_tracked=shell_fast_tracked,
                chat_type=chat_type,
            )

        except asyncio.TimeoutError as e:
            logger.warning("处理消息超时: %s", get_error_detail(e))
            try:
                self._reply_text(message_id, UI_TEXT["ws_message_timeout"])
            except (RuntimeError, OSError, TimeoutError, TypeError, ValueError):
                classify_ws_error(RuntimeError("reply timeout failed"), phase="dispatch")
                logger.debug("failed to reply timeout message", exc_info=True)
        except (RuntimeError, OSError, TimeoutError, TypeError, ValueError) as e:
            classification = classify_ws_error(e, phase="dispatch")
            if classification.action == WSErrorAction.REPLY_INTERNAL_ERROR:
                logger.error("处理消息异常: %s", get_error_detail(e), exc_info=True)
                try:
                    self._reply_text(message_id, UI_TEXT["ws_message_internal_error"])
                except (RuntimeError, OSError, TimeoutError, TypeError, ValueError):
                    classify_ws_error(RuntimeError("reply internal error failed"), phase="best_effort_notify")
                    logger.debug("failed to reply internal error message", exc_info=True)
            elif classification.action == WSErrorAction.LOG_AND_CONTINUE:
                logger.debug("处理消息 best-effort 失败: %s", get_error_detail(e), exc_info=True)
            elif classification.action == WSErrorAction.PROPAGATE:
                logger.error("处理消息异常: %s", get_error_detail(e), exc_info=True)
                raise
        finally:
            set_current_thread_id(None)
            set_current_sender_id(None)
            set_current_sender_name("")
            set_current_is_p2p(False)
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
            self._reply_text(message.message_id, UI_TEXT["ws_unsupported_msg_type"])
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
        getattr(message, "parent_id", None)
        getattr(message, "root_id", None)

        with self._pending_image_lock:
            self._pending_image_keys[message_id] = image_keys

        project, auto_enter_mode = self._resolve_message_context(message)

        try:
            if project:
                self._message_linker.register_origin(
                    message_id, request_id=request_id, chat_id=chat_id, project_id=project.project_id
                )
        except Exception as e:
            logger.debug("register_origin失败(image_msg): message_id=%s, err=%s", message_id, get_error_detail(e))

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
                text = UI_TEXT["ws_image_only_prefix"] + ref_text

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
                project = self._project_manager.get_project_for_chat(thread_ctx.project_id, chat_id)
                if not project:
                    project = self._project_manager.get_active_project(chat_id)
                logger.info(
                    "[Thread] Resolved context: msg_root=%s canonical=%s project=%s mode=%s project_found=%s",
                    root_id[:12], thread_ctx.thread_root_id[:12], thread_ctx.project_id, thread_ctx.mode, project is not None,
                )
                return project, auto_enter_mode

        return self._resolve_project_from_message(message_id, chat_id, parent_id or root_id)

    def _get_effective_mode(self, chat_id: str, project_id: Optional[str] = None):
        from ..mode import InteractionMode
        thread_id = get_current_thread_id()
        if thread_id:
            thread_ctx = self._thread_manager.get(thread_id)
            if thread_ctx and thread_ctx.mode != "smart":
                try:
                    return InteractionMode(thread_ctx.mode), True
                except ValueError:
                    logger.debug(
                        "thread mode is engine-only, not InteractionMode: %s",
                        thread_ctx.mode,
                    )
                    return InteractionMode.SMART, False
        return (
            self._mode_manager.get_mode(chat_id, project_id=project_id),
            self._mode_manager.is_programming_mode(chat_id, project_id=project_id),
        )

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

    def _find_active_thread(self, chat_id):
        if not self.settings.thread_programming_enabled:
            return None
        contexts = self._thread_manager.get_by_chat(chat_id)
        for ctx in contexts:
            if ctx.mode and ctx.mode != "smart":
                return ctx
        return None

    def _update_task_project(self, task_ctx, project_id):
        """将调度任务与 project_id 关联（便于任务看板/诊断）。"""
        try:
            self._scheduler.update_project_id(task_ctx.run_id, project_id)
        except Exception as e:
            logger.debug("update_project_id失败: run_id=%s, err=%s", task_ctx.run_id, get_error_detail(e))

    def _dispatch_empty_text(self, message_id, chat_id, project, task_ctx, *, root_id=None):
        """处理"文本为空"的情况：在编程模式下仍转发（保持会话），否则展示帮助。"""
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
            if project is None:
                project = self._project_manager.get_active_project(chat_id)

            handler = self._get_mode_handler(current_mode)
            if handler:
                handler.handle_message(message_id, chat_id, "", project)
        else:
            self._show_help(message_id, chat_id)

    def _dispatch_message_logic(
        self,
        message_id,
        chat_id,
        text,
        project,
        auto_enter_mode,
        *,
        command_match=_COMMAND_MATCH_MISSING,
        is_image_only=False,
        shell_fast_tracked=False,
        chat_type: str = "group",
    ):
        """根据 auto-enter 与当前模式，将消息路由到对应编程模式或 SMART 处理路径。"""
        # Compatibility: some unit tests call _dispatch_message_logic directly.
        # In the real message ingress path, command_match is always provided.
        if command_match is _COMMAND_MATCH_MISSING:
            try:
                command_match = SlashCommandParser.parse(text)
            except Exception:
                command_match = None

        if auto_enter_mode:
            if self._reply_if_topic_engine_switch_blocked(
                message_id,
                auto_enter_mode,
                command_match=command_match,
            ):
                return
            if self._is_exit_command(text):
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                _pid = project.project_id if project else None
                if self._control_plane.should_defer_exit(chat_id=chat_id, project_id=_pid):
                    self._control_plane.request_deferred_exit(message_id=message_id, chat_id=chat_id, project_id=_pid)
                    self._reply_text(message_id, UI_TEXT["ws_exit_deferred_msg"])
                    return
                self._exit_current_mode(message_id, chat_id, project=project)
                return
            # Interceptable system commands (/wt, /worktree, /help, /status, /codex, etc.)
            # must be routed to the system handler even inside thread programming mode,
            # otherwise they can be hidden behind same-mode/topic-hint handling.
            if self._is_interceptable_command_match(command_match):
                self._process_with_intent(
                    message_id,
                    chat_id,
                    text,
                    project,
                    command_match=command_match,
                    shell_fast_tracked=shell_fast_tracked,
                    chat_type=chat_type,
                )
                return
            normalized_entry = (text or "").strip().lower()
            same_mode_entries = {
                "coco": {"/coco", "/enter_coco"},
                "claude": {"/claude", "/enter_claude"},
                "aiden": {"/aiden", "/enter_aiden"},
                "codex": {"/codex", "/enter_codex"},
                "gemini": {"/gemini", "/enter_gemini"},
                "ttadk": {"/ttadk", "/acp"},
            }
            if normalized_entry in same_mode_entries.get(auto_enter_mode, set()):
                self._reply_text(
                    message_id,
                    UI_TEXT["ws_topic_hint_msg"],
                )
                return
            if self._is_programming_entry_command(text):
                self._reply_text(
                    message_id,
                    UI_TEXT["ws_topic_hint_msg"],
                )
                return
            if self._is_deep_command(text) or self._is_spec_command(text):
                self._process_with_intent(
                    message_id,
                    chat_id,
                    text,
                    project,
                    command_match=command_match,
                    shell_fast_tracked=shell_fast_tracked,
                    chat_type=chat_type,
                )
                return
        if auto_enter_mode in {"worktree", "deep", "spec"}:
            if command_match is not None:
                self._process_with_intent(
                    message_id,
                    chat_id,
                    text,
                    project,
                    command_match=command_match,
                    shell_fast_tracked=shell_fast_tracked,
                    chat_type=chat_type,
                )
                return
            if project is None:
                self._process_with_intent(
                    message_id,
                    chat_id,
                    text,
                    project,
                    command_match=command_match,
                    shell_fast_tracked=shell_fast_tracked,
                    chat_type=chat_type,
                )
                return
            self._add_reaction(message_id, EmojiReaction.on_processing())
            if auto_enter_mode == "worktree":
                self._handle_worktree_execute(message_id, chat_id, text, project)
            elif auto_enter_mode == "deep":
                self._start_deep_engine(message_id, chat_id, text, project)
            else:
                self._start_spec_engine(message_id, chat_id, text, project)
            return

        if auto_enter_mode and auto_enter_mode in {"coco", "claude", "aiden", "codex", "gemini", "ttadk"}:
            from ..mode import InteractionMode
            handler = self._get_mode_handler(InteractionMode(auto_enter_mode))
            if handler:
                self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                self._add_reaction(message_id, EmojiReaction.on_processing())
                handler.handle_message(message_id, chat_id, text, project)
            else:
                self._process_with_intent(
                    message_id,
                    chat_id,
                    text,
                    project,
                    command_match=command_match,
                    shell_fast_tracked=shell_fast_tracked,
                    chat_type=chat_type,
                )
        else:
            # Project-chat default: when the chat is bound to a project via
            # /new-chat and the message is neither a slash command, a shell-like
            # invocation, nor an image-only message, route free-form text into
            # the Coco programming flow (model-select card + pending prompt).
            # Slash commands (command_match is not None) always fall through to
            # _process_with_intent so that /coco, /help, /deep, /wt, /exit, ...
            # keep their highest priority.
            if (
                command_match is None
                and not is_image_only
                and text
                and not self._intent_recognizer.looks_like_shell(text)
            ):
                bound_project = self._project_manager.find_by_bound_chat_id(chat_id)
                if bound_project is not None:
                    bound_project_id = getattr(bound_project, "project_id", None)
                    current_mode, is_programming = self._get_effective_mode(
                        chat_id, project_id=bound_project_id
                    )
                    if is_programming:
                        self._process_with_intent(
                            message_id,
                            chat_id,
                            text,
                            bound_project,
                            command_match=command_match,
                            shell_fast_tracked=shell_fast_tracked,
                            chat_type=chat_type,
                        )
                        return

                    default_tool = str(
                        getattr(bound_project, "acp_tool_name", None)
                        or getattr(self.settings, "default_acp_tool", None)
                        or "coco"
                    ).strip().lower()
                    saved_tool = str(getattr(bound_project, "acp_tool_name", None) or "").strip().lower()
                    self._add_reaction(message_id, EmojiReaction.on_coco_mode())
                    self._add_reaction(message_id, EmojiReaction.on_processing())
                    if saved_tool in {"coco", "claude", "aiden", "codex", "gemini"}:
                        self._system_handler.handle_enter_acp_saved_selection(
                            message_id,
                            chat_id,
                            saved_tool,
                            bound_project,
                            pending_prompt=text,
                        )
                    elif default_tool == "coco":
                        self._message_dispatcher._handle_enter_coco(
                            message_id, chat_id, bound_project, pending_prompt=text,
                        )
                    elif default_tool in {"codex"}:
                        self._message_dispatcher._handle_enter_acp_mode(
                            default_tool, message_id, chat_id, bound_project, pending_prompt=text,
                        )
                    else:
                        self._message_dispatcher._handle_enter_coco(
                            message_id, chat_id, bound_project, pending_prompt=text,
                        )
                    return
            self._process_with_intent(
                message_id,
                chat_id,
                text,
                project,
                command_match=command_match,
                shell_fast_tracked=shell_fast_tracked,
                chat_type=chat_type,
            )

    @staticmethod
    def _requested_topic_engine(command_match) -> Optional[str]:
        command = getattr(command_match, "command", None)
        if command in {"/worktree", "/wt"}:
            return "worktree"
        if command in {"/deep", "/deep_update", "/deep_status", "/stop_deep"}:
            return "deep"
        if command in {
            "/spec",
            "/spec_status",
            "/spec_history",
            "/spec_metrics",
            "/spec_config",
            "/spec_export",
            "/spec_save",
            "/spec_pause",
            "/spec_resume",
            "/spec_recover",
            "/spec_guide",
            "/stop_spec",
        }:
            return "spec"
        return None

    @staticmethod
    def _engine_display_name(engine: str) -> str:
        return {
            "worktree": "WT",
            "deep": "Deep",
            "spec": "Spec",
        }.get(engine, engine)

    def _reply_if_topic_engine_switch_blocked(
        self,
        message_id: str,
        current_engine: str,
        *,
        command_match=None,
    ) -> bool:
        requested = self._requested_topic_engine(command_match)
        if not requested or requested == current_engine:
            return False
        if current_engine not in {"worktree", "deep", "spec"}:
            return False
        self._reply_text(
            message_id,
            UI_TEXT["topic_engine_switch_blocked"].format(
                current=self._engine_display_name(current_engine),
                requested=self._engine_display_name(requested),
            ),
        )
        return True

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
                except (TypeError, ValueError):
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
        except (AttributeError, TypeError, KeyError) as e:
            logger.warning("卡片回调基础信息解析失败: %s", get_error_detail(e))
        try:
            open_message_id = data.event.context.open_message_id
            open_chat_id = data.event.context.open_chat_id
        except (AttributeError, TypeError):
            open_message_id = None
            open_chat_id = "unknown"

        # Card actions: extract chat_type for p2p privilege detection
        card_chat_type = getattr(getattr(data.event, "context", None), "chat_type", None)
        card_is_p2p = card_chat_type == "p2p"

        action_type_preview = ""
        try:
            action_type_preview = CardActionInspector.action_type(data.event.action)
        except (AttributeError, TypeError, ValueError):
            classify_card_action_error(RuntimeError("action preview failed"), phase="payload_parse")
            action_type_preview = ""

        try:
            with self._system_cmd_gate_lock:
                inflight = int(self._system_cmd_inflight_by_chat.get(open_chat_id, 0) or 0)
            if inflight > 0 and action_type_preview not in _READONLY_CARD_ACTIONS:
                if open_message_id:
                    self._reply_text(open_message_id, UI_TEXT["ws_system_cmd_gate_blocked"])
                return None
        except (RuntimeError, OSError, TypeError, ValueError):
            classify_card_action_error(RuntimeError("system command gate failed"), phase="dispatch")
            logger.debug("failed to check system command gate", exc_info=True)

        operator_id = ""
        try:
            operator = data.event.operator
            operator_id = (
                getattr(operator, "open_id", None)
                or getattr(operator, "user_id", None)
                or getattr(operator, "union_id", None)
                or ""
            )
        except (AttributeError, TypeError):
            operator_id = ""

        if open_message_id and action_type_preview:
            dedupe_fingerprint = CardActionInspector.dedup_fingerprint(data.event.action)
            dedupe_key = f"{open_chat_id}:{open_message_id}:{operator_id}:{action_type_preview}:{dedupe_fingerprint}"
            try:
                if self._card_action_dedup_cache.is_duplicate(dedupe_key):
                    return {"toast": {"type": "info", "content": UI_TEXT["card_session_toast_dedup"]}}


            except (RuntimeError, OSError, TypeError, ValueError):
                classify_card_action_error(RuntimeError("dedup failed"), phase="dedup")
                # best-effort only
                logger.debug("failed to ack card action", exc_info=True)

        # Synchronous undo-lock expiry check: return toast if window has passed
        try:
            value_raw = data.event.action.value
            _val = value_raw if isinstance(value_raw, dict) else (
                json.loads(value_raw) if isinstance(value_raw, str) else None
            )
            if isinstance(_val, dict) and _val.get("_ul"):
                undo_expires = _val.get("_ue", 0)
                if undo_expires and time.time() > undo_expires:
                    return {"toast": {"type": "warning", "content": "撤销窗口已过期，请使用 /unlock 解锁"}}
        except (json.JSONDecodeError, TypeError, ValueError):
            classify_card_action_error(RuntimeError("undo payload parse failed"), phase="payload_parse")

        project_id = None
        try:
            project_id = CardActionInspector.project_id(data.event.action)
        except (AttributeError, TypeError, ValueError):
            classify_card_action_error(RuntimeError("project id parse failed"), phase="payload_parse")
            project_id = None

        if not project_id:
            try:
                active = self._project_manager.get_active_project(open_chat_id)
                project_id = active.project_id if active else None
            except (RuntimeError, OSError, TypeError, ValueError):
                project_id = None

        origin_message_id = None
        try:
            origin_message_id = self._message_linker.resolve_origin(reply_message_id=open_message_id)
        except (RuntimeError, OSError, TypeError, ValueError):
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
                is_p2p=card_is_p2p,
                sender_id=operator_id,
            )
            handle = self._scheduler.submit(spec, lambda ctx: self._process_card_action_async(data, task_ctx=ctx))
            try:
                self._message_linker.link_task(origin_message_id, handle.run_id)
            except (KeyError, AttributeError, RuntimeError) as e:
                logger.debug(
                    "link_task失败(card_action): origin=%s, run_id=%s, err=%s", origin_message_id, handle.run_id, e
                )
        return None

    @classmethod
    def _card_action_dedup_fingerprint(cls, action: Any) -> str:
        """Return a stable fingerprint for the concrete card interaction payload."""
        return CardActionInspector.dedup_fingerprint(action)

    @staticmethod
    def _normalize_card_action_dedup_value(value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
            return parsed
        return value

    def _is_system_card_action(self, data: P2CardActionTrigger) -> bool:
        """Check if the card action is a system action that should bypass project queue."""
        try:
            return CardActionInspector.is_system_action(data.event.action)
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            return False

    def _process_card_action_async(self, data: Any, task_ctx=None):
        """卡片动作处理逻辑（第二阶段实现）。

        该方法会把 `action.value` normalize 为 dict，提取 `action/project_id`，并通过
        `ActionDispatcher` 做 exact/prefix 路由。
        """
        from ..thread import set_current_is_p2p, set_current_sender_id, set_current_sender_name, set_current_thread_id

        try:
            start_time = time.perf_counter()
            action = data.event.action
            value_raw = action.value
            operator = data.event.operator
            open_message_id = data.event.context.open_message_id
            open_chat_id = data.event.context.open_chat_id

            # sender_id is carried in task_ctx.spec (set at submit time);
            # fall back to event operator extraction only when task_ctx is unavailable.
            _operator_id = (
                task_ctx.spec.sender_id
                if task_ctx and hasattr(task_ctx, "spec") and task_ctx.spec.sender_id
                else (
                    getattr(operator, "open_id", None)
                    or getattr(operator, "user_id", None)
                    or getattr(operator, "union_id", None)
                    or ""
                )
            )
            _card_is_p2p = (
                task_ctx.spec.is_p2p
                if task_ctx and hasattr(task_ctx, "spec")
                else getattr(getattr(data.event, "context", None), "chat_type", None) == "p2p"
            )
            set_current_sender_id(_operator_id)
            from .user_cache import resolve_display_name as _resolve_name
            _op_name = _resolve_name(_operator_id, self._get_api_client) if _operator_id else ""
            set_current_sender_name(_op_name or (_operator_id[:8] if _operator_id else ""))
            set_current_is_p2p(_card_is_p2p)

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
                logger.debug("failed to extract action input_value", exc_info=True)

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
                    logger.debug("update_project_id失败(card_action): run_id=%s, err=%s", task_ctx.run_id, get_error_detail(e))

            logger.info(
                "卡片按钮点击: action=%s, project_id=%s, value_keys=%s",
                action_type,
                project_id,
                list(value.keys()),
            )

            # --- Chat lock interception for card actions (fail-close) ---
            if self._chat_lock_gate.check_card_action(
                open_chat_id, _operator_id, open_message_id,
                action_type=action_type,
            ):
                return

            # --- Dispatch via ActionDispatcher ---
            matched = self._action_dispatcher.dispatch(action_type, open_message_id, open_chat_id, project_id, value)

            if not matched:
                logger.warning("未注册的卡片动作: action=%s, message_id=%s", action_type, open_message_id)
                self._reply_text(open_message_id, f"⚠️ 未识别的操作: {action_type}")

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            logger.debug("卡片回调处理耗时: %dms", elapsed_ms)

        except asyncio.TimeoutError as e:
            logger.warning("处理卡片动作超时: %s", get_error_detail(e))
            _mid = locals().get("open_message_id", "unknown")
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

                            _pid = locals().get("project_id")
                            msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(
                                "操作超时",
                                project_id=_pid or None,
                            )
                            self._reply_card(_mid, card_content)
                        except Exception:
                            self._reply_text(_mid, "⏳ 操作超时，请稍后重试或发送 /ttadk 重新进入")
                    else:
                        self._reply_text(_mid, f"⏳ 操作超时 ({_action}): {get_error_detail(e)}")
            except Exception:
                logger.debug("failed to reply timeout action error", exc_info=True)
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
                            self._reply_card(_mid, card_content)
                        except Exception:
                            self._reply_text(_mid, "⚠️ 操作未完成，请稍后重试或发送 /ttadk 重新进入")
                    else:
                        self._reply_text(_mid, f"❌ 操作失败 ({_action}): {get_error_detail(e)}")
            except Exception:
                logger.debug("failed to reply action failure error", exc_info=True)
        finally:
            set_current_thread_id(None)
            set_current_sender_id(None)
            set_current_sender_name("")
            set_current_is_p2p(False)

    def _process_with_intent(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional[ProjectContext] = None,
        *,
        command_match=_COMMAND_MATCH_MISSING,
        shell_fast_tracked: bool = False,
        chat_type: str = "group",
    ):
        """SMART 模式下的主路由：控制命令优先，其次进入意图识别/多任务执行。"""
        # Compatibility: allow callers outside ws message ingress to omit command_match.
        if command_match is _COMMAND_MATCH_MISSING:
            try:
                command_match = SlashCommandParser.parse(text)
            except Exception:
                command_match = None
        from .dispatcher import FeishuRequestContext

        self._message_dispatcher.process_request(
            FeishuRequestContext(
                message_id=message_id,
                chat_id=chat_id,
                text=text,
                project=project,
                command_match=command_match,
                shell_fast_tracked=shell_fast_tracked,
                chat_type=chat_type,
            )
        )

    def _execute_multi_tasks(
        self, message_id: str, chat_id: str, intent_result: IntentResult, project: Optional[ProjectContext] = None
    ):
        """执行多任务计划（逐步执行；遇到失败停止后续步骤）。"""
        self._message_dispatcher.execute_multi_tasks(message_id, chat_id, intent_result, project=project)

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
        self._message_dispatcher.execute_single_task(
            message_id, chat_id, task, original_text, project=project, shell_fast_tracked=shell_fast_tracked
        )

    def _execute_task_step(
        self,
        message_id: str,
        chat_id: str,
        task: TaskStep,
        step_num: int,
        total_steps: int,
        project: Optional[ProjectContext] = None,
    ) -> bool:
        """执行一个 TaskStep，并返回是否成功。"""
        return self._message_dispatcher.execute_task_step(
            message_id, chat_id, task, step_num, total_steps, project=project
        )

    def _get_task_description(self, task: TaskStep) -> str:
        """为 TaskStep 生成可读描述（用于多任务计划展示）。"""
        return self._message_dispatcher.get_task_description(task)

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

        self._message_cache.start_cleanup_thread()
        self._card_event_cache.start_cleanup_thread()
        self._ws_health_monitor.start_watchdog()

        # Restore slock engines from persisted marker files
        import os
        _root = os.getcwd()
        try:
            restored = self._slock_engine_manager.restore_from_disk(_root)
            if restored:
                logger.info("Restored %d slock engine(s) from disk", restored)
        except (OSError, ValueError, KeyError):
            logger.warning("Failed to restore slock engines from disk", exc_info=True)

        logger.info("正在建立飞书长连接...")
        logger.info("多项目管理已启用")

        reconnect_delay = getattr(self.settings, "feishu_ws_reconnect_delay_s", 5.0)

        while not self._closed:
            self._client = ObservedLarkWSClient(
                self.settings.app_id,
                self.settings.app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.DEBUG,
                on_activity=self._ws_health_monitor.record_activity,
            )
            try:
                self._client.start()
            except (RuntimeError, OSError, TimeoutError, TypeError, ValueError) as e:
                classify_ws_error(e, phase="dispatch")
                if self._closed:
                    break
                logger.exception("飞书 WS 连接异常退出")

            if self._closed:
                break

            logger.warning("飞书 WS 连接已断开，%.1fs 后重连...", reconnect_delay)
            time.sleep(reconnect_delay)
