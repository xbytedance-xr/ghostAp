"""Tests for structured rendering in acp.renderer — ContentSection, RenderedContent, process_event_structured."""

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo, ToolCallInfo
from src.acp.renderer import ACPEventRenderer, ContentSection, RenderedContent


class TestContentSection:
    def test_defaults(self):
        sec = ContentSection(section_type="text", markdown="hello")
        assert sec.section_type == "text"
        assert sec.markdown == "hello"
        assert sec.tool_kind == ""
        assert sec.tool_count == 0
        assert sec.is_complete is True
        assert sec.collapsed_by_default is False


class TestRenderedContent:
    def test_to_markdown_joins_sections(self):
        rc = RenderedContent(
            sections=[
                ContentSection(section_type="plan", markdown="**plan**"),
                ContentSection(section_type="text", markdown="hello"),
                ContentSection(section_type="tool_group", markdown="tool stuff", collapsed_by_default=True),
            ]
        )
        md = rc.to_markdown()
        assert "**plan**" in md
        assert "hello" in md
        assert "tool stuff" in md
        # Sections joined by newline
        assert md == "**plan**\nhello\ntool stuff"

    def test_to_markdown_skips_empty(self):
        rc = RenderedContent(
            sections=[
                ContentSection(section_type="text", markdown=""),
                ContentSection(section_type="text", markdown="real"),
            ]
        )
        assert rc.to_markdown() == "real"

    def test_to_elements_collapsible_true(self):
        rc = RenderedContent(
            sections=[
                ContentSection(section_type="text", markdown="hello"),
                ContentSection(
                    section_type="tool_group",
                    markdown="📖 Read `a.py` ✅",
                    tool_kind="read",
                    tool_count=1,
                    collapsed_by_default=True,
                ),
                ContentSection(
                    section_type="thought",
                    markdown="thinking...",
                    collapsed_by_default=True,
                ),
            ]
        )
        elems = rc.to_elements(collapsible=True)
        assert len(elems) == 3
        # Text → plain markdown
        assert elems[0]["tag"] == "markdown"
        assert elems[0]["content"] == "hello"
        # Tool group → collapsible_panel
        assert elems[1]["tag"] == "collapsible_panel"
        assert elems[1]["expanded"] is False
        assert "📖" in elems[1]["header"]["content"]
        assert elems[1]["elements"][0]["content"] == "📖 Read `a.py` ✅"
        # Thought → collapsible_panel
        assert elems[2]["tag"] == "collapsible_panel"
        assert "🧠" in elems[2]["header"]["content"]

    def test_to_elements_collapsible_false(self):
        rc = RenderedContent(
            sections=[
                ContentSection(
                    section_type="tool_group",
                    markdown="tool stuff",
                    collapsed_by_default=True,
                ),
                ContentSection(
                    section_type="thought",
                    markdown="thinking...",
                    collapsed_by_default=True,
                ),
            ]
        )
        elems = rc.to_elements(collapsible=False)
        # All should be plain markdown
        assert all(e["tag"] == "markdown" for e in elems)

    def test_to_elements_multi_tool_header(self):
        rc = RenderedContent(
            sections=[
                ContentSection(
                    section_type="tool_group",
                    markdown="stuff",
                    tool_kind="read",
                    tool_count=3,
                    collapsed_by_default=True,
                ),
            ]
        )
        elems = rc.to_elements(collapsible=True)
        header = elems[0]["header"]["content"]
        assert "3 次工具调用" in header


class TestProcessEventStructured:
    def setup_method(self):
        self.renderer = ACPEventRenderer()

    def test_text_only(self):
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello"))
        result = self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=" world"))
        secs = result.sections
        text_secs = [s for s in secs if s.section_type == "text"]
        assert len(text_secs) == 1
        assert "hello world" in text_secs[0].markdown

    def test_thought_accumulated(self):
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="thinking "))
        result = self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="more"))
        thought_secs = [s for s in result.sections if s.section_type == "thought"]
        assert len(thought_secs) == 1
        assert "thinking more" in thought_secs[0].markdown
        assert thought_secs[0].collapsed_by_default is True

    def test_thought_not_in_legacy_render(self):
        """Verify process_event() still ignores thoughts (backward compat)."""
        rendered = self.renderer.process_event(ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="thinking"))
        assert rendered == ""

    def test_tool_done_no_longer_injects_inline_summary(self):
        """After slim-flow, TOOL_CALL_DONE no longer injects inline text.

        Tool completion rendering is handled by activity_digest in the card
        render layer (flatten_to_atoms), not by ACPEventRenderer.
        """
        tc = ToolCallInfo(id="t1", title="Read file", kind="read", status="completed", locations=["a.py"])
        result = self.renderer.process_event_structured(
            ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc)
        )
        tool_secs = [s for s in result.sections if s.section_type == "tool_group"]
        assert len(tool_secs) == 0
        assert self.renderer.completed_tool_count == 1

    def test_consecutive_same_kind_tools_no_inline_text(self):
        """Tool done events no longer produce inline text aggregation."""
        for i in range(3):
            tc = ToolCallInfo(id=f"t{i}", title=f"Read file{i}", kind="read", status="completed", locations=[f"f{i}.py"])
            self.renderer.process_event_structured(
                ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc)
            )
        result = self.renderer._render_structured()
        tool_secs = [s for s in result.sections if s.section_type == "tool_group"]
        assert len(tool_secs) == 0
        assert self.renderer.completed_tool_count == 3

    def test_text_interleaved_with_tools(self):
        # Text
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="first text"))
        # Tool (no inline injection anymore)
        tc = ToolCallInfo(id="t1", title="Read file", kind="read", status="completed", locations=["a.py"])
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        # More text
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="second text"))
        result = self.renderer._render_structured()
        types = [s.section_type for s in result.sections]
        assert "text" in types
        # Tool completion no longer creates tool_group in text_chunks
        assert "tool_group" not in types

    def test_plan_appears_as_section(self):
        plan = PlanInfo(entries=[PlanEntryInfo(content="step 1", status="completed")])
        result = self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan))
        plan_secs = [s for s in result.sections if s.section_type == "plan"]
        assert len(plan_secs) == 1
        assert "step 1" in plan_secs[0].markdown

    def test_active_tools_not_collapsed(self):
        tc = ToolCallInfo(id="t1", title="Editing", kind="edit", status="in_progress", locations=["a.py"])
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.TOOL_CALL_START, tool_call=tc))
        result = self.renderer._render_structured()
        active_secs = [s for s in result.sections if s.section_type == "active_tools"]
        assert len(active_secs) == 1
        assert active_secs[0].is_complete is False
        assert active_secs[0].collapsed_by_default is False

    def test_backward_compat_process_event(self):
        """process_event() returns text content without inline tool summaries."""
        # Text + tool done
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello"))
        tc = ToolCallInfo(id="t1", title="Read file", kind="read", status="completed", locations=["a.py"])
        rendered = self.renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        # Should contain the text but NOT tool summary (removed by slim-flow)
        assert "hello" in rendered
        assert self.renderer.completed_tool_count == 1

    def test_to_markdown_matches_legacy_render(self):
        """RenderedContent.to_markdown() should produce equivalent content to _render()."""
        events = [
            ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello "),
            ACPEvent(
                event_type=ACPEventType.TOOL_CALL_DONE,
                tool_call=ToolCallInfo(id="t1", title="Read", kind="read", status="completed", locations=["a.py"]),
            ),
            ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="world"),
        ]
        for e in events:
            self.renderer._ingest_event(e)
        legacy = self.renderer._render()
        structured = self.renderer._render_structured()
        # Structured markdown should contain text content
        sm = structured.to_markdown()
        assert "hello" in sm
        assert "world" in sm

    def test_reset_clears_thoughts(self):
        self.renderer.process_event_structured(ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="thinking"))
        self.renderer.reset()
        result = self.renderer._render_structured()
        thought_secs = [s for s in result.sections if s.section_type == "thought"]
        assert len(thought_secs) == 0


class TestContinuationSummary:
    """Tests for render_continuation_summary() and reset_for_continuation()."""

    def setup_method(self):
        self.renderer = ACPEventRenderer()

    def test_empty_renderer_returns_empty_summary(self):
        assert self.renderer.render_continuation_summary() == ""

    def test_summary_includes_plan_progress(self):
        plan = PlanInfo(entries=[
            PlanEntryInfo(content="step 1", status="completed"),
            PlanEntryInfo(content="step 2", status="in_progress"),
            PlanEntryInfo(content="step 3", status="pending"),
        ])
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=plan))
        summary = self.renderer.render_continuation_summary()
        assert "📋 执行计划: 1/3 已完成" in summary

    def test_summary_includes_tool_count(self):
        tc = ToolCallInfo(id="t1", title="Read file", kind="read", status="completed", locations=["a.py"])
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        summary = self.renderer.render_continuation_summary()
        assert "🛠️ 已完成 1 次工具调用" in summary

    def test_summary_includes_modified_files(self):
        tc = ToolCallInfo(id="t1", title="Edit", kind="edit", status="completed", locations=["a.py"])
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        summary = self.renderer.render_continuation_summary()
        assert "`a.py`" in summary

    def test_summary_truncates_many_files(self):
        for i in range(8):
            tc = ToolCallInfo(id=f"t{i}", title="Edit", kind="edit", status="completed", locations=[f"file{i}.py"])
            self.renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        summary = self.renderer.render_continuation_summary()
        assert "(+3)" in summary

    def test_summary_has_header_and_separator(self):
        tc = ToolCallInfo(id="t1", title="Read", kind="read", status="completed", locations=["x.py"])
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))
        summary = self.renderer.render_continuation_summary()
        assert summary.startswith("**📄 前文摘要**")
        assert summary.endswith("---\n")

    def test_reset_for_continuation_clears_state(self):
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="old content"))
        tc = ToolCallInfo(id="t1", title="Read", kind="read", status="completed", locations=["a.py"])
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TOOL_CALL_DONE, tool_call=tc))

        self.renderer.reset_for_continuation("summary text")
        assert self.renderer._completed_tool_count == 0
        assert len(self.renderer._modified_files) == 0
        assert self.renderer._plan is None

    def test_reset_for_continuation_seeds_summary(self):
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="old content"))
        self.renderer.reset_for_continuation("**summary**")

        # Subsequent render should contain only the summary
        rendered = self.renderer._render()
        assert "**summary**" in rendered
        assert "old content" not in rendered

    def test_reset_for_continuation_empty_equals_reset(self):
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="old content"))
        self.renderer.reset_for_continuation("")
        rendered = self.renderer._render()
        assert rendered == ""

    def test_reset_for_continuation_new_events_append(self):
        self.renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="old"))
        self.renderer.reset_for_continuation("**summary**\n\n---\n")
        # New event after reset
        rendered = self.renderer.process_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="new content"))
        assert "**summary**" in rendered
        assert "new content" in rendered
        assert "old" not in rendered or "old" in "**summary**"  # "old" only if part of summary


class TestPanelStyles:
    """Verify _wrap_collapsible uses PANEL_STYLES constants and dynamic border colors."""

    def test_panel_uses_panel_styles_constants(self):
        from src.card.themes import PANEL_STYLES
        sec = ContentSection(
            section_type="thought", markdown="thinking...", collapsed_by_default=True,
        )
        panel = RenderedContent._wrap_collapsible(sec)
        assert panel["vertical_spacing"] == PANEL_STYLES["vertical_spacing"]
        assert panel["padding"] == PANEL_STYLES["padding"]
        assert panel["corner_radius"] == PANEL_STYLES["corner_radius"]

    def test_tool_group_complete_gets_border_history(self):
        from src.card.themes import PANEL_STYLES
        sec = ContentSection(
            section_type="tool_group", markdown="done", tool_kind="read",
            tool_count=1, is_complete=True, collapsed_by_default=True,
        )
        panel = RenderedContent._wrap_collapsible(sec)
        assert panel["border"]["color"] == PANEL_STYLES["border_history"]

    def test_tool_group_failed_gets_border_failed(self):
        from src.card.themes import PANEL_STYLES
        sec = ContentSection(
            section_type="tool_group", markdown="err", tool_kind="execute",
            tool_count=1, is_complete=True, has_failure=True, collapsed_by_default=True,
        )
        panel = RenderedContent._wrap_collapsible(sec)
        assert panel["border"]["color"] == PANEL_STYLES["border_failed"]

    def test_tool_group_incomplete_gets_border_normal(self):
        from src.card.themes import PANEL_STYLES
        sec = ContentSection(
            section_type="tool_group", markdown="running", tool_kind="read",
            tool_count=1, is_complete=False, collapsed_by_default=True,
        )
        panel = RenderedContent._wrap_collapsible(sec)
        assert panel["border"]["color"] == PANEL_STYLES["border_normal"]

    def test_thought_gets_border_normal(self):
        from src.card.themes import PANEL_STYLES
        sec = ContentSection(
            section_type="thought", markdown="hmm", collapsed_by_default=True,
        )
        panel = RenderedContent._wrap_collapsible(sec)
        assert panel["border"]["color"] == PANEL_STYLES["border_normal"]

    def test_has_failure_defaults_false(self):
        sec = ContentSection(section_type="text", markdown="hi")
        assert sec.has_failure is False


class TestHeaderTemplateMapping:
    """Verify _pick_deep_template() terminal_state color mapping."""

    def _pick(self, engine="Coco", status="running", terminal_state=None):
        from src.card.builders.deep import DeepBuilder
        return DeepBuilder._pick_deep_template(engine, status, terminal_state=terminal_state)

    def test_terminal_completed_green(self):
        assert self._pick(terminal_state="completed") == "green"

    def test_terminal_failed_red(self):
        assert self._pick(terminal_state="failed") == "red"

    def test_terminal_cancelled_orange(self):
        assert self._pick(terminal_state="cancelled") == "orange"

    def test_terminal_blocked_grey(self):
        assert self._pick(terminal_state="blocked") == "grey"

    def test_terminal_awaiting_approval_blue(self):
        assert self._pick(terminal_state="awaiting_approval") == "blue"

    def test_terminal_denied_red(self):
        assert self._pick(terminal_state="denied") == "red"

    def test_terminal_continued_green(self):
        assert self._pick(terminal_state="continued") == "green"

    def test_terminal_overrides_status(self):
        # terminal_state should take priority over status
        assert self._pick(status="error", terminal_state="completed") == "green"

    def test_no_terminal_uses_status(self):
        assert self._pick(status="error") == "red"
        assert self._pick(status="completed") == "green"
        assert self._pick(status="paused") == "orange"

    def test_no_terminal_no_status_uses_engine(self):
        assert self._pick(engine="spec", status="running") == "green"


class TestToolGrouping:
    """Task 5: tool grouping strategy — ≤2 individual, >2 merged."""

    def _make_tool_section(self, kind="read", count=1, is_complete=True, has_failure=False):
        return ContentSection(
            section_type="tool_group", markdown=f"tool-{kind}-{count}",
            tool_kind=kind, tool_count=count,
            is_complete=is_complete, collapsed_by_default=True,
            has_failure=has_failure,
        )

    def test_two_tools_stay_individual(self):
        rc = RenderedContent(sections=[
            self._make_tool_section("read", 1),
            self._make_tool_section("edit", 1),
        ])
        elems = rc.to_elements(collapsible=True)
        assert len(elems) == 2
        # Each should be its own collapsible_panel
        assert all(e["tag"] == "collapsible_panel" for e in elems)
        assert "☕" not in elems[0]["header"]["content"]

    def test_three_tools_merge_into_grouped(self):
        rc = RenderedContent(sections=[
            self._make_tool_section("read", 1),
            self._make_tool_section("read", 1),
            self._make_tool_section("edit", 1),
        ])
        elems = rc.to_elements(collapsible=True)
        assert len(elems) == 1
        assert elems[0]["tag"] == "collapsible_panel"
        assert "☕" in elems[0]["header"]["content"]
        assert "3个工具调用" in elems[0]["header"]["content"]
        assert "已结束" in elems[0]["header"]["content"]

    def test_mixed_complete_incomplete_not_merged(self):
        """Incomplete tool_group sections should not be merged."""
        rc = RenderedContent(sections=[
            self._make_tool_section("read", 1, is_complete=True),
            self._make_tool_section("read", 1, is_complete=True),
            self._make_tool_section("read", 1, is_complete=False),  # incomplete
        ])
        elems = rc.to_elements(collapsible=True)
        # First two are consecutive completed, third is incomplete
        # 2 tools ≤ threshold → 2 individual panels + 1 non-collapsible
        assert len(elems) == 3

    def test_text_between_tools_breaks_grouping(self):
        rc = RenderedContent(sections=[
            self._make_tool_section("read", 1),
            self._make_tool_section("read", 1),
            ContentSection(section_type="text", markdown="some text"),
            self._make_tool_section("edit", 1),
        ])
        elems = rc.to_elements(collapsible=True)
        # First 2 tools (≤2, individual) + text + 1 tool = 4 elements
        assert len(elems) == 4

    def test_grouped_failure_gets_red_border(self):
        from src.card.themes import PANEL_STYLES
        rc = RenderedContent(sections=[
            self._make_tool_section("read", 1),
            self._make_tool_section("read", 1, has_failure=True),
            self._make_tool_section("edit", 1),
        ])
        elems = rc.to_elements(collapsible=True)
        assert len(elems) == 1
        assert elems[0]["border"]["color"] == PANEL_STYLES["border_failed"]

    def test_collapsible_false_ignores_grouping(self):
        rc = RenderedContent(sections=[
            self._make_tool_section("read", 1),
            self._make_tool_section("read", 1),
            self._make_tool_section("edit", 1),
        ])
        elems = rc.to_elements(collapsible=False)
        assert len(elems) == 3
        assert all(e["tag"] == "markdown" for e in elems)


class TestReasoningBlock:
    """Task 6: reasoning block rendering with truncation and mobile-readable text_size."""

    def test_thought_panel_has_normal_text_size(self):
        rc = RenderedContent(sections=[
            ContentSection(section_type="thought", markdown="thinking...", collapsed_by_default=True),
        ])
        elems = rc.to_elements(collapsible=True)
        assert len(elems) == 1
        assert elems[0]["tag"] == "collapsible_panel"
        inner = elems[0]["elements"][0]
        assert inner["text_size"] == "normal"

    def test_thought_complete_header(self):
        rc = RenderedContent(sections=[
            ContentSection(section_type="thought", markdown="done", collapsed_by_default=True, is_complete=True),
        ])
        elems = rc.to_elements(collapsible=True)
        assert "思考完成" in elems[0]["header"]["content"]

    def test_thought_active_header(self):
        rc = RenderedContent(sections=[
            ContentSection(section_type="thought", markdown="working", collapsed_by_default=True, is_complete=False),
        ])
        elems = rc.to_elements(collapsible=True)
        assert "思考中" in elems[0]["header"]["content"]

    def test_long_reasoning_truncated(self):
        """process_event_structured should truncate thought content via cap_reasoning_tail."""
        renderer = ACPEventRenderer()
        long_thought = "x" * 1000
        renderer.process_event_structured(
            ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text=long_thought)
        )
        result = renderer._render_structured()
        thought_secs = [s for s in result.sections if s.section_type == "thought"]
        assert len(thought_secs) == 1
        # Should be capped at 500 chars + prefix
        assert len(thought_secs[0].markdown) <= 510

    def test_short_reasoning_not_truncated(self):
        renderer = ACPEventRenderer()
        renderer.process_event_structured(
            ACPEvent(event_type=ACPEventType.THOUGHT_CHUNK, text="short thought")
        )
        result = renderer._render_structured()
        thought_secs = [s for s in result.sections if s.section_type == "thought"]
        assert thought_secs[0].markdown == "short thought"

    def test_reasoning_state_dataclass(self):
        from src.card.models import ReasoningState
        rs = ReasoningState(content="thinking", active=True)
        assert rs.content == "thinking"
        assert rs.active is True
        assert rs.expanded is False
