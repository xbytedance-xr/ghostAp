"""Tests for acp.models — data models and enums."""

import time

from src.acp.models import (
    ACPEvent,
    ACPEventType,
    ACPSessionState,
    PlanEntryInfo,
    PlanInfo,
    PromptResult,
    ToolCallInfo,
)


class TestACPEventType:
    def test_values(self):
        assert ACPEventType.TEXT_CHUNK.value == "text_chunk"
        assert ACPEventType.THOUGHT_CHUNK.value == "thought_chunk"
        assert ACPEventType.TOOL_CALL_START.value == "tool_call_start"
        assert ACPEventType.TOOL_CALL_UPDATE.value == "tool_call_update"
        assert ACPEventType.TOOL_CALL_DONE.value == "tool_call_done"
        assert ACPEventType.PLAN_UPDATE.value == "plan_update"

    def test_all_values_unique(self):
        values = [e.value for e in ACPEventType]
        assert len(values) == len(set(values))


class TestToolCallInfo:
    def test_create(self):
        tc = ToolCallInfo(id="tc1", title="Read file", kind="read", status="completed")
        assert tc.id == "tc1"
        assert tc.title == "Read file"
        assert tc.kind == "read"
        assert tc.status == "completed"
        assert tc.content == ""
        assert tc.locations == []

    def test_with_locations(self):
        tc = ToolCallInfo(
            id="tc2", title="Edit", kind="edit", status="in_progress", locations=["/tmp/a.py", "/tmp/b.py"]
        )
        assert len(tc.locations) == 2


class TestPlanInfo:
    def test_empty_plan(self):
        plan = PlanInfo()
        assert plan.entries == []

    def test_plan_with_entries(self):
        plan = PlanInfo(
            entries=[
                PlanEntryInfo(content="step 1", status="completed"),
                PlanEntryInfo(content="step 2", status="in_progress"),
                PlanEntryInfo(content="step 3"),
            ]
        )
        assert len(plan.entries) == 3
        assert plan.entries[0].status == "completed"
        assert plan.entries[2].status == "pending"


class TestACPEvent:
    def test_text_event(self):
        event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello")
        assert event.event_type == ACPEventType.TEXT_CHUNK
        assert event.text == "hello"
        assert event.tool_call is None
        assert event.plan is None
        assert event.timestamp > 0

    def test_tool_call_event(self):
        tc = ToolCallInfo(id="tc1", title="Read", kind="read", status="completed")
        event = ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc)
        assert event.tool_call.id == "tc1"

    def test_plan_event(self):
        plan = PlanInfo(entries=[PlanEntryInfo(content="step 1")])
        event = ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan)
        assert len(event.plan.entries) == 1


class TestACPSessionState:
    def test_create(self):
        state = ACPSessionState(session_id="s1", agent_type="coco", cwd="/tmp")
        assert state.session_id == "s1"
        assert state.agent_type == "coco"
        assert state.message_count == 0
        assert state.is_active

    def test_to_dict(self):
        state = ACPSessionState(session_id="s1", agent_type="claude", cwd="/home")
        d = state.to_dict()
        assert d["session_id"] == "s1"
        assert d["agent_type"] == "claude"
        assert "created_at" in d

    def test_from_dict(self):
        d = {
            "session_id": "s1",
            "agent_type": "coco",
            "cwd": "/tmp",
            "message_count": 5,
            "is_active": False,
        }
        state = ACPSessionState.from_dict(d)
        assert state.session_id == "s1"
        assert state.message_count == 5
        assert not state.is_active

    def test_roundtrip(self):
        state = ACPSessionState(session_id="s1", agent_type="claude", cwd="/home", message_count=3, is_active=True)
        d = state.to_dict()
        state2 = ACPSessionState.from_dict(d)
        assert state2.session_id == state.session_id
        assert state2.agent_type == state.agent_type
        assert state2.message_count == state.message_count


class TestPromptResult:
    def test_create(self):
        result = PromptResult(stop_reason="end_turn", text="done")
        assert result.stop_reason == "end_turn"
        assert result.text == "done"
        assert result.tool_calls == []
        assert result.tool_results == []
        assert result.plan is None
        assert result.modified_files == set()

    def test_with_tools(self):
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="completed", locations=["/tmp/f.py"])
        result = PromptResult(
            stop_reason="end_turn",
            tool_calls=[tc],
            modified_files={"/tmp/f.py"},
        )
        assert len(result.tool_calls) == 1
        assert "/tmp/f.py" in result.modified_files

    def test_ingest_history_tracks_files(self):
        result = PromptResult(stop_reason="end_turn")
        result.ingest_history(
            [
                {"kind": "write_file", "data": {"path": "/a.py"}, "ts": time.time()},
                {"kind": "execute", "data": {"command": "echo hi", "exit_code": 0}, "ts": time.time()},
                "bad",
            ]
        )
        assert "/a.py" in result.modified_files
        assert any(e.get("kind") == "execute" for e in result.tool_results)

    def test_to_markdown_contains_sections(self):
        tc = ToolCallInfo(id="t1", title="Read", kind="read", status="completed", locations=["/tmp/a.txt"])
        result = PromptResult(stop_reason="end_turn")
        result.add_text("hello")
        result.add_tool_call(tc)
        md = result.to_markdown()
        assert "PromptResult" in md
        assert "hello" in md
        assert "工具调用" in md
        assert "改动文件" in md
