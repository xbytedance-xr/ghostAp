import json

from src.card.builder import CardBuilder
from src.card.models import EngineCardState


class TestCardOptimization:
    def test_content_truncation(self):
        """Verify content is truncated when exceeding max_chars."""
        # Create content slightly larger than limit
        limit = 100
        content = "a" * (limit + 50)

        element = CardBuilder._build_content_element(content, max_chars=limit)

        assert "content" in element
        result = element["content"]

        # Check for truncation marker
        assert "日志内容过长，已被截断" in result

        # Calculate expected length: marker text + kept tail
        # keep_chars = limit - 100. Since limit=100, keep_chars=0, which might be weird.
        # Let's use a larger limit for realistic test
        limit = 200
        content = "a" * 300
        element = CardBuilder._build_content_element(content, max_chars=limit)
        result = element["content"]

        keep_chars = limit - 100
        assert len(result) < 300  # Original length
        assert content[-keep_chars:] in result

    def test_loop_theme_color(self):
        """Verify Loop engine uses distinct theme color."""
        # Test Loop engine
        color = CardBuilder._pick_deep_template("Loop(Coco)", "running")
        assert color == "indigo"

        color = CardBuilder._pick_deep_template("loop", "running")
        assert color == "indigo"

        # Test other engines
        color = CardBuilder._pick_deep_template("Coco", "running")
        assert color == "turquoise"

        color = CardBuilder._pick_deep_template("Claude", "running")
        assert color == "violet"

        # Test status overrides
        color = CardBuilder._pick_deep_template("Loop(Coco)", "error")
        assert color == "red"

        color = CardBuilder._pick_deep_template("Loop(Coco)", "completed")
        assert color == "green"

    def test_mobile_layout_optimization(self):
        """Verify Loop engine status line uses newline separator."""
        status_line = "Status: OK"
        duration_line = "Time: 1s"

        # Test Loop Engine
        _, card_json = CardBuilder.build_engine_card(
            project=None,
            state=EngineCardState(
                title="Loop Test",
                content="Content",
                engine_name="Loop(Coco)",
                status_line=status_line,
                duration_line=duration_line,
            ),
        )

        card = json.loads(card_json)
        # Find the meta element (markdown with notation size)
        meta_element = None
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown" and el.get("text_size") == "notation":
                # Skip footer note if any (footer note is usually last)
                # But here we don't have footer note passed
                meta_element = el
                break

        assert meta_element
        assert "Status: OK\nTime: 1s" in meta_element["content"]

        # Test Deep Engine (Should still use dot separator)
        _, card_json = CardBuilder.build_engine_card(
            project=None,
            state=EngineCardState(
                title="Deep Test",
                content="Content",
                engine_name="Coco",
                status_line=status_line,
                duration_line=duration_line,
            ),
        )

        card = json.loads(card_json)
        meta_element = None
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown" and el.get("text_size") == "notation":
                meta_element = el
                break

        assert meta_element
        assert "Status: OK · Time: 1s" in meta_element["content"]

    def test_markdown_truncation_auto_repair(self):
        """Verify Markdown code blocks are auto-closed when truncated."""
        # Case 1: Truncation happens inside a code block
        # Start with a code block, then a lot of content
        limit = 100
        content = "Prefix\n```python\n" + "x" * 200 + "\n```"

        # Max chars 100.
        # It will keep tail (max_chars - 100). Wait, max_chars - 100 = 0 if limit=100.
        # Let's use larger limit.
        limit = 200
        limit - 100  # 100 chars
        content = "Prefix\n```python\n" + "x" * 300 + "\n```"
        # Tail will be roughly the last 100 'x's + "\n```"

        element = CardBuilder._build_content_element(content, max_chars=limit)
        result = element["content"]

        # Original content has 2 blocks.
        # Truncated tail will have 1 block (the closing one).
        # Because we cut in the middle of code.
        # "Prefix\n```python\n" is at start, so it is cut off.
        # So we are "inside" code block at cut point?
        # Pre-cut content: "Prefix\n```python\nxxxx..."
        # It has 1 marker. So is_inside = True.
        # Tail: "...xxx\n```" (has 1 marker).
        # We prepend "```\n".
        # Result: "```\n...xxx\n```".
        # Markers: 1 (prepended) + 1 (in tail) = 2. Even. No suffix needed.

        assert result.count("```") == 2
        assert result.strip().endswith("```")

        # Case 2: Truncation happens inside code block, but tail does NOT contain closing marker
        # (e.g. log is very long and still running)
        content = "Prefix\n```python\n" + "x" * 500
        # Tail: last 100 'x's.
        # Pre-cut has 1 marker. is_inside = True.
        # Tail has 0 markers.
        # We prepend "```\n".
        # Result: "```\n...xxx".
        # Markers: 1 (prepended) + 0 = 1. Odd.
        # We append "\n```".
        # Result: "```\n...xxx\n```".

        element = CardBuilder._build_content_element(content, max_chars=limit)
        result = element["content"]

        assert result.count("```") == 2
        assert result.strip().endswith("```")

        # Case 3: Truncation happens OUTSIDE code block
        content = "Text\n" + "x" * 500
        # Pre-cut markers: 0. is_inside = False.
        # Tail: last 100 'x's. Markers: 0.
        # Result: "...xxx". No markers added.

        element = CardBuilder._build_content_element(content, max_chars=limit)
        result = element["content"]
        assert "```" not in result

        # Case 4: Truncation happens outside, but tail contains a full code block
        content = "x" * 500 + "\n```\ncode\n```"
        # Pre-cut markers: 0.
        # Tail: "...x\n```\ncode\n```". Markers: 2.
        # Even. No changes.

        element = CardBuilder._build_content_element(content, max_chars=limit)
        result = element["content"]
        assert result.count("```") == 2
        assert result.strip().endswith("```")

    def test_loop_history_theme_color(self):
        """Verify Loop engine history list uses indigo theme."""
        _, card_json = CardBuilder.build_history_list_card(
            project=None,
            title="History",
            content="Items",
            history_buttons=[],
            page=1,
            has_next=False,
            engine_name="Loop(Coco)",
        )

        card = json.loads(card_json)
        assert card["header"]["template"] == "indigo"

        # Verify default behavior (Coco -> Turquoise)
        _, card_json = CardBuilder.build_history_list_card(
            project=None,
            title="History",
            content="Items",
            history_buttons=[],
            page=1,
            has_next=False,
            engine_name="Coco",
        )

        card = json.loads(card_json)
        assert card["header"]["template"] == "turquoise"

    def test_error_visibility_compact(self):
        """Verify error details are shown in compact mode."""
        # Create more lines than COMPACT_LINE_THRESHOLD (15) to trigger truncation
        error_lines = [f"Error line {i}" for i in range(1, 25)]
        error_msg = "\n".join(error_lines)

        # Compact mode + Error status
        _, card_json = CardBuilder.build_engine_card(
            project=None,
            state=EngineCardState(
                title="Task Error",  # Triggers status_key="error"
                content=error_msg,
                engine_name="Coco",
                compact=True,
            ),
        )

        card = json.loads(card_json)
        # Find content element
        content_element = None
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown" and "Error line 1" in el.get("content", ""):
                content_element = el
                break

        assert content_element
        # Should show first N lines
        assert "Error line 5" in content_element["content"]
        # Should show truncation hint for >COMPACT_LINE_THRESHOLD lines
        assert '(展开日志查看更多)' in content_element["content"]

        # Compact mode + Normal status
        normal_msg = "Normal line 1\n" + "a" * 2000
        _, card_json = CardBuilder.build_engine_card(
            project=None,
            state=EngineCardState(title="Task Running", content=normal_msg, engine_name="Coco", compact=True),
        )

        card = json.loads(card_json)
        content_element = None
        for el in card["body"]["elements"]:
            # Content should be truncated, so "Normal line 1" is gone.
            # Look for the truncated content (lots of 'a')
            if el.get("tag") == "markdown" and "aaaaa" in el.get("content", ""):
                content_element = el
                break

        assert content_element
        # Should be truncated to last COMPACT_CHAR_FALLBACK (1500) chars (approx)
        assert len(content_element["content"]) <= 1550 + len("**Title**\n\n")  # rough check

    def test_control_buttons_layout_merging(self):
        """Verify that Pause and Stop buttons are merged into the same row (ColumnSet)."""
        # Create a state that has Pause and Stop buttons (running state)
        state = EngineCardState(
            title="Running Task",
            content="Log content",
            engine_name="Loop(Coco)",
            is_executing=True,  # Should trigger Pause + Stop
            compact=True,  # Should trigger Mode switch
            expanded=False,  # Should trigger Expand button
        )

        _, card_json = CardBuilder.build_engine_card(project=None, state=state)
        card = json.loads(card_json)

        # Find the button section (usually at the end, after hr)
        # We look for column_set
        column_sets = [el for el in card["body"]["elements"] if el.get("tag") == "column_set"]

        # We expect at least one column_set containing buttons
        assert column_sets

        # We want to find a column_set that contains BOTH "loop_pause" and "loop_stop" actions
        # Currently, if they are separate, we might find them in different column_sets
        # OR in the same column_set but different rows? No, column_set IS a row.

        merged_row_found = False
        for cs in column_sets:
            actions_in_row = []
            for col in cs.get("columns", []):
                for el in col.get("elements", []):
                    if el.get("tag") == "button":
                        val = el.get("value", {})
                        if isinstance(val, dict):
                            actions_in_row.append(val.get("action"))

            # Check if both pause and stop are in this row
            has_pause = any("pause" in a for a in actions_in_row)
            has_stop = any("stop" in a for a in actions_in_row)

            if has_pause and has_stop:
                merged_row_found = True
                break

        # This assertion should FAIL currently if the user report is correct (that they are separate)
        # Or PASS if they are already merged but maybe the user sees something else.
        # Based on user feedback "各占一行", this implies separate column_sets (vertical stack).
        assert merged_row_found, "Pause and Stop buttons should be in the same ColumnSet row"
