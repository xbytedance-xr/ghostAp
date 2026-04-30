"""Tests for header/footer/buttons rendering."""
import pytest
from src.card.state.models import CardState, HeaderState, FooterState, ButtonSpec, CardMetadata
from src.card.render.header import render_header
from src.card.render.footer import render_footer
from src.card.render.buttons import render_buttons


class TestRenderHeader:
    def test_header_with_project(self):
        """Project name present → "emoji ProjectName · ModeName" """
        state = CardState(
            header=HeaderState(title="🧠 MyProject · Deep Agent", template="turquoise"),
            metadata=CardMetadata(project_name="MyProject", mode_name="Deep Agent", mode_emoji="🧠"),
        )
        result = render_header(state)
        assert result["title"]["content"] == "🧠 MyProject · Deep Agent"
        assert result["template"] == "turquoise"

    def test_header_without_project(self):
        """No project → "emoji ModeName 编程模式" """
        state = CardState(
            header=HeaderState(title="🤖 Coco 编程模式", template="blue"),
            metadata=CardMetadata(mode_name="Coco", mode_emoji="🤖"),
        )
        result = render_header(state)
        assert result["title"]["content"] == "🤖 Coco 编程模式"

    def test_header_subtitle_with_tool_and_model(self):
        """Both tool and model → "🔧 tool · model" """
        state = CardState(
            header=HeaderState(title="test", subtitle="🔧 coco · gpt-4o"),
            metadata=CardMetadata(tool_name="coco", model_name="gpt-4o"),
        )
        result = render_header(state)
        assert "subtitle" in result
        assert result["subtitle"]["content"] == "🔧 coco · gpt-4o"

    def test_header_subtitle_with_status(self):
        """Subtitle with status → "🔧 tool · model · status" """
        state = CardState(
            header=HeaderState(title="test", subtitle="🔧 coco · gpt-4o · 正在执行"),
        )
        result = render_header(state)
        assert result["subtitle"]["content"] == "🔧 coco · gpt-4o · 正在执行"

    def test_header_no_subtitle(self):
        """No subtitle → no subtitle key in result"""
        state = CardState(header=HeaderState(title="test", subtitle=None))
        result = render_header(state)
        assert "subtitle" not in result

    def test_header_template_running(self):
        """Running state uses mode color"""
        state = CardState(header=HeaderState(title="test", template="purple"))
        result = render_header(state)
        assert result["template"] == "purple"


class TestRenderFooter:
    def test_footer_thinking(self):
        """status=thinking → 💭 text"""
        state = CardState(footer=FooterState(status="thinking", status_text="💭 正在思考..."))
        result = render_footer(state)
        assert len(result) == 2  # hr + markdown
        assert result[0]["tag"] == "hr"
        assert result[1]["content"] == "💭 正在思考..."
        assert result[1]["text_size"] == "notation"

    def test_footer_tool_running(self):
        """status=tool_running → 🔧 text"""
        state = CardState(footer=FooterState(status="tool_running", status_text="🔧 执行中: bash"))
        result = render_footer(state)
        assert result[1]["content"] == "🔧 执行中: bash"

    def test_footer_with_progress(self):
        """Progress bar appended"""
        state = CardState(footer=FooterState(
            status="tool_running",
            status_text="🔧 执行中: bash",
            progress="▰▰▰▱▱▱▱▱▱▱ 30%"
        ))
        result = render_footer(state)
        assert len(result) == 3  # hr + status + progress
        assert result[2]["content"] == "▰▰▰▱▱▱▱▱▱▱ 30%"

    def test_footer_none(self):
        """status=None → empty list"""
        state = CardState(footer=FooterState(status=None))
        result = render_footer(state)
        assert result == []


class TestRenderButtons:
    def test_no_buttons(self):
        """No buttons → empty list"""
        state = CardState(buttons=())
        result = render_buttons(state)
        assert result == []

    def test_two_buttons_column_set(self):
        """≤2 buttons → column_set layout"""
        state = CardState(buttons=(
            ButtonSpec(text="停止", action_id="stop", type="danger"),
            ButtonSpec(text="继续", action_id="continue", type="primary"),
        ))
        result = render_buttons(state)
        assert len(result) == 1
        assert result[0]["tag"] == "column_set"
        assert len(result[0]["columns"]) == 2

    def test_many_buttons_action_flow(self):
        """≥3 buttons → action flow layout"""
        state = CardState(buttons=(
            ButtonSpec(text="A", action_id="a"),
            ButtonSpec(text="B", action_id="b"),
            ButtonSpec(text="C", action_id="c"),
        ))
        result = render_buttons(state)
        assert len(result) == 1
        assert result[0]["tag"] == "action"
        assert result[0]["layout"] == "flow"
        assert len(result[0]["actions"]) == 3

    def test_button_with_confirm(self):
        """Button with confirm → confirm dialog"""
        state = CardState(buttons=(
            ButtonSpec(text="删除", action_id="delete", type="danger", confirm="确定要删除吗？"),
            ButtonSpec(text="取消", action_id="cancel"),
        ))
        result = render_buttons(state)
        # Find the delete button
        columns = result[0]["columns"]
        delete_btn = columns[0]["elements"][0]
        assert "confirm" in delete_btn
        assert delete_btn["confirm"]["text"]["content"] == "确定要删除吗？"
