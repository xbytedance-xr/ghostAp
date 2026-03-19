import logging
from unittest.mock import MagicMock, patch

from src.utils.gc_monitor import GCMonitor, get_gc_monitor


def test_gc_monitor_threshold_triggered():
    monitor = GCMonitor(memory_threshold_percent=80.0, check_interval_seconds=0.0)

    with patch("src.utils.gc_monitor.psutil.Process") as mock_process, \
         patch("src.utils.gc_monitor.gc.collect") as mock_gc_collect:

        mock_proc_instance = MagicMock()
        mock_proc_instance.memory_percent.return_value = 85.0
        mock_process.return_value = mock_proc_instance

        monitor.check_and_collect(label="Test")

        mock_gc_collect.assert_called_once()
        assert mock_proc_instance.memory_percent.call_count == 2


def test_gc_monitor_threshold_not_triggered():
    monitor = GCMonitor(memory_threshold_percent=80.0, check_interval_seconds=0.0)

    with patch("src.utils.gc_monitor.psutil.Process") as mock_process, \
         patch("src.utils.gc_monitor.gc.collect") as mock_gc_collect:

        mock_proc_instance = MagicMock()
        mock_proc_instance.memory_percent.return_value = 75.0
        mock_process.return_value = mock_proc_instance

        monitor.check_and_collect(label="Test")

        mock_gc_collect.assert_not_called()
        assert mock_proc_instance.memory_percent.call_count == 1


def test_gc_monitor_throttle():
    monitor = GCMonitor(memory_threshold_percent=80.0, check_interval_seconds=5.0)

    with patch("src.utils.gc_monitor.psutil.Process") as mock_process, \
         patch("src.utils.gc_monitor.gc.collect") as mock_gc_collect:

        mock_proc_instance = MagicMock()
        mock_proc_instance.memory_percent.return_value = 85.0
        mock_process.return_value = mock_proc_instance

        monitor.check_and_collect(label="Test")
        monitor.check_and_collect(label="Test")

        mock_gc_collect.assert_called_once()


def test_gc_monitor_gracefully_skips_when_psutil_missing(caplog):
    monitor = GCMonitor(memory_threshold_percent=80.0, check_interval_seconds=0.0)

    with patch("src.utils.gc_monitor.psutil", None), \
         patch("src.utils.gc_monitor.gc.collect") as mock_gc_collect:
        with caplog.at_level(logging.DEBUG):
            monitor.check_and_collect(label="Test")

    mock_gc_collect.assert_not_called()
    assert "psutil unavailable" in caplog.text


def test_get_gc_monitor_singleton():
    monitor1 = get_gc_monitor(memory_threshold_percent=80.0)
    monitor2 = get_gc_monitor(memory_threshold_percent=90.0)

    assert monitor1 is monitor2
    assert monitor1._memory_threshold_percent == 90.0
