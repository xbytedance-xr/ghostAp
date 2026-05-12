"""Feishu WebSocket lifecycle helpers.

This module keeps low-level lark-oapi WebSocket lifecycle observation out of
``ws_client.py`` so the main client can stay focused on orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

import lark_oapi as lark
from lark_oapi.ws.const import HEADER_TYPE
from lark_oapi.ws.enum import MessageType


class WSLifecycleAction(str, Enum):
    RECORD_ACTIVITY_AND_CONTINUE = "record_activity_and_continue"
    PROPAGATE = "propagate"


@dataclass(frozen=True)
class WSLifecycleErrorClassification:
    action: WSLifecycleAction
    phase: str


def classify_lifecycle_error(error: Exception, *, phase: str) -> WSLifecycleErrorClassification:
    if phase == "disconnect":
        return WSLifecycleErrorClassification(WSLifecycleAction.RECORD_ACTIVITY_AND_CONTINUE, phase)
    return WSLifecycleErrorClassification(WSLifecycleAction.PROPAGATE, phase)


def frame_header_value(frame: Any, key: str) -> Optional[str]:
    for header in getattr(frame, "headers", []) or []:
        if getattr(header, "key", None) == key:
            return getattr(header, "value", None)
    return None


class ObservedLarkWSClient(lark.ws.Client):
    """Wrap lark-oapi WS client to expose connection activity hooks."""

    def __init__(self, *args, on_activity: Callable[[str], None], **kwargs):
        super().__init__(*args, **kwargs)
        self._on_activity = on_activity

    async def _connect(self) -> None:
        await super()._connect()
        self._on_activity("connected")

    async def _disconnect(self):
        try:
            return await super()._disconnect()
        finally:
            self._on_activity("disconnected")

    async def _handle_control_frame(self, frame):
        message_type = frame_header_value(frame, HEADER_TYPE)
        if message_type == MessageType.PONG.value:
            self._on_activity("pong")
        elif message_type == MessageType.PING.value:
            self._on_activity("ping")
        else:
            self._on_activity("control")
        return await super()._handle_control_frame(frame)

    async def _handle_data_frame(self, frame):
        self._on_activity("data")
        return await super()._handle_data_frame(frame)
