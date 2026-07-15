from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.autonomous.provisioning.channel_protocol import FrameType, decode_frame
from src.autonomous.provisioning.channel_worker import _FrameEmitter


class _AdmissionGateQueue:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.admission_started = threading.Event()
        self.release_admission = threading.Event()

    def _wait_for_release(self, item: object) -> None:
        if item is None:
            return
        self.admission_started.set()
        assert self.release_admission.wait(2.0)

    def put(self, item: object, timeout: float | None = None) -> None:
        self._wait_for_release(item)
        self._delegate.put(item, timeout=timeout)  # type: ignore[attr-defined]

    def put_nowait(self, item: object) -> None:
        self._wait_for_release(item)
        self._delegate.put_nowait(item)  # type: ignore[attr-defined]

    def get(self, timeout: float | None = None) -> object:
        return self._delegate.get(timeout=timeout)  # type: ignore[attr-defined,no-any-return]

    def get_nowait(self) -> object:
        return self._delegate.get_nowait()  # type: ignore[attr-defined,no-any-return]


def test_try_emit_admission_cannot_move_behind_close_sentinel() -> None:
    event_r, event_w = os.pipe()
    emitter = _FrameEmitter(event_w, "agt_try_close_race", 4)
    original_queue = emitter._requests
    gated_queue = _AdmissionGateQueue(original_queue)
    emitter._requests = gated_queue  # type: ignore[assignment]
    accepted: list[bool] = []
    close_completed = threading.Event()

    producer = threading.Thread(
        target=lambda: accepted.append(
            emitter.try_emit(FrameType.EVENT, {"race": "try_emit"})
        )
    )
    closer = threading.Thread(
        target=lambda: (emitter.close(), close_completed.set())
    )
    producer.start()
    try:
        assert gated_queue.admission_started.wait(1.0)
        closer.start()
        assert not close_completed.wait(0.1)
    finally:
        gated_queue.release_admission.set()
        producer.join(timeout=2.0)
        closer.join(timeout=2.0)
        emitter.close()
        os.close(event_r)

    assert accepted == [True]
    assert close_completed.is_set()
    assert original_queue.empty()


def test_required_emit_admission_cannot_time_out_behind_close_sentinel() -> None:
    event_r, event_w = os.pipe()
    emitter = _FrameEmitter(event_w, "agt_emit_close_race", 5)
    original_queue = emitter._requests
    gated_queue = _AdmissionGateQueue(original_queue)
    emitter._requests = gated_queue  # type: ignore[assignment]
    outcome: list[BaseException | None] = []
    close_completed = threading.Event()

    def emit() -> None:
        try:
            emitter.emit(
                FrameType.HEALTH,
                {"race": "emit"},
                deadline=time.monotonic() + 0.5,
            )
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    producer = threading.Thread(target=emit)
    closer = threading.Thread(
        target=lambda: (emitter.close(), close_completed.set())
    )
    producer.start()
    try:
        assert gated_queue.admission_started.wait(1.0)
        closer.start()
        assert not close_completed.wait(0.1)
    finally:
        gated_queue.release_admission.set()
        producer.join(timeout=2.0)
        closer.join(timeout=2.0)
        emitter.close()
        os.close(event_r)

    assert len(outcome) == 1
    assert not isinstance(outcome[0], TimeoutError)
    assert close_completed.is_set()
    assert original_queue.empty()


def test_close_waits_for_writer_to_release_the_owned_fd() -> None:
    event_r, event_w = os.pipe()
    emitter = _FrameEmitter(event_w, "agt_emitter", 6)
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def block_write(_request: object) -> None:
        entered.set()
        assert release.wait(2.0)

    emitter._write_request = block_write  # type: ignore[method-assign]
    assert emitter.try_emit(FrameType.EVENT, {"blocked": True})
    assert entered.wait(1.0)

    closer = threading.Thread(
        target=lambda: (emitter.close(), closed.set()),
        name="employee-channel-emitter-closer",
    )
    closer.start()
    try:
        assert not closed.wait(0.1)
    finally:
        release.set()
        closer.join(timeout=2.0)
        emitter.close()
        os.close(event_r)

    assert closed.is_set()
    assert not emitter._writer.is_alive()
    assert emitter._fd == -1
    with pytest.raises(OSError):
        os.fstat(event_w)


def test_close_completes_after_a_full_queue_drains() -> None:
    event_r, event_w = os.pipe()
    emitter = _FrameEmitter(event_w, "agt_full_queue_close", 7)
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    writes = 0

    def block_first_write(_request: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            entered.set()
            assert release.wait(2.0)

    emitter._write_request = block_first_write  # type: ignore[method-assign]
    assert emitter.try_emit(FrameType.EVENT, {"blocked": True})
    assert entered.wait(1.0)
    for index in range(emitter._QUEUE_CAPACITY):
        assert emitter.try_emit(FrameType.EVENT, {"queued": index})
    assert emitter._requests.full()

    closer = threading.Thread(
        target=lambda: (emitter.close(), closed.set()),
        name="employee-channel-full-queue-closer",
    )
    closer.start()
    try:
        assert not closed.wait(0.1)
        release.set()
        assert closed.wait(1.0)
    finally:
        release.set()
        if emitter._writer.is_alive():
            deadline = time.monotonic() + 1.0
            while emitter._requests.full() and time.monotonic() < deadline:
                time.sleep(0.01)
            emitter._requests.put_nowait(None)
        closer.join(timeout=2.0)
        os.close(event_r)

    assert not emitter._writer.is_alive()
    assert emitter._fd == -1


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
