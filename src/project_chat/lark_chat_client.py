"""Feishu Chat API wrapper for project-chat binding."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .errors import CreateChatError

logger = logging.getLogger(__name__)


@dataclass
class CreateChatResult:
    """Result of creating a Feishu group chat."""
    chat_id: str
    name: str


class LarkChatClient:
    """Wraps Feishu IM v1 Chat API with retry and error handling.

    Follows the same retry/backoff pattern as FeishuIMClient._execute_with_retry.
    """

    def __init__(self, api_client_factory: Callable[[], Any], max_retries: int = 3):
        self._api_client_factory = api_client_factory
        self._max_retries = max_retries

    def create_chat(
        self,
        *,
        name: str,
        description: str,
        user_id_list: list[str],
    ) -> CreateChatResult:
        """Create a Feishu group chat. Bot is auto-added as creator.

        Raises CreateChatError on failure.
        """
        from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody

        client = self._api_client_factory()
        body = CreateChatRequestBody.builder() \
            .name(name) \
            .description(description) \
            .user_id_list(user_id_list) \
            .chat_mode("group") \
            .chat_type("private") \
            .build()
        request = CreateChatRequest.builder() \
            .user_id_type("open_id") \
            .request_body(body) \
            .build()

        last_error = None
        for attempt in range(self._max_retries):
            try:
                response = client.im.v1.chat.create(request)
                if response.success():
                    return CreateChatResult(
                        chat_id=response.data.chat_id,
                        name=name,
                    )
                last_error = f"[{response.code}] {response.msg}"
                if response.code in (230001, 230020, 99991672):
                    break
            except Exception as e:
                last_error = str(e)
            if attempt < self._max_retries - 1:
                time.sleep(0.3 * (2 ** attempt))

        raise CreateChatError(f"建群失败: {last_error}")

    def delete_chat(self, chat_id: str) -> None:
        """Delete a Feishu group chat (best-effort, for rollback).

        Does NOT raise on failure — only logs warning.
        """
        from lark_oapi.api.im.v1 import DeleteChatRequest

        client = self._api_client_factory()
        request = DeleteChatRequest.builder().chat_id(chat_id).build()

        try:
            response = client.im.v1.chat.delete(request)
            if not response.success():
                logger.warning(
                    "delete_chat(%s) failed: [%s] %s",
                    chat_id[:12], response.code, response.msg,
                )
        except Exception as e:
            logger.warning("delete_chat(%s) exception: %s", chat_id[:12], e)

    def patch_description(self, chat_id: str, description: str) -> None:
        """Update group chat description (best-effort)."""
        from lark_oapi.api.im.v1 import UpdateChatRequest, UpdateChatRequestBody

        client = self._api_client_factory()
        body = UpdateChatRequestBody.builder().description(description).build()
        request = UpdateChatRequest.builder() \
            .chat_id(chat_id) \
            .request_body(body) \
            .build()

        try:
            response = client.im.v1.chat.update(request)
            if not response.success():
                logger.warning(
                    "patch_description(%s) failed: [%s] %s",
                    chat_id[:12], response.code, response.msg,
                )
        except Exception as e:
            logger.warning("patch_description(%s) exception: %s", chat_id[:12], e)
