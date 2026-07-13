"""Employee-scoped thread context assembly with fail-closed message source."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from typing import Protocol

from .models import (
    AssembledContext,
    ContextLayer,
    ContextLayerMetrics,
    ContextMessage,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeMessageScope,
    MessageRevision,
    MessageSourceError,
    ThreadContextConfig,
    ThreadWatermark,
    TrimmingRecord,
)
from .source import (
    CredentialResolver,
    EmployeeClientBuilder,
    EmployeeMessageSourceFactory,
    EmployeeScopedMessageSource,
    MessagePage,
    ResolvedThread,
)

logger = logging.getLogger(__name__)


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
        tenant_key: str = "",
        l1_memory: str = "",
        l2_group_memory: str = "",
        employee_bot_id: str = "",
    ) -> AssembledContext:
        """Assemble full context with strict ordering: Thread > Group > L1 > L2."""
        layers: list[ContextLayer] = []
        thread_messages: list[ContextMessage] = []
        group_messages: list[ContextMessage] = []
        if thread_root_id:
            if not tenant_key:
                raise ContextUnavailableError(ContextUnavailableReason.SCOPE)
            try:
                thread_messages = self._fetch_thread(chat_id, thread_root_id)
                layers.append(ContextLayer.THREAD_FULL)
            except Exception as exc:
                logger.warning(
                    "employee thread context source failed: %s",
                    type(exc).__name__,
                )
                raise ContextUnavailableError(
                    ContextUnavailableReason.SOURCE
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
                    replace(m, is_current=(m.message_id == current_message_id))
                    for m in thread_messages
                ]
                break
        thread_messages = self._dedup(thread_messages, group_messages)
        watermark = self._build_watermark(
            tenant_key=tenant_key,
            chat_id=chat_id,
            thread_root_id=thread_root_id,
            thread_messages=thread_messages,
        )
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

    @staticmethod
    def _build_watermark(
        *,
        tenant_key: str,
        chat_id: str,
        thread_root_id: str,
        thread_messages: list[ContextMessage],
    ) -> ThreadWatermark | None:
        """Capture source identity and revision before budget trimming."""
        if not thread_messages:
            return None
        last = thread_messages[-1]
        revisions = tuple(
            MessageRevision.from_message(message).digest
            for message in thread_messages
        )
        return ThreadWatermark(
            thread_root_id=thread_root_id,
            last_message_id=last.message_id,
            last_timestamp=last.timestamp,
            message_count=len(thread_messages),
            revision=max(message.update_time_ms for message in thread_messages),
            tenant_key=tenant_key,
            chat_id=chat_id,
            feishu_thread_id=last.thread_id,
            revision_digest=hashlib.sha256(
                "".join(revisions).encode("ascii")
            ).hexdigest(),
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


__all__ = [
    "AssembledContext",
    "ContextLayer",
    "ContextLayerMetrics",
    "ContextMessage",
    "ContextUnavailableError",
    "ContextUnavailableReason",
    "CredentialResolver",
    "EmployeeClientBuilder",
    "EmployeeMessageSourceFactory",
    "EmployeeMessageScope",
    "EmployeeScopedMessageSource",
    "EmployeeThreadContext",
    "FeishuMessageSource",
    "MessagePage",
    "MessageRevision",
    "MessageSourceError",
    "ResolvedThread",
    "ThreadContextConfig",
    "ThreadWatermark",
    "TrimmingRecord",
]
