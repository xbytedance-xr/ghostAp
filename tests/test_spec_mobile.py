import json
import unittest

from src.card.builder import CardBuilder
from src.card.models import DeepCardState


class TestSpecMobile(unittest.TestCase):
    def test_spec_engine_theme_color(self):
        """Test that Spec engine uses correct theme color (green)."""
        assert CardBuilder._pick_deep_template("spec") == "green"
        assert CardBuilder._pick_deep_template("Spec Engine") == "green"
        assert CardBuilder._pick_deep_template("Spec(Coco)") == "green"

        # Verify in card JSON
        state = DeepCardState(
            deep_project_id="test_proj",
            title="Spec Task",
            content="Running...",
            engine_name="Spec(Coco)",
            action_prefix="spec",
            compact=True,
        )
        _, card_json_str = CardBuilder.build_deep_card(None, state)
        card_json = json.loads(card_json_str)

        # Check header template color
        assert card_json["header"]["template"] == "green"

    def test_spec_compact_mode_buttons(self):
        """Test that Spec engine cards in compact mode have appropriate buttons."""
        state = DeepCardState(
            deep_project_id="test_proj",
            title="Spec Task",
            content="Running...",
            engine_name="Spec",
            action_prefix="spec",
            compact=True,
            is_executing=True,
        )
        _, card_json_str = CardBuilder.build_deep_card(None, state)
        card_json = json.loads(card_json_str)

        # Check for pause/stop buttons
        actions = []
        for elem in card_json["body"]["elements"]:
            if elem.get("tag") == "action":
                for action in elem.get("actions"):
                    actions.append(action["value"]["action"])
            elif elem.get("tag") == "column_set":  # Mobile responsive layout uses column_set
                for col in elem.get("columns"):
                    for item in col.get("elements"):
                        if item.get("tag") == "button":
                            actions.append(item["value"]["action"])

        assert "spec_pause" in actions
        assert "spec_stop" in actions

    def test_spec_compact_mode_log_truncation(self):
        """Test that Spec logs are truncated in compact mode."""
        long_log = "\n".join([f"Log line {i}" for i in range(20)])
        state = DeepCardState(
            deep_project_id="test_proj",
            title="Spec Task",
            content=long_log,
            engine_name="Spec",
            action_prefix="spec",
            compact=True,
            is_executing=True,
        )
        _, card_json_str = CardBuilder.build_deep_card(None, state)
        card_json = json.loads(card_json_str)

        # Find content
        content_text = ""
        for elem in card_json["body"]["elements"]:
            if elem.get("tag") == "markdown" and "Log line" in elem.get("content", ""):
                content_text = elem["content"]
                break

        # Should contain last few lines but not all
        assert "Log line 19" in content_text
        assert "Log line 0" not in content_text
        assert "..." in content_text or "展开" in content_text


if __name__ == "__main__":
    unittest.main()
