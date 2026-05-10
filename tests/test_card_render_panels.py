"""Tests for tool/reasoning/plan panel rendering."""
import pytest
from src.card.state.models import ContentBlock
from src.card.render.tools import render_tool_panel, generate_tool_summary
from src.card.render.reasoning import render_reasoning_panel
from src.card.render.plan import render_plan_panel
from src.card.render.budget import RenderBudget


class TestToolPanel:
    def test_tool_panel_running(self):
        """Active tool → ⏳ icon, grey border, expanded=True"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="active",
            tool_name="bash", tool_input="ls -la /src", tool_summary="ls -la /src",
            is_latest_active=True,
        )
        result = render_tool_panel(block)
        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is True
        assert "⏳" in result["header"]["title"]["content"]
        assert result["border"]["color"] == "grey"

    def test_tool_panel_active_but_not_latest_collapsed(self):
        """Only latest active tool expands; older active tools stay collapsed."""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="active",
            tool_name="bash", tool_input="ls -la /src", tool_summary="ls -la /src",
            is_latest_active=False,
        )

        result = render_tool_panel(block)

        assert result["expanded"] is False

    def test_tool_panel_completed(self):
        """Completed tool → ✅ icon, grey border, expanded=False"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="ls -la", tool_output="file1\nfile2",
            tool_summary="ls -la"
        )
        result = render_tool_panel(block)
        assert result["expanded"] is False
        assert "✅" in result["header"]["title"]["content"]

    def test_tool_panel_failed(self):
        """Failed tool → ❌ icon, red border"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="failed",
            tool_name="bash", tool_input="rm -rf /", tool_output="Permission denied"
        )
        result = render_tool_panel(block)
        assert "❌" in result["header"]["title"]["content"]
        assert result["border"]["color"] == "red"

    def test_tool_panel_bash_detail(self):
        """Bash tool shows Command/Result format"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="npm run build", tool_output="Build successful"
        )
        result = render_tool_panel(block)
        detail_content = result["elements"][0]["content"]
        assert "**命令**" in detail_content
        assert "```bash" in detail_content
        assert "npm run build" in detail_content

    def test_tool_panel_generic_detail(self):
        """Generic tool shows Input/Output format"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="read", tool_input='{"path": "/src/main.py"}', tool_output="content here"
        )
        result = render_tool_panel(block)
        detail_content = result["elements"][0]["content"]
        assert "**输入**" in detail_content
        assert "**输出**" in detail_content

    def test_tool_output_truncation(self):
        """Long output truncated to 2000 chars"""
        long_output = "x" * 3000
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="cmd", tool_output=long_output
        )
        result = render_tool_panel(block)
        detail_content = result["elements"][0]["content"]
        # Should contain truncation indicator (unicode ellipsis)
        assert "…" in detail_content


class TestToolSummary:
    def test_summary_bash(self):
        """Bash → command text truncated"""
        block = ContentBlock(kind="tool_call", tool_name="bash", tool_input="ls -la /very/long/path/here")
        result = generate_tool_summary(block)
        assert "ls -la" in result

    def test_summary_read(self):
        """Read → file path"""
        block = ContentBlock(kind="tool_call", tool_name="read", tool_input='{"path": "/src/main.py"}')
        result = generate_tool_summary(block)
        assert "/src/main.py" in result

    def test_summary_generic(self):
        """Generic → fallback to tool_summary or tool_name"""
        block = ContentBlock(kind="tool_call", tool_name="custom_tool", tool_summary="did something")
        result = generate_tool_summary(block)
        assert "did something" in result


class TestReasoningPanel:
    def test_reasoning_active(self):
        """Active reasoning → expanded=True, "深度思考中..." """
        block = ContentBlock(kind="reasoning", block_id="r1", status="active", content="thinking...")
        result = render_reasoning_panel(block)
        assert result["expanded"] is True
        assert "深度思考中" in result["header"]["title"]["content"]
        assert result["border"]["color"] == "grey"

    def test_reasoning_done(self):
        """Done reasoning → expanded=False, shows char count"""
        block = ContentBlock(kind="reasoning", block_id="r1", status="completed",
                           content="full thought", char_count=1500)
        result = render_reasoning_panel(block)
        assert result["expanded"] is False
        assert "1500" in result["header"]["title"]["content"]
        assert "思考完成" in result["header"]["title"]["content"]

    def test_reasoning_done_truncated(self):
        """Long reasoning shows tail only"""
        long_content = "a" * 1000
        block = ContentBlock(kind="reasoning", block_id="r1", status="completed",
                           content=long_content, char_count=1000)
        budget = RenderBudget(reasoning_tail_chars=500)
        result = render_reasoning_panel(block, budget=budget)
        element_content = result["elements"][0]["content"]
        assert element_content.startswith("…")
        assert len(element_content) <= 502  # 500 + "…"


class TestPlanPanel:
    def test_plan_panel(self):
        """Plan renders as indigo collapsible with step icons"""
        content = "1. ✅ 分析需求\n2. ⏳ 编写代码\n3. ○ 运行测试"
        block = ContentBlock(kind="plan", block_id="p1", content=content)
        result = render_plan_panel(block)
        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is True
        assert result["border"]["color"] == "indigo"
        assert "执行计划" in result["header"]["title"]["content"]
        assert result["elements"][0]["content"] == content


class TestToolPanelEmptyData:
    """AC7: render_tool_panel returns None when both input and output are empty.
    AC8: when only output is empty, panel only renders input section."""

    @pytest.mark.parametrize("tool_input,tool_output", [
        (None, None),
        ("", ""),
        ("  ", "  "),
        ({}, {}),
        ([], []),
        (None, ""),
        ("", None),
    ])
    def test_both_empty_returns_none(self, tool_input, tool_output):
        """AC7: Both input and output empty → returns None."""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="read", tool_input=tool_input, tool_output=tool_output,
        )
        result = render_tool_panel(block)
        assert result is None

    def test_only_output_empty_renders_input_only(self):
        """AC8: Output empty, input present → renders input without output section."""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="read", tool_input='{"path": "/foo.py"}', tool_output=None,
        )
        result = render_tool_panel(block)
        assert result is not None
        detail_content = result["elements"][0]["content"]
        assert "**输入**" in detail_content
        assert "**输出**" not in detail_content

    def test_only_output_empty_bash(self):
        """AC8: Bash tool with empty output → renders command without result section."""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="ls -la", tool_output="",
        )
        result = render_tool_panel(block)
        assert result is not None
        detail_content = result["elements"][0]["content"]
        assert "**命令**" in detail_content
        assert "**结果**" not in detail_content

    def test_only_input_empty_renders_output_only(self):
        """Input empty, output present → still renders panel (returns not None)."""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="read", tool_input=None, tool_output="some content",
        )
        result = render_tool_panel(block)
        assert result is not None

    def test_both_present_renders_normally(self):
        """Both input and output present → normal render."""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="read", tool_input='{"path": "/x.py"}', tool_output="content",
        )
        result = render_tool_panel(block)
        assert result is not None
        detail_content = result["elements"][0]["content"]
        assert "**输入**" in detail_content
        assert "**输出**" in detail_content
