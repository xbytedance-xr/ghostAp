from __future__ import annotations

import asyncio
import threading

from src.autonomous.provisioning.callback_bridge import AsyncCallbackBridge


def test_synchronous_callback_runs_off_employee_event_loop() -> None:
    async def scenario() -> None:
        loop_thread = threading.get_ident()
        callback_threads: list[int] = []
        bridge = AsyncCallbackBridge()

        def blocking_callback(value: str) -> None:
            callback_threads.append(threading.get_ident())
            assert value == "polling"

        bridge.callback(blocking_callback)("polling")
        await bridge.drain()

        assert callback_threads
        assert callback_threads[0] != loop_thread

    asyncio.run(scenario())
