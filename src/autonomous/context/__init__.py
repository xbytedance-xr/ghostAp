"""Employee-scoped thread context assembly with fail-closed message source."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)


class ContextUnavailableError(RuntimeError):
    """Thread context cannot be assembled; execution must not proceed."""


class MessageSourceError(RuntimeError):
    """Message fetch from Feishu API failed."""


class ContextLayer(str, Enum):
    THREAD_FULL = "thread_full"
    GROUP_RECENT = "group_recent"
    L1_MEMORY = "l1_memory"
    L2_GROUP = "l2_group"


@dataclass(frozen=True)
class ContextMessage:
    """One message in the assembled context."""

    message_id: str
    sender_id: str
    sender_type: str
    text: str
    timestamp: float
    is_system: bool = False
    is_current: bool = False
    edited: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class ThreadWatermark:
    """Stable cursor for incremental thread fetch."""

    thread_root_id: str
    last_message_id: str
    last_timestamp: float
    message_count: int
    revision: int = 0


@dataclass(frozen=True)
class AssembledContext:
    """Final layered context ready for ACP injection."""

    thread_messages: tuple[ContextMessage, ...]
    group_messages: tuple[ContextMessage, ...]
    l1_summary: str
    l2_summary: str
    total_tokens_estimate: int
    watermark: ThreadWatermark | None
    layers_used: tuple[ContextLayer, ...]
    truncated: bool = False


class FeishuMessageSource(Protocol):
    """Port for fetching messages from Feishu API."""

    def list_thread_messages(
        self,
        *,
        chat_id: str,
        thread_root_id: str,
        page_token: str = "",
        page_size: int = 50,
    ) -> MessagePage: ...

    def list_chat_messages(
        self,
        *,
        chat_id: str,
        start_time: str = "",
        page_token: str = "",
        page_size: int = 50,
    ) -> MessagePage: ...


@dataclass(frozen=True)
class MessagePage:
    """One page of Feishu messages."""

    messages: tuple[ContextMessage, ...]
    has_more: bool
    page_token: str = ""


@dataclass
class ThreadContextConfig:
    """Configuration for context assembly."""

    max_thread_messages: int = 200
    max_group_messages: int = 50
    max_context_tokens: int = 128_000
    tokens_per_char: float = 0.3
    thread_page_size: int = 50
    group_page_size: int = 20
    fetch_timeout_seconds: float = 30.0


class EmployeeThreadContext:
    """Assembles layered context for one employee execution."""

    def __init__(
        self,
        *,
        message_source: FeishuMessageSource,
        config: ThreadContextConfig | None = None,
    ) -> None:
        self._source = message_source
        self._config = config or ThreadContextConfig()

    def assemble(
        self,
        *,
        chat_id: str,
        thread_root_id: str,
        current_message_id: str,
        l1_memory: str = "",
        l2_group_memory: str = "",
        employee_bot_id: str = "",
    ) -> AssembledContext:
        """Assemble full context with strict ordering: Thread > Group > L1 > L2."""
        layers: list[ContextLayer] = []
        thread_messages: list[ContextMessage] = []
        group_messages: list[ContextMessage] = []
        if thread_root_id:
            try:
                thread_messages = self._fetch_thread(chat_id, thread_root_id)
                layers.append(ContextLayer.THREAD_FULL)
            except (MessageSourceError, Exception) as exc:
                raise ContextUnavailableError(
                    f"thread fetch failed: {exc}"
                ) from exc
        try:
            group_messages = self._fetch_group_recent(chat_id, thread_root_id)
            layers.append(ContextLayer.GROUP_RECENT)
        except (MessageSourceError, Exception):
            pass
        if l1_memory:
            layers.append(ContextLayer.L1_MEMORY)
        if l2_group_memory:
            layers.append(ContextLayer.L2_GROUP)
        for msg in thread_messages:
            if msg.message_id == current_message_id:
                thread_messages = [
                    ContextMessage(
                        message_id=m.message_id,
                        sender_id=m.sender_id,
                        sender_type=m.sender_type,
                        text=m.text,
                        timestamp=m.timestamp,
                        is_system=m.is_system,
                        is_current=(m.message_id == current_message_id),
                        edited=m.edited,
                        deleted=m.deleted,
                    )
                    for m in thread_messages
                ]
                break
        thread_messages = self._dedup(thread_messages, group_messages)
        total_estimate = self._estimate_tokens(
            thread_messages, group_messages, l1_memory, l2_group_memory
        )
        truncated = False
        if total_estimate > self._config.max_context_tokens:
            thread_messages, group_messages, l1_memory, l2_group_memory, truncated = (
                self._trim(thread_messages, group_messages, l1_memory, l2_group_memory)
            )
            total_estimate = self._estimate_tokens(
                thread_messages, group_messages, l1_memory, l2_group_memory
            )
        watermark = None
        if thread_messages:
            last = thread_messages[-1]
            watermark = ThreadWatermark(
                thread_root_id=thread_root_id,
                last_message_id=last.message_id,
                last_timestamp=last.timestamp,
                message_count=len(thread_messages),
            )
        return AssembledContext(
            thread_messages=tuple(thread_messages),
            group_messages=tuple(group_messages),
            l1_summary=l1_memory,
            l2_summary=l2_group_memory,
            total_tokens_estimate=total_estimate,
            watermark=watermark,
            layers_used=tuple(layers),
            truncated=truncated,
        )

    def _fetch_thread(self, chat_id: str, thread_root_id: str) -> list[ContextMessage]:
        """Full paginated thread fetch."""
        messages: list[ContextMessage] = []
        page_token = ""
        while True:
            page = self._source.list_thread_messages(
                chat_id=chat_id,
                thread_root_id=thread_root_id,
                page_token=page_token,
                page_size=self._config.thread_page_size,
            )
            messages.extend(page.messages)
            if not page.has_more or len(messages) >= self._config.max_thread_messages:
                break
            page_token = page.page_token
        return messages[: self._config.max_thread_messages]

    def _fetch_group_recent(self, chat_id: str, thread_root_id: str) -> list[ContextMessage]:
        """Fetch recent group messages excluding thread messages."""
        page = self._source.list_chat_messages(
            chat_id=chat_id,
            page_size=self._config.group_page_size,
        )
        return [
            msg for msg in page.messages
            if not msg.is_system
        ][: self._config.max_group_messages]

    def _dedup(
        self,
        thread: list[ContextMessage],
        group: list[ContextMessage],
    ) -> list[ContextMessage]:
        """Remove thread messages that also appear in group."""
        thread_ids = {m.message_id for m in thread}
        deduped_group = [m for m in group if m.message_id not in thread_ids]
        group.clear()
        group.extend(deduped_group)
        return thread

    def _estimate_tokens(
        self,
        thread: list[ContextMessage],
        group: list[ContextMessage],
        l1: str,
        l2: str,
    ) -> int:
        total_chars = sum(len(m.text) for m in thread)
        total_chars += sum(len(m.text) for m in group)
        total_chars += len(l1) + len(l2)
        return int(total_chars * self._config.tokens_per_char)

    def _trim(
        self,
        thread: list[ContextMessage],
        group: list[ContextMessage],
        l1: str,
        l2: str,
    ) -> tuple[list[ContextMessage], list[ContextMessage], str, str, bool]:
        """Trim from oldest thread first, preserving L2 > L1 > group > thread ordering."""
        budget = self._config.max_context_tokens
        l2_tokens = int(len(l2) * self._config.tokens_per_char)
        l1_tokens = int(len(l1) * self._config.tokens_per_char)
        group_tokens = int(sum(len(m.text) for m in group) * self._config.tokens_per_char)
        remaining = budget - l2_tokens - l1_tokens - group_tokens
        if remaining <= 0:
            l2 = ""
            remaining = budget - l1_tokens - group_tokens
            if remaining <= 0:
                current = [m for m in thread if m.is_current]
                return current or thread[-10:], group[:5], l1[:1000], "", True
        current_msg = next((m for m in thread if m.is_current), None)
        kept: list[ContextMessage] = []
        used = 0
        if current_msg:
            current_tokens = int(len(current_msg.text) * self._config.tokens_per_char)
            kept.append(current_msg)
            used += current_tokens
        for msg in reversed(thread):
            if msg.is_current:
                continue
            msg_tokens = int(len(msg.text) * self._config.tokens_per_char)
            if used + msg_tokens > remaining:
                break
            kept.append(msg)
            used += msg_tokens
        kept.sort(key=lambda m: m.timestamp)
        return kept, group, l1, l2, True
