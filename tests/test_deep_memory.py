import unittest
from unittest.mock import MagicMock, patch

from src.deep_engine.engine import DeepEngine


class TestDeepMemory(unittest.TestCase):
    def setUp(self):
        self.engine = DeepEngine(
            chat_id="test_chat",
            root_path="/tmp/test_project",
            agent_type="test_agent",
        )
        self.engine.settings = MagicMock()
        self.engine.settings.deep_memory_threshold = 80.0

    @patch("src.utils.gc_monitor.psutil.Process")
    @patch("src.utils.gc_monitor.gc.collect")
    def test_memory_monitor_high_usage(self, mock_gc, mock_process_cls):
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process

        # Simulate > 80% memory usage
        mock_process.memory_percent.side_effect = [85.0, 70.0]

        # Reset last check times to ensure it runs
        self.engine._last_mem_check = 0.0

        # Reset global GCMonitor's internal throttle
        from src.utils.gc_monitor import get_gc_monitor

        gc_mon = get_gc_monitor(memory_threshold_percent=80.0)
        gc_mon._last_mem_check = 0.0

        self.engine._check_memory_and_gc()
        mock_gc.assert_called_once()

    @patch("src.utils.gc_monitor.psutil.Process")
    @patch("src.utils.gc_monitor.gc.collect")
    def test_memory_monitor_normal_usage(self, mock_gc, mock_process_cls):
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process

        # Simulate < 80% memory usage
        mock_process.memory_percent.return_value = 50.0

        self.engine._last_mem_check = 0.0

        from src.utils.gc_monitor import get_gc_monitor

        gc_mon = get_gc_monitor(memory_threshold_percent=80.0)
        gc_mon._last_mem_check = 0.0

        self.engine._check_memory_and_gc()
        mock_gc.assert_not_called()

    @patch("time.time")
    @patch("src.utils.gc_monitor.psutil.Process")
    @patch("src.utils.gc_monitor.gc.collect")
    def test_memory_monitor_throttling_robust(self, mock_gc, mock_process_cls, mock_time):
        mock_process = MagicMock()
        mock_process_cls.return_value = mock_process
        mock_process.memory_percent.return_value = 90.0

        from src.utils.gc_monitor import get_gc_monitor

        gc_mon = get_gc_monitor(memory_threshold_percent=80.0)

        # Time starts at 1000
        mock_time.return_value = 1000.0
        self.engine._last_mem_check = 0.0
        gc_mon._last_mem_check = 0.0

        # First check (runs because 1000 - 0 > 5)
        self.engine._check_memory_and_gc()
        mock_gc.assert_called_once()
        self.assertEqual(self.engine._last_mem_check, 1000.0)

        mock_gc.reset_mock()

        # Advance time by 2s (1002.0)
        mock_time.return_value = 1002.0

        # Second check (throttled at engine level because 1002 - 1000 < 5)
        self.engine._check_memory_and_gc()
        mock_gc.assert_not_called()

        # Advance time by 6s (1008.0)
        mock_time.return_value = 1008.0
        gc_mon._last_mem_check = 0.0  # Reset GCMonitor throttle too

        # Third check (runs because 1008 - 1000 > 5)
        self.engine._check_memory_and_gc()
        mock_gc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
