"""Tests for activity_digest: slim-flow card rendering.

Covers:
- render_activity_digest_line() categorization and formatting
- render_activity_digest_panel() compact expandable details
- render_active_tool_line() compact indicator
- flatten_to_atoms integration: pending buffer → activity_digest atoms
- End-to-end: activity_digest appears in rendered card body
"""

from __future__ import annotations

from src.card.state.models import CardMetadata, CardState, ContentBlock


# ------------------------------------------------------------------
# §1  render_activity_digest_line — unit tests
# ------------------------------------------------------------------
class TestRenderActivityDigestLine:
    """Verify categorization and formatting of the one-line digest."""

    @staticmethod
    def _make_block(tool_name: str, status: str = "completed") -> ContentBlock:
        return ContentBlock(
            kind="tool_call",
            block_id=f"b-{tool_name}",
            tool_name=tool_name,
            status=status,
        )

    def test_empty_input(self):
        from src.card.render.tools import render_activity_digest_line
        assert render_activity_digest_line([]) == ""

    def test_single_explore(self):
        from src.card.render.tools import render_activity_digest_line
        blocks = [self._make_block("Read")]
        result = render_activity_digest_line(blocks)
        assert "已探索 1 项" in result
        assert result.startswith("▣")

    def test_single_edit(self):
        from src.card.render.tools import render_activity_digest_line
        blocks = [self._make_block("Edit")]
        result = render_activity_digest_line(blocks)
        assert "已编辑 1 个文件" in result

    def test_single_command(self):
        from src.card.render.tools import render_activity_digest_line
        blocks = [self._make_block("Bash")]
        result = render_activity_digest_line(blocks)
        assert "已运行 1 条命令" in result

    def test_mixed_categories(self):
        from src.card.render.tools import render_activity_digest_line
        blocks = [
            self._make_block("Read"),
            self._make_block("Read"),
            self._make_block("Grep"),
            self._make_block("Edit"),
            self._make_block("Bash"),
            self._make_block("Bash"),
            self._make_block("Bash"),
        ]
        result = render_activity_digest_line(blocks)
        assert "已探索 2 项" in result
        assert "已搜索 1 次" in result
        assert "已编辑 1 个文件" in result
        assert "已运行 3 条命令" in result

    def test_failed_tools_counted(self):
        """Failed tools count in 'failed' bucket, not in category buckets."""
        from src.card.render.tools import render_activity_digest_line
        blocks = [
            self._make_block("Read", status="failed"),
            self._make_block("Bash", status="completed"),
        ]
        result = render_activity_digest_line(blocks)
        assert "1 项失败" in result
        assert "已运行 1 条命令" in result
        # Failed Read should NOT be counted in explored
        assert "已探索" not in result

    def test_unknown_tool_counted_as_other(self):
        from src.card.render.tools import render_activity_digest_line
        blocks = [self._make_block("CustomTool")]
        result = render_activity_digest_line(blocks)
        assert "1 次其他调用" in result

    def test_failed_tool_not_double_counted(self):
        """A failed tool counts only in 'failed', not in its category."""
        from src.card.render.tools import render_activity_digest_line
        blocks = [
            self._make_block("Read", status="failed"),
            self._make_block("Read", status="completed"),
        ]
        result = render_activity_digest_line(blocks)
        assert "已探索 1 项" in result  # only the completed one
        assert "1 项失败" in result

    def test_tool_name_none(self):
        """tool_name=None falls into 'other' category without error."""
        from src.card.render.tools import render_activity_digest_line
        block = ContentBlock(kind="tool_call", block_id="b-none", tool_name=None, status="completed")
        result = render_activity_digest_line([block])
        assert "1 次其他调用" in result

    def test_tool_name_empty_string(self):
        """Empty tool_name falls into 'other' category."""
        from src.card.render.tools import render_activity_digest_line
        block = ContentBlock(kind="tool_call", block_id="b-empty", tool_name="", status="completed")
        result = render_activity_digest_line([block])
        assert "1 次其他调用" in result


class TestRenderActivityDigestPanel:
    """Verify the expandable aggregate panel stays compact."""

    def test_panel_lists_inputs_but_omits_tool_outputs(self):
        from src.card.render.tools import render_activity_digest_panel

        panel = render_activity_digest_panel([
            ContentBlock(
                kind="tool_call",
                block_id="read",
                tool_name="Read",
                status="completed",
                tool_input='{"path": "src/card/render/tools.py"}',
                tool_output="full file content should not appear",
            ),
            ContentBlock(
                kind="tool_call",
                block_id="cmd",
                tool_name="Bash",
                status="completed",
                tool_input="git diff --stat",
                tool_output="large command output should not appear",
            ),
        ])

        assert panel is not None
        assert panel["tag"] == "collapsible_panel"
        assert panel["expanded"] is False
        rendered = str(panel)
        assert "已探索 1 项" in rendered
        assert "已运行 1 条命令" in rendered
        assert "读取 `src/card/render/tools.py`" in rendered
        assert "运行 `git diff --stat`" in rendered
        assert "full file content" not in rendered
        assert "large command output" not in rendered

    def test_panel_limits_detail_rows(self):
        from src.card.render.tools import render_activity_digest_panel

        blocks = [
            ContentBlock(
                kind="tool_call",
                block_id=f"read-{i}",
                tool_name="Read",
                status="completed",
                tool_input=f'{{"path": "file{i}.py"}}',
            )
            for i in range(8)
        ]
        panel = render_activity_digest_panel(blocks)

        assert panel is not None
        detail = panel["elements"][0]["content"]
        assert "file0.py" in detail
        assert "file5.py" in detail
        assert "file6.py" not in detail
        assert "另有 2 项已折叠" in detail

    def test_panel_border_reflects_dominant_activity(self):
        from src.card.render.tools import render_activity_digest_panel

        panel = render_activity_digest_panel([
            ContentBlock(
                kind="tool_call",
                block_id="edit-1",
                tool_name="Edit",
                status="completed",
            ),
            ContentBlock(
                kind="tool_call",
                block_id="edit-2",
                tool_name="Write",
                status="completed",
            ),
            ContentBlock(
                kind="tool_call",
                block_id="read-1",
                tool_name="Read",
                status="completed",
            ),
        ])

        assert panel is not None
        assert panel["border"]["color"] == "green"


# ------------------------------------------------------------------
# §2  render_active_tool_line — unit tests
# ------------------------------------------------------------------
class TestRenderActiveToolLine:
    """Verify compact one-line indicator for active tools."""

    def test_tool_with_summary(self):
        from src.card.render.tools import render_active_tool_line
        block = ContentBlock(
            kind="tool_call",
            block_id="b1",
            tool_name="Read",
            status="active",
            tool_input='{"file_path": "src/main.py"}',
        )
        result = render_active_tool_line(block)
        assert result.startswith("⏳")
        assert "Read" in result
        assert "src/main.py" in result

    def test_tool_without_summary(self):
        from src.card.render.tools import render_active_tool_line
        block = ContentBlock(
            kind="tool_call",
            block_id="b1",
            tool_name="Think",
            status="active",
        )
        result = render_active_tool_line(block)
        assert "⏳ **Think**" in result

    def test_malformed_json_input(self):
        """Non-JSON tool_input should not crash, falls back to tool name only."""
        from src.card.render.tools import render_active_tool_line
        block = ContentBlock(
            kind="tool_call",
            block_id="b1",
            tool_name="Read",
            status="active",
            tool_input="this is not json at all",
        )
        result = render_active_tool_line(block)
        assert "⏳" in result
        assert "Read" in result


# ------------------------------------------------------------------
# §3  flatten_to_atoms — integration
# ------------------------------------------------------------------
class TestFlattenToAtomsDigest:
    """Verify that completed tools become activity_digest atoms."""

    def test_completed_tools_produce_digest(self):
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Read", status="completed"),
            ContentBlock(kind="tool_call", block_id="t2", tool_name="Read", status="completed"),
            ContentBlock(kind="text", block_id="txt1", content="Agent says hello"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        digest_atoms = [a for a in atoms if a.kind == "activity_digest"]
        assert len(digest_atoms) == 1
        assert "已探索 2 项" in digest_atoms[0].content

    def test_active_tool_produces_tool_panel(self):
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Read", status="active"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        tool_atoms = [a for a in atoms if a.kind == "tool_panel"]
        assert len(tool_atoms) == 1

    def test_mixed_completed_and_active_flush_before_active(self):
        """Completed tools flush to digest before active tool renders."""
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Read", status="completed"),
            ContentBlock(kind="tool_call", block_id="t2", tool_name="Bash", status="active"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        kinds = [a.kind for a in atoms]
        assert "activity_digest" in kinds
        assert "tool_panel" in kinds
        # digest comes before active tool panel
        assert kinds.index("activity_digest") < kinds.index("tool_panel")

    def test_text_between_tools_flushes_digest(self):
        """Text after completed tools triggers digest flush."""
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Edit", status="completed"),
            ContentBlock(kind="text", block_id="txt1", content="Review changes"),
            ContentBlock(kind="tool_call", block_id="t2", tool_name="Bash", status="completed"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        digest_atoms = [a for a in atoms if a.kind == "activity_digest"]
        # Two separate digest flushes
        assert len(digest_atoms) == 2

    def test_trailing_completed_tools_flushed(self):
        """Completed tools at the end of blocks are flushed to digest."""
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="text", block_id="txt1", content="Starting"),
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Read", status="completed"),
            ContentBlock(kind="tool_call", block_id="t2", tool_name="Read", status="completed"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        digest_atoms = [a for a in atoms if a.kind == "activity_digest"]
        assert len(digest_atoms) == 1
        assert "已探索 2 项" in digest_atoms[0].content

    def test_multiple_consecutive_active_tools(self):
        """Multiple active tools each produce their own tool_panel atom."""
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Read", status="active"),
            ContentBlock(kind="tool_call", block_id="t2", tool_name="Edit", status="active"),
            ContentBlock(kind="tool_call", block_id="t3", tool_name="Bash", status="active"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        tool_atoms = [a for a in atoms if a.kind == "tool_panel"]
        digest_atoms = [a for a in atoms if a.kind == "activity_digest"]
        assert len(tool_atoms) == 3
        assert len(digest_atoms) == 0

    def test_reasoning_block_flushes_pending_tools(self):
        """A reasoning block triggers flush of pending completed tools."""
        from src.card.render.atoms import flatten_to_atoms
        from src.card.render.budget import RenderBudget

        blocks = (
            ContentBlock(kind="tool_call", block_id="t1", tool_name="Read", status="completed"),
            ContentBlock(kind="reasoning", block_id="r1", content="Let me think..."),
            ContentBlock(kind="tool_call", block_id="t2", tool_name="Edit", status="completed"),
        )
        atoms = flatten_to_atoms(blocks, RenderBudget())
        digest_atoms = [a for a in atoms if a.kind == "activity_digest"]
        assert len(digest_atoms) == 2  # one before reasoning, one at end


# ------------------------------------------------------------------
# §4  End-to-end: activity_digest in rendered card
# ------------------------------------------------------------------
class TestActivityDigestInCard:
    """Verify activity_digest appears correctly in rendered card output."""

    def test_digest_in_body_not_appendix(self):
        from src.card.render.budget import RenderBudget
        from src.card.render.renderer import render_card

        state = CardState(
            blocks=(
                ContentBlock(kind="text", block_id="t1", content="Planning done."),
                ContentBlock(kind="tool_call", block_id="tool1", tool_name="Read", status="completed"),
                ContentBlock(kind="tool_call", block_id="tool2", tool_name="Edit", status="completed"),
                ContentBlock(kind="text", block_id="t2", content="All changes applied."),
            ),
            metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        body_str = str(body)
        assert "已探索" in body_str
        assert "已编辑" in body_str

    def test_digest_uses_normal_text_size_for_mobile_readability(self):
        from src.card.render.budget import RenderBudget
        from src.card.render.renderer import render_card

        state = CardState(
            blocks=(
                ContentBlock(kind="tool_call", block_id="tool1", tool_name="Bash", status="completed"),
                ContentBlock(kind="text", block_id="t1", content="Done."),
            ),
            metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]
        digest_els = [
            el for el in body
            if el.get("tag") == "collapsible_panel" and "已运行" in str(el)
        ]
        assert len(digest_els) >= 1
        assert digest_els[0]["elements"][0]["text_size"] == "normal"

    def test_running_tool_renders_as_distinct_banner(self):
        from src.card.render.budget import RenderBudget
        from src.card.render.renderer import render_card

        state = CardState(
            blocks=(
                ContentBlock(
                    kind="tool_call",
                    block_id="tool1",
                    tool_name="Bash",
                    status="active",
                    tool_input="uv run python -m pytest tests/ -q",
                ),
            ),
            metadata=CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep"),
        )
        cards = render_card(state, RenderBudget())
        body = cards[0]._card_json["body"]["elements"]

        running = [
            el for el in body
            if el.get("tag") == "column_set" and el.get("background_style") == "wathet"
        ]
        assert running
        assert "uv run python -m pytest tests/ -q" in str(running[0])
