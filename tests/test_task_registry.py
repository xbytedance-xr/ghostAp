"""Tests for src.tasking.registry module."""
from __future__ import annotations

import asyncio
import pytest

from src.tasking.registry import TaskRegistry


class TestTaskRegistry:
    """TaskRegistry unit tests."""

    def test_track_adds_task(self):
        reg = TaskRegistry()
        loop = asyncio.new_event_loop()
        try:
            async def _coro():
                await asyncio.sleep(100)

            task = loop.create_task(_coro())
            reg.track(task)
            assert len(reg.list_active_tasks()) == 1
            task.cancel()
            # Suppress "coroutine never awaited" by letting cancellation propagate
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        finally:
            loop.close()

    def test_done_task_auto_removed(self):
        reg = TaskRegistry()

        async def _run():
            async def _quick():
                return 42

            task = asyncio.get_event_loop().create_task(_quick())
            reg.track(task)
            await task
            # After the callback fires the task should be removed
            await asyncio.sleep(0)
            assert len(reg.list_active_tasks()) == 0

        asyncio.run(_run())

    @pytest.mark.asyncio
    async def test_close_cancels_tasks(self):
        reg = TaskRegistry()

        async def _slow():
            await asyncio.sleep(100)

        task = asyncio.get_event_loop().create_task(_slow())
        reg.track(task)
        assert len(reg.list_active_tasks()) == 1

        await reg.close(timeout=1.0)
        assert task.cancelled() or task.done()

    def test_track_during_closing_cancels_immediately(self):
        reg = TaskRegistry()

        async def _run():
            await reg.close(timeout=0.1)

            async def _late():
                await asyncio.sleep(100)

            task = asyncio.get_event_loop().create_task(_late())
            reg.track(task)
            # Yield control so cancellation propagates
            await asyncio.sleep(0)
            assert task.cancelled() or task.done()

        asyncio.run(_run())

    def test_list_active_tasks_is_snapshot(self):
        reg = TaskRegistry()
        tasks = reg.list_active_tasks()
        assert tasks == []
