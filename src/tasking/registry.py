from __future__ import annotations

import asyncio
import logging
import threading
from typing import Dict, List, Optional, Set, Any

logger = logging.getLogger(__name__)

class TaskRegistry:
    """追踪后台 asyncio 任务，支持优雅停机协议。
    
    该类提供了一个中心化的容器，用于追踪所有异步协程任务。
    在系统关闭时，可以安全地取消所有正在运行的任务并等待它们完成。
    """
    
    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task] = set()
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._is_closing = False

    def track(self, task: asyncio.Task) -> asyncio.Task:
        """注册并追踪一个 asyncio 任务。"""
        with self._lock:
            if self._is_closing:
                task.cancel()
                return task
            
            self._tasks.add(task)
            # 任务完成后自动从注册表中移除
            task.add_done_callback(self._on_task_done)
            return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务完成后的回调。"""
        with self._lock:
            self._tasks.discard(task)

    async def close(self, timeout: float = 5.0) -> None:
        """关闭所有任务：停止接收新任务 -> 取消当前运行任务 -> 等待完成。"""
        with self._lock:
            if self._is_closing:
                return
            self._is_closing = True
            active_tasks = list(self._tasks)

        if not active_tasks:
            return

        logger.info("TaskRegistry: 取消 %d 个后台任务...", len(active_tasks))
        for task in active_tasks:
            task.cancel()

        # 等待所有任务完成
        try:
            await asyncio.wait(active_tasks, timeout=timeout)
        except Exception as e:
            logger.warning("TaskRegistry: 等待任务关闭时发生异常: %s", str(e))
            
        remaining = [t for t in active_tasks if not t.done()]
        if remaining:
            logger.warning("TaskRegistry: 仍有 %d 个任务未能在 %.1fs 内关闭", len(remaining), timeout)
        else:
            logger.info("TaskRegistry: 所有后台任务已安全关闭")

    def list_active_tasks(self) -> List[asyncio.Task]:
        """返回当前活跃任务列表。"""
        with self._lock:
            return list(self._tasks)

# 全局默认任务注册表
_default_registry = TaskRegistry()

def get_task_registry() -> TaskRegistry:
    """获取全局默认任务注册表。"""
    return _default_registry
