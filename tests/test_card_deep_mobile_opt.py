import json

import pytest

from src.card.builder import CardBuilder
from src.card.models import EngineCardState
from src.project.context import ProjectContext


class TestCardDeepMobileOpt:
    """Tests for Deep Agent card mobile optimization features."""

    @pytest.fixture
    def mock_project(self):
        return ProjectContext(
            project_id="test_project", project_name="Test Project", root_path="/tmp/test", theme_color="blue"
        )

    def test_log_truncation_full_mode_collapsed(self, mock_project):
        """Test that logs are truncated in Full mode when collapsed (default)."""
        # Create 55 lines of content (> FULL_LINE_THRESHOLD=50)
        content = "\n".join([f"Log line {i}" for i in range(55)])

        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(
                title="Executing Task", content=content, is_executing=True, compact=False, expanded=False
            ),
        )

        card = json.loads(card_json)
        body_elements = card["body"]["elements"]

        # Find the content element (should be markdown)
        content_element = None
        for el in body_elements:
            if el["tag"] == "markdown" and "Log line" in el["content"]:
                content_element = el
                break

        assert content_element is not None
        assert "…" in content_element["content"]
        # Ensure only last N lines are shown (approx check)
        assert "Log line 54" in content_element["content"]
        assert "Log line 0" not in content_element["content"]

    def test_log_truncation_full_mode_expanded(self, mock_project):
        """Test that logs are NOT truncated in Full mode when expanded."""
        content = "\n".join([f"Log line {i}" for i in range(55)])

        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(
                title="Executing Task", content=content, is_executing=True, compact=False, expanded=True
            ),
        )

        card = json.loads(card_json)
        body_elements = card["body"]["elements"]

        content_element = None
        for el in body_elements:
            if el["tag"] == "markdown" and "Log line" in el["content"]:
                content_element = el
                break

        assert content_element is not None
        assert "…" not in content_element["content"]
        assert "Log line 0" in content_element["content"]
        assert "Log line 54" in content_element["content"]

    def test_compact_mode_truncation(self, mock_project):
        """Test that logs are heavily truncated in Compact mode."""
        # Create very long single line content (> COMPACT_CHAR_FALLBACK=1500)
        content = "A" * 2000

        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(title="Executing Task", content=content, is_executing=True, compact=True),
        )

        card = json.loads(card_json)
        body_elements = card["body"]["elements"]

        content_element = None
        for el in body_elements:
            if el["tag"] == "markdown" and "A" * 10 in el["content"]:
                content_element = el
                break

        assert content_element is not None
        # It should truncate, but maybe to COMPACT_CHAR_FALLBACK chars if it's one long line
        assert len(content_element["content"]) < 2000
        assert "…" in content_element["content"]

    def test_status_color_mapping(self, mock_project):
        """Test header color mapping based on status keywords."""

        # Error status
        _, card_json = CardBuilder.build_info_card(
            project=mock_project, state=EngineCardState(title="❌ Error Occurred", content="Details", engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "red"

        # Completed status
        _, card_json = CardBuilder.build_info_card(
            project=mock_project, state=EngineCardState(title="✅ Task Completed", content="Details", engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "green"

        # Planning status
        _, card_json = CardBuilder.build_info_card(
            project=mock_project, state=EngineCardState(title="🧠 Planning Step", content="Details", engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "blue"

        # Paused status (passed via flag)
        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(title="Executing", content="Details", is_paused=True, engine_name="Coco"),
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "orange"

        # Default Executing (Coco)
        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(title="Executing", content="Details", is_executing=True, engine_name="Coco"),
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "turquoise"  # Default for Coco/Other running

        # Default Executing (Claude)
        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(title="Executing", content="Details", is_executing=True, engine_name="Claude"),
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "violet"

    def test_mode_switch_buttons(self, mock_project):
        """Test presence of mode switch and expand/collapse buttons."""
        content = "\n".join([f"Line {i}" for i in range(55)])

        # Case 1: Full Mode, Not Expanded -> Should show Expand button and Switch to Compact button
        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(title="Title", content=content, is_executing=True, compact=False, expanded=False),
        )
        card = json.loads(card_json)
        buttons = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "action":  # Button layout
                buttons.extend(el.get("actions", []))
            elif el.get("tag") == "div":  # New responsive layout structure might use div?
                # build_responsive_layout returns a list of column_sets or actions
                pass

        # Our build_responsive_layout returns column_set usually.
        # Let's inspect the actions recursively or check values
        actions = self._extract_actions(card)

        action_values = [a.get("value", {}) for a in actions]
        action_types = [v.get("action") for v in action_values]

        assert "deep_expand" in action_types
        assert "deep_mode_compact" in action_types

        # Case 2: Full Mode, Expanded -> Should show Collapse button
        _, card_json = CardBuilder.build_info_card(
            project=mock_project,
            state=EngineCardState(title="Title", content=content, is_executing=True, compact=False, expanded=True),
        )
        card = json.loads(card_json)
        actions = self._extract_actions(card)
        action_types = [a.get("value", {}).get("action") for a in actions]

        assert "deep_collapse" in action_types

        # Case 3: Compact Mode -> Should show Switch to Full button, NO Expand/Collapse
        _, card_json = CardBuilder.build_info_card(
            project=mock_project, state=EngineCardState(title="Title", content=content, is_executing=True, compact=True)
        )
        card = json.loads(card_json)
        actions = self._extract_actions(card)
        action_types = [a.get("value", {}).get("action") for a in actions]

        assert "deep_mode_full" in action_types
        assert "deep_expand" in action_types
        assert "deep_collapse" not in action_types

    def _extract_actions(self, card_dict):
        actions = []
        for el in card_dict["body"]["elements"]:
            if el["tag"] == "action":
                actions.extend(el["actions"])
            elif el["tag"] == "column_set":
                for col in el["columns"]:
                    for sub_el in col["elements"]:
                        if sub_el["tag"] == "action":
                            actions.extend(sub_el["actions"])
                        elif sub_el["tag"] == "button":  # standalone button in some contexts?
                            actions.append(sub_el)
        return actions


# ---------------------------------------------------------------------------
# Merged from test_card_mobile_log.py
# ---------------------------------------------------------------------------


class TestCardMobileLog:
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
        _, card_json_str = CardBuilder.build_info_card(None, state)
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
        _, card_json_str = CardBuilder.build_info_card(None, state)
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
        _, card_json_str = CardBuilder.build_info_card(None, state)
        card_json = json.loads(card_json_str)

        # Verify expand/collapse button logic
        # In compact mode, we might expect a button to switch to full mode or expand
        # Let's check for specific action values
        json_str = json.dumps(card_json)
        assert "deep_mode_full" in json_str or "deep_expand" in json_str

    def test_compact_error_log_truncation(self):
        """Test that error logs in compact mode show enough context (first N lines)."""
        error_lines = [f"Error line {i}" for i in range(40)]
        error_log = "\n".join(error_lines)

        state = EngineCardState(
            engine_project_id="test_proj",
            title="Task Error Failed",  # Triggers error status (needs 'error' or '失败')
            content=error_log,
            compact=True,
            engine_name="Deep",
            action_prefix="deep",
        )
        _, card_json_str = CardBuilder.build_info_card(None, state)
        card_json = json.loads(card_json_str)

        # Check header color (should be red for error)
        assert card_json["header"]["template"] == "red"

        # Check content
        json_str = json.dumps(card_json)
        # Should show first few lines of error
        assert "Error line 0" in json_str
        assert "Error line 4" in json_str
        # Should truncate later lines (COMPACT_LINE_THRESHOLD is 15)
        assert "Error line 25" not in json_str
