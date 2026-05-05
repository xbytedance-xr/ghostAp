"""Tests for src/card/render/renderer.py — main render entry point."""

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
        assert any(el.get("tag") == "collapsible_panel" for el in body)

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
        assert any(el.get("tag") == "collapsible_panel" for el in body)

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
        # 3 content elements + 0 footer/buttons
        assert len(body) >= 3
        assert body[0]["tag"] == "markdown"
        assert body[0]["content"] == "Intro"
        assert body[1]["tag"] == "collapsible_panel"
        assert body[2]["tag"] == "markdown"
        assert body[2]["content"] == "Conclusion"


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
