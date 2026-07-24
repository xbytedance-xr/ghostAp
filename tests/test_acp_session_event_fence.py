"""Regression tests for per-prompt ACP event handler ownership."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.acp.client import GhostAPClient
from src.acp.models import ACPEvent, ACPEventType
from src.acp.session import ACPSession


def _text_event(text: str) -> ACPEvent:
    return ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=text)


def test_prompt_exception_clears_handler_and_drops_late_events(
    tmp_path: Path,
) -> None:
    class FailingConnection:
        async def prompt(self, **_kwargs):
            raise RuntimeError("prompt failed")

    received: list[str] = []
    session = ACPSession(agent_cmd="test", agent_args=[], cwd=str(tmp_path))
    session._conn = FailingConnection()
    session._session_id = "session-failure"

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="prompt failed"):
            await session.prompt(
                "fail",
                on_event=lambda event: received.append(event.text or ""),
            )

        assert session._event_handler is None
        session._dispatch_event(_text_event("late-old-event"))

    asyncio.run(exercise())

    assert received == []


def test_overlapping_prompt_is_rejected_without_rebinding_real_dispatch(
    tmp_path: Path,
) -> None:
    session = ACPSession(agent_cmd="test", agent_args=[], cwd=str(tmp_path))

    class OverlappingConnection:
        def __init__(self) -> None:
            self.calls = 0
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def prompt(self, **_kwargs):
            self.calls += 1
            self.first_started.set()
            await self.release_first.wait()
            return SimpleNamespace(stop_reason="end_turn")

    first_received: list[str] = []
    second_received: list[str] = []

    async def exercise():
        connection = OverlappingConnection()
        session._conn = connection
        session._session_id = "session-overlap"

        first = asyncio.create_task(
            session.prompt(
                "first",
                on_event=lambda event: first_received.append(event.text or ""),
            )
        )
        await connection.first_started.wait()

        with pytest.raises(RuntimeError, match="already running"):
            await session.prompt(
                "second",
                on_event=lambda event: second_received.append(event.text or ""),
            )

        # Exercise the real callback entry point. The rejected prompt must not
        # replace the first prompt's handler and receive this late update.
        session._dispatch_event(_text_event("late-first"))

        connection.release_first.set()
        return await first, connection.calls

    result, calls = asyncio.run(exercise())

    assert first_received == ["late-first"]
    assert second_received == []
    assert result.text == "late-first"
    assert result.stop_reason == "end_turn"
    assert calls == 1
    assert session._event_handler is None


def test_close_clears_handler_and_releases_active_image_snapshot(
    tmp_path: Path,
) -> None:
    session = ACPSession(agent_cmd="test", agent_args=[], cwd=str(tmp_path))
    client = GhostAPClient(
        on_event=session._dispatch_event,
        root_dir=str(tmp_path),
    )
    snapshot = client.snapshot_local_images()
    session._client = client
    session._event_handler = lambda _event: None

    asyncio.run(session.close())

    assert session._event_handler is None
    assert client._current_image_snapshot() is None
    assert snapshot.active is False
