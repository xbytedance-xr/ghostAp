"""Publish ACP image events into the transport-neutral card state."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.card.events import CardEvent

if TYPE_CHECKING:
    from src.acp.models import ACPEvent, ACPImageInfo
    from src.card.protocols import Dispatchable

logger = logging.getLogger(__name__)


class ACPImagePublisher:
    """Upload each ACP image once and dispatch only its Feishu image key."""

    def __init__(
        self,
        dispatchable: "Dispatchable",
        image_uploader: Callable[["ACPImageInfo"], str | None] | None = None,
    ) -> None:
        self._dispatchable = dispatchable
        self._image_uploader = image_uploader
        self._seen: set[str] = set()
        self._inflight: set[str] = set()
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def bind(self, dispatchable: "Dispatchable") -> None:
        """Route future media events to a rotated card session."""
        with self._lock:
            self._dispatchable = dispatchable

    def handle(self, event: "ACPEvent") -> bool:
        """Handle an image event, returning False for non-image ACP events."""
        from src.acp.models import ACPEventType

        if event.event_type is not ACPEventType.IMAGE_CHUNK:
            return False
        image = event.image
        if image is None:
            logger.warning("ACP image event missing image payload")
            return True

        image_id = image.image_id
        with self._lock:
            if image_id in self._seen or image_id in self._inflight:
                return True
            self._inflight.add(image_id)

        image_key: str | None = None
        try:
            if self._image_uploader is not None:
                image_key = self._image_uploader(image)
        except Exception as exc:
            logger.warning(
                "ACP image publication failed: %s",
                type(exc).__name__,
            )

        with self._lock:
            self._inflight.discard(image_id)
            self._seen.add(image_id)
            dispatchable = self._dispatchable

        if image_key:
            dispatchable.dispatch(
                CardEvent.image_added(image_id, image_key, image.name)
            )
        else:
            dispatchable.dispatch(CardEvent.image_failed(image_id, image.name))
        return True
