"""Monotonic journal anchor contracts."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from .frame import GENESIS_HASH


@dataclass(frozen=True)
class AnchorState:
    """The externally anchored journal high-water mark."""

    sequence: int = 0
    frame_hash: str = GENESIS_HASH


class AnchorProvider(Protocol):
    """Monotonic compare-and-swap boundary for journal commits."""

    def read(self) -> AnchorState: ...

    def compare_and_swap(
        self,
        expected_sequence: int,
        expected_hash: str,
        new_sequence: int,
        new_hash: str,
    ) -> bool: ...


class MemoryAnchor:
    """Thread-safe in-memory anchor for tests and offline probes only."""

    def __init__(self) -> None:
        self._state = AnchorState()
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def read(self) -> AnchorState:
        with self._lock:
            return self._state

    def compare_and_swap(
        self,
        expected_sequence: int,
        expected_hash: str,
        new_sequence: int,
        new_hash: str,
    ) -> bool:
        if (
            isinstance(expected_sequence, bool)
            or isinstance(new_sequence, bool)
            or expected_sequence < 0
            or new_sequence != expected_sequence + 1
            or len(expected_hash) != 64
            or len(new_hash) != 64
        ):
            return False
        with self._lock:
            if self._state != AnchorState(expected_sequence, expected_hash):
                return False
            self._state = AnchorState(new_sequence, new_hash)
            return True
