from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.autonomous.provisioning.channel_protocol import FrameType, decode_frame
from src.autonomous.provisioning.channel_worker import _FrameEmitter


def test_single_writer_keeps_concurrent_required_and_best_effort_frames_intact() -> None:
    event_r, event_w = os.pipe()
    emitter = _FrameEmitter(event_w, "agt_emitter", 7)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            required = tuple(
                pool.submit(
                    emitter.emit,
                    FrameType.HEALTH,
                    {"required": index},
                )
                for index in range(24)
            )
            queued = tuple(
                emitter.try_emit(FrameType.EVENT, {"notification": index})
                for index in range(24)
            )
            for future in required:
                future.result(timeout=2)
        emitter.emit(FrameType.HEALTH, {"fence": True})
        emitter.close()

        with os.fdopen(event_r, "rb", buffering=0) as stream:
            frames = tuple(decode_frame(raw) for raw in stream.readlines())

        assert all(queued)
        assert len(frames) == 49
        assert [frame.sequence for frame in frames] == list(range(1, 50))
        assert all(frame.agent_id == "agt_emitter" for frame in frames)
        assert all(frame.generation == 7 for frame in frames)
    finally:
        emitter.close()
        try:
            os.close(event_r)
        except OSError:
            pass


def test_partial_frame_timeout_closes_emitter_and_rejects_future_frames() -> None:
    event_r, event_w = os.pipe()
    emitter = _FrameEmitter(event_w, "agt_emitter", 8)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="IPC emit timed out"):
            emitter.emit(
                FrameType.EVENT,
                {"text": "x" * (200 * 1024)},
                deadline=time.monotonic() + 0.1,
            )
        assert time.monotonic() - started < 0.5
        with pytest.raises(EOFError, match="emitter failed"):
            emitter.emit(FrameType.HEALTH, {"after": "partial"})
        emitter._writer.join(timeout=0.5)
        assert not emitter._writer.is_alive()

        os.set_blocking(event_r, False)
        partial = bytearray()
        while True:
            try:
                chunk = os.read(event_r, 64 * 1024)
            except BlockingIOError:
                break
            if not chunk:
                break
            partial.extend(chunk)
        assert partial
        assert not partial.endswith(b"\n")
    finally:
        emitter.close()
        os.close(event_r)
