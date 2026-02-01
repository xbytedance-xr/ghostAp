"""Task scheduling and tracking utilities.

This package provides a lightweight, thread-based task scheduler with:
- per-chat ordered execution
- global concurrency limit
- task status tracking and progress events
"""

from .scheduler import (
    TaskScheduler,
    TaskPriority,
    TaskStatus,
    TaskSpec,
    TaskHandle,
    TaskEvent,
    TaskResult,
)

__all__ = [
    "TaskScheduler",
    "TaskPriority",
    "TaskStatus",
    "TaskSpec",
    "TaskHandle",
    "TaskEvent",
    "TaskResult",
]

