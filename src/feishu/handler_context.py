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
    from ..agent.intent_recognizer import IntentRecognizer
    from ..card.streaming import StreamingCardManager
    from ..coco.session import CocoSessionManager
    from ..claude.session import ClaudeSessionManager
    from ..deep_engine import DeepEngineManager, ProgressReporter
    from ..mode import ModeManager
    from ..project import MessageProjectMapper, ProjectContextManager, ProjectManager
    from ..project.mapper import MessageLinker
    from ..tasking import TaskScheduler
    from .image_handler import FeishuImageHandler


@dataclass
class HandlerContext:
    """All shared dependencies that handlers need, injected once."""

    settings: Any
    api_client_factory: Callable[[], "lark.Client"]
    message_callback: Callable[[str, str, str, Optional[str]], None]

    # Session managers
    coco_manager: "CocoSessionManager"
    claude_manager: "ClaudeSessionManager"

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

    # Lazy-initialized singletons
    streaming_manager_factory: Callable[[], "StreamingCardManager"]
    image_handler_factory: Callable[[], "FeishuImageHandler"]

    # Shared mutable state
    working_dirs: dict[str, str] = field(default_factory=dict)
    working_dir_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_image_keys: dict[str, list[str]] = field(default_factory=dict)
    pending_image_lock: threading.Lock = field(default_factory=threading.Lock)
    enable_streaming: bool = True
