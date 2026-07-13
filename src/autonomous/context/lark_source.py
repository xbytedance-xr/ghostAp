"""Official lark-oapi employee-scoped message source.

Every opened source owns a freshly built employee client. The manager bot is
deliberately not accepted as a dependency or used as a fallback.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar, cast

import lark_oapi as lark
from lark_oapi.api.application.v6 import GetApplicationRequest
from lark_oapi.api.im.v1 import GetMessageRequest, ListMessageRequest
from lark_oapi.channel.normalize import flatten, parse_message_content
from lark_oapi.channel.types import UnknownContent
from requests.exceptions import Timeout as RequestsTimeout

from ..domain.employees import BotPrincipal
from .models import (
    ContextMessage,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeMessageScope,
)
from .source import (
    CredentialResolver,
    EmployeeClientBuilder,
    MessagePage,
    ResolvedThread,
)

_T = TypeVar("_T")


def _fail(reason: ContextUnavailableReason) -> ContextUnavailableError:
    return ContextUnavailableError(reason)


def _default_client_builder(
    *,
    app_id: str,
    app_secret: str,
    timeout: float,
) -> Any:
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(lark.LogLevel.WARNING)
        .timeout(timeout)
        .build()
    )


def _platform_failure(
    response: Any,
    *,
    deleted_reason: ContextUnavailableReason,
) -> ContextUnavailableError:
    code = getattr(response, "code", None)
    if code in {230006, 230027}:
        return _fail(ContextUnavailableReason.PERMISSION)
    if code in {230002, 230050, 230073}:
        return _fail(ContextUnavailableReason.VISIBILITY)
    if code == 230110:
        return _fail(deleted_reason)
    return _fail(ContextUnavailableReason.SOURCE)


def _transport_failure(error: Exception) -> ContextUnavailableError:
    if isinstance(error, (TimeoutError, RequestsTimeout)):
        return _fail(ContextUnavailableReason.DEADLINE)
    return _fail(ContextUnavailableReason.SOURCE)


def _strict_time_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise _fail(ContextUnavailableReason.REVISION)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdecimal():
        result = int(value)
    else:
        raise _fail(ContextUnavailableReason.REVISION)
    if result < 0:
        raise _fail(ContextUnavailableReason.REVISION)
    return result


def _strict_position(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _fail(ContextUnavailableReason.ORDERING)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdecimal():
        result = int(value)
    else:
        raise _fail(ContextUnavailableReason.ORDERING)
    if result < 0:
        raise _fail(ContextUnavailableReason.ORDERING)
    return result


def _required_string(value: Any, reason: ContextUnavailableReason) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(reason)
    return value


def _message_text(message: Any, *, deleted: bool, msg_type: str) -> str:
    if deleted:
        return ""
    body = getattr(message, "body", None)
    raw_content = getattr(body, "content", None)
    if not isinstance(raw_content, str):
        raise _fail(ContextUnavailableReason.CONTENT)
    try:
        decoded = json.loads(raw_content)
    except (TypeError, ValueError):
        raise _fail(ContextUnavailableReason.CONTENT) from None
    if not isinstance(decoded, dict):
        raise _fail(ContextUnavailableReason.CONTENT)
    required_key = {
        "text": "text",
        "image": "image_key",
        "file": "file_key",
        "audio": "file_key",
        "media": "file_key",
        "video": "file_key",
        "sticker": "file_key",
        "folder": "file_key",
        "system": "template",
    }.get(msg_type)
    if required_key is not None:
        value = decoded.get(required_key)
        if not isinstance(value, str) or not value:
            raise _fail(ContextUnavailableReason.CONTENT)
    post_text = _normalize_post_content(decoded) if msg_type == "post" else ""
    interactive_text = (
        _normalize_interactive_content(decoded)
        if msg_type == "interactive"
        else ""
    )
    try:
        parsed = parse_message_content(msg_type, raw_content)
        if isinstance(parsed, UnknownContent):
            raise _fail(ContextUnavailableReason.CONTENT)
        _validate_parsed_content(parsed, msg_type=msg_type)
        text, resources = flatten(parsed)
    except ContextUnavailableError as exc:
        raise _fail(exc.reason) from None
    except Exception:
        raise _fail(ContextUnavailableReason.CONTENT) from None
    if msg_type == "post":
        return post_text
    if msg_type == "interactive":
        return interactive_text
    resource_types = {
        "image",
        "file",
        "audio",
        "media",
        "video",
        "sticker",
        "folder",
    }
    if msg_type in resource_types or resources:
        return f"[{msg_type}]"
    if not text:
        raise _fail(ContextUnavailableReason.CONTENT)
    return text


def _normalize_post_content(decoded: dict[str, Any]) -> str:
    if isinstance(decoded.get("content"), list):
        payload = decoded
    else:
        locales = ["zh_cn", "en_us", "ja_jp"]
        locale_key = next(
            (key for key in locales if isinstance(decoded.get(key), dict)),
            None,
        )
        if locale_key is None:
            locale_key = next(
                (
                    key
                    for key in sorted(decoded)
                    if isinstance(decoded.get(key), dict)
                ),
                None,
            )
        if locale_key is None:
            raise _fail(ContextUnavailableReason.CONTENT)
        payload = decoded[locale_key]
    title = payload.get("title", "")
    content = payload.get("content")
    if not isinstance(title, str) or not isinstance(content, list) or not content:
        raise _fail(ContextUnavailableReason.CONTENT)
    rendered_paragraphs: list[str] = []
    for paragraph in content:
        if not isinstance(paragraph, list) or not paragraph:
            raise _fail(ContextUnavailableReason.CONTENT)
        rendered_elements = [_normalize_post_element(element) for element in paragraph]
        rendered_paragraphs.append("".join(rendered_elements))
    body = "\n".join(rendered_paragraphs)
    rendered = "\n\n".join(part for part in (title, body) if part)
    if not rendered:
        raise _fail(ContextUnavailableReason.CONTENT)
    return rendered


def _normalize_post_element(element: Any) -> str:
    if not isinstance(element, dict):
        raise _fail(ContextUnavailableReason.CONTENT)
    tag = element.get("tag")
    if not isinstance(tag, str) or not tag:
        raise _fail(ContextUnavailableReason.CONTENT)
    if tag in {"text", "a", "code_block", "md"}:
        text = element.get("text")
        if not isinstance(text, str) or not text:
            raise _fail(ContextUnavailableReason.CONTENT)
        if tag == "a" and not isinstance(element.get("href"), str):
            raise _fail(ContextUnavailableReason.CONTENT)
        return text
    if tag == "at":
        if not isinstance(element.get("user_id"), str) or not element["user_id"]:
            raise _fail(ContextUnavailableReason.CONTENT)
        return "[@mention]"
    key_name = {
        "img": "image_key",
        "media": "file_key",
        "audio": "file_key",
        "file": "file_key",
    }.get(tag)
    if key_name is not None:
        key = element.get(key_name)
        if not isinstance(key, str) or not key:
            raise _fail(ContextUnavailableReason.CONTENT)
        return "[image]" if tag == "img" else f"[{tag}]"
    if tag == "emotion":
        if not isinstance(element.get("emoji_type"), str) or not element["emoji_type"]:
            raise _fail(ContextUnavailableReason.CONTENT)
        return "[emotion]"
    if tag == "hr":
        return "\n---\n"
    raise _fail(ContextUnavailableReason.CONTENT)


def _normalize_interactive_content(decoded: dict[str, Any]) -> str:
    has_v1_elements = isinstance(decoded.get("elements"), list)
    body = decoded.get("body")
    has_v2_body = isinstance(body, dict) and isinstance(
        body.get("elements"), list
    )
    has_header = isinstance(decoded.get("header"), dict)
    has_default_title = isinstance(decoded.get("title"), str)
    if not (has_v1_elements or has_v2_body or has_header or has_default_title):
        raise _fail(ContextUnavailableReason.CONTENT)
    text_parts: list[str] = []
    _collect_card_text(decoded, text_parts, key="")
    normalized = "\n".join(
        part for index, part in enumerate(text_parts) if part not in text_parts[:index]
    )
    return normalized or "[interactive]"


def _collect_card_text(value: Any, output: list[str], *, key: str) -> None:
    ignored_keys = {
        "action",
        "behaviors",
        "fallback",
        "file_key",
        "href",
        "image_key",
        "multi_url",
        "url",
        "value",
    }
    text_keys = {"content", "markdown", "text", "title"}
    if key in ignored_keys:
        return
    if isinstance(value, str):
        if key in text_keys and value:
            output.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_card_text(item, output, key=key)
        return
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _collect_card_text(child_value, output, key=child_key)


def _validate_parsed_content(parsed: Any, *, msg_type: str) -> None:
    required_attribute = {
        "share_chat": "chat_id",
        "share_user": "user_id",
        "hongbao": "text",
        "general_calendar": "summary",
        "share_calendar_event": "summary",
        "video_chat": "topic",
        "calendar": "summary",
        "vote": "topic",
    }.get(msg_type)
    if required_attribute is not None:
        value = getattr(parsed, required_attribute, None)
        if not isinstance(value, str) or not value:
            raise _fail(ContextUnavailableReason.CONTENT)
    if msg_type == "interactive":
        card = getattr(parsed, "card", None)
        if not isinstance(card, dict) or not card:
            raise _fail(ContextUnavailableReason.CONTENT)
        if not {"header", "elements", "body", "schema"}.intersection(card):
            raise _fail(ContextUnavailableReason.CONTENT)
    if msg_type == "location":
        if (
            not isinstance(getattr(parsed, "name", None), str)
            or not getattr(parsed, "name", "")
            or getattr(parsed, "longitude", None) is None
            or getattr(parsed, "latitude", None) is None
        ):
            raise _fail(ContextUnavailableReason.CONTENT)
    if msg_type == "todo":
        title = getattr(parsed, "title", None)
        body = getattr(parsed, "body", None)
        if not all(isinstance(value, str) for value in (title, body)) or not (
            title or body
        ):
            raise _fail(ContextUnavailableReason.CONTENT)
    if msg_type == "merge_forward":
        raw = getattr(parsed, "raw", None)
        if not isinstance(raw, dict) or raw.get("content") != (
            "Merged and Forwarded Message"
        ):
            raise _fail(ContextUnavailableReason.CONTENT)


@dataclass
class _TraversalState:
    expected_token: str = ""
    returned_tokens: set[str] = field(default_factory=set)
    message_ids: set[str] = field(default_factory=set)
    message_positions: set[int] = field(default_factory=set)
    thread_positions: set[int] = field(default_factory=set)
    last_message_position: int | None = None
    last_thread_position: int | None = None
    last_message: ContextMessage | None = None

    def reset(self) -> None:
        self.expected_token = ""
        self.returned_tokens.clear()
        self.message_ids.clear()
        self.message_positions.clear()
        self.thread_positions.clear()
        self.last_message_position = None
        self.last_thread_position = None
        self.last_message = None

    def start_page(self, page_token: str) -> None:
        if not page_token:
            if self.expected_token:
                raise _fail(ContextUnavailableReason.PAGINATION)
            self.reset()
            return
        if page_token != self.expected_token:
            raise _fail(ContextUnavailableReason.PAGINATION)

    def finish_page(self, *, has_more: bool, next_token: str) -> None:
        if has_more:
            if not next_token or next_token in self.returned_tokens:
                raise _fail(ContextUnavailableReason.PAGINATION)
            self.returned_tokens.add(next_token)
            self.expected_token = next_token
        else:
            self.expected_token = ""


def _normalize_message(
    message: Any,
    *,
    scope: EmployeeMessageScope,
    expected_thread_id: str | None,
) -> ContextMessage:
    message_id = _required_string(
        getattr(message, "message_id", None),
        ContextUnavailableReason.ROOT_THREAD_BINDING,
    )
    chat_id = _required_string(
        getattr(message, "chat_id", None),
        ContextUnavailableReason.SCOPE,
    )
    if chat_id != scope.chat_id:
        raise _fail(ContextUnavailableReason.SCOPE)

    thread_id = getattr(message, "thread_id", "") or ""
    root_id = getattr(message, "root_id", "") or ""
    parent_id = getattr(message, "parent_id", "") or ""
    if not all(isinstance(value, str) for value in (thread_id, root_id, parent_id)):
        raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
    if expected_thread_id is not None:
        if thread_id != expected_thread_id:
            raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
        if message_id == scope.thread_root_message_id:
            if root_id not in ("", scope.thread_root_message_id):
                raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
        elif root_id != scope.thread_root_message_id:
            raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)

    sender = getattr(message, "sender", None)
    sender_id = _required_string(
        getattr(sender, "id", None), ContextUnavailableReason.CONTENT
    )
    sender_id_type = _required_string(
        getattr(sender, "id_type", None), ContextUnavailableReason.CONTENT
    )
    sender_type = _required_string(
        getattr(sender, "sender_type", None), ContextUnavailableReason.CONTENT
    )
    sender_tenant_key = _required_string(
        getattr(sender, "tenant_key", None), ContextUnavailableReason.CONTENT
    )
    msg_type = _required_string(
        getattr(message, "msg_type", None), ContextUnavailableReason.CONTENT
    )
    create_time_ms = _strict_time_ms(getattr(message, "create_time", None))
    update_time_ms = _strict_time_ms(getattr(message, "update_time", None))
    if update_time_ms < create_time_ms:
        raise _fail(ContextUnavailableReason.REVISION)
    deleted = getattr(message, "deleted", None)
    updated = getattr(message, "updated", None)
    if not isinstance(deleted, bool) or not isinstance(updated, bool):
        raise _fail(ContextUnavailableReason.REVISION)

    try:
        return ContextMessage(
            message_id=message_id,
            sender_id=sender_id,
            sender_type=sender_type,
            text=_message_text(message, deleted=deleted, msg_type=msg_type),
            timestamp=create_time_ms / 1000,
            is_system=msg_type == "system",
            edited=updated,
            deleted=deleted,
            chat_id=chat_id,
            thread_id=thread_id,
            root_id=root_id,
            parent_id=parent_id,
            sender_id_type=sender_id_type,
            sender_tenant_key=sender_tenant_key,
            msg_type=msg_type,
            create_time_ms=create_time_ms,
            update_time_ms=update_time_ms,
            message_position=_strict_position(
                getattr(message, "message_position", None)
            ),
            thread_message_position=_strict_position(
                getattr(message, "thread_message_position", None)
            ),
        )
    except ContextUnavailableError as exc:
        raise _fail(exc.reason) from None
    except (TypeError, ValueError):
        raise _fail(ContextUnavailableReason.CONTENT) from None


@dataclass(repr=False, eq=False)
class _LarkEmployeeMessageSource:
    scope: EmployeeMessageScope
    _client: Any = field(repr=False)
    _resolved: ResolvedThread | None = field(default=None, init=False, repr=False)
    _thread_state: _TraversalState = field(
        default_factory=_TraversalState,
        init=False,
        repr=False,
    )
    _chat_state: _TraversalState = field(
        default_factory=_TraversalState,
        init=False,
        repr=False,
    )
    _state_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )
    _operation_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )
    _generation: int = field(default=0, init=False, repr=False)
    closed: bool = field(default=False, init=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(scope={self.scope!r}, "
            f"closed={self.closed!r})"
        )

    def __enter__(self) -> _LarkEmployeeMessageSource:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        with self._state_lock:
            client = self._client
            self._client = None
            self._resolved = None
            self._thread_state = _TraversalState()
            self._chat_state = _TraversalState()
            self._generation += 1
            self.closed = True
        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    def _require_open(self) -> None:
        with self._state_lock:
            if self.closed or self._client is None:
                raise _fail(ContextUnavailableReason.SOURCE)

    def _begin_operation(self) -> int:
        with self._state_lock:
            if self.closed or self._client is None:
                raise _fail(ContextUnavailableReason.SOURCE)
            return self._generation

    def _require_generation(self, generation: int) -> None:
        if (
            generation != self._generation
            or self.closed
            or self._client is None
        ):
            raise _fail(ContextUnavailableReason.SOURCE)

    def _message_api(self) -> Any:
        with self._state_lock:
            if self.closed or self._client is None:
                raise _fail(ContextUnavailableReason.SOURCE)
            try:
                return self._client.im.v1.message
            except Exception as exc:
                raise _transport_failure(exc) from None

    def resolve_thread(self) -> ResolvedThread:
        with self._operation_lock:
            return self._resolve_thread()

    def _resolve_thread(self) -> ResolvedThread:
        generation = self._begin_operation()
        request = (
            GetMessageRequest.builder()
            .message_id(self.scope.current_message_id)
            .user_id_type("open_id")
            .card_msg_content_type("user_card_content")
            .build()
        )
        try:
            response = self._message_api().get(request)
        except ContextUnavailableError as exc:
            raise _fail(exc.reason) from None
        except Exception as exc:
            raise _transport_failure(exc) from None
        self._require_open()
        try:
            data = self._require_response_data(
                response,
                deleted_reason=ContextUnavailableReason.CURRENT_MESSAGE,
            )
            items = getattr(data, "items", None)
            if not isinstance(items, (list, tuple)) or len(items) != 1:
                raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
            message = items[0]
            message_id = getattr(message, "message_id", None)
            chat_id = getattr(message, "chat_id", None)
            thread_id = getattr(message, "thread_id", None)
            raw_root_id = getattr(message, "root_id", None)
            if raw_root_id is None:
                root_id = ""
            elif isinstance(raw_root_id, str):
                root_id = raw_root_id
            else:
                raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
            if (
                message_id != self.scope.current_message_id
                or chat_id != self.scope.chat_id
                or not isinstance(thread_id, str)
                or re.fullmatch(r"omt_[A-Za-z0-9][A-Za-z0-9_-]*", thread_id)
                is None
            ):
                raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
            if message_id == self.scope.thread_root_message_id:
                root_matches = root_id in ("", self.scope.thread_root_message_id)
            else:
                root_matches = root_id == self.scope.thread_root_message_id
            if not root_matches:
                raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
            if self.scope.feishu_thread_id and thread_id != self.scope.feishu_thread_id:
                raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
            deleted = getattr(message, "deleted", None)
            if not isinstance(deleted, bool):
                raise _fail(ContextUnavailableReason.REVISION)
            if deleted:
                raise _fail(ContextUnavailableReason.CURRENT_MESSAGE)
            resolved = ResolvedThread(
                thread_root_message_id=self.scope.thread_root_message_id,
                feishu_thread_id=thread_id,
                current_message_id=self.scope.current_message_id,
            )
            with self._state_lock:
                self._require_generation(generation)
                self._resolved = resolved
                return resolved
        except ContextUnavailableError as exc:
            raise _fail(exc.reason) from None
        except Exception as exc:
            raise _transport_failure(exc) from None

    @staticmethod
    def _require_response_data(
        response: Any,
        *,
        deleted_reason: ContextUnavailableReason,
    ) -> Any:
        if response is None:
            raise _fail(ContextUnavailableReason.SOURCE)
        success_method = getattr(response, "success", None)
        if not callable(success_method):
            raise _fail(ContextUnavailableReason.SOURCE)
        success = success_method()
        if not isinstance(success, bool):
            raise _fail(ContextUnavailableReason.SOURCE)
        if not success:
            raise _platform_failure(response, deleted_reason=deleted_reason)
        data = getattr(response, "data", None)
        if data is None:
            raise _fail(ContextUnavailableReason.SOURCE)
        return data

    def list_thread_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        with self._operation_lock:
            with self._state_lock:
                self._require_generation(self._generation)
                resolved = self._resolved
                if resolved is None:
                    raise _fail(ContextUnavailableReason.ROOT_THREAD_BINDING)
                thread_id = resolved.feishu_thread_id
            return self._list_messages(
                container_id_type="thread",
                container_id=thread_id,
                sort_type="ByCreateTimeAsc",
                page_token=page_token,
                page_size=page_size,
                expected_thread_id=thread_id,
            )

    def list_chat_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 20,
    ) -> MessagePage:
        with self._operation_lock:
            return self._list_messages(
                container_id_type="chat",
                container_id=self.scope.chat_id,
                sort_type="ByCreateTimeDesc",
                page_token=page_token,
                page_size=page_size,
                expected_thread_id=None,
            )

    def reset_chat_traversal(self) -> None:
        """Abort a bounded recent-chat window without closing the lease."""
        with self._operation_lock:
            generation = self._begin_operation()
            with self._state_lock:
                self._require_generation(generation)
                self._chat_state.reset()

    def _list_messages(
        self,
        *,
        container_id_type: str,
        container_id: str,
        sort_type: str,
        page_token: str,
        page_size: int,
        expected_thread_id: str | None,
    ) -> MessagePage:
        generation = self._begin_operation()
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 50
            or not isinstance(page_token, str)
        ):
            raise _fail(ContextUnavailableReason.PAGINATION)
        state = (
            self._thread_state
            if expected_thread_id is not None
            else self._chat_state
        )
        state.start_page(page_token)
        builder = (
            ListMessageRequest.builder()
            .container_id_type(container_id_type)
            .container_id(container_id)
            .sort_type(sort_type)
            .page_size(page_size)
            .card_msg_content_type("user_card_content")
        )
        if page_token:
            builder = builder.page_token(page_token)
        request = builder.build()
        try:
            response = self._message_api().list(request)
        except ContextUnavailableError as exc:
            raise _fail(exc.reason) from None
        except Exception as exc:
            raise _transport_failure(exc) from None
        self._require_open()
        try:
            data = self._require_response_data(
                response,
                deleted_reason=ContextUnavailableReason.SOURCE,
            )
            items = getattr(data, "items", None)
            has_more = getattr(data, "has_more", None)
            next_token = getattr(data, "page_token", "") or ""
            if (
                not isinstance(items, (list, tuple))
                or not isinstance(has_more, bool)
                or not isinstance(next_token, str)
            ):
                raise _fail(ContextUnavailableReason.PAGINATION)
            messages = tuple(
                _normalize_message(
                    item,
                    scope=self.scope,
                    expected_thread_id=expected_thread_id,
                )
                for item in items
            )
            self._validate_page_order(
                messages,
                ascending=expected_thread_id is not None,
                state=state,
                thread_page=expected_thread_id is not None,
            )
            result = MessagePage(
                messages=messages,
                has_more=has_more,
                page_token=next_token,
            )
            with self._state_lock:
                self._require_generation(generation)
                state.finish_page(has_more=has_more, next_token=next_token)
                return result
        except ContextUnavailableError as exc:
            raise _fail(exc.reason) from None
        except Exception as exc:
            raise _transport_failure(exc) from None

    @staticmethod
    def _validate_page_order(
        messages: tuple[ContextMessage, ...],
        *,
        ascending: bool,
        state: _TraversalState,
        thread_page: bool,
    ) -> None:
        for message in messages:
            if message.message_id in state.message_ids:
                raise _fail(ContextUnavailableReason.ORDERING)
            state.message_ids.add(message.message_id)
            positions = [(message.message_position, state.message_positions)]
            if thread_page:
                positions.append(
                    (message.thread_message_position, state.thread_positions)
                )
            for position, seen in positions:
                if position is not None:
                    if position in seen:
                        raise _fail(ContextUnavailableReason.ORDERING)
                    seen.add(position)
            for position, last_attribute in (
                (message.message_position, "last_message_position"),
                (
                    message.thread_message_position if thread_page else None,
                    "last_thread_position",
                ),
            ):
                if position is None:
                    continue
                previous_position = getattr(state, last_attribute)
                if previous_position is not None:
                    moved_backwards = (
                        position <= previous_position
                        if ascending
                        else position >= previous_position
                    )
                    if moved_backwards:
                        raise _fail(ContextUnavailableReason.ORDERING)
                setattr(state, last_attribute, position)
            previous = state.last_message
            if previous is not None:
                invalid = (
                    message.create_time_ms < previous.create_time_ms
                    if ascending
                    else message.create_time_ms > previous.create_time_ms
                )
                if invalid:
                    raise _fail(ContextUnavailableReason.ORDERING)
            state.last_message = message


@dataclass(repr=False)
class _EmployeeSourceLease:
    _factory: LarkEmployeeMessageSourceFactory = field(repr=False)
    scope: EmployeeMessageScope
    _principal: BotPrincipal = field(repr=False)
    _source: _LarkEmployeeMessageSource | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )
    _entered: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._source is None or self._source.closed

    def __enter__(self) -> _EmployeeSourceLease:
        with self._lock:
            if self._entered:
                raise _fail(ContextUnavailableReason.SOURCE)
            self._entered = True
            failure_reason: ContextUnavailableReason | None = None
            try:
                self._source = self._factory._acquire(
                    scope=self.scope,
                    principal=self._principal,
                )
            except ContextUnavailableError as exc:
                failure_reason = exc.reason
            except Exception:
                failure_reason = ContextUnavailableReason.CREDENTIALS
            if failure_reason is not None:
                self._source = None
                raise _fail(failure_reason)
            return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        with self._lock:
            source = self._source
            self._source = None
        if source is not None:
            self._factory._release(source)

    def _begin_call(self) -> _LarkEmployeeMessageSource:
        with self._lock:
            if self._source is None:
                raise _fail(ContextUnavailableReason.SOURCE)
            source = self._source
            self._factory._begin_call(source)
            return source

    def resolve_thread(self) -> ResolvedThread:
        return self._invoke(lambda source: source.resolve_thread())

    def list_thread_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        return self._invoke(
            lambda source: source.list_thread_messages(
                page_token=page_token,
                page_size=page_size,
            )
        )

    def list_chat_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 20,
    ) -> MessagePage:
        return self._invoke(
            lambda source: source.list_chat_messages(
                page_token=page_token,
                page_size=page_size,
            )
        )

    def reset_chat_traversal(self) -> None:
        self._invoke(lambda source: source.reset_chat_traversal())

    def _invoke(
        self,
        operation: Callable[[_LarkEmployeeMessageSource], _T],
    ) -> _T:
        source = self._begin_call()
        failure_reason: ContextUnavailableReason | None = None
        result: _T | None = None
        try:
            try:
                result = operation(source)
            except ContextUnavailableError as exc:
                failure_reason = exc.reason
            except Exception:
                failure_reason = ContextUnavailableReason.SOURCE
        finally:
            self._factory._end_call(source)
        if failure_reason is not None:
            raise _fail(failure_reason)
        return cast(_T, result)


@dataclass(repr=False, init=False)
class LarkEmployeeMessageSourceFactory:
    credential_resolver: CredentialResolver
    request_timeout_seconds: float
    _client_builder: EmployeeClientBuilder = field(repr=False)
    _active_sources: set[_LarkEmployeeMessageSource] = field(repr=False)
    _lock: threading.RLock = field(repr=False, compare=False)
    _condition: threading.Condition = field(repr=False, compare=False)
    _closed: threading.Event = field(repr=False, compare=False)
    _pending_acquires: int = field(repr=False)
    _active_calls: int = field(repr=False)

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self.credential_resolver = credential_resolver
        self.request_timeout_seconds = request_timeout_seconds
        self._client_builder = _default_client_builder
        self._active_sources = set()
        self._invalidated_agents: set[str] = set()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._closed = threading.Event()
        self._pending_acquires = 0
        self._active_calls = 0
        self._pending_by_agent: dict[str, int] = {}
        self._active_calls_by_agent: dict[str, int] = {}
        self.__post_init__()

    @classmethod
    def _with_client_builder_for_testing(
        cls,
        *,
        credential_resolver: CredentialResolver,
        client_builder: EmployeeClientBuilder,
        request_timeout_seconds: float = 10.0,
    ) -> LarkEmployeeMessageSourceFactory:
        factory = cls(
            credential_resolver=credential_resolver,
            request_timeout_seconds=request_timeout_seconds,
        )
        if not callable(client_builder):
            raise TypeError("client_builder must be callable")
        factory._client_builder = client_builder
        return factory

    def __post_init__(self) -> None:
        timeout = self.request_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("request_timeout_seconds must be positive and finite")
        if not callable(getattr(self.credential_resolver, "resolve", None)):
            raise TypeError("credential_resolver must provide resolve")
        if not callable(self._client_builder):
            raise TypeError("client_builder must be callable")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(request_timeout_seconds="
            f"{self.request_timeout_seconds!r})"
        )

    def open(
        self,
        *,
        scope: EmployeeMessageScope,
        principal: BotPrincipal,
    ) -> _EmployeeSourceLease:
        with self._condition:
            if (
                not isinstance(scope, EmployeeMessageScope)
                or not isinstance(principal, BotPrincipal)
                or self._closed.is_set()
                or scope.agent_id in self._invalidated_agents
            ):
                raise _fail(ContextUnavailableReason.CREDENTIALS)
        return _EmployeeSourceLease(
            _factory=self,
            scope=scope,
            _principal=principal,
        )

    def probe(self, principal: BotPrincipal) -> bool:
        """Verify employee credentials and app identity via an employee client."""
        with self._condition:
            if (
                self._closed.is_set()
                or not isinstance(principal, BotPrincipal)
                or not principal.app_id
                or not principal.credential_ref
                or principal.agent_id in self._invalidated_agents
            ):
                return False
            self._pending_acquires += 1
            self._increment_agent_count(
                self._pending_by_agent,
                principal.agent_id,
            )
        try:
            secret = self.credential_resolver.resolve(
                principal.credential_ref,
                principal.agent_id,
                principal.app_id,
            )
            if not isinstance(secret, str) or not secret:
                return False
            client = self._client_builder(
                app_id=principal.app_id,
                app_secret=secret,
                timeout=float(self.request_timeout_seconds),
            )
            request = (
                GetApplicationRequest.builder()
                .app_id(principal.app_id)
                .build()
            )
            response = client.application.v6.application.get(request)
            success = getattr(response, "success", None)
            data = getattr(response, "data", None)
            app = getattr(data, "app", None)
            with self._condition:
                return (
                    client is not None
                    and callable(success)
                    and success() is True
                    and getattr(response, "code", None) == 0
                    and getattr(app, "app_id", None) == principal.app_id
                    and not self._closed.is_set()
                    and principal.agent_id not in self._invalidated_agents
                )
        except Exception:
            return False
        finally:
            if "secret" in locals():
                del secret
            if "client" in locals():
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                del client
            if "request" in locals():
                del request
            if "response" in locals():
                del response
            with self._condition:
                self._pending_acquires -= 1
                self._decrement_agent_count(
                    self._pending_by_agent,
                    principal.agent_id,
                )
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed.set()
            sources = tuple(self._active_sources)
        for source in sources:
            source.close()
        with self._condition:
            while self._pending_acquires or self._active_calls:
                self._condition.wait()
            self._active_sources.clear()

    def invalidate_employee(self, agent_id: str) -> None:
        """Revoke every active lease for one employee and wait for calls to drain."""
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id is required")
        with self._condition:
            self._invalidated_agents.add(agent_id)
            sources = tuple(
                source
                for source in self._active_sources
                if source.scope.agent_id == agent_id
            )
        for source in sources:
            source.close()
        with self._condition:
            while (
                self._pending_by_agent.get(agent_id, 0)
                or self._active_calls_by_agent.get(agent_id, 0)
            ):
                self._condition.wait()
            for source in sources:
                self._active_sources.discard(source)

    def reactivate_employee(self, agent_id: str) -> None:
        """Allow fresh leases only after a caller has installed a new binding."""
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id is required")
        with self._condition:
            if self._closed.is_set():
                raise _fail(ContextUnavailableReason.CREDENTIALS)
            self._invalidated_agents.discard(agent_id)

    def _acquire(
        self,
        *,
        scope: EmployeeMessageScope,
        principal: BotPrincipal,
    ) -> _LarkEmployeeMessageSource:
        with self._condition:
            if (
                self._closed.is_set()
                or scope.agent_id in self._invalidated_agents
            ):
                raise _fail(ContextUnavailableReason.CREDENTIALS)
            self._pending_acquires += 1
            self._increment_agent_count(
                self._pending_by_agent,
                scope.agent_id,
            )
        source: _LarkEmployeeMessageSource | None = None
        try:
            source = self._build_source(scope=scope, principal=principal)
            with self._condition:
                if (
                    self._closed.is_set()
                    or scope.agent_id in self._invalidated_agents
                ):
                    source.close()
                    raise _fail(ContextUnavailableReason.CREDENTIALS)
                self._active_sources.add(source)
            return source
        finally:
            with self._condition:
                self._pending_acquires -= 1
                self._decrement_agent_count(
                    self._pending_by_agent,
                    scope.agent_id,
                )
                self._condition.notify_all()

    def _build_source(
        self,
        *,
        scope: EmployeeMessageScope,
        principal: BotPrincipal,
    ) -> _LarkEmployeeMessageSource:
        if not isinstance(scope, EmployeeMessageScope) or not isinstance(
            principal, BotPrincipal
        ):
            raise _fail(ContextUnavailableReason.CREDENTIALS)
        scope_identity = (
            scope.tenant_key,
            scope.agent_id,
            scope.bot_principal_id,
            scope.app_id,
        )
        principal_identity = (
            principal.tenant_key,
            principal.agent_id,
            principal.bot_principal_id,
            principal.app_id,
        )
        if scope_identity != principal_identity or not principal.credential_ref:
            raise _fail(ContextUnavailableReason.CREDENTIALS)
        try:
            secret = self.credential_resolver.resolve(
                principal.credential_ref,
                principal.agent_id,
                principal.app_id,
            )
            if not isinstance(secret, str) or not secret:
                raise _fail(ContextUnavailableReason.CREDENTIALS)
            client = self._client_builder(
                app_id=principal.app_id,
                app_secret=secret,
                timeout=float(self.request_timeout_seconds),
            )
            if client is None:
                raise _fail(ContextUnavailableReason.CREDENTIALS)
        except ContextUnavailableError:
            raise _fail(ContextUnavailableReason.CREDENTIALS) from None
        except Exception:
            raise _fail(ContextUnavailableReason.CREDENTIALS) from None
        finally:
            if "secret" in locals():
                del secret
        return _LarkEmployeeMessageSource(scope=scope, _client=client)

    def _release(self, source: _LarkEmployeeMessageSource) -> None:
        source.close()
        with self._condition:
            while self._active_calls_by_agent.get(source.scope.agent_id, 0):
                self._condition.wait()
            self._active_sources.discard(source)

    def _begin_call(self, source: _LarkEmployeeMessageSource) -> None:
        with self._condition:
            if (
                self._closed.is_set()
                or source not in self._active_sources
                or source.closed
            ):
                raise _fail(ContextUnavailableReason.SOURCE)
            self._active_calls += 1
            self._increment_agent_count(
                self._active_calls_by_agent,
                source.scope.agent_id,
            )

    def _end_call(self, source: _LarkEmployeeMessageSource) -> None:
        with self._condition:
            self._active_calls -= 1
            self._decrement_agent_count(
                self._active_calls_by_agent,
                source.scope.agent_id,
            )
            self._condition.notify_all()

    @staticmethod
    def _increment_agent_count(counts: dict[str, int], agent_id: str) -> None:
        counts[agent_id] = counts.get(agent_id, 0) + 1

    @staticmethod
    def _decrement_agent_count(counts: dict[str, int], agent_id: str) -> None:
        remaining = counts.get(agent_id, 0) - 1
        if remaining > 0:
            counts[agent_id] = remaining
        else:
            counts.pop(agent_id, None)


__all__ = ["LarkEmployeeMessageSourceFactory"]
