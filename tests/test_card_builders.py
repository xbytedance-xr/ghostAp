import json
from typing import Optional

import pytest

from src.card.builder import CardBuilder
from src.card.builders.core import CoreBuilder
from src.card.builders.project import ProjectBuilder
from src.card.styles import UI_TEXT
from src.card.builders.system import SystemBuilder
from src.card.builders.deep import DeepBuilder
from src.card.models import (
    EngineCardState,
    EngineStatusEntry,
    ModelOptionView,
    ToolOptionView,
)
from src.project.context import ProjectContext


def _collect_buttons(card: dict) -> list[dict]:
    buttons: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                buttons.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return buttons


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


def test_help_card_mentions_new_chat_project_group():
    SystemBuilder._build_help_card_cached.cache_clear()

    msg_type, card_json = SystemBuilder.build_help_card(
        session_idle_timeout=600,
        session_idle_warn_at_remaining=120,
        lock_undo_window_seconds=300,
    )

    assert msg_type == "interactive"
    card_text = json.dumps(json.loads(card_json), ensure_ascii=False)
    assert "/new-chat" in card_text
    assert "项目群" in card_text


def test_help_card_mentions_worktree_wt_alias_and_separators():
    """Verify Worktree help text documents /wt, /worktree and separators."""
    SystemBuilder._build_help_card_cached.cache_clear()

    msg_type, card_json = SystemBuilder.build_help_card(
        session_idle_timeout=600,
        session_idle_warn_at_remaining=120,
        lock_undo_window_seconds=300,
    )

    assert msg_type == "interactive"
    card_text = json.dumps(json.loads(card_json), ensure_ascii=False)
    assert "/wt" in card_text
    assert "/worktree" in card_text
    assert "分隔符支持空格/Tab" in card_text


def test_project_status_card_includes_switch_and_group_jump_without_duplicate_path():
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    project.bound_chat_id = "oc_group_123"
    project.bound_chat_name = "P1-dev"

    msg_type, card_json = ProjectBuilder.build_project_status_report_card(project, "/tmp/p1")

    assert msg_type == "interactive"
    card = json.loads(card_json)
    buttons = _collect_buttons(card)
    assert any(
        b["text"]["content"] == UI_TEXT["project_btn_open_group"]
        and b["multi_url"]["url"].endswith("openChatId=oc_group_123")
        for b in buttons
    )
    assert any(
        b["text"]["content"] == UI_TEXT["project_board_btn_switch"]
        and b.get("behaviors", [{}])[0].get("value", {}).get("action") == "switch_project"
        for b in buttons
    )

    markdown_contents = [
        e.get("content", "")
        for e in card["body"]["elements"]
        if isinstance(e, dict) and e.get("tag") == "markdown"
    ]
    assert sum("/tmp/p1" in content for content in markdown_contents) == 1


def test_project_board_includes_group_jump_for_bound_project():
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    project.bound_chat_id = "oc_group_123"
    project.bound_chat_name = "P1-dev"

    msg_type, card_json = ProjectBuilder.build_status_board_card([project], current_project_id=None)

    assert msg_type == "interactive"
    card = json.loads(card_json)
    buttons = _collect_buttons(card)
    assert any(
        b["text"]["content"] == UI_TEXT["project_btn_open_group"]
        and "openChatId=oc_group_123" in b["multi_url"]["url"]
        for b in buttons
    )


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
    """Verify that DeepBuilder.build_info_card uses the banner for warnings."""
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    state = EngineCardState(
        title="Running",
        content="Thinking...",
        engine_name="Coco",
        warning_banner="Low credits warning"
    )

    msg_type, card_json = DeepBuilder.build_info_card(project, state)
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
            mode="Spec",
            task_id="task-ffffeeee1111",
            name="SpecRun",
            status="failed",
            info="error",
        ),
    ]

    content = CardBuilder.build_unified_status_content(entries, include_done=False, project_name="DemoProject")

    # Header should contain total count
    assert str(len(entries)) in content
    # Per-entry lines should include mode, name, info and mapped emojis
    assert "Deep" in content and "Spec" in content
    assert "DeepRun" in content and "SpecRun" in content
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


# ---------------------------------------------------------------------------
# Verify stop/stop_danger confirm dialog fields
# ---------------------------------------------------------------------------


class TestStopButtonConfirmDialogs:
    """Verify stop and stop_danger buttons include proper confirm dialogs."""

    def _make_executing_state(self):
        """Create an EngineCardState that is executing."""
        from src.card.builders.deep import DeepBuilder, EngineCardState
        return EngineCardState(
            is_executing=True,
            is_paused=False,
            content="x\n" * 60,
            compact=False,
            expanded=False,
            action_prefix="deep",
            engine_project_id="proj1",
            project_id="proj1",
        )

    def test_stop_danger_confirm_has_title_and_body(self):
        """stop_danger button JSON includes confirm.title.content and confirm.text.content."""
        from src.card.builders.deep import DeepBuilder
        state = self._make_executing_state()
        btn = DeepBuilder._create_button("stop_danger", state)
        assert "confirm" in btn
        assert "title" in btn["confirm"]
        assert "content" in btn["confirm"]["title"]
        assert "text" in btn["confirm"]
        assert "content" in btn["confirm"]["text"]
        # Verify it uses the danger body text
        from src.card.ui_text import UI_TEXT
        expected = UI_TEXT["card_btn_confirm_stop_danger_body"].format(engine_cmd="/deep")
        assert btn["confirm"]["text"]["content"] == expected

    def test_stop_normal_has_no_confirm(self):
        """Normal stop button should NOT have a confirm dialog (direct execution)."""
        from src.card.builders.deep import DeepBuilder
        state = self._make_executing_state()
        btn = DeepBuilder._create_button("stop", state)
        assert "confirm" not in btn


# ---------------------------------------------------------------------------
# timeout_display formatting (system help card)
# ---------------------------------------------------------------------------


class TestTimeoutDisplayFormatting:
    """Test timeout_display formatting logic in system help card."""

    @pytest.mark.parametrize("timeout_seconds,expected_substring", [
        (300, "5 分钟"),
        (600, "10 分钟"),
        (1800, "30 分钟"),
        (3600, "60 分钟"),
        (7200, "2 小时"),
        (10800, "3 小时"),
    ])
    def test_timeout_display_format(self, timeout_seconds, expected_substring):
        """Verify timeout display formatting for various second values."""
        import math

        timeout_minutes = max(1, math.ceil(timeout_seconds / 60))
        if timeout_minutes >= 120:
            hours = timeout_minutes // 60
            timeout_display = f"{hours} 小时" if timeout_seconds % 3600 == 0 else f"约 {hours} 小时"
        else:
            timeout_display = f"{timeout_minutes} 分钟" if timeout_seconds % 60 == 0 else f"约 {timeout_minutes} 分钟"

        assert timeout_display == expected_substring


# ---------------------------------------------------------------------------
# button_size parameter pass-through tests
# ---------------------------------------------------------------------------


class TestButtonSizePassthrough:
    """Verify button_size propagates from build_mode_buttons to final button dict."""

    @pytest.mark.parametrize("size", ["small", "medium", "large"])
    def test_button_size_in_mode_buttons(self, size):
        from src.card.shared import build_mode_buttons

        buttons = build_mode_buttons(mode=None, button_size=size)
        assert buttons, "Expected at least one button"
        for btn in buttons:
            assert btn["size"] == size, f"Expected size={size}, got {btn.get('size')}"


# ---------------------------------------------------------------------------
# stop button type config test
# ---------------------------------------------------------------------------


class TestStopButtonConfig:
    """Verify stop button uses danger type with confirm dialog, and stop_danger uses danger for force-stop."""

    def test_stop_button_type_is_danger(self):
        from src.card.buttons_config import BUTTON_CONFIG

        assert BUTTON_CONFIG["stop"]["type"] == "danger"

    def test_stop_button_has_confirm(self):
        from src.card.buttons_config import BUTTON_CONFIG

        assert "confirm" in BUTTON_CONFIG["stop"]
        assert BUTTON_CONFIG["stop"]["confirm"]["title"] == "确认停止"

    def test_stop_danger_button_type_is_danger(self):
        from src.card.buttons_config import BUTTON_CONFIG

        assert BUTTON_CONFIG["stop_danger"]["type"] == "danger"


# ---------------------------------------------------------------------------
# Review hardening tests (from review cycle feedback)
# ---------------------------------------------------------------------------


class TestDeepActionPrefixStrip:
    """Verify action_prefix edge cases with empty/whitespace strings."""

    def test_empty_string_uses_fallback(self):
        """action_prefix='' should use deep_error_fallback_no_prefix."""
        from src.card.ui_text import UI_TEXT

        state = EngineCardState(
            title="Error occurred",
            content="",
            compact=True,
            terminal_state="error",
            action_prefix="",
        )
        _, card_json = CardBuilder.build_info_card(None, state)
        card = json.loads(card_json)
        body_text = json.dumps(card, ensure_ascii=False)
        assert UI_TEXT["deep_error_fallback_no_prefix"] in body_text
        assert "发送 / " not in body_text  # no broken "/ " command

    def test_whitespace_string_uses_fallback(self):
        """action_prefix='  ' should use deep_error_fallback_no_prefix."""
        from src.card.ui_text import UI_TEXT

        state = EngineCardState(
            title="Error occurred",
            content="",
            compact=True,
            terminal_state="error",
            action_prefix="   ",
        )
        _, card_json = CardBuilder.build_info_card(None, state)
        card = json.loads(card_json)
        body_text = json.dumps(card, ensure_ascii=False)
        assert UI_TEXT["deep_error_fallback_no_prefix"] in body_text


class TestStopConfirmNoRollbackWording:
    """Verify stop confirm body does not contain alarming '回滚' text."""

    def test_danger_body_no_rollback(self):
        from src.card.ui_text import UI_TEXT

        assert "回滚" not in UI_TEXT["card_btn_confirm_stop_danger_body"]

    def test_danger_body_is_decision_oriented(self):
        """Confirm body should lead with risk statement and compare with normal stop."""
        from src.card.ui_text import UI_TEXT

        body = UI_TEXT["card_btn_confirm_stop_danger_body"]
        # Risk statement with comparison to normal stop
        assert "强制停止" in body
        assert "普通停止" in body
        # Must include engine_cmd placeholder for re-execution hint
        assert "{engine_cmd}" in body



class TestHelpCardTimeoutNotePosition:
    """Verify timeout_note is rendered in the bottom notation area, not between quick buttons and sections."""

    def test_timeout_note_after_sections(self):
        """timeout_note should appear after command sections, near tips (bottom)."""
        SystemBuilder._build_help_card_cached.cache_clear()
        _, card_json = SystemBuilder.build_help_card(
            session_idle_timeout=1800,
            session_idle_warn_at_remaining=180,
            lock_undo_window_seconds=300,
        )
        card = json.loads(card_json)
        body_elements = card["body"]["elements"]

        # Find positions of key elements
        timeout_note_idx = None
        tips_idx = None
        quick_entry_idx = None
        for i, el in enumerate(body_elements):
            content = el.get("content", "")
            if "⏰" in content:
                timeout_note_idx = i
            if "system_help_tips" in content or "/help" in content:
                # tips is a notation-size markdown at the bottom
                if el.get("text_size") == "notation" and "发送" in content:
                    tips_idx = i
            if "快速入口" in content or "system_help_quick_entry" in content:
                quick_entry_idx = i

        # timeout_note should exist and be near the end (after sections, near tips)
        assert timeout_note_idx is not None, "timeout_note not found in card elements"
        # It should NOT be directly after quick_entry (that was the old position)
        if quick_entry_idx is not None:
            # There should be at least several elements between quick_entry and timeout_note
            assert timeout_note_idx - quick_entry_idx > 3, (
                f"timeout_note (idx={timeout_note_idx}) is too close to quick_entry (idx={quick_entry_idx})"
            )


class TestBuildDeepCardDeprecation:
    """Verify build_deep_card alias emits DeprecationWarning."""

    def test_deep_builder_deprecation_warning(self):
        import warnings

        from src.card.builders.deep import DeepBuilder
        from src.card.models import EngineCardState

        state = EngineCardState(title="test", content="hello")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            DeepBuilder.build_deep_card(None, state)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "build_deep_card" in str(w[0].message)

    def test_card_builder_deprecation_warning(self):
        import warnings

        from src.card.builder import CardBuilder
        from src.card.models import EngineCardState

        state = EngineCardState(title="test", content="hello")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            CardBuilder.build_deep_card(None, state)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "build_deep_card" in str(w[0].message)
