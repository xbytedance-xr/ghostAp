"""Tests for render_tool_panel empty data suppression (AC7/AC8).

AC7: When both tool_input AND tool_output are empty → returns None.
AC8: When only tool_output is empty (tool_input non-empty) → renders panel with input section only.
"""

from __future__ import annotations

import pytest

from src.card.render.tools import render_tool_panel
from src.card.state.models import ToolBlock


class TestAC7BothEmpty:
    """AC7: render_tool_panel returns None when both input and output are empty."""

    def test_both_none(self):
        block = ToolBlock(block_id="t1", tool_name="bash", tool_input=None, tool_output=None)
        assert render_tool_panel(block) is None

    def test_input_empty_string_output_none(self):
        block = ToolBlock(block_id="t2", tool_name="bash", tool_input="", tool_output=None)
        assert render_tool_panel(block) is None

    def test_input_whitespace_output_empty(self):
        block = ToolBlock(block_id="t3", tool_name="bash", tool_input="   ", tool_output="")
        assert render_tool_panel(block) is None

    def test_input_json_empty_list_output_whitespace(self):
        """_is_empty_data only treats actual empty dict/list as empty, not the string '{}'."""
        # String '{}' has content (2 chars) → not empty; but actual empty list/dict is empty
        # tool_input/tool_output are str|None, so we test with None/whitespace combos
        block = ToolBlock(block_id="t4", tool_name="read", tool_input=None, tool_output="\t\n")
        assert render_tool_panel(block) is None


class TestAC8InputOnlyNoOutput:
    """AC8: render_tool_panel returns panel with input section only when output is empty."""

    def test_input_nonempty_output_none(self):
        block = ToolBlock(
            block_id="t5", tool_name="read", status="completed",
            tool_input='{"path": "/tmp/file.txt"}', tool_output=None,
        )
        result = render_tool_panel(block)
        assert result is not None
        assert result["tag"] == "collapsible_panel"
        detail = result["elements"][0]["content"]
        assert "/tmp/file.txt" in detail
        # AC8: no output section rendered
        assert "输出" not in detail and "output" not in detail.lower()

    def test_input_nonempty_output_empty_string(self):
        block = ToolBlock(
            block_id="t6", tool_name="bash", status="completed",
            tool_input="ls -la", tool_output="",
        )
        result = render_tool_panel(block)
        assert result is not None
        detail = result["elements"][0]["content"]
        assert "ls -la" in detail
        # Only one code block (command), no result block
        assert detail.count("```") == 2  # opening + closing for command only

    def test_input_nonempty_output_whitespace(self):
        block = ToolBlock(
            block_id="t7", tool_name="bash", status="active",
            tool_input="echo hello", tool_output="   ",
        )
        result = render_tool_panel(block)
        assert result is not None
        detail = result["elements"][0]["content"]
        assert "echo hello" in detail
        assert detail.count("```") == 2


class TestNonEmptyBoth:
    """Sanity check: when both input and output are present, panel renders both."""

    def test_full_panel_renders(self):
        block = ToolBlock(
            block_id="t8", tool_name="bash", status="completed",
            tool_input="cat /etc/hostname", tool_output="myhost",
        )
        result = render_tool_panel(block)
        assert result is not None
        detail = result["elements"][0]["content"]
        assert "cat /etc/hostname" in detail
        assert "myhost" in detail
        # Two code blocks: command + result
        assert detail.count("```") == 4
