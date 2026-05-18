from typing import Any, Callable

from ..utils.registry import ServiceRegistry


def register_default_class_types(registry: ServiceRegistry) -> None:
    """Register default class types as DI pivot points.

    These registrations allow tests to override specific classes by pre-registering
    different classes before FeishuWSClient initialization.
    Uses register_instance_if_absent to preserve any pre-registered overrides.
    """
    from ..agent.intent_recognizer import IntentRecognizer
    from ..deep_engine import DeepEngineManager, ProgressReporter
    from ..mode import ModeManager
    from ..project import MessageLinker, MessageProjectMapper, ProjectContextManager, ProjectManager
    from ..slock_engine import SlockEngineManager
    from ..spec_engine import SpecEngineManager, SpecReporter
    from ..tasking import TaskScheduler
    from ..thread import get_thread_manager
    from .action_dispatcher import ActionDispatcher
    from .control_plane import ControlPlane
    from .dispatcher import MessageDispatcher
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
    from .image_handler import FeishuImageHandler
    from .router import HandlerDispatcher, MessageRouter
    from .ws_health import WSHealthMonitor

    # Class type registrations — these serve as DI override points for tests.
    # Using register_instance (override=True) ensures current module-level classes
    # are captured (important when tests monkeypatch modules).
    _class_map = {
        "IntentRecognizer": IntentRecognizer,
        "TaskScheduler": TaskScheduler,
        "ProjectManager": ProjectManager,
        "MessageProjectMapper": MessageProjectMapper,
        "MessageLinker": MessageLinker,
        "ModeManager": ModeManager,
        "get_thread_manager": get_thread_manager,
        "DeepEngineManager": DeepEngineManager,
        "ProgressReporter": ProgressReporter,
        "SpecEngineManager": SpecEngineManager,
        "SpecReporter": SpecReporter,
        "SlockEngineManager": SlockEngineManager,
        "ProjectContextManager": ProjectContextManager,
        "CocoModeHandler": CocoModeHandler,
        "ClaudeModeHandler": ClaudeModeHandler,
        "AidenModeHandler": AidenModeHandler,
        "CodexModeHandler": CodexModeHandler,
        "GeminiModeHandler": GeminiModeHandler,
        "TTADKModeHandler": TTADKModeHandler,
        "DeepHandler": DeepHandler,
        "SpecHandler": SpecHandler,
        "SlockHandler": SlockHandler,
        "ProjectHandler": ProjectHandler,
        "SystemHandler": SystemHandler,
        "DiagnosticsHandler": DiagnosticsHandler,
        "HandlerDispatcher": HandlerDispatcher,
        "MessageRouter": MessageRouter,
        "MessageDispatcher": MessageDispatcher,
        "ActionDispatcher": ActionDispatcher,
        "ControlPlane": ControlPlane,
        "WSHealthMonitor": WSHealthMonitor,
        "FeishuImageHandler": FeishuImageHandler,
    }
    for key, cls in _class_map.items():
        registry.register_instance(key, cls, override=True)

def setup_feishu_services(
    registry: ServiceRegistry,
    settings: Any,
    message_callback: Callable,
    ws_client: Any = None
) -> None:
    """Register all Feishu-related services to the registry."""
    from ..acp.telemetry import build_idle_health_config_for_manager
    from ..agent.intent_recognizer import IntentRecognizer
    from ..deep_engine import DeepEngineManager, ProgressReporter
    from ..mode import ModeManager
    from ..project import MessageLinker, MessageProjectMapper, ProjectContextManager, ProjectManager
    from ..slock_engine import SlockEngineManager
    from ..spec_engine import SpecEngineManager, SpecReporter
    from ..tasking import TaskScheduler
    from ..thread import get_thread_manager
    from ..utils.circuit_breaker import CircuitBreaker
    from ..utils.rate_limit import RateLimiter
    from .action_dispatcher import ActionDispatcher
    from .control_plane import ControlPlane
    from .dispatcher import CardDispatcher, MessageDispatcher
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
    from .image_handler import FeishuImageHandler
    from .message_cache import MessageCache
    from .router import HandlerDispatcher, MessageRouter
    from .session_hub import SessionManagerHub
    from .ws_health import WSHealthMonitor

    # 1. Base instances
    registry.register_instance("settings", settings, override=True)
    registry.register_instance("message_callback", message_callback, override=True)

    # 2. Managers & Utils
    registry.register_factory_if_absent("idle_health_cfg", build_idle_health_config_for_manager)

    def create_session_hub():
        return SessionManagerHub(registry.get("settings"), registry.get("idle_health_cfg"))
    registry.register_factory_if_absent("session_hub", create_session_hub)

    registry.register_factory_if_absent("coco_manager", lambda: registry.get("session_hub").coco)
    registry.register_factory_if_absent("claude_manager", lambda: registry.get("session_hub").claude)
    registry.register_factory_if_absent("aiden_manager", lambda: registry.get("session_hub").aiden)
    registry.register_factory_if_absent("codex_manager", lambda: registry.get("session_hub").codex)
    registry.register_factory_if_absent("gemini_manager", lambda: registry.get("session_hub").gemini)
    registry.register_factory_if_absent("ttadk_manager", lambda: registry.get("session_hub").ttadk)

    intent_recognizer_cls = registry.get("IntentRecognizer", default=IntentRecognizer)
    registry.register_factory_if_absent("intent_recognizer", lambda: intent_recognizer_cls(settings=registry.get("settings")))

    s = registry.get("settings")
    registry.register_factory_if_absent("message_cache", lambda: MessageCache(ttl=s.message_cache_ttl, max_size=s.message_cache_max_size, cleanup_interval=s.message_cache_cleanup_interval))
    registry.register_factory_if_absent("card_event_cache", lambda: MessageCache(ttl=s.message_cache_ttl, max_size=s.message_cache_max_size, cleanup_interval=s.message_cache_cleanup_interval))
    registry.register_factory_if_absent("card_action_dedup_cache", lambda: MessageCache(ttl=s.card_action_dedup_ttl, max_size=s.card_action_dedup_max_size, cleanup_interval=s.card_action_dedup_cleanup_interval))

    scheduler_cls = registry.get("TaskScheduler", default=TaskScheduler)
    def create_scheduler():
        sched = scheduler_cls(
            max_concurrent=s.task_scheduler_max_concurrent,
            per_key_concurrency=s.task_scheduler_per_key_concurrency,
            system_concurrency=s.system_command_concurrency,
            thread_name_prefix="ghost_worker",
        )
        sched.register_policy(
            "spec_command",
            rate_limiter=RateLimiter(capacity=s.spec_rate_limit_capacity, fill_rate=s.spec_rate_limit_fill_rate),
            circuit_breaker=CircuitBreaker(failure_threshold=s.spec_circuit_breaker_threshold, recovery_timeout=s.spec_circuit_breaker_recovery),
        )
        return sched
    registry.register_factory_if_absent("scheduler", create_scheduler)

    from ..tasking.registry import get_task_registry
    registry.register_instance_if_absent("task_registry", get_task_registry())

    registry.register_factory_if_absent("project_manager", lambda: registry.get("ProjectManager", default=ProjectManager)())
    registry.register_factory_if_absent("message_mapper", lambda: registry.get("MessageProjectMapper", default=MessageProjectMapper)())
    registry.register_factory_if_absent("message_linker", lambda: registry.get("MessageLinker", default=MessageLinker)())

    registry.register_factory_if_absent("mode_manager", lambda: registry.get("ModeManager", default=ModeManager)())

    registry.register_factory_if_absent("thread_manager", lambda: registry.get("get_thread_manager", default=get_thread_manager)())

    registry.register_factory_if_absent("deep_engine_manager", lambda: registry.get("DeepEngineManager", default=DeepEngineManager)())
    registry.register_factory_if_absent("progress_reporter", lambda: registry.get("ProgressReporter", default=ProgressReporter)())
    registry.register_factory_if_absent("spec_engine_manager", lambda: registry.get("SpecEngineManager", default=SpecEngineManager)())
    registry.register_factory_if_absent("spec_reporter", lambda: registry.get("SpecReporter", default=SpecReporter)())
    registry.register_factory_if_absent("slock_engine_manager", lambda: registry.get("SlockEngineManager", default=SlockEngineManager)())
    registry.register_factory_if_absent("context_manager", lambda: registry.get("ProjectContextManager", default=ProjectContextManager)())

    # 3. Handler Context
    registry.register_factory_if_absent("handler_ctx", lambda: HandlerContext(registry))

    # 4. Handlers
    registry.register_factory_if_absent("coco_handler", lambda: registry.get("CocoModeHandler", default=CocoModeHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("claude_handler", lambda: registry.get("ClaudeModeHandler", default=ClaudeModeHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("aiden_handler", lambda: registry.get("AidenModeHandler", default=AidenModeHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("codex_handler", lambda: registry.get("CodexModeHandler", default=CodexModeHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("gemini_handler", lambda: registry.get("GeminiModeHandler", default=GeminiModeHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("ttadk_handler", lambda: registry.get("TTADKModeHandler", default=TTADKModeHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("deep_handler", lambda: registry.get("DeepHandler", default=DeepHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("spec_handler", lambda: registry.get("SpecHandler", default=SpecHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("slock_handler", lambda: registry.get("SlockHandler", default=SlockHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("project_handler", lambda: registry.get("ProjectHandler", default=ProjectHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("system_handler", lambda: registry.get("SystemHandler", default=SystemHandler)(registry.get("handler_ctx")))
    registry.register_factory_if_absent("diagnostics_handler", lambda: registry.get("DiagnosticsHandler", default=DiagnosticsHandler)(registry.get("handler_ctx")))

    # 5. Dispatchers & Router
    registry.register_factory_if_absent("handler_dispatcher", lambda: registry.get("HandlerDispatcher", default=HandlerDispatcher)(registry.get("handler_ctx"), dispatcher=ws_client))
    registry.register_factory_if_absent("router", lambda: registry.get("MessageRouter", default=MessageRouter)(registry.get("handler_ctx"), dispatcher=ws_client))
    registry.register_factory_if_absent("message_dispatcher", lambda: registry.get("MessageDispatcher", default=MessageDispatcher)(ws_client))
    registry.register_factory_if_absent("card_dispatcher", lambda: registry.get("CardDispatcher", default=CardDispatcher)(ws_client))
    registry.register_factory_if_absent("action_dispatcher", lambda: registry.get("ActionDispatcher", default=ActionDispatcher)())

    # 6. Control Plane & Health
    control_plane_cls = registry.get("ControlPlane", default=ControlPlane)
    def create_control_plane():
        return control_plane_cls(
            scheduler=registry.get("scheduler"),
            project_manager=registry.get("project_manager"),
            exit_handler_fn=lambda *args, **kwargs: ws_client._exit_current_mode(*args, **kwargs) if ws_client else None,
        )
    registry.register_factory_if_absent("control_plane", create_control_plane)

    ws_health_monitor_cls = registry.get("WSHealthMonitor", default=WSHealthMonitor)
    def create_ws_health_monitor():
        return ws_health_monitor_cls(ws_client, registry.get("settings"))
    registry.register_factory_if_absent("ws_health_monitor", create_ws_health_monitor)

    feishu_image_handler_cls = registry.get("FeishuImageHandler", default=FeishuImageHandler)
    def create_image_handler():
        # Requires api_client_factory which will be registered later if needed,
        # but FeishuWSClient has _get_api_client
        return feishu_image_handler_cls(ws_client._get_api_client if ws_client else None, registry.get("settings"))
    registry.register_factory_if_absent("image_handler", create_image_handler)
