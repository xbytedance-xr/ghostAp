import threading
import time
from types import SimpleNamespace


def test_ws_client_start_reconnects_if_underlying_start_returns(monkeypatch):
    """Ensure WS client doesn't stop the whole service when lark client exits.

    We simulate lark-oapi WS client's `.start()` returning unexpectedly. GhostAP
    should reconnect (create client again) until `close()` is called.
    """

    from src.feishu import ws_client as ws

    fake_settings = SimpleNamespace(
        app_id="test_app_id",
        app_secret="test_secret",
        coco_session_timeout=60,
        claude_session_timeout=60,
        acp_keepalive_interval=10,
        acp_session_idle_healthcheck_s=0,
        task_scheduler_max_concurrent=1,
        task_scheduler_per_key_concurrency=1,
        message_cache_ttl=300,
        message_cache_max_size=1000,
        card=SimpleNamespace(action_dedup_ttl=1, action_dedup_max_size=5000),
        system_command_concurrency=10,
        spec_rate_limit_capacity=100,
        spec_rate_limit_fill_rate=50.0,
        spec_circuit_breaker_threshold=10,
        spec_circuit_breaker_recovery=5.0,
        message_expire_seconds=30,
        streaming_enabled=False,
        thread_programming_enabled=False,
        feishu_ws_reconnect_delay_s=0.02,
        feishu_ws_watchdog_interval=999,
    )
    monkeypatch.setattr(ws, "get_settings", lambda: fake_settings)

    created = []

    class DummyClient:
        def __init__(self, *args, **kwargs):
            created.append(1)

        def start(self):
            # Simulate immediate exit (disconnect / internal error).
            time.sleep(0.01)

        async def _disconnect(self):
            return None

    # Avoid background watchdog behavior in this unit test.
    monkeypatch.setattr(ws, "ObservedLarkWSClient", DummyClient)
    from src.feishu.ws_health import WSHealthMonitor
    monkeypatch.setattr(WSHealthMonitor, "start_watchdog", lambda self: None)
    monkeypatch.setattr(WSHealthMonitor, "stop_watchdog", lambda self: None)

    # Avoid starting extra cache threads here; close() logic is covered elsewhere.
    monkeypatch.setattr(ws.MessageCache, "start_cleanup_thread", lambda self: None)
    monkeypatch.setattr(ws.MessageCache, "stop_cleanup_thread", lambda self: None)

    client = ws.FeishuWSClient(message_callback=lambda *a, **k: None)

    t = threading.Thread(target=client.start, daemon=True)
    t.start()

    # Wait until at least one reconnect attempt happens.
    deadline = time.time() + 1.0
    while time.time() < deadline and len(created) < 2:
        time.sleep(0.01)

    assert len(created) >= 2

    client.close()
    t.join(timeout=1.0)
    assert not t.is_alive()
