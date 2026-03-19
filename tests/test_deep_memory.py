import unittest
from unittest.mock import MagicMock, patch
from src.deep_engine.engine import DeepEngine
import psutil
import gc
import time

class TestDeepMemory(unittest.TestCase):
    def setUp(self):
        self.engine = DeepEngine(
            chat_id="test_chat",
            root_path="/tmp/test_project",
            agent_type="test_agent"
        )
        self.engine.settings = MagicMock()

    @patch("psutil.Process")
    @patch("gc.collect")
    def test_memory_monitor_high_usage(self, mock_gc, mock_process_cls):
        # Setup mock process
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        
        # Simulate > 80% memory usage
        mock_process.memory_percent.side_effect = [85.0, 70.0]  # First call high, second call low (after GC)
        
        # Reset last check time to ensure it runs
        self.engine._last_mem_check = 0.0
        
        # Trigger check
        self.engine._check_memory_and_gc()
        
        # Verify GC was called
        mock_gc.assert_called_once()

    @patch("psutil.Process")
    @patch("gc.collect")
    def test_memory_monitor_normal_usage(self, mock_gc, mock_process_cls):
        # Setup mock process
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        
        # Simulate < 80% memory usage
        mock_process.memory_percent.return_value = 50.0
        
        # Reset last check time
        self.engine._last_mem_check = 0.0
        
        # Trigger check
        self.engine._check_memory_and_gc()
        
        # Verify GC was NOT called
        mock_gc.assert_not_called()

    @patch("psutil.Process")
    @patch("gc.collect")
    def test_memory_monitor_throttling(self, mock_gc, mock_process_cls):
        # Setup mock process
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        mock_process.memory_percent.return_value = 90.0
        
        # First check
        self.engine._last_mem_check = 0.0
        self.engine._check_memory_and_gc()
        mock_gc.assert_called_once()
        
        mock_gc.reset_mock()
        
        # Immediate second check (should be throttled)
        # _check_memory_and_gc updates _last_mem_check, but logic says if now - last < 5: return
        # so we rely on time.time() not changing much or explicitly mocking time
        
        with patch("time.time", side_effect=[100.0, 101.0]): # 1s elapsed
             # The first call to _check_memory_and_gc used real time in the previous step?
             # Actually I should mock time in both steps or rely on system clock.
             # Let's reset and mock time for this test to be robust.
             pass
             
        # Re-do with time mock
        self.engine._last_mem_check = 0.0
        
    @patch("time.time")
    @patch("psutil.Process")
    @patch("gc.collect")
    def test_memory_monitor_throttling_robust(self, mock_gc, mock_process_cls, mock_time):
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        mock_process.memory_percent.return_value = 90.0
        
        # Time starts at 1000
        mock_time.return_value = 1000.0
        self.engine._last_mem_check = 0.0
        
        # First check (runs because 1000 - 0 > 5)
        self.engine._check_memory_and_gc()
        mock_gc.assert_called_once()
        self.assertEqual(self.engine._last_mem_check, 1000.0)
        
        mock_gc.reset_mock()
        
        # Advance time by 2s (1002.0)
        mock_time.return_value = 1002.0
        
        # Second check (throttled because 1002 - 1000 < 5)
        self.engine._check_memory_and_gc()
        mock_gc.assert_not_called()
        
        # Advance time by 6s (1008.0)
        mock_time.return_value = 1008.0
        
        # Third check (runs because 1008 - 1000 > 5)
        self.engine._check_memory_and_gc()
        mock_gc.assert_called_once()

if __name__ == "__main__":
    unittest.main()
