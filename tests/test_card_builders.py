import json
from typing import Optional

from src.card.builders.core import CoreBuilder
from src.card.builders.project import ProjectBuilder
from src.card.builders.worktree import WorktreeBuilder
from src.card.builders.system import SystemBuilder
from src.card.builders.deep import DeepBuilder
from src.card.models import EngineCardState
from src.project.context import ProjectContext


def test_core_builder_banner_element():
    """Verify that CoreBuilder._build_banner_element produces the correct column_set structure."""
    message = "Test Banner Message"
    
    # Test info banner (default)
    banner = CoreBuilder._build_banner_element(message, type="info")
    assert banner["tag"] == "column_set"
    assert banner["background_style"] == "blue"
    assert "ℹ️" in banner["columns"][0]["elements"][0]["content"]
    assert message in banner["columns"][0]["elements"][0]["content"]
    
    # Test success banner
    banner = CoreBuilder._build_banner_element(message, type="success")
    assert banner["background_style"] == "green"
    assert "✅" in banner["columns"][0]["elements"][0]["content"]
    
    # Test warning banner
    banner = CoreBuilder._build_banner_element(message, type="warning")
    assert banner["background_style"] == "yellow"
    assert "⚠️" in banner["columns"][0]["elements"][0]["content"]
    
    # Test error banner
    banner = CoreBuilder._build_banner_element(message, type="error")
    assert banner["background_style"] == "red"
    assert "❌" in banner["columns"][0]["elements"][0]["content"]


def test_project_builder_with_banner():
    """Verify that ProjectBuilder.build_project_response_card includes the banner."""
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    message = "Successfully added Claude"
    banner = CoreBuilder._build_banner_element(message, type="success")
    
    msg_type, card_json = ProjectBuilder.build_project_response_card(
        project, "Title", "Content", banner=banner
    )
    
    assert msg_type == "interactive"
    card = json.loads(card_json)
    
    # Banner should be the third element (Directory, HR, Banner, HR, Content, Buttons)
    elements = card["body"]["elements"]
    assert elements[2]["tag"] == "column_set"
    assert elements[2]["background_style"] == "green"
    assert message in elements[2]["columns"][0]["elements"][0]["content"]


def test_worktree_builder_with_message_banner():
    """Verify that WorktreeBuilder cards include the banner when message is provided."""
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    message = "Tool selected: Claude"
    
    # 1. Tool select card
    msg_type, card_json = WorktreeBuilder.build_worktree_tool_select_card(
        tools=[], selected_items=[], project_id=project.project_id, message=message
    )
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "green"
    assert message in elements[0]["columns"][0]["elements"][0]["content"]
    
    # 2. Model select card
    msg_type, card_json = WorktreeBuilder.build_worktree_model_select_card(
        models=[], tool_display_name="Claude", selected_items=[], project_id=project.project_id, message=message
    )
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    
    # 3. Progress card (uses info banner)
    msg_type, card_json = WorktreeBuilder.build_worktree_progress_card(
        units=[], project_id=project.project_id, message=message
    )
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "blue"


def test_system_builder_soft_failure_banner():
    """Verify that SystemBuilder.build_ttadk_soft_failure_card uses the banner."""
    message = "TTADK Timeout"
    msg_type, card_json = SystemBuilder.build_ttadk_soft_failure_card(message)
    
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "yellow"
    assert message in elements[0]["columns"][0]["elements"][0]["content"]


def test_deep_builder_warning_banner():
    """Verify that DeepBuilder.build_engine_card uses the banner for warnings."""
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    state = EngineCardState(
        title="Running",
        content="Thinking...",
        engine_name="Coco",
        warning_banner="Low credits warning"
    )
    
    msg_type, card_json = DeepBuilder.build_engine_card(project, state)
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    
    # Find the banner in elements
    banner_found = False
    for el in elements:
        if el.get("tag") == "column_set" and el.get("background_style") == "yellow":
            assert "Low credits warning" in el["columns"][0]["elements"][0]["content"]
            banner_found = True
            break
    
    assert banner_found is True
