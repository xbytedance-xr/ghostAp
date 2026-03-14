import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
from src.feishu.handlers.base import BaseHandler

class TestMessageThrottling(unittest.TestCase):
    def setUp(self):
        self.mock_ctx = MagicMock()
        # Mock settings
        self.mock_ctx.settings.app_id = "test_app"
        self.mock_ctx.settings.app_secret = "test_secret"
        self.mock_ctx.api_client_factory = MagicMock()
        
        self.handler = BaseHandler(self.mock_ctx)
        
        # Mock IM client
        self.handler.im_client = MagicMock()
        self.handler.im_client.patch_message.return_value = MagicMock(success=lambda: True)

    def test_immediate_patch(self):
        """测试立即发送模式 (throttle=False)"""
        msg_id = "msg_123"
        content = "content_immediate"
        
        # Execute
        result = self.handler.patch_message(msg_id, content, throttle=False)
        
        # Verify
        self.assertTrue(result)
        self.handler.im_client.patch_message.assert_called_once_with(msg_id, content, max_retries=None)
        
        # Verify no state residue
        self.assertNotIn(msg_id, self.handler._pending_patches)
        self.assertNotIn(msg_id, self.handler._patch_tasks)

    async def _run_throttled_sequence(self):
        msg_id = "msg_async"
        
        # 1. First throttled call
        self.handler.patch_message(msg_id, "content_1", throttle=True)
        self.assertIn(msg_id, self.handler._pending_patches)
        self.assertIn(msg_id, self.handler._patch_tasks)
        self.assertEqual(self.handler._pending_patches[msg_id], "content_1")
        
        task = self.handler._patch_tasks[msg_id]
        
        # 2. Second throttled call (update content only)
        self.handler.patch_message(msg_id, "content_2", throttle=True)
        self.assertEqual(self.handler._pending_patches[msg_id], "content_2")
        self.assertIs(self.handler._patch_tasks[msg_id], task) # Same task
        
        # 3. Wait for task to complete (we need to mock sleep or wait real time)
        # For this test, we let it run. The default delay is 0.5s.
        # To speed up, we can patch asyncio.sleep or just wait.
        # But we need to await the task.
        await task
        
        # 4. Verify API called only once with latest content
        self.handler.im_client.patch_message.assert_called_once_with(msg_id, "content_2", max_retries=None)
        
        # 5. Verify cleanup
        self.assertNotIn(msg_id, self.handler._pending_patches)
        self.assertNotIn(msg_id, self.handler._patch_tasks)

    def test_throttled_merge(self):
        """测试节流合并逻辑 (Async wrapper)"""
        # Patch sleep to be instant to speed up test
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            asyncio.run(self._run_throttled_sequence())
            mock_sleep.assert_called_once_with(0.5)

    async def _run_immediate_override(self):
        msg_id = "msg_override"
        
        # 1. Throttled call
        self.handler.patch_message(msg_id, "content_pending", throttle=True)
        task = self.handler._patch_tasks[msg_id]
        
        # 2. Immediate call (Override)
        self.handler.patch_message(msg_id, "content_final", throttle=False)
        
        # Verify first task cancelled by awaiting it and expecting CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await task
        
        self.assertTrue(task.cancelled())
        
        # Verify API called immediately
        self.handler.im_client.patch_message.assert_called_with(msg_id, "content_final", max_retries=None)
        
        # Verify state cleaned
        self.assertNotIn(msg_id, self.handler._pending_patches)
        self.assertNotIn(msg_id, self.handler._patch_tasks)

    def test_immediate_override(self):
        """测试立即发送覆盖节流任务"""
        asyncio.run(self._run_immediate_override())

if __name__ == '__main__':
    unittest.main()
