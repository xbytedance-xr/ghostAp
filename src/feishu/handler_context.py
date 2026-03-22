"""Shared dependency container for all message handlers.

HandlerContext bundles every collaborator that a handler might need so that
individual handlers don't require 16+ constructor parameters.  The FeishuWSClient
creates a single HandlerContext instance in its ``__init__`` and passes it to every
handler.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    import lark_oapi as lark

    from ..acp.manager import ACPSessionManager
    from ..agent.intent_recognizer import IntentRecognizer
    from ..card.streaming import StreamingCardManager
    from ..deep_engine import DeepEngineManager, ProgressReporter
    from ..loop_engine import LoopEngineManager, LoopReporter
    from ..mode import ModeManager
    from ..project import MessageProjectMapper, ProjectContextManager, ProjectManager
    from ..project.mapper import MessageLinker
    from ..spec_engine import SpecEngineManager, SpecReporter
    from ..tasking import TaskScheduler
    from .image_handler import FeishuImageHandler


@dataclass
class HandlerContext:
    """All shared dependencies that handlers need, injected once."""

    settings: Any
    api_client_factory: Callable[[], "lark.Client"]
    message_callback: Callable[[str, str, str, Optional[str]], None]

    # Session managers (ACP-based)
    coco_manager: "ACPSessionManager"
    claude_manager: "ACPSessionManager"
    aiden_manager: "ACPSessionManager"
    codex_manager: "ACPSessionManager"
    gemini_manager: "ACPSessionManager"
    ttadk_manager: "ACPSessionManager"

    # Core services
    intent_recognizer: "IntentRecognizer"
    scheduler: "TaskScheduler"
    project_manager: "ProjectManager"
    message_mapper: "MessageProjectMapper"
    message_linker: "MessageLinker"
    mode_manager: "ModeManager"
    context_manager: "ProjectContextManager"
    deep_engine_manager: "DeepEngineManager"
    progress_reporter: "ProgressReporter"
    loop_engine_manager: "LoopEngineManager"
    loop_reporter: "LoopReporter"
    spec_engine_manager: "SpecEngineManager"
    spec_reporter: "SpecReporter"

    # Lazy-initialized singletons
    streaming_manager_factory: Callable[[], "StreamingCardManager"]
    image_handler_factory: Callable[[], "FeishuImageHandler"]

    # Shared mutable state
    working_dirs: dict[str, str] = field(default_factory=dict)
    working_dir_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_image_keys: dict[str, list[str]] = field(default_factory=dict)
    pending_image_lock: threading.Lock = field(default_factory=threading.Lock)
    enable_streaming: bool = True
