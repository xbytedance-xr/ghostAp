"""Task scheduling and tracking utilities.

This package provides a lightweight, thread-based task scheduler with:
- per-key ordered execution (project-level isolation)
- global concurrency limit
- task status tracking and progress events
- system command fast-track (bypasses per-key limit)

Queue key routing:
- System commands: {chat_id}:SYSTEM (high concurrency)
- Project tasks: {chat_id}:{project_id} (serial within project)
- No project: {chat_id}:DEFAULT (serial)
"""

from .scheduler import (
    TaskScheduler,
    TaskPriority,
    TaskStatus,
    TaskSpec,
    TaskHandle,
    TaskEvent,
    TaskResult,
    SYSTEM_QUEUE_SUFFIX,
    DEFAULT_QUEUE_SUFFIX,
)

__all__ = [
    "TaskScheduler",
    "TaskPriority",
    "TaskStatus",
    "TaskSpec",
    "TaskHandle",
    "TaskEvent",
    "TaskResult",
    "SYSTEM_QUEUE_SUFFIX",
    "DEFAULT_QUEUE_SUFFIX",
]

