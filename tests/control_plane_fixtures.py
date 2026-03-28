"""测试辅助：控制面/执行面相关的可复现实验夹具。

这些夹具用于在 pytest 中稳定复现：
- 长任务占用 worker（模拟编程任务执行中）
- 系统指令快速回显/不打断任务
- 按钮点击去重/门禁
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from src.tasking.scheduler import TaskEvent, TaskPriority, TaskScheduler, TaskSpec


@dataclass
class EventRecorder:
    """记录 TaskScheduler 广播的 TaskEvent（用于断言时序与延迟）。"""

    events: List[TaskEvent] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, ev: TaskEvent) -> None:
        with self.lock:
            self.events.append(ev)

    def snapshot(self) -> List[TaskEvent]:
        with self.lock:
            return list(self.events)

    def first_event_ts(self, *, run_id: str, status: str) -> Optional[float]:
        with self.lock:
            for ev in self.events:
                if ev.run_id == run_id and ev.status == status:
                    return ev.timestamp
        return None


@dataclass
class BlockedTask:
    """一个可控的阻塞任务：用于模拟长时间运行的编程任务。"""

    started: threading.Event = field(default_factory=threading.Event)
    unblock: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def fn(self) -> Callable[[Any], Any]:
        def _inner(_ctx: Any) -> Any:
            self.started.set()
            # wait until released
            while not self.unblock.wait(0.01):
                pass
            self.finished.set()
            return "ok"

        return _inner


def make_scheduler(*, max_concurrent: int = 1, per_key_concurrency: int = 1, system_concurrency: int = 10) -> TaskScheduler:
    return TaskScheduler(
        max_concurrent=max_concurrent,
        per_key_concurrency=per_key_concurrency,
        system_concurrency=system_concurrency,
        thread_name_prefix="test_worker",
    )


def make_spec(
    *,
    chat_id: str,
    name: str,
    task_type: str,
    project_id: Optional[str] = None,
    is_system_command: bool = False,
    priority: TaskPriority = TaskPriority.NORMAL,
    queue_key: Optional[str] = None,
) -> TaskSpec:
    return TaskSpec(
        chat_id=chat_id,
        name=name,
        task_type=task_type,
        project_id=project_id,
        is_system_command=is_system_command,
        priority=priority,
        queue_key=queue_key,
    )


def wait_event(ev: threading.Event, *, timeout_s: float = 2.0, interval_s: float = 0.01) -> bool:
    """尽量避免 busy-spin 的等待工具。"""

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ev.is_set():
            return True
        time.sleep(interval_s)
    return ev.is_set()

