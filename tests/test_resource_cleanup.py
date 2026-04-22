import asyncio
import pytest
import unittest
from src.tasking.registry import TaskRegistry

class TestResourceCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_task_registry_tracking(self):
        registry = TaskRegistry()
        
        async def mock_task():
            await asyncio.sleep(10)
            
        task = asyncio.create_task(mock_task())
        registry.track(task)
        
        self.assertIn(task, registry.list_active_tasks())
        
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
            
        # 任务完成后应自动移除
        self.assertNotIn(task, registry.list_active_tasks())

    async def test_task_registry_close_cancels_all(self):
        registry = TaskRegistry()
        
        task_started = asyncio.Event()
        task_canceled = asyncio.Event()
        
        async def mock_task():
            task_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                task_canceled.set()
                raise
                
        task = asyncio.create_task(mock_task())
        registry.track(task)
        
        await task_started.wait()
        
        # 关闭注册表应取消任务
        await registry.close(timeout=0.1)
        
        self.assertTrue(task_canceled.is_set())
        self.assertTrue(task.done())
        self.assertEqual(len(registry.list_active_tasks()), 0)

    async def test_task_registry_rejects_new_tasks_after_close(self):
        registry = TaskRegistry()
        await registry.close()
        
        async def mock_task():
            await asyncio.sleep(1)
            
        task = asyncio.create_task(mock_task())
        tracked_task = registry.track(task)
        
        # 追踪后应被标记为取消
        self.assertTrue(tracked_task.cancelling() > 0 or tracked_task.done())
        
        try:
            await tracked_task
        except asyncio.CancelledError:
            pass
        self.assertTrue(tracked_task.done())

if __name__ == "__main__":
    unittest.main()
