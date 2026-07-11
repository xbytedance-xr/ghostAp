"""Feishu adapter — durable ingress and delivery via official lark-oapi SDK.

Uses lark-oapi for REST API calls (send messages, create bots, manage apps)
and lark-channel-sdk for WebSocket event subscriptions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

logger = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    success: bool
    message_id: str = ""
    error: str = ""


@dataclass
class IngressEvent:
    event_id: str
    event_type: str
    tenant_key: str
    chat_id: str
    user_id: str
    message_id: str
    text: str = ""
    command: str = ""
    args: str = ""
    received_at: float = field(default_factory=time.time)


class FeishuAdapter:
    """Durable Feishu ingress/delivery adapter using official lark-oapi SDK.

    Responsibilities:
    - Parse incoming WebSocket events into IngressEvent
    - Deliver cards and messages via lark-oapi REST API
    - Persist ingress before acknowledgement (durable inbox contract)
    - Support employee bot provisioning via app management API
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        lark_client: lark.Client | None = None,
    ) -> None:
        self._client = lark_client or lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .build()

    def parse_event(self, raw: dict[str, Any]) -> IngressEvent | None:
        """Parse raw WebSocket event into structured IngressEvent."""
        header = raw.get("header", {})
        event = raw.get("event", {})
        message = event.get("message", {})

        event_type = header.get("event_type", "")
        if event_type != "im.message.receive_v1":
            return None

        content = message.get("content", "{}")
        import json
        try:
            parsed_content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed_content = {}

        text = parsed_content.get("text", "")
        command = ""
        args = ""
        if text.startswith("/"):
            parts = text.split(None, 1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else ""

        return IngressEvent(
            event_id=header.get("event_id", ""),
            event_type=event_type,
            tenant_key=header.get("tenant_key", ""),
            chat_id=message.get("chat_id", ""),
            user_id=event.get("sender", {}).get("sender_id", {}).get("open_id", ""),
            message_id=message.get("message_id", ""),
            text=text,
            command=command,
            args=args,
        )

    async def send_message(
        self,
        chat_id: str,
        content: str,
        msg_type: str = "text",
    ) -> DeliveryResult:
        """Send a message to a chat using lark-oapi."""
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            ).build()

        try:
            response = self._client.im.v1.message.create(request)
            if response.success():
                return DeliveryResult(
                    success=True,
                    message_id=response.data.message_id if response.data else "",
                )
            return DeliveryResult(
                success=False,
                error=f"code={response.code}, msg={response.msg}",
            )
        except Exception as exc:
            logger.error("Failed to send message: %s", exc)
            return DeliveryResult(success=False, error=str(exc))

    async def reply_message(
        self,
        message_id: str,
        content: str,
        msg_type: str = "text",
    ) -> DeliveryResult:
        """Reply to a specific message using lark-oapi."""
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .build()
            ).build()

        try:
            response = self._client.im.v1.message.reply(request)
            if response.success():
                return DeliveryResult(
                    success=True,
                    message_id=response.data.message_id if response.data else "",
                )
            return DeliveryResult(
                success=False,
                error=f"code={response.code}, msg={response.msg}",
            )
        except Exception as exc:
            logger.error("Failed to reply message: %s", exc)
            return DeliveryResult(success=False, error=str(exc))

    async def send_card(
        self,
        chat_id: str,
        card_content: dict[str, Any],
    ) -> DeliveryResult:
        """Send interactive card using lark-oapi."""
        import json
        return await self.send_message(
            chat_id=chat_id,
            content=json.dumps(card_content, ensure_ascii=False),
            msg_type="interactive",
        )

    async def update_card(
        self,
        message_id: str,
        card_content: dict[str, Any],
    ) -> DeliveryResult:
        """Update an existing card message."""
        import json

        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card_content, ensure_ascii=False))
                .build()
            ).build()

        try:
            response = self._client.im.v1.message.patch(request)
            if response.success():
                return DeliveryResult(success=True, message_id=message_id)
            return DeliveryResult(
                success=False,
                error=f"code={response.code}, msg={response.msg}",
            )
        except Exception as exc:
            logger.error("Failed to update card: %s", exc)
            return DeliveryResult(success=False, error=str(exc))
