from unittest.mock import MagicMock, patch

from src.feishu.ws_health import WSHealthMonitor


class TestWSHealthMonitor:
    def test_record_activity(self):
        settings = MagicMock()
        monitor = WSHealthMonitor(None, settings)

        with patch("time.time", return_value=1000.0):
            monitor.record_activity("connected")
            assert monitor._last_connect_at == 1000.0
            assert monitor._last_frame_at == 1000.0
            assert monitor._last_pong_at == 1000.0

            monitor.record_activity("data")
            assert monitor._last_frame_at == 1000.0

            monitor.record_activity("pong")
            assert monitor._last_pong_at == 1000.0

    def test_check_health_once_healthy(self):
        settings = MagicMock()
        settings.feishu_ws_stale_timeout = 300.0
        client = MagicMock()
        client._client = MagicMock()
        client._client._conn = MagicMock()

        monitor = WSHealthMonitor(client, settings)

        with patch("time.time", return_value=1000.0):
            monitor.record_activity("connected")

        # 100s later, still healthy (timeout is 300s)
        assert monitor.check_health_once(now=1100.0) is False

    def test_check_health_once_stale_triggers_disconnect(self):
        settings = MagicMock()
        settings.feishu_ws_stale_timeout = 300.0
        settings.feishu_ws_stale_grace_seconds = 30.0
        client = MagicMock()
        client._client = MagicMock()
        client._client._conn = MagicMock()

        monitor = WSHealthMonitor(client, settings)

        with patch("time.time", return_value=1000.0):
            monitor.record_activity("connected")

        # 400s later, stale
        with patch.object(monitor, "_trigger_disconnect", return_value=True) as mock_trigger:
            assert monitor.check_health_once(now=1400.0) is True
            mock_trigger.assert_called_once_with(reason="idle_for=400.0s > timeout=300.0s")

    def test_check_health_once_throttles_reconnect(self):
        settings = MagicMock()
        settings.feishu_ws_stale_timeout = 300.0
        client = MagicMock()
        client._client = MagicMock()
        client._client._conn = MagicMock()

        monitor = WSHealthMonitor(client, settings)

        with patch("time.time", return_value=1000.0):
            monitor.record_activity("connected")

        monitor._reconnect_requested_at = 1390.0 # Requested 10s ago

        with patch.object(monitor, "_trigger_disconnect") as mock_trigger:
            assert monitor.check_health_once(now=1400.0) is False
            mock_trigger.assert_not_called()

    @patch("src.feishu.ws_health.asyncio.run_coroutine_threadsafe")
    def test_trigger_disconnect(self, mock_run_coro):
        settings = MagicMock()
        client_instance = MagicMock()
        client_instance._client = MagicMock()
        client_instance._client._conn = MagicMock()

        monitor = WSHealthMonitor(client_instance, settings)

        mock_fut = MagicMock()
        mock_run_coro.return_value = mock_fut

        assert monitor._trigger_disconnect(reason="test") is True
        mock_run_coro.assert_called_once()
        mock_fut.result.assert_called_once_with(timeout=5)

    def test_watchdog_lifecycle(self):
        settings = MagicMock()
        settings.feishu_ws_watchdog_interval = 0.1
        monitor = WSHealthMonitor(None, settings)

        monitor.start_watchdog()
        assert monitor._watchdog_thread.is_alive()

        monitor.stop_watchdog()
        assert monitor._watchdog_thread is None
