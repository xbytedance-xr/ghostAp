"""Tests for sub-reducers."""
from dataclasses import replace
from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardState, CardMetadata, ContentBlock, FooterState, HeaderState
from src.card.state.reducers.text import reduce_text
from src.card.state.reducers.tool import reduce_tool
from src.card.state.reducers.reasoning import reduce_reasoning
from src.card.state.reducers.plan import reduce_plan
from src.card.state.reducers.lifecycle import reduce_lifecycle


def _base_state() -> CardState:
    return CardState(metadata=CardMetadata(project_name="Test", mode_name="Deep Agent", mode_emoji="🧠"))


class TestTextReducer:
    def test_text_started_creates_block(self):
        s = reduce_text(_base_state(), CardEvent.text_started("b1"))
        assert len(s.blocks) == 1
        assert s.blocks[0].kind == "text"
        assert s.blocks[0].block_id == "b1"
        assert s.blocks[0].status == "active"

    def test_text_delta_appends(self):
        s = _base_state()
        s = reduce_text(s, CardEvent.text_started("b1"))
        s = reduce_text(s, CardEvent.text_delta("b1", "hello "))
        s = reduce_text(s, CardEvent.text_delta("b1", "world"))
        assert s.blocks[0].content == "hello world"

    def test_text_delta_auto_creates(self):
        s = reduce_text(_base_state(), CardEvent.text_delta("b1", "hi"))
        assert len(s.blocks) == 1
        assert s.blocks[0].content == "hi"

    def test_text_done_marks_completed(self):
        s = reduce_text(_base_state(), CardEvent.text_started("b1"))
        s = reduce_text(s, CardEvent.text_done("b1"))
        assert s.blocks[0].status == "completed"
        assert s.blocks[0].element_id is None


class TestToolReducer:
    def test_tool_started(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("t1", "bash", "ls"))
        assert len(s.blocks) == 1
        assert s.blocks[0].tool_name == "bash"
        assert s.footer.status == "tool_running"

    def test_tool_done(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("t1", "bash"))
        s = reduce_tool(s, CardEvent.tool_done("t1", tool_output="ok", tool_summary="ls -la"))
        assert s.blocks[0].status == "completed"
        assert s.blocks[0].tool_output == "ok"
        assert s.footer.status is None

    def test_tool_failed(self):
        s = reduce_tool(_base_state(), CardEvent.tool_started("t1", "bash"))
        s = reduce_tool(s, CardEvent.tool_failed("t1", error="not found"))
        assert s.blocks[0].status == "failed"
        assert s.blocks[0].tool_output == "not found"


class TestReasoningReducer:
    def test_reasoning_started(self):
        s = reduce_reasoning(_base_state(), CardEvent.reasoning_started("r1"))
        assert s.blocks[0].kind == "reasoning"
        assert s.footer.status == "thinking"

    def test_reasoning_delta_accumulates(self):
        s = reduce_reasoning(_base_state(), CardEvent.reasoning_started("r1"))
        s = reduce_reasoning(s, CardEvent.reasoning_delta("r1", "think"))
        s = reduce_reasoning(s, CardEvent.reasoning_delta("r1", "ing"))
        assert s.blocks[0].content == "thinking"
        assert s.blocks[0].char_count == 8

    def test_reasoning_done(self):
        s = reduce_reasoning(_base_state(), CardEvent.reasoning_started("r1"))
        s = reduce_reasoning(s, CardEvent.reasoning_done("r1"))
        assert s.blocks[0].status == "completed"


class TestPlanReducer:
    def test_plan_creates_block(self):
        s = reduce_plan(_base_state(), CardEvent.plan_updated("step1\nstep2"))
        assert len(s.blocks) == 1
        assert s.blocks[0].kind == "plan"
        assert s.blocks[0].content == "step1\nstep2"

    def test_plan_updates_existing(self):
        s = reduce_plan(_base_state(), CardEvent.plan_updated("v1"))
        s = reduce_plan(s, CardEvent.plan_updated("v2"))
        assert len(s.blocks) == 1
        assert s.blocks[0].content == "v2"


class TestLifecycleReducer:
    def test_started(self):
        s = reduce_lifecycle(_base_state(), CardEvent.started())
        assert s.terminal == "running"
        assert s.header.title == "🧠 Test · Deep Agent"

    def test_completed_green(self):
        s = reduce_lifecycle(_base_state(), CardEvent.completed())
        assert s.terminal == "completed"
        assert s.header.template == "green"

    def test_failed_red(self):
        s = reduce_lifecycle(_base_state(), CardEvent.failed("err"))
        assert s.terminal == "failed"
        assert s.header.template == "red"

    def test_cancelled_grey(self):
        s = reduce_lifecycle(_base_state(), CardEvent.cancelled())
        assert s.terminal == "cancelled"
        assert s.header.template == "grey"
