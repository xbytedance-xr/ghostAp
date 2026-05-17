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
        self.renderer.process_event(event)
        assert self.renderer.text_content == ""

    def test_tool_call_lifecycle(self):
        # Start
        tc_start = ToolCallInfo(id="t1", title="Read file", kind="read", status="in_progress", locations=["/tmp/a.py"])
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc_start,
            )
        )
        assert "/tmp/a.py" in self.renderer.modified_files

        # In progress — should show active tool in render
        rendered = self.renderer._render()
        assert "Read file" in rendered

        # Done
        tc_done = ToolCallInfo(id="t1", title="Read file", kind="read", status="completed", locations=["/tmp/a.py"])
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc_done,
            )
        )
        assert self.renderer.completed_tool_count == 1

    def test_tool_done_adds_inline_summary(self):
        """After slim-flow, TOOL_CALL_DONE no longer adds inline text.

        Tool completion is tracked via completed_tool_count and rendered
        by activity_digest in the card render layer.
        """
        tc = ToolCallInfo(id="t1", title="Edit main.py", kind="edit", status="completed", locations=["/tmp/main.py"])
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc,
            )
        )
        text = self.renderer.text_content
        # No longer injected into text
        assert "Edit main.py" not in text
        # But completion is still tracked
        assert self.renderer.completed_tool_count == 1

    def test_plan_update(self):
        plan = PlanInfo(
            entries=[
                PlanEntryInfo(content="Analyze code", status="completed"),
                PlanEntryInfo(content="Write tests", status="in_progress"),
                PlanEntryInfo(content="Deploy", status="pending"),
            ]
        )
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.PLAN_UPDATE,
                plan=plan,
            )
        )
        rendered = self.renderer._render()
        assert "执行计划" in rendered
        assert "Analyze code" in rendered
        assert "Write tests" in rendered

    def test_plan_update_skips_empty_entries(self):
        plan = PlanInfo(
            entries=[
                PlanEntryInfo(content="", status="completed"),
                PlanEntryInfo(content="   ", status="completed"),
                PlanEntryInfo(content="Step 1", status="completed"),
            ]
        )
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.PLAN_UPDATE,
                plan=plan,
            )
        )
        rendered = self.renderer._render()
        assert "Step 1" in rendered
        # Should not contain blank checklist lines
        assert "✅ " not in rendered.replace("✅ Step 1", "")

    def test_get_final_content_clears_active(self):
        tc = ToolCallInfo(id="t1", title="Running", kind="execute", status="in_progress")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc,
            )
        )
        # Active tool should appear in render
        assert "Running" in self.renderer._render()

        # Final content should not show active tools
        self.renderer.get_final_content()
        # After get_final_content, active tools are cleared
        assert self.renderer._active_tools == {}

    def test_modified_files_accumulated(self):
        for i in range(3):
            tc = ToolCallInfo(
                id=f"t{i}", title=f"Edit f{i}.py", kind="edit", status="completed", locations=[f"/tmp/f{i}.py"]
            )
            self.renderer.process_event(
                ACPEvent(
                    event_type=ACPEventType.TOOL_CALL_DONE,
                    tool_call=tc,
                )
            )
        assert len(self.renderer.modified_files) == 3

    def test_empty_render(self):
        assert self.renderer._render() == ""

    def test_render_plan_only(self):
        plan = PlanInfo(entries=[PlanEntryInfo(content="Step 1", status="pending")])
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.PLAN_UPDATE,
                plan=plan,
            )
        )
        rendered = self.renderer._render()
        assert "Step 1" in rendered

    def test_kind_icons_coverage(self):
        """Verify different tool kinds get rendered."""
        for kind in ["read", "edit", "execute", "search", "think", "fetch"]:
            tc = ToolCallInfo(id=f"t-{kind}", title=f"Tool {kind}", kind=kind, status="completed")
            self.renderer.process_event(
                ACPEvent(
                    event_type=ACPEventType.TOOL_CALL_DONE,
                    tool_call=tc,
                )
            )
        assert self.renderer.completed_tool_count == 6

    def test_empty_title_tool_done_not_in_text(self):
        """Empty-title TOOL_CALL_DONE should not appear in text_content."""
        tc = ToolCallInfo(id="t1", title="", kind="other", status="completed", locations=["/tmp/x.py"])
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc,
            )
        )
        # Still counted as completed
        assert self.renderer.completed_tool_count == 1
        # But no text added (no "🔧  ✅" lines)
        assert self.renderer.text_content.strip() == ""

    def test_empty_title_active_tool_not_rendered(self):
        """Active tool with empty title should not appear in rendered output."""
        tc = ToolCallInfo(id="t1", title="", kind="other", status="in_progress")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc,
            )
        )
        rendered = self.renderer._render()
        # Should not contain the "🔧 ..." pattern for empty title
        assert "🔧" not in rendered

    def test_render_plan_view_excludes_text(self):
        """render_plan_view() should only contain plan + active tools, not text history."""
        # Add some text
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TEXT_CHUNK,
                text="Some agent output",
            )
        )
        # Add a completed tool with title (adds to text_chunks)
        tc_done = ToolCallInfo(id="t1", title="Read config", kind="read", status="completed", locations=["/tmp/cfg.py"])
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc_done,
            )
        )
        # Add a plan
        plan = PlanInfo(
            entries=[
                PlanEntryInfo(content="Step A", status="completed"),
                PlanEntryInfo(content="Step B", status="in_progress"),
            ]
        )
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.PLAN_UPDATE,
                plan=plan,
            )
        )
        # Add an active tool
        tc_active = ToolCallInfo(id="t2", title="Running tests", kind="execute", status="in_progress")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc_active,
            )
        )

        plan_view = self.renderer.render_plan_view()
        full_render = self.renderer._render()

        # Plan view should have plan and active tool
        assert "Step A" in plan_view
        assert "Step B" in plan_view
        assert "Running tests" in plan_view
        # Plan view should NOT have text chunks
        assert "Some agent output" not in plan_view
        # Full render should have text output
        assert "Some agent output" in full_render
        # Tool completions no longer appear in text (slim-flow activity_digest)

    def test_render_plan_view_empty_state(self):
        """render_plan_view() returns empty string when no plan or active tools."""
        assert self.renderer.render_plan_view() == ""

    # ------------------------------------------------------------------
    # TodoWrite content tracking
    # ------------------------------------------------------------------
    def test_todo_content_tracked_on_tool_start(self):
        """TodoWrite content should be tracked when tool starts."""
        todo_text = "✅ Research code\n🔄 Implement fix\n⏳ Run tests"
        tc = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="in_progress", content=todo_text)
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc,
            )
        )
        assert self.renderer.todo_content == todo_text
        rendered = self.renderer._render()
        assert "任务进度" in rendered
        assert "Research code" in rendered
        assert "Implement fix" in rendered

    def test_todo_content_updated_on_tool_done(self):
        """TodoWrite content should update on completion."""
        tc_start = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="in_progress", content="⏳ Step 1")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc_start,
            )
        )
        tc_done = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="completed", content="✅ Step 1")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc_done,
            )
        )
        assert self.renderer.todo_content == "✅ Step 1"

    def test_todo_done_not_in_text_chunks(self):
        """TodoWrite completion should NOT add lines to text_chunks."""
        tc = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="completed", content="✅ Done")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc,
            )
        )
        # text_content should be empty (TodoWrite doesn't pollute text buffer)
        assert self.renderer.text_content.strip() == ""
        # But completed_tool_count should still increment
        assert self.renderer.completed_tool_count == 1

    def test_todo_active_tool_not_in_active_tools_render(self):
        """Active TodoWrite should not appear in active tools section (shown in todo section instead)."""
        tc = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="in_progress", content="🔄 Working")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=tc,
            )
        )
        active_tools = self.renderer._render_active_tools()
        # Should NOT show "TodoWrite..." in active tools
        assert "TodoWrite" not in active_tools
        # But todo content should be in render
        rendered = self.renderer._render()
        assert "Working" in rendered

    def test_todo_in_plan_view(self):
        """render_plan_view() should include todo content."""
        tc = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="completed", content="✅ Step A\n🔄 Step B")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc,
            )
        )
        plan_view = self.renderer.render_plan_view()
        assert "Step A" in plan_view
        assert "Step B" in plan_view

    def test_todo_persists_across_other_tools(self):
        """Todo content should persist when other (non-todo) tools run."""
        # First, a TodoWrite
        tc_todo = ToolCallInfo(
            id="t1", title="TodoWrite", kind="other", status="completed", content="🔄 Building feature"
        )
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc_todo,
            )
        )
        # Then, a regular tool
        tc_edit = ToolCallInfo(
            id="t2", title="Edit main.py", kind="edit", status="completed", locations=["/tmp/main.py"]
        )
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc_edit,
            )
        )
        # Todo content should still be there
        assert self.renderer.todo_content == "🔄 Building feature"
        rendered = self.renderer._render()
        assert "Building feature" in rendered

    def test_todo_latest_wins(self):
        """Multiple TodoWrite calls should keep only the latest content."""
        tc1 = ToolCallInfo(id="t1", title="TodoWrite", kind="other", status="completed", content="⏳ Old task")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc1,
            )
        )
        tc2 = ToolCallInfo(id="t2", title="TodoWrite", kind="other", status="completed", content="🔄 New task")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc2,
            )
        )
        assert self.renderer.todo_content == "🔄 New task"
        assert "Old task" not in self.renderer._render()

    # ------------------------------------------------------------------
    # render_summary()
    # ------------------------------------------------------------------
    def test_render_summary_with_tools_and_files(self):
        """render_summary() should return compact summary with tool count and file count."""
        for i in range(3):
            tc = ToolCallInfo(
                id=f"t{i}", title=f"Edit f{i}.py", kind="edit", status="completed", locations=[f"/tmp/f{i}.py"]
            )
            self.renderer.process_event(
                ACPEvent(
                    event_type=ACPEventType.TOOL_CALL_DONE,
                    tool_call=tc,
                )
            )
        summary = self.renderer.render_summary()
        assert "🛠️ 3 次工具调用" in summary
        assert "🗂️ 3 个文件" in summary
        assert "·" in summary

    def test_render_summary_tools_only(self):
        """render_summary() with tools but no files."""
        tc = ToolCallInfo(id="t1", title="Think", kind="think", status="completed")
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=tc,
            )
        )
        summary = self.renderer.render_summary()
        assert "🛠️ 1 次工具调用" in summary
        assert "🗂️" not in summary

    def test_render_summary_empty(self):
        """render_summary() with no tools or files should return empty string."""
        assert self.renderer.render_summary() == ""

    # ------------------------------------------------------------------
    # get_final_content() empty scenario
    # ------------------------------------------------------------------
    def test_get_final_content_empty_returns_empty(self):
        """get_final_content() with no events returns empty string."""
        assert self.renderer.get_final_content() == ""

    def test_get_final_content_thought_only_returns_thought_fallback(self):
        """get_final_content() when only THOUGHT_CHUNKs were received returns thought content as fallback."""
        self.renderer.process_event(
            ACPEvent(
                event_type=ACPEventType.THOUGHT_CHUNK,
                text="thinking hard...",
            )
        )
        result = self.renderer.get_final_content()
        assert "thinking hard..." in result
        assert "思考过程" in result


class TestParseToolCallTodoWrite:
    """Tests for _parse_tool_call with TodoWrite raw_input extraction."""

    _MISSING = object()

    @staticmethod
    def _make_call(
        *,
        tool_call_id="tc",
        title="TodoWrite",
        kind="other",
        status="completed",
        locations=None,
        raw_input=_MISSING,
        raw_output=_MISSING,
    ):
        attrs = {
            "tool_call_id": tool_call_id,
            "title": title,
            "kind": kind,
            "status": status,
            "locations": locations,
        }
        if raw_input is not TestParseToolCallTodoWrite._MISSING:
            attrs["raw_input"] = raw_input
        if raw_output is not TestParseToolCallTodoWrite._MISSING:
            attrs["raw_output"] = raw_output
        return type("MockToolCall", (), attrs)()

    @pytest.mark.parametrize("call_kwargs, expected_substrings, not_expected_substrings, check_title", [
        # test_todo_content_from_raw_input
        (
            {
                "tool_call_id": "tc1",
                "title": "TodoWrite",
                "raw_input": {
                    "todos": [
                        {"content": "Research codebase", "status": "completed", "activeForm": "Researching codebase"},
                        {"content": "Implement fix", "status": "in_progress", "activeForm": "Implementing fix"},
                        {"content": "Run tests", "status": "pending", "activeForm": "Running tests"},
                    ]
                },
            },
            ["✅ Research codebase", "🔄 Implementing fix", "⏳ Run tests"],
            [],
            None,
        ),
        # test_todo_detected_by_raw_input_key
        (
            {
                "tool_call_id": "tc3",
                "title": "Update task list",
                "raw_input": {
                    "todos": [
                        {"content": "Task A", "status": "completed", "activeForm": "Doing A"},
                    ]
                },
            },
            ["✅ Task A"],
            [],
            None,
        ),
        # test_todo_no_raw_input — no raw_input attribute (uses _MISSING)
        (
            {
                "tool_call_id": "tc4",
                "title": "TodoWrite",
            },
            [""],  # content == ""
            [],
            None,
        ),
        # test_non_todo_tool_no_content
        (
            {
                "tool_call_id": "tc2",
                "title": "Read file",
                "kind": "read",
                "status": "in_progress",
                "raw_input": {"path": "/tmp/a.py"},
            },
            [""],  # content == ""
            [],
            None,
        ),
        # test_task_tool_extracts_description_for_child_card_label
        (
            {
                "tool_call_id": "task_1",
                "title": "task",
                "status": "in_progress",
                "raw_input": {
                    "description": "依赖分析",
                    "prompt": "检查 package.json 中的依赖是否有过时、安全或冲突问题",
                },
                "raw_output": None,
            },
            ["依赖分析", "检查 package.json"],
            [],
            "task",
        ),
    ])
    def test_parse_tool_call(self, call_kwargs, expected_substrings, not_expected_substrings, check_title):
        from src.acp.client import _parse_tool_call

        tc = _parse_tool_call(self._make_call(**call_kwargs))
        if check_title is not None:
            assert tc.title == check_title
        # Special handling: if expected is exactly [""], we assert content == ""
        if expected_substrings == [""]:
            assert tc.content == ""
        else:
            for sub in expected_substrings:
                assert sub in tc.content
        for sub in not_expected_substrings:
            assert sub not in tc.content

    def test_todo_in_progress_prefers_active_form(self):
        """In-progress items should use activeForm for display."""
        from src.acp.client import _format_todo_content

        raw_input = {
            "todos": [
                {"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing bug"},
            ]
        }
        result = _format_todo_content(raw_input)
        assert "🔄 Fixing bug" in result
        assert "Fix bug" not in result  # Should use activeForm, not content
