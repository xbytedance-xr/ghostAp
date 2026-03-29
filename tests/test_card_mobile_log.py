import json

from src.card.builder import CardBuilder
from src.card.models import EngineCardState


class TestCardMobileLog:
    def test_loop_engine_color(self):
        """Test that Loop engine uses indigo color."""
        # Using _pick_deep_template directly
        assert CardBuilder._pick_deep_template("loop") == "indigo"
        assert CardBuilder._pick_deep_template("Loop Engine") == "indigo"
        assert CardBuilder._pick_deep_template("loop_engine") == "indigo"

        # Verify in card JSON
        state = EngineCardState(
            engine_project_id="test_proj",
            title="Loop Task",
            content="Running...",
            engine_name="Loop",
            action_prefix="loop",
            compact=True,
        )
        # build_engine_card returns tuple(type, json_str)
        _, card_json_str = CardBuilder.build_engine_card(None, state)
        card_json = json.loads(card_json_str)

        # Check header template color
        assert card_json["header"]["template"] == "indigo"

    def test_deep_engine_color(self):
        """Test that Deep engine uses turquoise color."""
        assert CardBuilder._pick_deep_template("deep") == "turquoise"
        assert CardBuilder._pick_deep_template("DeepSeek") == "turquoise"

        state = EngineCardState(
            engine_project_id="test_proj",
            title="Deep Task",
            content="Running...",
            engine_name="Deep",
            action_prefix="deep",
            compact=True,
        )
        _, card_json_str = CardBuilder.build_engine_card(None, state)
        card_json = json.loads(card_json_str)

        assert card_json["header"]["template"] == "turquoise"

    def test_compact_mode_log_truncation(self):
        """Test that logs are truncated to ~5 lines in compact mode."""
        # Create a log with 20 lines
        lines = [f"Log line {i}" for i in range(20)]
        long_log = "\n".join(lines)

        state = EngineCardState(
            engine_project_id="test_proj",
            title="Test Task",
            content=long_log,
            compact=True,  # Mobile/Compact mode
            engine_name="Deep",
            action_prefix="deep",
        )
        _, card_json_str = CardBuilder.build_engine_card(None, state)
        card_json = json.loads(card_json_str)

        # Find the content element (usually after directory and hr)
        elements = card_json["body"]["elements"]
        # The content might start with "..." so we search for substring
        content_element = next((e for e in elements if "Log line" in e.get("content", "")), None)

        assert content_element is not None, "Log content element not found"
        content = content_element["content"]

        # Should contain LAST 5 lines (tail)
        # Log lines are 0..19. Last 5 are 15, 16, 17, 18, 19
        assert "Log line 15" in content
        assert "Log line 19" in content

        # Should NOT contain earlier lines (e.g. Log line 0)
        assert "Log line 0" not in content

        # Should contain truncation indicator
        assert "..." in content or "展开" in content

    def test_compact_mode_has_expand_option(self):
        """Test that compact mode offers a way to see full logs."""
        long_log = "\n".join([f"Line {i}" for i in range(20)])
        state = EngineCardState(
            engine_project_id="test_proj",
            title="Test Task",
            content=long_log,
            compact=True,
            engine_name="Deep",
            action_prefix="deep",
        )
        _, card_json_str = CardBuilder.build_engine_card(None, state)
        card_json = json.loads(card_json_str)

        # Verify expand/collapse button logic
        # In compact mode, we might expect a button to switch to full mode or expand
        # Let's check for specific action values
        json_str = json.dumps(card_json)
        assert "deep_mode_full" in json_str or "deep_expand" in json_str

    def test_compact_error_log_truncation(self):
        """Test that error logs in compact mode show enough context (first 5 lines)."""
        error_lines = [f"Error line {i}" for i in range(20)]
        error_log = "\n".join(error_lines)

        state = EngineCardState(
            engine_project_id="test_proj",
            title="Task Error Failed",  # Triggers error status (needs 'error' or '失败')
            content=error_log,
            compact=True,
            engine_name="Deep",
            action_prefix="deep",
        )
        _, card_json_str = CardBuilder.build_engine_card(None, state)
        card_json = json.loads(card_json_str)

        # Check header color (should be red for error)
        assert card_json["header"]["template"] == "red"

        # Check content
        json_str = json.dumps(card_json)
        # Should show first few lines of error
        assert "Error line 0" in json_str
        assert "Error line 4" in json_str
        # Should truncate later lines
        assert "Error line 10" not in json_str
