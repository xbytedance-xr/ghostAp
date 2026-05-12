"""FeishuCardAPIClient: bridges CardAPIClient protocol to actual Feishu SDK calls."""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import lark_oapi as lark

from src.card.delivery.types import SequenceConflictError, TransportError
from src.config import get_settings

logger = logging.getLogger(__name__)


class FeishuCardAPIClient:
    """Implements CardAPIClient protocol using lark_oapi SDK.

    Maps the abstract card operations to concrete Feishu IM APIs:
    - create_card → im.v1.message.reply / im.v1.message.create
    - update_card → im.v1.message.patch
    - update_element → im.v1.message.patch (full card rebuild;
      can be upgraded to cardkit/v2/elements API later)

    Note: In Feishu's API, card_id == message_id for interactive messages.
    The `create_card` returns (message_id, message_id) since there's no
    separate card_id at the message-level API.
    """

    def __init__(self, client: "lark.Client") -> None:
        self._client = client
        self._settings = get_settings()

    def create_card(
        self,
        chat_id: str,
        card_json: dict,
        *,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[str, str]:
        """Create a card message. Returns (message_id, card_id).

        Uses reply API if reply_to is set, otherwise creates directly.
        Respects reply_in_thread setting from config.
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        content = json.dumps(card_json, ensure_ascii=False)

        if reply_to:
            reply_in_thread = self._settings.default_reply_mode == "thread"
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(content)
                    .reply_in_thread(reply_in_thread)
                    .uuid(idempotency_key)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.reply(request)
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .uuid(idempotency_key)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)

        if not response.success():
            raise TransportError(
                f"Card create failed: code={response.code}, msg={response.msg}"
            )

        message_id = response.data.message_id
        # In Feishu IM API, card_id == message_id for interactive messages
        return message_id, message_id

    def update_card(self, card_id: str, card_json: dict, *, sequence: int = 0) -> None:
        """Update (PATCH) a card by card_id (== message_id).

        Uses im.v1.message.patch to replace the entire card content.
        Sequence conflicts (code 300317) are raised as SequenceConflictError.
        """
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        content = json.dumps(card_json, ensure_ascii=False)

        request = (
            PatchMessageRequest.builder()
            .message_id(card_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            )
            .build()
        )

        response = self._client.im.v1.message.patch(request)

        if not response.success():
            if response.code == 300317:
                raise SequenceConflictError(next_floor=sequence + 1)
            raise TransportError(
                f"Patch failed: code={response.code}, msg={response.msg}, card_id={card_id}",
                code=response.code,
            )

    def update_element(
        self, card_id: str, element_id: str, content: str, *, sequence: int = 0
    ) -> None:
        """Update a single element's content via CardKit v2 element_content API."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        request = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(content)
                .sequence(sequence)
                .uuid(str(_uuid.uuid4()))
                .build()
            )
            .build()
        )

        response = self._client.cardkit.v1.card_element.content(request)

        if not response.success():
            if response.code == 300317:  # Sequence conflict
                raise SequenceConflictError(next_floor=sequence + 1)
            raise TransportError(
                f"Element update failed: code={response.code}, msg={response.msg}",
                code=response.code,
            )

    def create_streaming_card(self, card_json: dict) -> str:
        """Create a card entity via CardKit API with streaming mode enabled.

        The card_json must have "config": {"streaming_mode": true} set.
        Returns the card_id from the created card entity.
        """
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest,
            CreateCardRequestBody,
        )

        # Ensure streaming_mode is set in config
        config = card_json.setdefault("config", {})
        config["streaming_mode"] = True

        request = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card_json, ensure_ascii=False))
                .build()
            )
            .build()
        )

        response = self._client.cardkit.v1.card.create(request)

        if not response.success():
            raise TransportError(
                f"Streaming card create failed: code={response.code}, msg={response.msg}"
            )

        return response.data.card_id

    def send_card_reference(
        self,
        chat_id: str,
        card_id: str,
        *,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Send an IM message referencing a CardKit card entity.

        Returns the message_id of the sent message.
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        content = json.dumps({"type": "card", "data": {"card_id": card_id}})

        if reply_to:
            reply_in_thread = self._settings.default_reply_mode == "thread"
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(content)
                    .reply_in_thread(reply_in_thread)
                    .uuid(idempotency_key)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.reply(request)
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .uuid(idempotency_key)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)

        if not response.success():
            raise TransportError(
                f"Card reference send failed: code={response.code}, msg={response.msg}"
            )

        return response.data.message_id
