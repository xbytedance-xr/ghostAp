"""Tests for acp.renderer — ACPEventRenderer."""

import pytest

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo, ToolCallInfo
from src.acp.renderer import ACPEventRenderer


class TestACPEventRenderer:
    def setup_method(self):
        self.renderer = ACPEventRenderer()

    def test_initial_state(self):
        assert self.renderer.text_content == ""
        assert self.renderer.modified_files == set()
        assert self.renderer.completed_tool_count == 0

    def test_text_chunk(self):
        event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello ")
        self.renderer.process_event(event)
        assert self.renderer.text_content == "hello "

        event2 = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="world")
        self.renderer.process_event(event2)
        assert self.renderer.text_content == "hello world"

    def test_thought_chunk_ignored(self):
        event = ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="thinking...")
        result = self.renderer.process_event(event)
        assert self.renderer.text_content == ""

    def test_tool_call_lifecycle(self):
        # Start
        tc_start = ToolCallInfo(id="t1", title="Read file", kind="read",
                                status="in_progress", locations=["/tmp/a.py"])
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START, tool_call=tc_start,
        ))
        assert "/tmp/a.py" in self.renderer.modified_files

        # In progress — should show active tool in render
        rendered = self.renderer._render()
        assert "Read file" in rendered

        # Done
        tc_done = ToolCallInfo(id="t1", title="Read file", kind="read",
                               status="completed", locations=["/tmp/a.py"])
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc_done,
        ))
        assert self.renderer.completed_tool_count == 1

    def test_tool_done_adds_inline_summary(self):
        tc = ToolCallInfo(id="t1", title="Edit main.py", kind="edit",
                          status="completed", locations=["/tmp/main.py"])
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc,
        ))
        text = self.renderer.text_content
        assert "Edit main.py" in text
        assert "main.py" in text

    def test_plan_update(self):
        plan = PlanInfo(entries=[
            PlanEntryInfo(content="Analyze code", status="completed"),
            PlanEntryInfo(content="Write tests", status="in_progress"),
            PlanEntryInfo(content="Deploy", status="pending"),
        ])
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE, plan=plan,
        ))
        rendered = self.renderer._render()
        assert "执行计划" in rendered
        assert "Analyze code" in rendered
        assert "Write tests" in rendered

    def test_plan_update_skips_empty_entries(self):
        plan = PlanInfo(entries=[
            PlanEntryInfo(content="", status="completed"),
            PlanEntryInfo(content="   ", status="completed"),
            PlanEntryInfo(content="Step 1", status="completed"),
        ])
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE, plan=plan,
        ))
        rendered = self.renderer._render()
        assert "Step 1" in rendered
        # Should not contain blank checklist lines
        assert "✅ " not in rendered.replace("✅ Step 1", "")

    def test_get_final_content_clears_active(self):
        tc = ToolCallInfo(id="t1", title="Running", kind="execute",
                          status="in_progress")
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START, tool_call=tc,
        ))
        # Active tool should appear in render
        assert "Running" in self.renderer._render()

        # Final content should not show active tools
        final = self.renderer.get_final_content()
        # After get_final_content, active tools are cleared
        assert self.renderer._active_tools == {}

    def test_modified_files_accumulated(self):
        for i in range(3):
            tc = ToolCallInfo(id=f"t{i}", title=f"Edit f{i}.py", kind="edit",
                              status="completed", locations=[f"/tmp/f{i}.py"])
            self.renderer.process_event(ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc,
            ))
        assert len(self.renderer.modified_files) == 3

    def test_empty_render(self):
        assert self.renderer._render() == ""

    def test_render_plan_only(self):
        plan = PlanInfo(entries=[PlanEntryInfo(content="Step 1", status="pending")])
        self.renderer.process_event(ACPEvent(
            event_type=ACPEventType.PLAN_UPDATE, plan=plan,
        ))
        rendered = self.renderer._render()
        assert "Step 1" in rendered

    def test_kind_icons_coverage(self):
        """Verify different tool kinds get rendered."""
        for kind in ["read", "edit", "execute", "search", "think", "fetch"]:
            tc = ToolCallInfo(id=f"t-{kind}", title=f"Tool {kind}", kind=kind,
                              status="completed")
            self.renderer.process_event(ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc,
            ))
        assert self.renderer.completed_tool_count == 6
