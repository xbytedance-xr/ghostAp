"""Secret-safe employee-owned outbound transport using official lark-oapi."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any


class EmployeeOutboundError(RuntimeError):
    """The employee application did not acknowledge an outbound operation."""


@dataclass(frozen=True, slots=True)
class EmployeeOutboundResult:
    success: bool
    message_id: str


def _text(
    value: Any,
    name: str,
    *,
    maximum: int = 100_000,
    allow_layout: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(
            unicodedata.category(character) == "Cc"
            and not (allow_layout and character in {"\n", "\t"})
            for character in value
        )
    ):
        raise ValueError(f"invalid {name}")
    return value


def _json(value: Any, name: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name}") from None
    if len(encoded.encode("utf-8")) > 100_000:
        raise ValueError(f"invalid {name}")
    return encoded


class LarkEmployeeOutbound:
    """Send only through the exact employee application's lark-oapi client."""

    def __init__(self, client: Any) -> None:
        if client is None:
            raise TypeError("employee lark client is required")
        self._client = client

    def send(
        self,
        target: str,
        message: Any,
        options: Any = None,
    ) -> EmployeeOutboundResult:
        target = _text(target, "target", maximum=256)
        msg_type, content = self._message(message)
        normalized = self._options(options)
        comment = normalized.get("comment_reply")
        if comment is not None:
            if msg_type != "text":
                raise ValueError("document comment replies must be text")
            return self._reply_comment(content, comment)
        reply_to = normalized.get("reply_to")
        if reply_to is not None:
            return self._reply_message(
                reply_to,
                msg_type,
                content,
                reply_in_thread=normalized.get("reply_in_thread", False),
                uuid_value=normalized.get("uuid"),
            )
        return self._create_message(
            target,
            msg_type,
            content,
            uuid_value=normalized.get("uuid"),
        )

    def update_card(
        self,
        message_id: str,
        card: dict[str, Any],
    ) -> EmployeeOutboundResult:
        message_id = _text(message_id, "message_id", maximum=256)
        if not isinstance(card, dict):
            raise ValueError("invalid card")
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(_json(card, "card"))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.patch(request)
        self._require_success(response)
        return EmployeeOutboundResult(True, message_id)

    @staticmethod
    def _message(message: Any) -> tuple[str, str]:
        if not isinstance(message, dict) or len(message) != 1:
            raise ValueError("invalid employee message")
        if "text" in message:
            return "text", _json(
                {
                    "text": _text(
                        message["text"],
                        "message text",
                        allow_layout=True,
                    )
                },
                "text",
            )
        if "card" in message and isinstance(message["card"], dict):
            return "interactive", _json(message["card"], "card")
        if "post" in message and isinstance(message["post"], dict):
            return "post", _json(message["post"], "post")
        raise ValueError("invalid employee message")

    @staticmethod
    def _options(options: Any) -> dict[str, Any]:
        if options is None:
            return {}
        if not isinstance(options, dict):
            raise ValueError("invalid send options")
        allowed = {"uuid", "reply_to", "reply_in_thread", "comment_reply"}
        if set(options) - allowed:
            raise ValueError("invalid send options")
        normalized = dict(options)
        for name in ("uuid", "reply_to"):
            if name in normalized:
                normalized[name] = _text(normalized[name], name, maximum=256)
        if "reply_in_thread" in normalized and type(normalized["reply_in_thread"]) is not bool:
            raise ValueError("invalid reply_in_thread")
        if "comment_reply" in normalized:
            comment = normalized["comment_reply"]
            if not isinstance(comment, dict) or set(comment) != {
                "file_token",
                "file_type",
                "comment_id",
            }:
                raise ValueError("invalid comment reply")
            comment = dict(comment)
            comment["file_token"] = _text(comment["file_token"], "file_token", maximum=256)
            comment["file_type"] = _text(comment["file_type"], "file_type", maximum=32)
            comment["comment_id"] = _text(comment["comment_id"], "comment_id", maximum=128)
            if comment["file_type"] not in {"doc", "docx", "sheet", "slides", "file"}:
                raise ValueError("invalid file_type")
            normalized["comment_reply"] = comment
        if "comment_reply" in normalized and (
            "reply_to" in normalized or "reply_in_thread" in normalized or "uuid" in normalized
        ):
            raise ValueError("ambiguous send options")
        if "reply_in_thread" in normalized and "reply_to" not in normalized:
            raise ValueError("reply_in_thread requires reply_to")
        return normalized

    def _create_message(
        self,
        target: str,
        msg_type: str,
        content: str,
        *,
        uuid_value: str | None,
    ) -> EmployeeOutboundResult:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(target)
            .msg_type(msg_type)
            .content(content)
        )
        if uuid_value is not None:
            body = body.uuid(uuid_value)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id" if target.startswith("oc_") else "open_id")
            .request_body(body.build())
            .build()
        )
        response = self._client.im.v1.message.create(request)
        return self._message_result(response)

    def _reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool,
        uuid_value: str | None,
    ) -> EmployeeOutboundResult:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .content(content)
            .msg_type(msg_type)
            .reply_in_thread(reply_in_thread)
        )
        if uuid_value is not None:
            body = body.uuid(uuid_value)
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body.build())
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        return self._message_result(response)

    def _reply_comment(
        self,
        content: str,
        comment: dict[str, str],
    ) -> EmployeeOutboundResult:
        from lark_oapi.api.drive.v1 import (
            CreateFileCommentReplyRequest,
            CreateFileCommentReplyRequestBody,
            ReplyContent,
            ReplyElement,
            TextRun,
        )

        text = json.loads(content)["text"]
        element = (
            ReplyElement.builder()
            .type("text_run")
            .text_run(TextRun.builder().text(text).build())
            .build()
        )
        body = (
            CreateFileCommentReplyRequestBody.builder()
            .content(ReplyContent.builder().elements([element]).build())
            .build()
        )
        request = (
            CreateFileCommentReplyRequest.builder()
            .file_token(comment["file_token"])
            .file_type(comment["file_type"])
            .comment_id(comment["comment_id"])
            .user_id_type("open_id")
            .request_body(body)
            .build()
        )
        response = self._client.drive.v1.file_comment_reply.create(request)
        self._require_success(response)
        reply_id = getattr(getattr(response, "data", None), "reply_id", "")
        if not isinstance(reply_id, str) or not reply_id:
            raise EmployeeOutboundError("employee comment reply returned no receipt")
        return EmployeeOutboundResult(True, reply_id)

    def _message_result(self, response: Any) -> EmployeeOutboundResult:
        self._require_success(response)
        message_id = getattr(getattr(response, "data", None), "message_id", "")
        if not isinstance(message_id, str) or not message_id:
            raise EmployeeOutboundError("employee message returned no receipt")
        return EmployeeOutboundResult(True, message_id)

    @staticmethod
    def _require_success(response: Any) -> None:
        success = getattr(response, "success", None)
        if not callable(success) or success() is not True:
            raise EmployeeOutboundError("employee outbound operation was rejected")


__all__ = [
    "EmployeeOutboundError",
    "EmployeeOutboundResult",
    "LarkEmployeeOutbound",
]
