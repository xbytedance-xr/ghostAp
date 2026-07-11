"""Production composition root for the autonomous work system."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AutonomousContainer:
    """Single entry point for autonomous system lifecycle.

    Owns: journal, projections, scheduler, supervisor, manager, migration layer.
    Injected into HandlerContext for Feishu callbacks.
    """

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        mode: str = "off",
    ) -> None:
        self._data_dir = data_dir or Path("data/autonomous")
        self._mode = mode
        self._started = False
        self._supervisor: Any = None
        self._manager_handler: Any = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def supervisor(self) -> Any:
        return self._supervisor

    @property
    def manager_handler(self) -> Any:
        return self._manager_handler

    async def start(self) -> None:
        if self._mode == "off":
            logger.info("autonomous system disabled (mode=off)")
            return

        self._data_dir.mkdir(parents=True, exist_ok=True)
        logger.info("autonomous system starting (mode=%s)", self._mode)
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        logger.info("autonomous system shutting down")
        self._started = False

    def health_check(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "started": self._started,
            "data_dir": str(self._data_dir),
        }
