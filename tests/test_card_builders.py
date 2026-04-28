import json
from typing import Optional

from src.card.builder import CardBuilder
from src.card.builders.core import CoreBuilder
from src.card.builders.project import ProjectBuilder
from src.card.builders.worktree import WorktreeBuilder
from src.card.styles import UI_TEXT
from src.card.builders.system import SystemBuilder
from src.card.builders.deep import DeepBuilder
from src.card.models import (
    BannerKind,
    EngineCardState,
    EngineStatusEntry,
    ModelOptionView,
    ToolOptionView,
    WorktreeBannerContext,
)
from src.project.context import ProjectContext


def test_shorten_goal_for_banner_cleans_newlines():
    """Verify that _shorten_goal_for_banner correctly cleans up newlines and excessive spaces."""
    
    # 1. 包含换行符的短文本
    goal1 = "This is a\nmultiline\ngoal"
    res1 = WorktreeBuilder._shorten_goal_for_banner(goal1)
    assert res1 == "This is a multiline goal"
    assert "\n" not in res1
    
    # 2. 包含连续空行及多余空白的文本
    goal2 = "Task with \n\n  multiple \r\n\n blank lines"
    res2 = WorktreeBuilder._shorten_goal_for_banner(goal2)
    assert res2 == "Task with multiple blank lines"
    
    # 3. 超长且包含换行符的文本需要被清洗并正确截断
    # 我们期望截断发生在单行化之后的第 max_len 处
    goal3 = "A\n" * 40 + "B" * 50  # 长文本
    res3 = WorktreeBuilder._shorten_goal_for_banner(goal3, max_len=80)
    assert len(res3) == 80
    assert res3.endswith("...")
    assert "\n" not in res3
    
    # 4. 包含 Markdown 标记的文本应该被清洗，防止加粗语法被破坏
    goal4 = "**Bold** and \n**newline**"
    res4 = WorktreeBuilder._shorten_goal_for_banner(goal4)
    assert res4 == "Bold and newline"
    assert "**" not in res4
    
    # 5. 超长带有 Markdown 标记的截断测试
    # "**" + "A"*80 + "**" 移除 "**" 后是 "A"*80
    # 长度刚好等于 max_len，所以不应该被截断补省略号
    goal5 = "**" + "A" * 80 + "**"
    res5 = WorktreeBuilder._shorten_goal_for_banner(goal5, max_len=80)
    assert len(res5) <= 80
    assert res5 == "A" * 80
    assert "**" not in res5
    
    # 6. 超过 max_len 带有 Markdown 标记的截断测试
    goal6 = "**" + "A" * 85 + "**"
    res6 = WorktreeBuilder._shorten_goal_for_banner(goal6, max_len=80)
    assert len(res6) <= 80
    assert res6.endswith("...")
    assert "**" not in res6

    # 7. 空文本
    assert WorktreeBuilder._shorten_goal_for_banner(None) == ""
    assert WorktreeBuilder._shorten_goal_for_banner("") == ""
    assert WorktreeBuilder._shorten_goal_for_banner("   \n  ") == ""


def test_tool_option_view_defaults():
    opt = ToolOptionView(name="coco")

    assert opt.name == "coco"
    assert opt.description == ""
    assert opt.is_default is False
    assert opt.emoji == "🤖"
    assert opt.disabled is False


def test_model_option_view_defaults():
    opt = ModelOptionView(name="gpt-5.2")

    assert opt.name == "gpt-5.2"
    assert opt.description == ""
    assert opt.is_default is False
    assert opt.display_name is None


def test_core_builder_banner_element():
    """Verify that CoreBuilder._build_banner_element produces the correct column_set structure."""
    message = "Test Banner Message"

    # Test info banner (default)
    banner = CoreBuilder._build_banner_element(message, type="info")
    assert banner["tag"] == "column_set"
    assert banner["background_style"] == "wathet"  # info 使用浅蓝色
    assert "ℹ️" in banner["columns"][0]["elements"][0]["content"]
    assert message in banner["columns"][0]["elements"][0]["content"]

    # Test success banner
    banner = CoreBuilder._build_banner_element(message, type="success")
    assert banner["background_style"] == "green"  # success 使用绿色
    assert "✅" in banner["columns"][0]["elements"][0]["content"]

    # Test warning banner - Apple 风格优化：使用橙色代替黄色
    banner = CoreBuilder._build_banner_element(message, type="warning")
    assert banner["background_style"] == "orange"  # warning 使用橙色（更温和、更现代）
    assert "⚠️" in banner["columns"][0]["elements"][0]["content"]

    # Test error banner
    banner = CoreBuilder._build_banner_element(message, type="error")
    assert banner["background_style"] == "red"  # error 使用红色
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
    assert elements[2]["background_style"] == "green"  # success 使用绿色
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
    assert elements[0]["background_style"] == "green"  # worktree 使用 success 类型
    assert message in elements[0]["columns"][0]["elements"][0]["content"]
    
    # 2. Model select card
    msg_type, card_json = WorktreeBuilder.build_worktree_model_select_card(
        models=[], tool_display_name="Claude", selected_items=[], project_id=project.project_id, message=message
    )
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "green"  # worktree 使用 success 类型
    
    # 3. Progress card (uses info banner)
    msg_type, card_json = WorktreeBuilder.build_worktree_progress_card(
        units=[], project_id=project.project_id, message=message
    )
    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "wathet"  # progress card 使用 info 类型


def test_system_builder_soft_failure_banner():
    """Verify that SystemBuilder.build_ttadk_soft_failure_card uses the banner."""
    message = "TTADK Timeout"
    msg_type, card_json = SystemBuilder.build_ttadk_soft_failure_card(message)

    card = json.loads(card_json)
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert elements[0]["background_style"] == "orange"  # soft failure 使用 warning 类型（Apple 风格优化：橙色）
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
        if el.get("tag") == "column_set" and el.get("background_style") == "orange":  # warning 使用橙色（Apple 风格优化）
            assert "Low credits warning" in el["columns"][0]["elements"][0]["content"]
            banner_found = True
            break

    assert banner_found is True


def test_worktree_auto_execute_banner_helper_basic():
    """_build_auto_execute_banner_text 应拼接基础文案 + goal 摘要 + 工具摘要。"""

    ctx = WorktreeBannerContext(
        message=UI_TEXT["worktree_auto_executing_banner"],
        goal="Refactor everything",
        selected_items=[{"display_label": "Coco / gpt-5.1"}],
        banner_kind=BannerKind.AUTO_EXECUTE,
    )

    banner = WorktreeBuilder._build_auto_execute_banner_text(ctx)

    # 1. 原始自动执行文案
    assert UI_TEXT["worktree_auto_executing_banner"] in banner
    # 2. goal 摘要以「」包裹
    assert "「Refactor everything" in banner
    # 3. 工具/模型标签采用 "Coco · gpt-5.1" 形式
    assert "Coco · gpt-5.1" in banner


def test_worktree_auto_execute_banner_helper_empty_fields():
    """空 goal / selected_items 时仅保留基础文案，不应报错。"""

    ctx = WorktreeBannerContext(
        message=UI_TEXT["worktree_auto_executing_banner"],
        goal="",
        selected_items=None,
        banner_kind=BannerKind.AUTO_EXECUTE,
    )

    banner = WorktreeBuilder._build_auto_execute_banner_text(ctx)

    # 仅包含基础自动执行文案，不包含 goal 行和 "使用：" 行
    assert UI_TEXT["worktree_auto_executing_banner"] in banner
    assert "「" not in banner
    assert "使用：" not in banner


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
    assert "请在下方输入您的任务目标" in elements[1]["columns"][0]["elements"][0]["content"]

    hot_area = elements[2]
    assert hot_area["tag"] == "column_set"
    assert hot_area["background_style"] == "wathet"

    # Inside hot area column
    column_elements = hot_area["columns"][0]["elements"]
    assert column_elements[0]["tag"] == "input"
    assert column_elements[0]["name"] == "worktree_goal"

    assert column_elements[1]["tag"] == "button"
    assert column_elements[1]["text"]["content"] == "确认并开始执行"


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
    assert hot_area["columns"][0]["elements"][1]["tag"] == "button"


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


# ------------------------------------------------------------------
# CardBuilder facade diagnostics delegates
# ------------------------------------------------------------------


def test_card_builder_diagnostics_task_board_empty():
    """CardBuilder.build_task_board_content should show empty hint when no tasks exist."""

    content = CardBuilder.build_task_board_content(tasks=[])

    # DiagnosticsBuilder uses diag_no_active_tasks as the primary empty-state hint.
    assert UI_TEXT["diag_no_active_tasks"] in content


def test_card_builder_diagnostics_task_board_grouped_view():
    """Grouped task board view should include project name, status emoji and message."""

    class DummySpec:
        def __init__(self, name: str, task_type: str) -> None:
            self.name = name
            self.task_type = task_type

    class DummyState:
        def __init__(self) -> None:
            self.status = "running"
            self.progress_percent = 42
            self.progress_message = "syncing context"
            self.run_id = "run-12345"
            self.spec = DummySpec("Sync", "update")

    class DummyProject:
        def __init__(self, name: str) -> None:
            self.project_name = name

    class DummyProjectManager:
        def get_project_for_diagnostics(self, pid: str) -> DummyProject:
            return DummyProject(f"Project-{pid}")

    state = DummyState()
    groups = {"p1": [state]}

    content = CardBuilder.build_task_board_content(
        tasks=[state], groups=groups, project_manager=DummyProjectManager()
    )

    # Project name should be rendered from project manager
    assert "Project-p1" in content
    # Status emoji for "running" and progress message should be present
    assert "🔄" in content
    assert state.progress_message in content
    # Run id short form should contain the tail of run_id
    assert "run-12345" in content


def test_card_builder_unified_status_content_basic():
    """Unified status content should summarize engine entries with emojis."""

    entries = [
        EngineStatusEntry(
            mode="Deep",
            task_id="task-abcdef123456",
            name="DeepRun",
            status="running",
            info="executing",
        ),
        EngineStatusEntry(
            mode="Loop",
            task_id="task-ffffeeee1111",
            name="LoopRun",
            status="failed",
            info="error",
        ),
    ]

    content = CardBuilder.build_unified_status_content(entries, include_done=False, project_name="DemoProject")

    # Header should contain total count
    assert str(len(entries)) in content
    # Per-entry lines should include mode, name, info and mapped emojis
    assert "Deep" in content and "Loop" in content
    assert "DeepRun" in content and "LoopRun" in content
    assert "executing" in content and "error" in content
    assert "🔄" in content  # running
    assert "❌" in content  # failed
    # When include_done is False, the all-tasks hint should be appended
    assert UI_TEXT["diag_status_all_hint"] in content


def test_card_builder_unified_status_content_include_done_suppresses_hint():
    """include_done=True should suppress the 'show all' hint."""

    entries = [
        EngineStatusEntry(
            mode="Deep",
            task_id="task-xyz",
            name="DeepRun",
            status="completed",
            info="done",
        )
    ]

    content = CardBuilder.build_unified_status_content(entries, include_done=True)
    assert UI_TEXT["diag_status_all_hint"] not in content


def test_card_builder_format_engine_status_info_deep_uses_status_when_no_duration():
    """Deep mode with zero duration should fall back to raw status string."""

    class Dummy:
        def __init__(self) -> None:
            self.status = "RUNNING"

        def duration(self) -> float:
            return 0.0

    mode_label = UI_TEXT["diag_engine_deep"]
    info = CardBuilder.format_engine_status_info(mode_label, Dummy())
    assert info == "RUNNING"


def test_card_builder_format_engine_status_info_loop_includes_iteration_and_criteria():
    """Loop mode should include iteration index and criteria ratio when available."""

    class Dummy:
        def __init__(self) -> None:
            self.status = "LOOP"
            self.current_iteration = 3
            self.satisfied_count = 1
            self.total_criteria = 4

        def duration(self) -> float:
            return 0.0

    mode_label = UI_TEXT["diag_engine_loop"]
    info = CardBuilder.format_engine_status_info(mode_label, Dummy())

    assert "3" in info
    assert "1/4" in info


def test_card_builder_format_engine_status_info_spec_includes_cycle_phase_and_criteria():
    """Spec mode should include cycle number, phase label and criteria ratio when available."""

    class Phase:
        def __init__(self, display_name: str) -> None:
            self.display_name = display_name

    class Cycle:
        def __init__(self) -> None:
            self.phase = Phase("Plan")

    class Dummy:
        def __init__(self) -> None:
            self.status = "SPEC"
            self.current_cycle_number = 2
            self.current_cycle = Cycle()
            self.satisfied_count = 2
            self.total_criteria = 5

        def duration(self) -> float:
            return 0.0

    mode_label = UI_TEXT["diag_engine_spec"]
    info = CardBuilder.format_engine_status_info(mode_label, Dummy())

    assert "2" in info
    assert "Plan" in info
    assert "2/5" in info
