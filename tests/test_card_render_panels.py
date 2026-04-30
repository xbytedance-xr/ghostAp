"""Tests for tool/reasoning/plan panel rendering."""
import pytest
from src.card.state.models import ContentBlock
from src.card.render.tools import render_tool_panel, render_tool_history_panel, generate_tool_summary
from src.card.render.reasoning import render_reasoning_panel
from src.card.render.plan import render_plan_panel
from src.card.render.budget import RenderBudget


class TestToolPanel:
    def test_tool_panel_running(self):
        """Active tool → ⏳ icon, grey border, expanded=True"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="active",
            tool_name="bash", tool_input="ls -la /src", tool_summary="ls -la /src"
        )
        result = render_tool_panel(block)
        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is True
        assert "⏳" in result["header"]["title"]["content"]
        assert result["border"]["color"] == "grey"

    def test_tool_panel_completed(self):
        """Completed tool → ✓ icon, grey border, expanded=False"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="ls -la", tool_output="file1\nfile2",
            tool_summary="ls -la"
        )
        result = render_tool_panel(block)
        assert result["expanded"] is False
        assert "✓" in result["header"]["title"]["content"]

    def test_tool_panel_failed(self):
        """Failed tool → ✗ icon, red border"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="failed",
            tool_name="bash", tool_input="rm -rf /", tool_output="Permission denied"
        )
        result = render_tool_panel(block)
        assert "✗" in result["header"]["title"]["content"]
        assert result["border"]["color"] == "red"

    def test_tool_panel_bash_detail(self):
        """Bash tool shows Command/Result format"""
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="npm run build", tool_output="Build successful"
        )
        result = render_tool_panel(block)
        detail_content = result["elements"][0]["content"]
        assert "**Command**" in detail_content
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
        assert "**Input**" in detail_content
        assert "**Output**" in detail_content

    def test_tool_output_truncation(self):
        """Long output truncated to 2000 chars"""
        long_output = "x" * 3000
        block = ContentBlock(
            kind="tool_call", block_id="t1", status="completed",
            tool_name="bash", tool_input="cmd", tool_output=long_output
        )
        result = render_tool_panel(block)
        detail_content = result["elements"][0]["content"]
        # Should contain truncation indicator
        assert "..." in detail_content


class TestToolHistoryPanel:
    def test_tool_history_panel_structure(self):
        """Multiple tools → blue border, nested panels"""
        blocks = [
            ContentBlock(kind="tool_call", block_id=f"t{i}", status="completed",
                        tool_name="bash", tool_input=f"cmd{i}", tool_summary=f"cmd{i}")
            for i in range(4)
        ]
        result = render_tool_history_panel(blocks)
        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is False
        assert result["border"]["color"] == "blue"
        assert "4 个工具调用已完成" in result["header"]["title"]["content"]
        assert len(result["elements"]) == 4


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
        assert element_content.startswith("...")
        assert len(element_content) <= 510  # 500 + "..."


class TestPlanPanel:
    def test_plan_panel(self):
        """Plan renders as blue collapsible with step icons"""
        content = "1. ✅ 分析需求\n2. ⏳ 编写代码\n3. ○ 运行测试"
        block = ContentBlock(kind="plan", block_id="p1", content=content)
        result = render_plan_panel(block)
        assert result["tag"] == "collapsible_panel"
        assert result["expanded"] is True
        assert result["border"]["color"] == "blue"
        assert "执行计划" in result["header"]["title"]["content"]
        assert result["elements"][0]["content"] == content
