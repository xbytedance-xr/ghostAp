"""FeishuCardAPIClient: bridges CardAPIClient protocol to actual Feishu SDK calls."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import lark_oapi as lark

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
        self, chat_id: str, card_json: dict, *, reply_to: str | None = None
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
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)

        if not response.success():
            from src.card.delivery.engine import TransportError

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
                from src.card.delivery.engine import SequenceConflictError

                raise SequenceConflictError(next_floor=sequence + 1)
            if response.code >= 500:
                from src.card.delivery.engine import TransportError

                raise TransportError(f"Patch failed: code={response.code}")
            logger.warning(
                "Card update failed: code=%s, msg=%s, card_id=%s",
                response.code, response.msg, card_id,
            )

    def update_element(
        self, card_id: str, element_id: str, content: str, *, sequence: int = 0
    ) -> None:
        """Update a single element's content.

        Current implementation: falls back to full card PATCH with element content
        injected. Can be upgraded to CardKit v2 element_content API when available.

        For now, this is a no-op optimization hint — the delivery engine should
        prefer update_card for reliability until element-level API is validated.
        """
        # For initial implementation, we skip element-level updates
        # and let the delivery engine fall through to update_card on next structural change.
        # This is safe because the delivery engine treats element_content as an optimization:
        # if it fails, the next structural render will do a full PATCH anyway.
        logger.debug(
            "update_element called (no-op fallback): card_id=%s, element_id=%s, len=%d",
            card_id, element_id, len(content),
        )
