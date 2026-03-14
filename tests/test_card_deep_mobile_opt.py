import pytest
import json
from src.card.builder import CardBuilder
from src.card.models import DeepCardState
from src.project.context import ProjectContext

class TestCardDeepMobileOpt:
    """Tests for Deep Agent card mobile optimization features."""

    @pytest.fixture
    def mock_project(self):
        return ProjectContext(
            project_id="test_project",
            project_name="Test Project",
            root_path="/tmp/test",
            theme_color="blue"
        )

    def test_log_truncation_full_mode_collapsed(self, mock_project):
        """Test that logs are truncated in Full mode when collapsed (default)."""
        # Create 20 lines of content
        content = "\n".join([f"Log line {i}" for i in range(20)])
        
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(
                title="Executing Task",
                content=content,
                is_executing=True,
                compact=False,
                expanded=False
            )
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
        assert "..." in content_element["content"]
        assert "(已折叠" in content_element["content"]
        # Ensure only last 10 lines are shown (approx check)
        assert "Log line 19" in content_element["content"]
        assert "Log line 0" not in content_element["content"]

    def test_log_truncation_full_mode_expanded(self, mock_project):
        """Test that logs are NOT truncated in Full mode when expanded."""
        content = "\n".join([f"Log line {i}" for i in range(20)])
        
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(
                title="Executing Task",
                content=content,
                is_executing=True,
                compact=False,
                expanded=True
            )
        )
        
        card = json.loads(card_json)
        body_elements = card["body"]["elements"]
        
        content_element = None
        for el in body_elements:
            if el["tag"] == "markdown" and "Log line" in el["content"]:
                content_element = el
                break
        
        assert content_element is not None
        assert "..." not in content_element["content"]
        assert "Log line 0" in content_element["content"]
        assert "Log line 19" in content_element["content"]

    def test_compact_mode_truncation(self, mock_project):
        """Test that logs are heavily truncated in Compact mode."""
        # Create very long single line content
        content = "A" * 600
    
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(
                title="Executing Task",
                content=content,
                is_executing=True,
                compact=True
            )
        )
        
        card = json.loads(card_json)
        body_elements = card["body"]["elements"]
        
        content_element = None
        for el in body_elements:
            if el["tag"] == "markdown" and "A" * 10 in el["content"]:
                content_element = el
                break
        
        assert content_element is not None
        # It should truncate, but maybe to 500 chars fallback if it's one long line
        assert len(content_element["content"]) < 600
        assert "..." in content_element["content"]

    def test_status_color_mapping(self, mock_project):
        """Test header color mapping based on status keywords."""
        
        # Error status
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(title="❌ Error Occurred", content="Details", engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "red"
        
        # Completed status
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(title="✅ Task Completed", content="Details", engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "green"
        
        # Planning status
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(title="🧠 Planning Step", content="Details", engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "blue"
        
        # Paused status (passed via flag)
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(title="Executing", content="Details", is_paused=True, engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "orange"
        
        # Default Executing (Coco)
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(title="Executing", content="Details", is_executing=True, engine_name="Coco")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "turquoise" # Default for Coco/Other running

        # Default Executing (Claude)
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(title="Executing", content="Details", is_executing=True, engine_name="Claude")
        )
        card = json.loads(card_json)
        assert card["header"]["template"] == "violet"

    def test_mode_switch_buttons(self, mock_project):
        """Test presence of mode switch and expand/collapse buttons."""
        content = "\n".join([f"Line {i}" for i in range(15)])
        
        # Case 1: Full Mode, Not Expanded -> Should show Expand button and Switch to Compact button
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(
                title="Title", content=content, is_executing=True,
                compact=False, expanded=False
            )
        )
        card = json.loads(card_json)
        buttons = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "action": # Button layout
                buttons.extend(el.get("actions", []))
            elif el.get("tag") == "div": # New responsive layout structure might use div?
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
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(
                title="Title", content=content, is_executing=True,
                compact=False, expanded=True
            )
        )
        card = json.loads(card_json)
        actions = self._extract_actions(card)
        action_types = [a.get("value", {}).get("action") for a in actions]
        
        assert "deep_collapse" in action_types
        
        # Case 3: Compact Mode -> Should show Switch to Full button, NO Expand/Collapse
        _, card_json = CardBuilder.build_deep_card(
            project=mock_project,
            state=DeepCardState(
                title="Title", content=content, is_executing=True,
                compact=True
            )
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
                        elif sub_el["tag"] == "button": # standalone button in some contexts?
                             actions.append(sub_el)
        return actions
