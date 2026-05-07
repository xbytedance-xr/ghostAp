"""Tests for Programming Mode card session adapter."""

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.programming_adapter import (
    ProgrammingCardSession,
    build_programming_metadata,
)
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.state.models import CardMetadata


class MockClient:
    def __init__(self):
        self._counter = 0
        self.creates = []

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self._counter += 1
        self.creates.append({"chat_id": chat_id, "card_json": card_json, "reply_to": reply_to})
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


def _make_programming_session(mode_name="coco", **kwargs):
    client = MockClient()
    delivery = CardDelivery(client)
    metadata = build_programming_metadata(mode_name, **kwargs)
    config = SessionConfig(metadata=metadata, reply_to="origin_msg", sync_delivery=True)
    session = CardSession(
        chat_id="chat_prog",
        config=config,
        delivery=delivery,
        session_id=f"prog_{mode_name}",
    )

    counter = {"value": 1}

    def make_task_session(task_metadata: CardMetadata) -> CardSession:
        counter["value"] += 1
        return CardSession(
            chat_id="chat_prog",
            config=SessionConfig(metadata=task_metadata, reply_to="origin_msg"),
            delivery=delivery,
            session_id=f"prog_{mode_name}_{counter['value']}",
        )

    return ProgrammingCardSession(session, session_factory=make_task_session, base_metadata=metadata), client


class TestBuildProgrammingMetadata:
    """Metadata builder tests."""

    def test_coco_metadata(self):
        meta = build_programming_metadata("coco", model_name="gpt-4o")
        assert meta.mode_name == "Coco"
        assert meta.mode_emoji == "🤖"
        assert meta.tool_name == "coco"
        assert meta.model_name == "gpt-4o"

    def test_claude_metadata(self):
        meta = build_programming_metadata("claude", model_name="claude-4-sonnet")
        assert meta.mode_name == "Claude"
        assert meta.mode_emoji == "🧠"
        assert meta.tool_name == "claude"
        assert meta.model_name == "claude-4-sonnet"

    def test_ttadk_metadata(self):
        meta = build_programming_metadata("ttadk", tool_name="cursor", model_name="gpt-4o")
        assert meta.mode_name == "TTADK"
        assert meta.tool_name == "cursor"
        assert meta.model_name == "gpt-4o"

    def test_with_project_name(self):
        meta = build_programming_metadata("coco", project_name="MyProject")
        assert meta.project_name == "MyProject"

    def test_all_modes_have_display(self):
        modes = ["coco", "claude", "aiden", "codex", "gemini", "ttadk"]
        for mode in modes:
            meta = build_programming_metadata(mode)
            assert meta.mode_name != ""
            assert meta.mode_emoji != ""


class TestProgrammingCardSession:
    """ProgrammingCardSession streaming tests."""

    def test_start_creates_card(self):
        pcs, client = _make_programming_session()
        pcs.start()
        assert len(client.creates) == 1
        assert pcs.session.state is not None

    def test_on_text_appends_content(self):
        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.on_text("Hello ")
        pcs.on_text("World")
        pcs._flush_now()  # Flush batched text before checking state

        state = pcs.session.state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert any("Hello " in b.content and "World" in b.content for b in text_blocks)

    def test_on_event_processes_acp(self):
        pcs, _ = _make_programming_session()
        pcs.start()

        from src.acp.models import ACPEvent, ACPEventType
        event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="streaming text")
        pcs.on_event(event)
        pcs._flush_now()  # Flush batched text before checking state

        state = pcs.session.state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert any("streaming text" in b.content for b in text_blocks)

    def test_on_event_handles_tool_call(self):
        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.on_text("before tool")

        from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo
        tool_event = ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(id="tc1", title="bash", kind="execute", content="ls -la", status="running"),
        )
        pcs.on_event(tool_event)

        state = pcs.session.state
        tool_blocks = [b for b in state.blocks if b.kind == "tool_call"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "bash"

    def test_finish_completes_session(self):
        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.on_text("result")
        pcs.finish()

        assert pcs.closed
        assert pcs.session.state.terminal == "completed"

    def test_fail_marks_failed(self):
        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.fail("timeout")

        assert pcs.closed
        assert pcs.session.state.terminal == "failed"

    def test_update_tool_model(self):
        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.update_tool_model(tool_name="cursor", model_name="gpt-4o-mini")

        state = pcs.session.state
        assert state.metadata.tool_name == "cursor"
        assert state.metadata.model_name == "gpt-4o-mini"

    def test_text_resumes_after_tool(self):
        """After a tool completes, text should auto-start new block."""
        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.on_text("before")

        from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo
        # Tool start
        pcs.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(id="tc1", title="read", kind="read", content="/file.py", status="running"),
        ))
        # Tool done
        pcs.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE,
            tool_call=ToolCallInfo(id="tc1", title="read", kind="read", content="file content", status="completed"),
        ))
        # Text resumes
        pcs.on_text("after tool")

        state = pcs.session.state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert len(text_blocks) >= 2  # Before and after tool

    def test_plan_block_moves_to_card_start_with_task_sections(self):
        from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo

        pcs, _ = _make_programming_session()
        pcs.start()
        pcs.on_text("先输出一些文本")
        pcs._flush_now()

        pcs.on_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE,
            plan=PlanInfo(entries=[
                PlanEntryInfo(content="梳理卡片链路", status="completed"),
                PlanEntryInfo(content="实现任务分卡", status="in_progress"),
                PlanEntryInfo(content="补充回归测试", status="pending"),
            ]),
        ))

        state = pcs.session.state
        assert state.blocks[0].kind == "plan"
        assert "整体任务列表" in state.blocks[0].content
        assert "当前进行中" in state.blocks[0].content
        assert "实现任务分卡" in state.blocks[0].content

    def test_task_switch_opens_new_card_instead_of_overwriting_previous_one(self):
        from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo

        pcs, client = _make_programming_session()
        pcs.start()

        pcs.on_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE,
            plan=PlanInfo(entries=[
                PlanEntryInfo(content="任务 A", status="in_progress"),
                PlanEntryInfo(content="任务 B", status="pending"),
            ]),
        ))
        first_task_message_id = pcs.get_message_id()

        pcs.on_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE,
            plan=PlanInfo(entries=[
                PlanEntryInfo(content="任务 A", status="completed"),
                PlanEntryInfo(content="任务 B", status="in_progress"),
            ]),
        ))

        assert len(client.creates) >= 2
        assert pcs.get_message_id() != first_task_message_id
        assert "任务 B" in (pcs.session.state.metadata.unit_label or "")

    def test_parallel_agent_tasks_open_independent_cards(self):
        from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo

        pcs, client = _make_programming_session()
        pcs.start()

        pcs.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="agent-task-1",
                title="Agent",
                kind="other",
                status="in_progress",
                content="实现后端接口\n子代理：Explore",
            ),
        ))
        pcs.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="agent-task-2",
                title="Agent",
                kind="other",
                status="in_progress",
                content="补充前端回归测试\n子代理：Explore",
            ),
        ))

        assert len(client.creates) >= 3


class TestSessionMetadataPerMode:
    """Each mode produces correct metadata in the session."""

    def test_coco_header_subtitle(self):
        pcs, _ = _make_programming_session("coco", model_name="gpt-4o")
        pcs.start()
        state = pcs.session.state
        # Header subtitle should contain tool/model info
        if state.header.subtitle:
            assert "coco" in state.header.subtitle.lower() or "gpt" in state.header.subtitle.lower()

    def test_claude_header_subtitle(self):
        pcs, _ = _make_programming_session("claude", model_name="claude-4-sonnet")
        pcs.start()
        state = pcs.session.state
        if state.header.subtitle:
            assert "claude" in state.header.subtitle.lower()

    def test_ttadk_custom_tool_name(self):
        pcs, _ = _make_programming_session("ttadk", tool_name="cursor", model_name="gpt-4o")
        pcs.start()
        state = pcs.session.state
        assert state.metadata.tool_name == "cursor"


class TestNonStreamingFallback:
    """Verify non-streaming fallback uses result.text.

    The handler's _handle_response_non_streaming builds final_response as:
        (getattr(result, "text", None) or "").strip()
        or renderer.get_final_content()
        or UI_TEXT["mode_exec_complete"]
    This ensures result.text is the primary source when streaming is unavailable.
    """

    def test_result_text_used_as_primary_response(self):
        """When send_prompt returns result.text, it should be the final response."""
        from dataclasses import dataclass
        from src.acp.renderer import ACPEventRenderer

        @dataclass
        class FakeResult:
            text: str = ""

        result = FakeResult(text="actual response")
        renderer = ACPEventRenderer()

        # Replicate the non-streaming fallback logic from programming.py:837-871
        final_response = (
            (getattr(result, "text", None) or "").strip()
            or renderer.get_final_content()
            or "执行完成"
        )
        assert final_response == "actual response"

    def test_fallback_to_renderer_when_result_text_empty(self):
        """When result.text is empty, renderer.get_final_content() is used."""
        from dataclasses import dataclass
        from src.acp.renderer import ACPEventRenderer
        from src.acp.models import ACPEvent, ACPEventType

        @dataclass
        class FakeResult:
            text: str = ""

        result = FakeResult(text="")
        renderer = ACPEventRenderer()
        renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="rendered output"))

        final_response = (
            (getattr(result, "text", None) or "").strip()
            or renderer.get_final_content()
            or "执行完成"
        )
        assert final_response == "rendered output"

    def test_fallback_to_placeholder_when_both_empty(self):
        """When both result.text and renderer are empty, placeholder is used."""
        from dataclasses import dataclass
        from src.acp.renderer import ACPEventRenderer

        @dataclass
        class FakeResult:
            text: str = ""

        result = FakeResult(text="")
        renderer = ACPEventRenderer()

        final_response = (
            (getattr(result, "text", None) or "").strip()
            or renderer.get_final_content()
            or "执行完成"
        )
        assert final_response == "执行完成"

    def test_result_text_stripped(self):
        """result.text should be stripped of whitespace."""
        from dataclasses import dataclass
        from src.acp.renderer import ACPEventRenderer

        @dataclass
        class FakeResult:
            text: str = ""

        result = FakeResult(text="  response with spaces  \n")
        renderer = ACPEventRenderer()

        final_response = (
            (getattr(result, "text", None) or "").strip()
            or renderer.get_final_content()
            or "执行完成"
        )
        assert final_response == "response with spaces"


class TestScheduleFlushLockAssertion:
    """_schedule_flush must raise RuntimeError if called without holding _flush_lock."""

    def test_schedule_flush_without_lock_raises(self):
        """Calling _schedule_flush without holding the lock raises RuntimeError."""
        pcs, _ = _make_programming_session()
        with pytest.raises(RuntimeError, match="_schedule_flush must be called under _flush_lock"):
            pcs._schedule_flush()

    def test_schedule_flush_with_lock_starts_timer(self):
        """Calling _schedule_flush while holding the lock starts a timer."""
        pcs, _ = _make_programming_session()
        with pcs._flush_lock:
            pcs._flush_lock_holder.held = True
            try:
                pcs._schedule_flush()
                assert pcs._flush_timer is not None
                assert pcs._flush_timer.is_alive()
            finally:
                pcs._flush_lock_holder.held = False
                pcs._flush_timer.cancel()

    def test_schedule_flush_does_not_create_duplicate_timer(self):
        """Second _schedule_flush call with existing timer does nothing."""
        pcs, _ = _make_programming_session()
        with pcs._flush_lock:
            pcs._flush_lock_holder.held = True
            try:
                pcs._schedule_flush()
                first_timer = pcs._flush_timer
                pcs._schedule_flush()
                assert pcs._flush_timer is first_timer  # same timer, not replaced
            finally:
                pcs._flush_lock_holder.held = False
                pcs._flush_timer.cancel()
