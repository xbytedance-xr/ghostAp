"""SyncACPSession close-path cleanup regressions."""

from __future__ import annotations

import asyncio
import threading

from src.acp.sync_adapter import SyncACPSession


def test_close_drains_pending_loop_callbacks_before_loop_close():
    marker: list[str] = []
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert started.wait(timeout=2)

    class FakeACPSession:
        async def close(self) -> None:
            asyncio.get_running_loop().call_soon(marker.append, "pipe-close-callback")

    session = SyncACPSession.__new__(SyncACPSession)
    session._agent_type = "test"
    session._loop = loop
    session._loop_thread = thread
    session._acp_session = FakeACPSession()
    session._watchdog_stop = threading.Event()
    session._watchdog_thread = None

    session.close()

    assert marker == ["pipe-close-callback"]
    assert session._loop is None
    assert not thread.is_alive()
