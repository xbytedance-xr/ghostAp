"""Reusable repo-lock heartbeat thread.

``RepoLockHeartbeat`` encapsulates the daemon-thread + ``Event.wait`` loop
pattern that was previously duplicated across ``lock_helper.py`` and
``programming.py`` (non-streaming and streaming safety-net paths).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class RepoLockHeartbeat:
    """Periodically calls *touch_fn* in a daemon thread until stopped.

    Parameters
    ----------
    stop_event:
        A ``threading.Event`` that, when set, terminates the loop.
    touch_fn:
        Callable invoked each heartbeat (e.g. ``repo_lock_mgr.touch``).
    interval:
        Seconds between heartbeats (default 30).
    max_beats:
        Optional upper bound on iterations.  ``None`` means unlimited.
    name:
        Thread name suffix for diagnostics.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        touch_fn: Callable[[], None],
        interval: float = 30,
        max_beats: Optional[int] = None,
        name: str = "heartbeat",
    ) -> None:
        self._stop = stop_event
        self._touch = touch_fn
        self._interval = interval
        self._max_beats = max_beats
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"RepoLockHeartbeat-{name}",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat thread."""
        self._thread.start()

    def join(self, timeout: float = 2) -> None:
        """Wait for the heartbeat thread to finish."""
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        beat = 0
        while not self._stop.wait(self._interval):
            beat += 1
            if self._max_beats is not None and beat > self._max_beats:
                logger.debug(
                    "Heartbeat max beats reached (%d), stopping",
                    self._max_beats,
                )
                break
            try:
                self._touch()
            except Exception as exc:
                logger.debug("Heartbeat touch failed: %s", str(exc))
