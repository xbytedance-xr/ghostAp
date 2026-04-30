"""Safe callback invocation utilities."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def safe_invoke(
    callback: Optional[Callable[..., Any]],
    *args: Any,
    label: str = "callback",
) -> None:
    """Invoke a callback safely, logging any exception at DEBUG level.

    Does nothing if callback is None.
    """
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        logger.debug("%s failed", label, exc_info=True)
