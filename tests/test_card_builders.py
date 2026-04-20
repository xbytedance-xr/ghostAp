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


def test_worktree_confirm_card_grouping():
    """Verify that build_worktree_confirm_card groups input and action correctly."""
    selected_items = [{"display_label": "Coco (GPT-4)"}]
    project_id = "test-project"

    msg_type, card_json = WorktreeBuilder.build_worktree_confirm_card(
        selected_items=selected_items, project_id=project_id
    )

    assert msg_type == "interactive"
    card = json.loads(card_json)
    elements = card["body"]["elements"]

    # Check elements:
    # 0: Content (Selection list)
    # 1: Banner (column_set)
    # 2: Hot Area (column_set with wathet background)

    assert elements[0]["tag"] == "markdown"
    assert "即将启动以下工具-模型组合" in elements[0]["content"]

    assert elements[1]["tag"] == "column_set"
    assert "请在下方输入您的任务需求" in elements[1]["columns"][0]["elements"][0]["content"]

    hot_area = elements[2]
    assert hot_area["tag"] == "column_set"
    assert hot_area["background_style"] == "wathet"

    # Inside hot area column
    column_elements = hot_area["columns"][0]["elements"]
    assert column_elements[0]["tag"] == "input"
    assert column_elements[0]["name"] == "worktree_goal"

    assert column_elements[1]["tag"] == "action"
    assert column_elements[1]["actions"][0]["text"]["content"] == "确认并开始执行"


def test_worktree_progress_card_ready_grouping():
    """Verify that build_worktree_progress_card groups input and action when ready."""
    units = [
        {"tool_name": "coco", "display_name": "Coco", "status": "ready", "task_title": "Ready task"}
    ]
    project_id = "test-project"

    msg_type, card_json = WorktreeBuilder.build_worktree_progress_card(
        units=units, project_id=project_id
    )

    card = json.loads(card_json)
    elements = card["body"]["elements"]

    # elements[0]: Info banner (if message provided, here message="")
    # elements[0]: Content (Progress list)
    # elements[1]: Banner (Ready guidance)
    # elements[2]: Hot Area (column_set)

    # Note: build_worktree_progress_card adds message banner at index 0 if message exists.
    # If no message, elements[0] is content.

    assert elements[0]["tag"] == "markdown"
    assert "**执行进度：**" in elements[0]["content"]

    assert elements[1]["tag"] == "column_set"
    assert "所有单元已就绪" in elements[1]["columns"][0]["elements"][0]["content"]

    hot_area = elements[2]
    assert hot_area["tag"] == "column_set"
    assert hot_area["background_style"] == "wathet"
    assert hot_area["columns"][0]["elements"][0]["tag"] == "input"
    assert hot_area["columns"][0]["elements"][1]["tag"] == "action"


def test_worktree_progress_card_with_failure():
    """Verify that build_worktree_progress_card shows error details for failed units."""
    units = [
        {
            "tool_name": "coco",
            "display_name": "Coco",
            "status": "failed",
            "task_title": "Fix bugs",
            "error": "执行超时: connection lost"
        }
    ]
    project_id = "test-project"

    msg_type, card_json = WorktreeBuilder.build_worktree_progress_card(
        units=units, project_id=project_id
    )

    card = json.loads(card_json)
    content = card["body"]["elements"][0]["content"]

    assert "❌ **Coco** · `failed` · Fix bugs" in content
    assert "> 🔍 **失败原因**：执行超时: connection lost" in content


# ------------------------------------------------------------------
# Dynamic progress card title tests
# ------------------------------------------------------------------


def _get_progress_card_header(units):
    """Helper: build progress card and return (header_title, header_color)."""
    _, card_json = WorktreeBuilder.build_worktree_progress_card(units=units)
    card = json.loads(card_json)
    header = card["header"]
    return header["title"]["content"], header["template"]


def test_worktree_progress_card_title_ready():
    """All units ready → title contains '就绪', color turquoise."""
    units = [
        {"tool_name": "coco", "display_name": "Coco", "status": "ready", "task_title": ""},
        {"tool_name": "claude", "display_name": "Claude", "status": "ready", "task_title": ""},
    ]
    title, color = _get_progress_card_header(units)
    assert "就绪" in title
    assert color == "turquoise"


def test_worktree_progress_card_title_running():
    """At least one unit running → title contains '执行中', color blue."""
    units = [
        {"tool_name": "coco", "display_name": "Coco", "status": "running", "task_title": "Task A"},
        {"tool_name": "claude", "display_name": "Claude", "status": "ready", "task_title": ""},
    ]
    title, color = _get_progress_card_header(units)
    assert "执行中" in title
    assert color == "blue"


def test_worktree_progress_card_title_completed():
    """All units completed → title contains '已完成', color green."""
    units = [
        {"tool_name": "coco", "display_name": "Coco", "status": "completed", "task_title": "Done"},
        {"tool_name": "claude", "display_name": "Claude", "status": "completed", "task_title": "Done"},
    ]
    title, color = _get_progress_card_header(units)
    assert "已完成" in title
    assert color == "green"


def test_worktree_progress_card_title_partial_failure():
    """Failed units with no running → title contains '部分失败', color red."""
    units = [
        {"tool_name": "coco", "display_name": "Coco", "status": "completed", "task_title": "Done"},
        {"tool_name": "claude", "display_name": "Claude", "status": "failed", "task_title": "Oops", "error": "timeout"},
    ]
    title, color = _get_progress_card_header(units)
    assert "部分失败" in title
    assert color == "red"


def test_worktree_progress_card_title_empty_units():
    """Empty units list → default title '执行中', color blue."""
    title, color = _get_progress_card_header([])
    assert "执行中" in title
    assert color == "blue"
