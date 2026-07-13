"""Tests for employee thread context assembly."""

from __future__ import annotations

import pytest

from src.autonomous.context import (
    ContextLayer,
    ContextMessage,
    ContextUnavailableError,
    EmployeeThreadContext,
    MessagePage,
    MessageSourceError,
    ThreadContextConfig,
)


class _FakeSource:
    def __init__(
        self,
        *,
        thread_messages: list[ContextMessage] | None = None,
        group_messages: list[ContextMessage] | None = None,
        thread_fail: bool = False,
    ) -> None:
        self._thread = thread_messages or []
        self._group = group_messages or []
        self._thread_fail = thread_fail

    def list_thread_messages(self, *, chat_id, thread_root_id, page_token="", page_size=50):
        if self._thread_fail:
            raise MessageSourceError("API error")
        return MessagePage(messages=tuple(self._thread), has_more=False)

    def list_chat_messages(self, *, chat_id, start_time="", page_token="", page_size=50):
        return MessagePage(messages=tuple(self._group), has_more=False)


def _msg(msg_id: str, text: str = "hello", ts: float = 1000.0) -> ContextMessage:
    canonical_id = msg_id if msg_id.startswith("om_") else f"om_{msg_id}"
    return ContextMessage(
        message_id=canonical_id,
        sender_id="ou_1",
        sender_type="user",
        text=text,
        timestamp=ts,
        chat_id="oc_1",
        thread_id="omt_1",
        root_id="om_root",
        sender_id_type="open_id",
        sender_tenant_key="tenant_1",
        msg_type="text",
    )


class TestThreadContextAssembly:
    def test_assembles_thread_and_group(self) -> None:
        source = _FakeSource(
            thread_messages=[_msg("t1", "thread msg", 100), _msg("t2", "thread 2", 200)],
            group_messages=[_msg("g1", "group msg", 50)],
        )
        ctx = EmployeeThreadContext(message_source=source)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_t2",
            tenant_key="tenant_1",
            l1_memory="# Memory",
            l2_group_memory="# Group",
        )
        assert len(result.thread_messages) == 2
        assert len(result.group_messages) == 1
        assert ContextLayer.THREAD_FULL in result.layers_used
        assert ContextLayer.GROUP_RECENT in result.layers_used
        assert ContextLayer.L1_MEMORY in result.layers_used
        assert ContextLayer.L2_GROUP in result.layers_used
        assert result.watermark is not None
        assert result.watermark.message_count == 2

    def test_thread_fetch_failure_raises_context_unavailable(self) -> None:
        source = _FakeSource(thread_fail=True)
        ctx = EmployeeThreadContext(message_source=source)
        with pytest.raises(ContextUnavailableError, match="CONTEXT_UNAVAILABLE:source"):
            ctx.assemble(
                chat_id="oc_1",
                thread_root_id="om_root",
                current_message_id="om_t1",
                tenant_key="tenant_1",
            )

    def test_no_thread_root_skips_thread_fetch(self) -> None:
        source = _FakeSource(
            group_messages=[_msg("g1", "group")],
        )
        ctx = EmployeeThreadContext(message_source=source)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="",
            current_message_id="om_g1",
        )
        assert len(result.thread_messages) == 0
        assert ContextLayer.THREAD_FULL not in result.layers_used

    def test_deduplication_removes_thread_from_group(self) -> None:
        shared_msg = _msg("shared_1", "appears in both")
        source = _FakeSource(
            thread_messages=[shared_msg, _msg("t2", "only thread")],
            group_messages=[shared_msg, _msg("g2", "only group")],
        )
        ctx = EmployeeThreadContext(message_source=source)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_t2",
            tenant_key="tenant_1",
        )
        assert len(result.group_messages) == 1
        assert result.group_messages[0].message_id == "om_g2"

    def test_trimming_when_over_budget(self) -> None:
        long_msgs = [_msg(f"t{i}", "x" * 10000, ts=float(i)) for i in range(50)]
        source = _FakeSource(thread_messages=long_msgs)
        config = ThreadContextConfig(max_context_tokens=1000)
        ctx = EmployeeThreadContext(message_source=source, config=config)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_t49",
            tenant_key="tenant_1",
        )
        assert result.truncated
        assert len(result.thread_messages) < 50
        assert result.watermark is not None
        assert result.watermark.message_count == 50
        assert result.watermark.last_message_id == "om_t49"

    def test_current_message_marked(self) -> None:
        source = _FakeSource(
            thread_messages=[_msg("t1", "first"), _msg("t2", "current")],
        )
        ctx = EmployeeThreadContext(message_source=source)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_t2",
            tenant_key="tenant_1",
        )
        current_msgs = [m for m in result.thread_messages if m.is_current]
        assert len(current_msgs) == 1
        assert current_msgs[0].message_id == "om_t2"

    def test_marking_current_preserves_revision_and_scope_fields(self) -> None:
        message = ContextMessage(
            message_id="om_current",
            sender_id="ou_1",
            sender_type="user",
            text="current",
            timestamp=1.0,
            chat_id="oc_1",
            thread_id="omt_1",
            root_id="om_root",
            sender_id_type="open_id",
            sender_tenant_key="external_tenant",
            msg_type="text",
            create_time_ms=1000,
            update_time_ms=2000,
            message_position=4,
            thread_message_position=3,
            edited=True,
        )
        result = EmployeeThreadContext(
            message_source=_FakeSource(thread_messages=[message])
        ).assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_current",
            tenant_key="tenant_1",
        )

        current = result.thread_messages[0]
        assert current.is_current is True
        assert current.chat_id == "oc_1"
        assert current.thread_id == "omt_1"
        assert current.root_id == "om_root"
        assert current.update_time_ms == 2000
        assert current.message_position == 4
        assert current.thread_message_position == 3
        assert result.watermark is not None
        assert result.watermark.tenant_key == "tenant_1"

    def test_thread_scope_requires_authoritative_tenant_before_source_read(self) -> None:
        source = _FakeSource(thread_messages=[_msg("t1")])

        with pytest.raises(ContextUnavailableError, match="CONTEXT_UNAVAILABLE:scope"):
            EmployeeThreadContext(message_source=source).assemble(
                chat_id="oc_1",
                thread_root_id="om_root",
                current_message_id="om_t1",
            )

    def test_watermark_captures_last_message(self) -> None:
        source = _FakeSource(
            thread_messages=[_msg("t1", ts=100), _msg("t2", ts=200), _msg("t3", ts=300)],
        )
        ctx = EmployeeThreadContext(message_source=source)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_t3",
            tenant_key="tenant_1",
        )
        assert result.watermark.last_message_id == "om_t3"
        assert result.watermark.last_timestamp == 300.0
        assert result.watermark.message_count == 3

    def test_empty_l1_and_l2_excluded_from_layers(self) -> None:
        source = _FakeSource(thread_messages=[_msg("t1")])
        ctx = EmployeeThreadContext(message_source=source)
        result = ctx.assemble(
            chat_id="oc_1",
            thread_root_id="om_root",
            current_message_id="om_t1",
            tenant_key="tenant_1",
            l1_memory="",
            l2_group_memory="",
        )
        assert ContextLayer.L1_MEMORY not in result.layers_used
        assert ContextLayer.L2_GROUP not in result.layers_used
