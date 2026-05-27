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


# ---------------------------------------------------------------------------
# WS lifecycle tests (merged from test_ws_lifecycle.py)
# ---------------------------------------------------------------------------


def test_ws_lifecycle_helpers_are_extracted_from_ws_client():
    from src.feishu.ws_lifecycle import ObservedLarkWSClient, frame_header_value

    frame = SimpleNamespace(
        headers=[
            SimpleNamespace(key="irrelevant", value="x"),
            SimpleNamespace(key="type", value="pong"),
        ]
    )

    assert ObservedLarkWSClient.__name__ == "ObservedLarkWSClient"
    assert frame_header_value(frame, "type") == "pong"
    assert frame_header_value(frame, "missing") is None


def test_lifecycle_fatal_errors_are_not_silently_swallowed():
    from src.feishu.ws_lifecycle import WSLifecycleAction, classify_lifecycle_error

    disconnect = classify_lifecycle_error(RuntimeError("disconnect cleanup"), phase="disconnect")
    assert disconnect.action == WSLifecycleAction.RECORD_ACTIVITY_AND_CONTINUE

    data = classify_lifecycle_error(RuntimeError("bad frame"), phase="data_frame")
    assert data.action == WSLifecycleAction.PROPAGATE

    startup = classify_lifecycle_error(RuntimeError("auth failed"), phase="startup")
    assert startup.action == WSLifecycleAction.PROPAGATE


# ---------------------------------------------------------------------------
# WS resource manager tests (merged from test_ws_resource_manager.py)
# ---------------------------------------------------------------------------


class _Engine:
    def __init__(self, running: bool):
        self.is_running = running
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True
        self.is_running = False


class _Manager:
    def __init__(self, engines):
        self._engines = engines
        self.cleaned = False

    def list_engines(self):
        return self._engines

    def cleanup_all(self):
        self.cleaned = True


def test_engine_resource_group_stops_running_engines_and_cleans_manager():
    from src.feishu.ws_resource_manager import EngineResourceGroup

    running = _Engine(True)
    stopped = _Engine(False)
    manager = _Manager([running, stopped])

    group = EngineResourceGroup("test", manager)

    engines = group.stop_running_engines()
    group.wait_stopped(engines, timeout_s=0.1, interval_s=0.001)
    group.cleanup_all()

    assert running.stopped is True
    assert stopped.stopped is False
    assert manager.cleaned is True
