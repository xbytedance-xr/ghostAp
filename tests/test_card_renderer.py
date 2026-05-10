"""Tests for src/card/render/renderer.py — main render entry point."""

import time

import pytest

from src.card.render.budget import RenderBudget
from src.card.render.renderer import (
    ActiveElement,
    RenderedCard,
    compute_structure_signature,
    render_card,
)
from src.card.state.models import (
    ButtonSpec,
    CardMetadata,
    CardState,
    ContentBlock,
    FooterState,
    HeaderState,
)


def _iter_dict_nodes(obj):
    """Yield all dict nodes in a nested (dict/list) structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dict_nodes(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_dict_nodes(it)


class TestRenderCardBasic:
    """Basic render_card() behavior."""

    def test_empty_state_returns_single_page(self):
        state = CardState()
        cards = render_card(state, RenderBudget())
        assert len(cards) == 1
        assert cards[0].page_index == 0
        assert cards[0].total_pages == 1

    def test_single_text_block(self):
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="Hello world"),),
            header=HeaderState(title="Test", template="blue"),
        )
        cards = render_card(state, RenderBudget())
        assert len(cards) == 1
        card_json = cards[0]._card_json
        assert card_json["schema"] == "2.0"
        assert card_json["header"]["title"]["content"] == "Test"
        # body should have the text element
        body = card_json["body"]["elements"]
        assert any(el.get("content") == "Hello world" for el in body)

    def test_card_json_structure(self):
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="content"),),
            header=HeaderState(title="Title", template="green"),
        )
        cards = render_card(state, RenderBudget())
        card_json = cards[0]._card_json
        assert "schema" in card_json
        assert "config" in card_json
        assert "header" in card_json
        assert "body" in card_json
        assert card_json["config"]["wide_screen_mode"] is True
        assert card_json["config"]["update_multi"] is True


class TestUnifiedCardSections:
    def test_header_includes_execution_unit_label(self):
        from src.card.state.reducers._shared import build_header

        metadata = CardMetadata(
            engine_type="loop",
            mode_name="Loop · Coco",
            mode_emoji="🔁",
            unit_label="第 2 轮",
        )

        header = build_header(metadata, "running")

        assert "第 2 轮" in header.title

    def test_render_card_orders_status_body_and_appendix_sections(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="tool_call",
                    block_id="tool1",
                    tool_name="Read",
                    tool_input='{"path": "/src/main.py"}',
                    tool_output="read ok",
                    tool_summary="read ok",
                    status="completed",
                ),
                ContentBlock(kind="text", block_id="body1", content="正文内容"),
                ContentBlock(kind="phase", block_id="phase1", content="第 1 轮 · Build"),
            ),
            metadata=CardMetadata(engine_type="spec", mode_name="Spec · Coco", mode_emoji="📋"),
        )

        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]

        phase_idx = next(i for i, el in enumerate(body) if "第 1 轮 · Build" in str(el))
        text_idx = next(i for i, el in enumerate(body) if el.get("content") == "正文内容")
        # Completed tools now render as activity_digest (one-line summary) in body
        digest_idx = next(i for i, el in enumerate(body) if "已探索" in str(el))

        # Status (phase) comes first, then body atoms in original order (digest, text)
        assert phase_idx < digest_idx < text_idx

    def test_bridge_phrase_is_prepended_to_first_text_body_atom(self):
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="继续执行正文"),),
            metadata=CardMetadata(bridge_phrase="续接上一张卡片："),
        )

        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]

        assert any(
            el.get("content") == "续接上一张卡片：\n\n继续执行正文"
            for el in body
            if el.get("tag") == "markdown"
        )

    def test_bridge_phrase_prepended_to_reasoning_column_set(self):
        """Bridge phrase prepends into column_set's second column (reasoning panel)."""
        state = CardState(
            blocks=(ContentBlock(kind="reasoning", block_id="r1", content="分析中...", status="active"),),
            metadata=CardMetadata(bridge_phrase="续接："),
        )

        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]

        # Find the column_set (reasoning panel)
        col_sets = [el for el in body if el.get("tag") == "column_set" and el.get("background_style") == "grey"]
        assert len(col_sets) >= 1
        # Bridge phrase should be in the content column (second column, weight=20)
        content_col = col_sets[0]["columns"][1]
        md_content = content_col["elements"][0]["content"]
        assert md_content.startswith("续接：")

    def test_programming_card_does_not_inject_activity_summary_panel(self):
        """Completed tools render as compact activity_digest (not full activity_summary_panel)."""
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="tool_call",
                    block_id="tool1",
                    tool_name="read",
                    tool_input='{"path": "src/app.py"}',
                    tool_output="ok",
                    tool_summary="src/app.py",
                    status="completed",
                ),
                ContentBlock(kind="text", block_id="body1", content="正文内容"),
            ),
            metadata=CardMetadata(tool_name="Coco"),
        )

        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]

        # Activity digest should appear as a compact notation-size line
        assert "已探索" in str(body)
        assert "正文内容" in str(body)
        # Should NOT have a full collapsible_panel for tools
        tool_panels = [el for el in body if el.get("tag") == "collapsible_panel" and "read" in str(el)]
        assert len(tool_panels) == 0


class TestStreamingMode:
    """streaming_mode in config."""

    def test_streaming_enabled_when_active_text_and_running(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="text",
                    block_id="t1",
                    content="typing...",
                    element_id="el_1",
                    status="active",
                ),
            ),
            terminal="running",
        )
        cards = render_card(state, RenderBudget())
        assert cards[0]._card_json["config"].get("streaming_mode") is True

    def test_streaming_disabled_when_completed(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="text",
                    block_id="t1",
                    content="done",
                    element_id="el_1",
                    status="active",
                ),
            ),
            terminal="completed",
        )
        cards = render_card(state, RenderBudget())
        assert "streaming_mode" not in cards[0]._card_json["config"]


class TestSchemaDivStyleSafety:
    """Regression tests: avoid illegal style fields on `div` (Feishu Schema 2.0)."""

    def test_warning_banner_does_not_style_div(self):
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="hello"),),
            footer=FooterState(warning_banner="卡片解析应成功", warning_type="warning"),
        )
        cards = render_card(state, RenderBudget())
        card_json = cards[0]._card_json
        for node in _iter_dict_nodes(card_json):
            if node.get("tag") == "div":
                assert "padding" not in node
                assert "background_style" not in node

    def test_phase_panel_does_not_style_div(self):
        state = CardState(
            blocks=(ContentBlock(kind="phase", block_id="p1", content="Spec · Build"),),
        )
        cards = render_card(state, RenderBudget())
        card_json = cards[0]._card_json
        for node in _iter_dict_nodes(card_json):
            if node.get("tag") == "div":
                assert "padding" not in node
                assert "background_style" not in node

    def test_worktree_failed_units_does_not_style_div(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="worktree_units",
                    block_id="w1",
                    content="",
                    data={
                        "message": "执行中",
                        "units": [
                            {"name": "unit-a", "status": "failed", "error": "boom"},
                            {"name": "unit-b", "status": "running", "metadata": {"started_at": time.time()}},
                        ],
                    },
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        card_json = cards[0]._card_json
        for node in _iter_dict_nodes(card_json):
            if node.get("tag") == "div":
                assert "padding" not in node
                assert "background_style" not in node

    def test_streaming_disabled_when_no_active_element(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="text",
                    block_id="t1",
                    content="done text",
                    status="completed",
                ),
            ),
            terminal="running",
        )
        cards = render_card(state, RenderBudget())
        assert "streaming_mode" not in cards[0]._card_json["config"]


class TestActiveElement:
    """ActiveElement detection."""

    def test_active_text_detected(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="text",
                    block_id="t1",
                    content="streaming text",
                    element_id="el_stream",
                    status="active",
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        assert cards[0].active_element is not None
        assert cards[0].active_element.element_id == "el_stream"
        assert cards[0].active_element.text == "streaming text"

    def test_no_active_element_when_completed(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="text",
                    block_id="t1",
                    content="final",
                    element_id="el_1",
                    status="completed",
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        assert cards[0].active_element is None

    def test_no_active_element_without_element_id(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="text", block_id="t1", content="text", status="active"
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        assert cards[0].active_element is None


class TestColumnSetSignature:
    """column_set content changes should affect page signature."""

    def test_column_set_content_change_updates_signature(self):
        """Reasoning panel (column_set) with different content should produce different signatures."""
        s1 = CardState(
            blocks=(ContentBlock(kind="reasoning", block_id="r1", content="thought A", status="active"),),
            terminal="running",
        )
        s2 = CardState(
            blocks=(ContentBlock(kind="reasoning", block_id="r1", content="thought B", status="active"),),
            terminal="running",
        )
        cards1 = render_card(s1, RenderBudget())
        cards2 = render_card(s2, RenderBudget())
        assert cards1[0].structure_signature != cards2[0].structure_signature


class TestStructureSignature:
    """compute_structure_signature tests."""

    def test_same_structure_same_signature(self):
        s1 = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="hello"),),
            terminal="running",
        )
        s2 = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="world"),),
            terminal="running",
        )
        # Content differs but structure is the same
        assert compute_structure_signature(s1) == compute_structure_signature(s2)

    def test_different_structure_different_signature(self):
        s1 = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="hello"),),
            terminal="running",
        )
        s2 = CardState(
            blocks=(
                ContentBlock(kind="text", block_id="t1", content="hello"),
                ContentBlock(kind="tool_call", block_id="tc1", tool_name="bash"),
            ),
            terminal="running",
        )
        assert compute_structure_signature(s1) != compute_structure_signature(s2)

    def test_terminal_change_changes_signature(self):
        s1 = CardState(terminal="running")
        s2 = CardState(terminal="completed")
        assert compute_structure_signature(s1) != compute_structure_signature(s2)

    def test_signature_is_md5_hex(self):
        state = CardState()
        sig = compute_structure_signature(state)
        assert len(sig) == 32
        assert all(c in "0123456789abcdef" for c in sig)


class TestFooterAndButtons:
    """Footer and buttons only appear on last page."""

    def test_footer_on_single_page(self):
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="text"),),
            footer=FooterState(status="thinking", status_text="🤔 思考中..."),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        # Should have hr separator from footer
        assert any(el.get("tag") == "hr" for el in body)

    def test_buttons_on_single_page(self):
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content="text"),),
            buttons=(ButtonSpec(text="Stop", action_id="stop", type="danger"),),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        # Single button renders as column_set with flex_mode 'none' (full width)
        assert any(el.get("tag") == "column_set" and el.get("flex_mode") == "none" for el in body)


class TestMultipleBlockTypes:
    """Rendering mixed block types."""

    def test_tool_block_renders_collapsible_panel(self):
        """Completed tool renders as compact activity_digest (notation-size markdown)."""
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="tool_call",
                    block_id="tc1",
                    tool_name="bash",
                    tool_input="ls -la",
                    status="completed",
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        # Completed tools now render as activity_digest (notation markdown), not collapsible_panel
        assert any(el.get("text_size") == "notation" and "已运行" in str(el.get("content", "")) for el in body)

    def test_reasoning_block_renders(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="reasoning",
                    block_id="r1",
                    content="thinking about...",
                    status="active",
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        assert any(el.get("tag") == "column_set" and el.get("background_style") == "grey" for el in body)

    def test_plan_block_renders(self):
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="plan",
                    block_id="p1",
                    content="✅ Step 1\n⏳ Step 2",
                    status="active",
                ),
            ),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        panels = [el for el in body if el.get("tag") == "collapsible_panel"]
        assert len(panels) == 1

    def test_mixed_blocks_render_in_order(self):
        """Text, tool, text blocks render in original order with activity_digest inline."""
        state = CardState(
            blocks=(
                ContentBlock(kind="text", block_id="t1", content="Intro"),
                ContentBlock(
                    kind="tool_call",
                    block_id="tc1",
                    tool_name="bash",
                    tool_input="echo hi",
                    status="completed",
                ),
                ContentBlock(kind="text", block_id="t2", content="Conclusion"),
            ),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        # 3 content elements: text + activity_digest + text (+ footer/buttons)
        assert len(body) >= 3
        intro_idx = next(i for i, el in enumerate(body) if el.get("content") == "Intro")
        conclusion_idx = next(i for i, el in enumerate(body) if el.get("content") == "Conclusion")
        digest_idx = next(i for i, el in enumerate(body) if el.get("text_size") == "notation" and "已运行" in str(el.get("content", "")))
        assert intro_idx < digest_idx < conclusion_idx


class TestPagination:
    """Multi-page rendering."""

    def test_large_content_creates_multiple_pages(self):
        # Create content that exceeds budget
        big_text = "x" * 30000  # ~90KB estimated, way over 27KB budget
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content=big_text),),
        )
        budget = RenderBudget(byte_budget=5000)  # Very small budget
        cards = render_card(state, budget)
        assert len(cards) > 1
        # Check page indexing
        for i, card in enumerate(cards):
            assert card.page_index == i
            assert card.total_pages == len(cards)

    def test_all_pages_have_header(self):
        big_text = "line\n" * 5000
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content=big_text),),
            header=HeaderState(title="Multi-page", template="blue"),
        )
        budget = RenderBudget(byte_budget=5000)
        cards = render_card(state, budget)
        for card in cards:
            assert card._card_json["header"]["title"]["content"] == "Multi-page"

    def test_section_layout_repeats_sticky_phase_banner_on_every_page(self):
        big_text = "line\n" * 5000
        state = CardState(
            blocks=(ContentBlock(kind="text", block_id="t1", content=big_text),),
            metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        )
        budget = RenderBudget(byte_budget=5000)

        cards = render_card(state, budget)

        assert len(cards) > 1
        for card in cards:
            body = card._card_json["body"]["elements"]
            assert body[0]["tag"] == "markdown"
            assert "Deep" in body[0]["content"]

    def test_section_layout_keeps_appendix_on_last_page_only(self):
        """Completed tools render as activity_digest in body (not appendix).

        With the slim-flow redesign, completed tools are aggregated into a
        one-line activity_digest atom placed in body alongside text, rather
        than as collapsible panels in the appendix section.
        """
        big_text = "line\n" * 5000
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="tool_call",
                    block_id="tool1",
                    tool_name="Bash",
                    tool_output="done",
                    status="completed",
                ),
                ContentBlock(kind="text", block_id="t1", content=big_text),
            ),
            metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        )
        budget = RenderBudget(byte_budget=5000)

        cards = render_card(state, budget)

        assert len(cards) > 1
        # activity_digest is small and sits in body on the first page
        first_body = cards[0]._card_json["body"]["elements"]
        assert "已运行" in str(first_body), "activity_digest should appear in first page body"

    def test_section_layout_renders_sticky_task_list_once_per_page(self):
        big_text = "line\n" * 5000
        state = CardState(
            blocks=(
                ContentBlock(
                    kind="task_list",
                    block_id="tasks",
                    current_task_id="t2",
                    tasks=(
                        {"task_id": "t1", "name": "完成需求", "status": "completed"},
                        {"task_id": "t2", "name": "实现渲染", "status": "in_progress"},
                    ),
                ),
                ContentBlock(kind="text", block_id="t1", content=big_text),
            ),
            metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        )
        budget = RenderBudget(byte_budget=5000)

        cards = render_card(state, budget)

        assert len(cards) > 1
        for card in cards:
            body = card._card_json["body"]["elements"]
            task_panels = [el for el in body if "任务列表" in str(el)]
            assert len(task_panels) == 1
            assert "实现渲染" in str(task_panels[0])
            assert "完成需求" in str(task_panels[0])
            assert "进行中 (1)" in str(task_panels[0])
            assert "已完成 (1)" in str(task_panels[0])


class TestApprovalRendering:
    """Approval state rendering: header color, buttons, footer text."""

    def _make_approval_state(self, tool_name: str = "bash") -> CardState:
        """Build a state with APPROVAL_REQUESTED applied via the reducer."""
        from src.card.events import CardEvent, CardEventType
        from src.card.state.reducer import reduce_card_state

        meta = CardMetadata(
            project_name="Ghost", mode_name="Deep Agent", mode_emoji="🧠",
            tool_name="coco", model_name="gpt-4o", engine_type="deep",
        )
        s = reduce_card_state(None, CardEvent.started(), metadata=meta)
        s = reduce_card_state(s, CardEvent(
            type=CardEventType.APPROVAL_REQUESTED,
            payload={"tool_name": tool_name, "description": "rm -rf /tmp/test"},
        ))
        return s

    def test_header_template_is_indigo(self):
        state = self._make_approval_state()
        cards = render_card(state, RenderBudget())
        assert len(cards) >= 1
        assert cards[0]._card_json["header"]["template"] == "indigo"

    def test_buttons_approve_reject_present(self):
        state = self._make_approval_state()
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        # Buttons render as column_set (2 buttons)
        column_sets = [el for el in body if el.get("tag") == "column_set"]
        assert len(column_sets) >= 1, "Should have a column_set for approve/reject buttons"
        # Extract button texts from columns
        buttons = []
        for cs in column_sets:
            for col in cs.get("columns", []):
                for el in col.get("elements", []):
                    if el.get("tag") == "button":
                        buttons.append(el)
        assert len(buttons) == 2
        button_texts = [b["text"]["content"] for b in buttons]
        assert "✅ 批准" in button_texts
        assert "❌ 拒绝" in button_texts
        # Check button types
        button_types = {b["text"]["content"]: b["type"] for b in buttons}
        assert button_types["✅ 批准"] == "primary"
        assert button_types["❌ 拒绝"] == "danger"

    def test_footer_status_text_contains_tool_name(self):
        state = self._make_approval_state(tool_name="bash")
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        # Footer is: hr + markdown(status_text) [+ optional progress]
        hr_elements = [el for el in body if el.get("tag") == "hr"]
        assert len(hr_elements) >= 1, "Footer should have hr separator"
        # Find footer text (markdown element after hr)
        footer_texts = [
            el["content"] for el in body
            if el.get("tag") == "markdown" and el.get("text_size") == "notation"
        ]
        assert any("等待审批" in t and "bash" in t for t in footer_texts), \
            f"Footer should mention '等待审批' and 'bash', got: {footer_texts}"

    def test_no_streaming_mode_during_approval(self):
        state = self._make_approval_state()
        cards = render_card(state, RenderBudget())
        assert "streaming_mode" not in cards[0]._card_json["config"]
