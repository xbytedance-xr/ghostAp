"""Tests for card event types and conversion."""
import pytest
from src.card.events import CardEvent, CardEventType
from src.acp.models import ACPEvent, ACPEventType, ToolCallInfo, PlanInfo, PlanEntryInfo


class TestCardEventCreation:
    def test_all_event_types_exist(self):
        assert len(CardEventType) == 21

    def test_started_factory(self):
        e = CardEvent.started()
        assert e.type == CardEventType.STARTED
        assert e.payload == {}

    def test_completed_factory(self):
        e = CardEvent.completed()
        assert e.type == CardEventType.COMPLETED

    def test_failed_factory(self):
        e = CardEvent.failed("oops")
        assert e.type == CardEventType.FAILED
        assert e.payload["error"] == "oops"

    def test_text_delta_factory(self):
        e = CardEvent.text_delta("b1", "hello")
        assert e.type == CardEventType.TEXT_DELTA
        assert e.payload == {"block_id": "b1", "text": "hello"}

    def test_tool_started_factory(self):
        e = CardEvent.tool_started("t1", "bash", "ls -la")
        assert e.payload == {"block_id": "t1", "tool_name": "bash", "tool_input": "ls -la"}

    def test_frozen(self):
        e = CardEvent.started()
        with pytest.raises(Exception):
            e.type = CardEventType.COMPLETED


class TestFromACP:
    def test_text_chunk(self):
        acp = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hi")
        ce = CardEvent.from_acp(acp)
        assert ce.type == CardEventType.TEXT_DELTA
        assert ce.payload["text"] == "hi"

    def test_thought_chunk(self):
        acp = ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="hmm")
        ce = CardEvent.from_acp(acp)
        assert ce.type == CardEventType.REASONING_DELTA
        assert ce.payload["text"] == "hmm"

    def test_tool_call_start(self):
        tc = ToolCallInfo(id="tc1", title="bash", kind="execute", status="in_progress", content="ls")
        acp = ACPEvent(event_type=ACPEventType.TOOL_CALL_START, tool_call=tc)
        ce = CardEvent.from_acp(acp)
        assert ce.type == CardEventType.TOOL_STARTED
        assert ce.payload["block_id"] == "tc1"
        assert ce.payload["tool_name"] == "bash"

    def test_tool_call_done(self):
        tc = ToolCallInfo(id="tc1", title="bash", kind="execute", status="completed", content="output")
        acp = ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc)
        ce = CardEvent.from_acp(acp)
        assert ce.type == CardEventType.TOOL_DONE
        assert ce.payload["tool_output"] == "output"

    def test_tool_call_done_failed(self):
        tc = ToolCallInfo(id="tc1", title="bash", kind="execute", status="failed", content="err")
        acp = ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc)
        ce = CardEvent.from_acp(acp)
        assert ce.type == CardEventType.TOOL_FAILED
        assert ce.payload["error"] == "err"

    def test_plan_update(self):
        plan = PlanInfo(entries=[
            PlanEntryInfo(content="Step 1", status="completed"),
            PlanEntryInfo(content="Step 2", status="in_progress"),
            PlanEntryInfo(content="Step 3", status="pending"),
        ])
        acp = ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan)
        ce = CardEvent.from_acp(acp)
        assert ce.type == CardEventType.PLAN_UPDATED
        assert "✅ Step 1" in ce.payload["content"]
        assert "⏳ Step 2" in ce.payload["content"]
        assert "○ Step 3" in ce.payload["content"]
