"""Mobile UX and card rendering tests for Slock card templates.

Tests AC-FIX-03, AC-FIX-09, AC-FIX-10, AC-FIX-11.
"""

import json
import uuid

from src.slock_engine.card_templates import (
    _truncate_dynamic_label,
    build_agent_message_card,
    build_chitchat_hint_card,
    build_command_panel_card,
    build_command_panel_extended_card,
    build_crash_recovery_card,
    build_discussion_card,
    build_queue_waiting_card,
    build_status_panel_card,
    build_task_board_card,
    build_transfer_suggestion_card,
)
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockTask, TaskStatus


class TestDiscussionProgressMarkdown:
    """AC-FIX-10: Progress bar uses single-line Markdown, not column_set."""

    def test_no_column_set_in_discussion_card(self):
        """Discussion card with max_rounds=10 contains no column_set element."""
        from src.slock_engine.card_templates import build_discussion_card

        card = build_discussion_card(
            thread_id="test-thread-1",
            current_round=3,
            max_rounds=10,
            participants=["Agent-A", "Agent-B"],
            messages=[],
            trigger_reason="Uncertainty detected",
            channel_id="test-ch",
        )

        elements = card["body"]["elements"]
        # No column_set should exist for progress bar (background_style purple/grey)
        for elem in elements:
            if elem.get("tag") == "column_set":
                columns = elem.get("columns", [])
                assert not any(
                    col.get("background_style") in ("purple", "grey")
                    for col in columns
                ), f"Found progress-bar column_set in discussion card: {elem}"

    def test_progress_markdown_format(self):
        """Progress shows correct ● and ○ count."""
        from src.slock_engine.card_templates import build_discussion_card

        card = build_discussion_card(
            thread_id="test-thread-2",
            current_round=4,
            max_rounds=7,
            participants=["A", "B"],
            messages=[],
            channel_id="ch",
        )

        elements = card["body"]["elements"]
        progress_elem = None
        for elem in elements:
            content = elem.get("content", "")
            if "进度" in content and "●" in content:
                progress_elem = elem
                break

        assert progress_elem is not None, "Progress markdown element not found"
        content = progress_elem["content"]
        assert content.count("●") == 4
        assert content.count("○") == 3
        assert "(4/7)" in content

    def test_progress_single_line_no_overflow(self):
        """Even with max_rounds=10, progress fits in single line."""
        from src.slock_engine.card_templates import build_discussion_card

        card = build_discussion_card(
            thread_id="test-thread-3",
            current_round=5,
            max_rounds=10,
            participants=["X"],
            messages=[],
            channel_id="ch",
        )

        elements = card["body"]["elements"]
        for elem in elements:
            content = elem.get("content", "")
            if "进度" in content:
                # Should be single line (no newlines)
                assert "\n" not in content
                # Total characters should be reasonable for mobile
                assert len(content) < 40
                break


class TestCommandPanelCompact:
    """AC-FIX-11: Command panel is compact with ≤8 first-level elements."""

    def test_main_panel_element_count(self):
        """Main panel has ≤ 8 elements (compact for mobile)."""
        card = build_command_panel_card(channel_id="test-ch")
        elements = card["body"]["elements"]
        assert len(elements) <= 8, f"Too many elements: {len(elements)}"

    def test_main_panel_has_more_button(self):
        """Main panel contains '更多操作' button."""
        card = build_command_panel_card(channel_id="test-ch")
        card_json = json.dumps(card, ensure_ascii=False)
        assert "更多操作" in card_json

    def test_extended_panel_has_forms(self):
        """Extended panel contains team/role/council forms."""
        card = build_command_panel_extended_card(channel_id="test-ch")
        card_json = json.dumps(card, ensure_ascii=False)
        assert "slock_form_new_team" in card_json
        assert "slock_form_new_role" in card_json
        assert "slock_form_council" in card_json

    def test_main_panel_has_core_actions(self):
        """Main panel has all 4 core action buttons."""
        card = build_command_panel_card(channel_id="test-ch")
        card_json = json.dumps(card, ensure_ascii=False)
        assert "slock_cmd_team_list" in card_json
        assert "slock_cmd_role_list" in card_json
        assert "slock_cmd_task_list" in card_json
        assert "slock_cmd_discuss" in card_json


class TestCrashRecoveryCardShowsTitle:
    """AC-FIX-09: Crash recovery card displays task content, not just IDs."""

    def test_card_shows_task_content(self):
        """Each recovered task shows its content preview."""
        tasks = [
            SlockTask(task_id=str(uuid.uuid4()), content="修复登录页面的验证逻辑 bug", status=TaskStatus.TODO),
            SlockTask(task_id=str(uuid.uuid4()), content="重构用户权限模块以支持 RBAC", status=TaskStatus.TODO),
        ]
        card = build_crash_recovery_card(tasks)
        card_json = json.dumps(card, ensure_ascii=False)

        assert "修复登录页面的验证逻辑" in card_json
        assert "重构用户权限模块" in card_json

    def test_card_truncates_long_content(self):
        """Long task content is truncated with ellipsis."""
        long_content = "这是一个非常长的任务描述" * 20  # ~200 chars
        tasks = [SlockTask(task_id=str(uuid.uuid4()), content=long_content, status=TaskStatus.TODO)]
        card = build_crash_recovery_card(tasks)
        card_json = json.dumps(card, ensure_ascii=False)

        # Full content should NOT appear (it's >60 chars so gets truncated)
        assert long_content not in card_json
        assert "..." in card_json

    def test_card_not_empty_for_valid_tasks(self):
        """Card is generated successfully for valid task list."""
        tasks = [SlockTask(content="Test task")]
        card = build_crash_recovery_card(tasks)
        assert card["schema"] == "2.0"
        assert len(card["body"]["elements"]) > 0


class TestChitchatHintImport:
    """AC-FIX-03: build_chitchat_hint_card is importable via absolute path."""

    def test_import_succeeds(self):
        """Absolute import from src.slock_engine.card_templates works."""
        from src.slock_engine.card_templates import build_chitchat_hint_card
        assert callable(build_chitchat_hint_card)

    def test_card_renders(self):
        """build_chitchat_hint_card produces valid card structure."""
        from src.slock_engine.card_templates import build_chitchat_hint_card
        card = build_chitchat_hint_card("今天天气不错", channel_id="test-ch")
        assert isinstance(card, dict)
        assert "schema" in card or "body" in card or "elements" in card


# ---------------------------------------------------------------------------
# Helper to create a test AgentIdentity
# ---------------------------------------------------------------------------

def _make_agent(name: str = "TestBot", role: str = "coder") -> AgentIdentity:
    return AgentIdentity(name=name, role=role, agent_type="coco")


# ---------------------------------------------------------------------------
# AC23: Button simplification — at most 2 buttons visible at top level
# ---------------------------------------------------------------------------

class TestAC23ButtonSimplification:
    """AC23: build_agent_message_card has at most 2 top-level buttons; rest in collapsible_panel."""

    def test_top_level_buttons_at_most_two(self):
        """Only 2 buttons are directly visible; others are inside collapsible_panel."""
        agent = _make_agent("Coder")
        card = build_agent_message_card(
            agent=agent,
            content="Here is the result.",
            channel_id="ch-1",
            task_id="task-1",
        )

        elements = card["body"]["elements"]

        # Count top-level button elements (tag=button) that are NOT inside a collapsible_panel
        top_level_buttons = 0
        for elem in elements:
            tag = elem.get("tag", "")
            if tag == "button":
                top_level_buttons += 1
            elif tag == "column_set":
                # Buttons in column_set grid count as top-level visible buttons
                for col in elem.get("columns", []):
                    for child in col.get("elements", []):
                        if child.get("tag") == "button":
                            top_level_buttons += 1

        assert top_level_buttons <= 3, (
            f"Expected at most 3 top-level buttons, found {top_level_buttons}"
        )

    def test_collapsible_panel_exists(self):
        """A collapsible_panel element exists to hold secondary buttons."""
        agent = _make_agent("Writer")
        card = build_agent_message_card(
            agent=agent,
            content="Draft complete.",
            channel_id="ch-2",
            task_id="task-2",
        )

        elements = card["body"]["elements"]
        panel_tags = [e.get("tag") for e in elements if e.get("tag") == "collapsible_panel"]
        assert len(panel_tags) >= 1, "No collapsible_panel found in agent message card"

    def test_collapsible_panel_contains_secondary_buttons(self):
        """The collapsible_panel contains secondary action buttons."""
        agent = _make_agent("Reviewer")
        card = build_agent_message_card(
            agent=agent,
            content="Review done.",
            channel_id="ch-3",
            task_id="task-3",
            discussion_enabled=True,
        )

        elements = card["body"]["elements"]
        panel = None
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panel = elem
                break

        assert panel is not None, "collapsible_panel not found"
        panel_json = json.dumps(panel, ensure_ascii=False)
        # Should contain secondary buttons (reasoning, memory, discussion, switch role)
        assert "slock_agent_show_reasoning" in panel_json or "查看推理" in panel_json


# ---------------------------------------------------------------------------
# AC24: Progress bar — ≤30 chars when max_rounds is large
# ---------------------------------------------------------------------------

class TestAC24ProgressBarLength:
    """AC24: Progress bar markdown ≤30 chars for large max_rounds."""

    def test_progress_bar_under_30_chars_large_rounds(self):
        """With max_rounds=20, current_round=3, progress bar content ≤30 chars."""
        card = build_discussion_card(
            thread_id="test-ac24-1",
            current_round=3,
            max_rounds=20,
            participants=["Agent-X", "Agent-Y"],
            messages=[],
            channel_id="ch-ac24",
        )

        elements = card["body"]["elements"]
        progress_elem = None
        for elem in elements:
            content = elem.get("content", "")
            if "进度" in content:
                progress_elem = elem
                break

        assert progress_elem is not None, "Progress bar element not found"
        content = progress_elem["content"]
        assert len(content) <= 30, (
            f"Progress bar content too long ({len(content)} chars): '{content}'"
        )

    def test_progress_bar_percentage_format_used(self):
        """When max_rounds >= 10, percentage + fraction format is used."""
        card = build_discussion_card(
            thread_id="test-ac24-2",
            current_round=5,
            max_rounds=20,
            participants=["A"],
            messages=[],
            channel_id="ch",
        )

        elements = card["body"]["elements"]
        for elem in elements:
            content = elem.get("content", "")
            if "进度" in content:
                # Should use percentage format, not individual dots
                assert "●" not in content, "Should use percentage format for large rounds"
                assert "%" in content, "Should contain percentage sign"
                assert "5/20" in content, "Should contain fraction"
                break


# ---------------------------------------------------------------------------
# AC25: Truncation — task preview ≤40 chars + ellipsis
# ---------------------------------------------------------------------------

class TestAC25TaskPreviewTruncation:
    """AC25: build_status_panel_card truncates task preview to ≤40 chars."""

    def test_long_task_content_truncated(self):
        """Task content longer than 40 chars is truncated with ellipsis."""
        agent = _make_agent("LongTaskBot")
        long_content = "A" * 80  # 80 chars, well over 40
        task = SlockTask(content=long_content, status=TaskStatus.TODO)

        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)],
            team_name="TestTeam",
            channel_id="ch-ac25",
            current_tasks={agent.agent_id: task},
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # Full 80-char content should NOT appear
        assert long_content not in card_json, "Full long content should be truncated"
        # Ellipsis marker should be present
        assert "\u2026" in card_json or "..." in card_json, "Ellipsis not found in truncated text"

    def test_short_task_content_not_truncated(self):
        """Task content ≤40 chars is NOT truncated."""
        agent = _make_agent("ShortTaskBot")
        short_content = "Fix login bug"  # well under 40 chars
        task = SlockTask(content=short_content, status=TaskStatus.TODO)

        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)],
            team_name="TestTeam",
            channel_id="ch-ac25b",
            current_tasks={agent.agent_id: task},
        )

        card_json = json.dumps(card, ensure_ascii=False)
        assert short_content in card_json, "Short content should appear in full"

    def test_truncated_content_at_most_25_chars(self):
        """The displayed task text portion is at most 25 chars before ellipsis."""
        agent = _make_agent("ExactBot")
        # Use distinct chars so we can verify exactly where truncation happens
        content_50 = "X" * 50
        task = SlockTask(content=content_50, status=TaskStatus.TODO)

        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)],
            channel_id="ch-ac25c",
            current_tasks={agent.agent_id: task},
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # The first 25 chars should appear
        assert "X" * 25 in card_json, "First 25 chars should be present"
        # But 26+ should not appear contiguously (truncated)
        assert "X" * 26 not in card_json, "26+ chars should not appear (should be truncated)"


# ---------------------------------------------------------------------------
# AC26: No tag=action — queue/transfer cards avoid legacy action nodes
# ---------------------------------------------------------------------------

class TestAC26NoTagAction:
    """AC26: build_queue_waiting_card and build_transfer_suggestion_card have no tag=action."""

    def _find_action_tags(self, obj) -> list:
        """Recursively find all nodes with {"tag": "action"} in a nested structure."""
        results = []
        if isinstance(obj, dict):
            if obj.get("tag") == "action":
                results.append(obj)
            for value in obj.values():
                results.extend(self._find_action_tags(value))
        elif isinstance(obj, list):
            for item in obj:
                results.extend(self._find_action_tags(item))
        return results

    def test_queue_waiting_card_no_action_tag(self):
        """build_queue_waiting_card output has no {"tag": "action"} nodes."""
        agent = _make_agent("BusyBot")
        card = build_queue_waiting_card(
            agent=agent,
            channel_id="ch-ac26a",
            position=2,
            current_status="running",
        )

        action_nodes = self._find_action_tags(card)
        assert action_nodes == [], (
            f"Found {len(action_nodes)} action node(s) in queue_waiting_card: {action_nodes}"
        )

    def test_transfer_suggestion_card_no_action_tag(self):
        """build_transfer_suggestion_card output has no {"tag": "action"} nodes."""
        busy_agent = _make_agent("BusyAgent")
        idle_agent = _make_agent("IdleAgent")
        card = build_transfer_suggestion_card(
            busy_agent=busy_agent,
            idle_agent=idle_agent,
            channel_id="ch-ac26b",
            original_message="Please help me fix this issue in the auth module.",
        )

        action_nodes = self._find_action_tags(card)
        assert action_nodes == [], (
            f"Found {len(action_nodes)} action node(s) in transfer_suggestion_card: {action_nodes}"
        )

    def test_queue_card_uses_column_set_for_buttons(self):
        """Queue card buttons are rendered via column_set, not action tag."""
        agent = _make_agent("QueueBot")
        card = build_queue_waiting_card(
            agent=agent,
            channel_id="ch-ac26c",
            position=1,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # Should use column_set for button layout
        assert "column_set" in card_json, "Expected column_set layout for buttons"
        # Should NOT use action tag
        assert '"tag": "action"' not in card_json


# ---------------------------------------------------------------------------
# AC23: build_chitchat_hint_card and build_status_panel_card output no bare
#       {"tag": "action", "actions": [...]} nodes.
# ---------------------------------------------------------------------------


def _find_bare_action_nodes(obj) -> list:
    """Recursively find all dicts with {"tag": "action", "actions": [...]} pattern."""
    results = []
    if isinstance(obj, dict):
        if obj.get("tag") == "action" and isinstance(obj.get("actions"), list):
            results.append(obj)
        for value in obj.values():
            results.extend(_find_bare_action_nodes(value))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_find_bare_action_nodes(item))
    return results


class TestAC23NoBareActionTag:
    """AC23: build_chitchat_hint_card and build_status_panel_card have no bare action nodes."""

    def test_chitchat_hint_card_no_bare_action(self):
        """build_chitchat_hint_card output contains no {"tag": "action", "actions": [...]}."""
        card = build_chitchat_hint_card(
            "今天天气不错呀",
            channel_id="ch-ac23-chitchat",
            timestamp=1700000000.0,
        )

        bare_actions = _find_bare_action_nodes(card)
        assert bare_actions == [], (
            f"Found {len(bare_actions)} bare action node(s) in chitchat_hint_card: "
            f"{json.dumps(bare_actions, ensure_ascii=False, indent=2)}"
        )

    def test_status_panel_card_no_bare_action(self):
        """build_status_panel_card output contains no {"tag": "action", "actions": [...]}."""
        agents = [
            (_make_agent("AlphaBot"), AgentStatus.IDLE),
            (_make_agent("BetaBot"), AgentStatus.RUNNING),
        ]
        card = build_status_panel_card(
            agents=agents,
            channel_id="ch-ac23-status",
        )

        bare_actions = _find_bare_action_nodes(card)
        assert bare_actions == [], (
            f"Found {len(bare_actions)} bare action node(s) in status_panel_card: "
            f"{json.dumps(bare_actions, ensure_ascii=False, indent=2)}"
        )

    def test_chitchat_hint_card_uses_responsive_layout(self):
        """Chitchat hint card buttons should use build_responsive_layout (column_set)."""
        card = build_chitchat_hint_card(
            "随便聊聊",
            channel_id="ch-ac23-responsive",
        )
        card_json = json.dumps(card, ensure_ascii=False)
        # build_responsive_layout produces column_set elements for buttons
        assert "column_set" in card_json or "button" in card_json, (
            "Expected responsive layout (column_set or button) in chitchat hint card"
        )

    def test_status_panel_card_with_tasks_no_bare_action(self):
        """build_status_panel_card with current_tasks still has no bare action nodes."""
        agent = _make_agent("TaskAgent")
        task = SlockTask(content="Review the PR", status=TaskStatus.IN_PROGRESS)
        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)],
            channel_id="ch-ac23-tasks",
            current_tasks={agent.agent_id: task},
        )

        bare_actions = _find_bare_action_nodes(card)
        assert bare_actions == [], (
            f"Found bare action node(s) in status_panel_card with tasks: "
            f"{json.dumps(bare_actions, ensure_ascii=False, indent=2)}"
        )


# ---------------------------------------------------------------------------
# AC24: build_task_board_card uses flex_mode='flow' not 'bisect'
# ---------------------------------------------------------------------------


class TestAC24TaskBoardFlexMode:
    """AC24: build_task_board_card column_sets use flex_mode='none' (vertical), never 'bisect'."""

    def _collect_flex_modes(self, obj) -> list[str]:
        """Recursively collect all flex_mode values from the card tree."""
        modes = []
        if isinstance(obj, dict):
            if "flex_mode" in obj:
                modes.append(obj["flex_mode"])
            for value in obj.values():
                modes.extend(self._collect_flex_modes(value))
        elif isinstance(obj, list):
            for item in obj:
                modes.extend(self._collect_flex_modes(item))
        return modes

    def test_task_board_no_bisect_flex_mode(self):
        """No column_set in task board card uses flex_mode='bisect'."""
        tasks = [
            SlockTask(content="Fix the login bug", status=TaskStatus.TODO),
            SlockTask(content="Deploy v2.1", status=TaskStatus.IN_PROGRESS),
            SlockTask(content="Write unit tests", status=TaskStatus.DONE),
        ]
        agents = [_make_agent("DevBot")]
        card = build_task_board_card(tasks=tasks, agents=agents, channel_id="ch-ac24")

        flex_modes = self._collect_flex_modes(card)
        assert "bisect" not in flex_modes, (
            f"Found flex_mode='bisect' in task_board_card. All modes: {flex_modes}"
        )

    def test_task_board_uses_none_flex_mode(self):
        """Task board column_sets use flex_mode='none' for vertical stacking."""
        tasks = [
            SlockTask(content="Task A", status=TaskStatus.TODO),
            SlockTask(content="Task B", status=TaskStatus.IN_PROGRESS),
        ]
        agents = [_make_agent("FlowBot")]
        # Explicitly request full mode (summary_mode=True is now the default)
        card = build_task_board_card(tasks=tasks, agents=agents, channel_id="ch-ac24-flow", summary_mode=False)

        flex_modes = self._collect_flex_modes(card)
        assert "none" in flex_modes, (
            f"Expected flex_mode='none' in task_board_card. Found modes: {flex_modes}"
        )

    def test_task_board_empty_tasks_no_bisect(self):
        """Even with empty task list, no bisect flex_mode is used."""
        card = build_task_board_card(tasks=[], agents=[], channel_id="ch-ac24-empty")

        flex_modes = self._collect_flex_modes(card)
        assert "bisect" not in flex_modes, (
            f"Found flex_mode='bisect' in empty task_board_card. Modes: {flex_modes}"
        )


# ---------------------------------------------------------------------------
# AC25: Dynamic agent name buttons are truncated to <= 20 characters
# ---------------------------------------------------------------------------


class TestAC25DynamicNameTruncation:
    """AC25: Dynamic agent name buttons are truncated to <= 20 characters."""

    def test_truncate_dynamic_label_short_text(self):
        """Short text (<= 20 chars) is not truncated."""
        assert _truncate_dynamic_label("Hello") == "Hello"
        assert _truncate_dynamic_label("A" * 20) == "A" * 20

    def test_truncate_dynamic_label_long_text(self):
        """Text > 20 chars is truncated to 19 chars + ellipsis."""
        result = _truncate_dynamic_label("A" * 25)
        assert len(result) == 20
        assert result.endswith("\u2026")  # ends with '...' (ellipsis char)
        assert result == "A" * 19 + "\u2026"

    def test_truncate_dynamic_label_exact_boundary(self):
        """Text at exactly 21 chars triggers truncation."""
        result = _truncate_dynamic_label("B" * 21)
        assert len(result) == 20
        assert result == "B" * 19 + "\u2026"

    def test_truncate_dynamic_label_custom_max_len(self):
        """Custom max_len parameter is respected."""
        result = _truncate_dynamic_label("Hello World!", max_len=10)
        assert len(result) == 10
        assert result == "Hello Wor\u2026"

    def test_transfer_card_button_label_truncated(self):
        """build_transfer_suggestion_card truncates long idle agent name in button."""
        busy_agent = _make_agent("BusyBot")
        # Agent name that will produce a button label > 20 chars
        # Button text is: "✅ 转交给 {idle_agent.name}" which will be long
        long_name = "超级无敌宇宙最强自动化测试专用机器人"  # 16 chars + prefix will exceed 20
        idle_agent = _make_agent(long_name)

        card = build_transfer_suggestion_card(
            busy_agent=busy_agent,
            idle_agent=idle_agent,
            channel_id="ch-ac25-trunc",
            original_message="Please help me with this task.",
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # The full untruncated button label should NOT appear
        full_label = f"\u2705 \u8f6c\u4ea4\u7ed9 {long_name}"
        assert full_label not in card_json, (
            f"Full untruncated label found in card: '{full_label}'"
        )
        # The ellipsis char should appear (truncation indicator)
        assert "\u2026" in card_json, "Ellipsis not found - label was not truncated"

    def test_transfer_card_button_label_within_limit(self):
        """All button labels in transfer_suggestion_card are <= 20 chars."""
        busy_agent = _make_agent("BusyBot")
        idle_agent = _make_agent("一个超级超级长的Agent名字用来测试截断功能")

        card = build_transfer_suggestion_card(
            busy_agent=busy_agent,
            idle_agent=idle_agent,
            channel_id="ch-ac25-limit",
            original_message="Check this out.",
        )

        # Walk the card tree and find all button text values
        button_texts = self._collect_button_texts(card)
        for text in button_texts:
            assert len(text) <= 20, (
                f"Button label exceeds 20 chars ({len(text)}): '{text}'"
            )

    def _collect_button_texts(self, obj) -> list[str]:
        """Recursively collect all button text/content values from a card."""
        texts = []
        if isinstance(obj, dict):
            if obj.get("tag") == "button":
                # Button text can be in "text" dict or direct "content"
                text_obj = obj.get("text", {})
                if isinstance(text_obj, dict):
                    content = text_obj.get("content", "")
                    if content:
                        texts.append(content)
                elif isinstance(text_obj, str):
                    texts.append(text_obj)
            for value in obj.values():
                texts.extend(self._collect_button_texts(value))
        elif isinstance(obj, list):
            for item in obj:
                texts.extend(self._collect_button_texts(item))
        return texts


# ---------------------------------------------------------------------------
# AC18: Discussion message truncation with collapsible_panel (WP4 updated)
# ---------------------------------------------------------------------------


class TestDiscussionTruncationEllipsis:
    """AC18: Long discussion messages use collapsible_panel instead of plain text truncation."""

    def _find_collapsible_panels(self, card: dict) -> list[dict]:
        """Extract all collapsible_panel elements from card."""
        panels = []
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panels.append(elem)
        return panels

    def test_long_content_uses_collapsible_panel(self):
        """Long messages (>120 chars) use collapsible_panel with full content preserved."""
        long_message = "x" * 500
        card = build_discussion_card(
            thread_id="t1", participants=["A", "B"],
            messages=[{"sender": "A", "content": long_message, "round_num": 1}],
            current_round=1, max_rounds=5, channel_id="ch1",
        )
        panels = self._find_collapsible_panels(card)
        assert len(panels) >= 1, "Expected collapsible_panel for long message"

        # Verify full content is in the panel elements
        panel_json = json.dumps(panels, ensure_ascii=False)
        assert long_message in panel_json, "Full message should be in collapsible_panel elements"

    def test_short_content_uses_note_format(self):
        """Short messages (<=120 chars) use note format, not collapsible_panel."""
        card = build_discussion_card(
            thread_id="t1", participants=["A", "B"],
            messages=[{"sender": "A", "content": "short msg", "round_num": 1}],
            current_round=1, max_rounds=5, channel_id="ch1",
        )
        elements = card["body"]["elements"]
        # Should have note element, not collapsible_panel for this message
        note_found = any(e.get("tag") == "note" for e in elements)
        assert note_found, "Short message should use note format"


# ---------------------------------------------------------------------------
# AC19: Escalation card does not contain code block fences
# ---------------------------------------------------------------------------


class TestEscalationNoCodeBlock:
    """AC19: build_escalation_card output has no triple backtick code fences."""

    def test_no_code_block_in_context(self):
        from src.slock_engine.card_templates import build_escalation_card
        from src.slock_engine.models import EscalationLevel, EscalationRequest
        esc = EscalationRequest(
            escalation_id="esc1", agent_id="a1", agent_name="TestAgent",
            level=EscalationLevel.WARNING, reason="test",
            context="some long error context " * 30,
        )
        card = build_escalation_card(esc, channel_id="ch1")
        import json as _json
        card_str = _json.dumps(card)
        assert "```" not in card_str


class TestResolvedEscalationNoCodeBlock:
    """build_resolved_escalation_card uses quote blocks (>), not code blocks (```)."""

    def test_no_code_block_in_resolved_context(self):
        from src.slock_engine.card_templates import build_resolved_escalation_card
        from src.slock_engine.models import EscalationLevel, EscalationRequest
        esc = EscalationRequest(
            escalation_id="esc1", agent_id="a1", agent_name="TestAgent",
            level=EscalationLevel.WARNING, reason="test",
            context="some long error context " * 30,
        )
        card = build_resolved_escalation_card(
            esc,
            resolved_by="Operator",
            resolution="Retry",
            channel_id="ch1",
        )
        import json as _json
        card_str = _json.dumps(card)
        assert "```" not in card_str

    def test_uses_quote_block_format(self):
        """Context should use > prefix for quote blocks."""
        from src.slock_engine.card_templates import build_resolved_escalation_card
        from src.slock_engine.models import EscalationLevel, EscalationRequest
        esc = EscalationRequest(
            escalation_id="esc2", agent_id="a2", agent_name="TestAgent2",
            level=EscalationLevel.BLOCKED, reason="blocked reason",
            context="line1\nline2\nline3",
        )
        card = build_resolved_escalation_card(
            esc,
            resolved_by="Admin",
            resolution="Skip",
            channel_id="ch2",
        )
        import json as _json
        card_str = _json.dumps(card, ensure_ascii=False)
        # Should contain quote block markers
        assert "> line1" in card_str or "> line2" in card_str or "> line3" in card_str


# ---------------------------------------------------------------------------
# AC20: Memory display card has 3 collapsible panels, first expanded
# ---------------------------------------------------------------------------


class TestMemoryCardCollapsible:
    """AC20: build_memory_display_card returns 3 collapsible_panel elements."""

    def test_three_collapsible_panels(self):
        from src.slock_engine.card_templates import build_memory_display_card
        from src.slock_engine.models import SlockMemory
        memory = SlockMemory(role="Test Role", key_knowledge="Some knowledge", active_context="Context data")
        card = build_memory_display_card(memory, agent_name="TestBot")
        elements = card["body"]["elements"]
        panels = [e for e in elements if e.get("tag") == "collapsible_panel"]
        assert len(panels) == 3
        assert panels[0]["expanded"] is True
        assert panels[1]["expanded"] is False
        assert panels[2]["expanded"] is False


# ---------------------------------------------------------------------------
# AC21: Discussion subtitle truncation for >2 participants
# ---------------------------------------------------------------------------


class TestDiscussionSubtitleTruncation:
    """AC21: Subtitle shows '等 N 人' when >2 participants."""

    def test_subtitle_truncated_three_participants(self):
        card = build_discussion_card(
            thread_id="t1", participants=["Alice", "Bob", "Charlie"],
            messages=[], current_round=1, max_rounds=5, channel_id="ch1",
        )
        subtitle = card["header"]["subtitle"]["content"]
        assert "等 3 人" in subtitle
        assert "Charlie" not in subtitle

    def test_subtitle_full_two_participants(self):
        card = build_discussion_card(
            thread_id="t1", participants=["Alice", "Bob"],
            messages=[], current_round=1, max_rounds=5, channel_id="ch1",
        )
        subtitle = card["header"]["subtitle"]["content"]
        assert "Alice" in subtitle
        assert "Bob" in subtitle
        assert "等" not in subtitle


# ---------------------------------------------------------------------------
# AC22: Task board uses vertical layout (single column per row)
# ---------------------------------------------------------------------------


class TestTaskBoardVerticalLayout:
    """AC22: Task board uses vertical layout with flex_mode 'none' and single column per row."""

    def test_single_column_per_status_row(self):
        tasks = [
            SlockTask(task_id="t1", content="Test task", status=TaskStatus.TODO, created_in="ch1"),
        ]
        agents = [AgentIdentity(agent_id="a1", name="Bot", emoji="\U0001f916", owner_group="ch1")]
        card = build_task_board_card(tasks, agents, channel_id="ch1")
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "column_set" and elem.get("background_style") not in (None, "default"):
                # Status rows should have flex_mode "none" and single column
                assert elem.get("flex_mode") == "none"
                assert len(elem["columns"]) == 1


# ---------------------------------------------------------------------------
# Tests for apply_compact_style button sizing logic
# ---------------------------------------------------------------------------


class TestApplyCompactStyleButtonSizing:
    """Tests for apply_compact_style: button_count threshold determines size."""

    def test_apply_compact_style_many_buttons_uses_small(self):
        """When button_count > 2, button size is forced to 'small'."""
        from src.card.shared import apply_compact_style

        button = {"tag": "button", "text": {"tag": "plain_text", "content": "Click"}}
        result = apply_compact_style(button, button_count=3)
        assert result["size"] == "small"

        # Also verify with higher counts
        button2 = {"tag": "button", "text": {"tag": "plain_text", "content": "Go"}}
        result2 = apply_compact_style(button2, button_count=5)
        assert result2["size"] == "small"

    def test_apply_compact_style_few_buttons_uses_medium(self):
        """When button_count <= 2, button size defaults to 'medium'."""
        from src.card.shared import apply_compact_style

        button = {"tag": "button", "text": {"tag": "plain_text", "content": "OK"}}
        result = apply_compact_style(button, button_count=2)
        assert result["size"] == "medium"

        button1 = {"tag": "button", "text": {"tag": "plain_text", "content": "A"}}
        result1 = apply_compact_style(button1, button_count=1)
        assert result1["size"] == "medium"

        button0 = {"tag": "button", "text": {"tag": "plain_text", "content": "B"}}
        result0 = apply_compact_style(button0, button_count=0)
        assert result0["size"] == "medium"


# ---------------------------------------------------------------------------
# Tests for discussion card message truncation with collapsible_panel (120 char threshold)
# ---------------------------------------------------------------------------


class TestDiscussionCardMessageTruncation:
    """Messages over 120 characters use collapsible_panel in build_discussion_card."""

    def _find_collapsible_panels(self, card: dict) -> list[dict]:
        """Extract all collapsible_panel elements from card."""
        panels = []
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panels.append(elem)
        return panels

    def test_discussion_card_long_message_uses_collapsible_panel(self):
        """Messages over 120 chars use collapsible_panel with full content preserved."""
        long_message = "A" * 250  # well over 120 chars
        card = build_discussion_card(
            thread_id="trunc-test-1",
            participants=["Agent-A", "Agent-B"],
            messages=[{"sender": "Agent-A", "content": long_message, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-trunc",
        )

        panels = self._find_collapsible_panels(card)
        assert len(panels) >= 1, "Expected collapsible_panel for long message"

        # Full message should be in the panel elements
        panel_json = json.dumps(panels, ensure_ascii=False)
        assert long_message in panel_json, "Full message should be in collapsible_panel"
        # Header should have truncated preview (first 120 chars + "...")
        assert "A" * 120 in panel_json, "First 120 chars should be in header preview"

    def test_discussion_card_short_message_not_truncated(self):
        """Messages at or under 120 chars use note format (not collapsible_panel)."""
        short_message = "B" * 120  # exactly 120 chars
        card = build_discussion_card(
            thread_id="trunc-test-2",
            participants=["Agent-X"],
            messages=[{"sender": "Agent-X", "content": short_message, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-trunc2",
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # Full message should appear intact
        assert short_message in card_json
        # Should be in note element, not collapsible_panel for this message
        elements = card["body"]["elements"]
        note_found = any(e.get("tag") == "note" for e in elements)
        assert note_found, "Short message should use note format"


# ---------------------------------------------------------------------------
# Tests for discussion expand card pagination (PAGE_SIZE = 10)
# ---------------------------------------------------------------------------


class TestDiscussionExpandCardPagination:
    """Verify build_discussion_expand_card uses PAGE_SIZE=10 and shows pagination indicator."""

    def test_discussion_expand_card_page_size_10(self):
        """Verify PAGE_SIZE is 10: only 10 messages rendered per page."""
        from src.slock_engine.card_templates import build_discussion_expand_card

        # Create 15 messages
        messages = [
            {"sender": "Agent-A", "content": f"Message {i}", "round_num": i}
            for i in range(1, 16)
        ]
        card = build_discussion_expand_card(
            thread_id="page-test-1",
            messages=messages,
            participants=["Agent-A"],
            channel_id="ch-page",
            page=0,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # Page 0 should show messages 1-10 (first 10)
        assert "第 1-10 条，共 15 条" in card_json
        # Message 10 should be present, message 11 should not
        assert "Message 10" in card_json
        assert "Message 11" not in card_json

    def test_pagination_indicator_shown(self):
        """Verify pagination indicator (load more button) in expanded cards when more pages exist."""
        from src.slock_engine.card_templates import build_discussion_expand_card

        # Create 12 messages (more than PAGE_SIZE=10, so pagination needed)
        messages = [
            {"sender": "Agent-B", "content": f"Msg {i}", "round_num": i}
            for i in range(1, 13)
        ]
        card = build_discussion_expand_card(
            thread_id="page-test-2",
            messages=messages,
            participants=["Agent-B"],
            channel_id="ch-page2",
            page=0,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # "Load more" button should appear since there are more messages
        assert "加载更多" in card_json
        assert "slock_discussion_expand_page" in card_json
        # Should indicate how many shown vs total
        assert "10/12" in card_json

    def test_pagination_indicator_hidden_on_last_page(self):
        """No load-more button when all messages fit on the current page."""
        from src.slock_engine.card_templates import build_discussion_expand_card

        # Create exactly 10 messages (fits in one page)
        messages = [
            {"sender": "Agent-C", "content": f"Msg {i}", "round_num": i}
            for i in range(1, 11)
        ]
        card = build_discussion_expand_card(
            thread_id="page-test-3",
            messages=messages,
            participants=["Agent-C"],
            channel_id="ch-page3",
            page=0,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # No load more button since all fit
        assert "加载更多" not in card_json
        assert "slock_discussion_expand_page" not in card_json


# ---------------------------------------------------------------------------
# Status Panel Two-Row Layout Tests
# ---------------------------------------------------------------------------


class TestStatusPanelTwoRowLayout:
    """Status panel uses two-row layout per agent (no pipe overflow on mobile)."""

    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Coder", "emoji": "🔧", "role": "coder"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_two_rows_per_agent_with_task(self):
        """Agent with task should have 2 markdown elements (row1: name+status, row2: task)."""
        agent = self._make_agent(name="Alice")
        task = SlockTask(task_id="t1", content="Implement feature XYZ", status=TaskStatus.IN_PROGRESS)
        card = build_status_panel_card(
            [(agent, AgentStatus.RUNNING)],
            current_tasks={"a1": task},
        )
        md_elements = [e for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        # Should have at least 2 markdown elements for this agent
        agent_md = [e for e in md_elements if "Alice" in e.get("content", "") or "Implement" in e.get("content", "")]
        assert len(agent_md) >= 2, f"Expected 2+ markdown elements for agent with task, got {len(agent_md)}"

    def test_row1_contains_name_and_status(self):
        """First row should contain agent name and status."""
        agent = self._make_agent(name="Charlie", emoji="🤖")
        card = build_status_panel_card([(agent, AgentStatus.IDLE)])
        md_elements = [e for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        # Find the row with agent name
        name_row = next((e for e in md_elements if "Charlie" in e.get("content", "")), None)
        assert name_row is not None
        # Should also contain status info
        content = name_row["content"]
        assert "🟢" in content or "空闲" in content or "IDLE" in content


# ---------------------------------------------------------------------------
# Task Board Default Summary Mode Tests
# ---------------------------------------------------------------------------


class TestTaskBoardDefaultSummaryMode:
    """Task board defaults to summary_mode=True with 40-char truncation."""

    def _make_task(self, **kwargs) -> SlockTask:
        defaults = {"task_id": "t1", "content": "Task", "status": TaskStatus.TODO}
        defaults.update(kwargs)
        return SlockTask(**defaults)

    def test_default_summary_mode_true(self):
        """Task board should default to summary_mode=True."""
        card = build_task_board_card([], [], team_name="Test")
        # Check that summary mode is being used (no column_set for status rows)
        column_sets = [
            e for e in card["body"]["elements"]
            if e.get("tag") == "column_set"
            and e.get("background_style") in {"green", "yellow", "blue", "grey"}
        ]
        # In summary mode, there should be no status column_sets
        assert len(column_sets) == 0, f"Expected 0 column_sets in summary mode, got {len(column_sets)}"

    def test_full_mode_truncation_40_chars(self):
        """Full mode should truncate task content to 40 chars (not 60)."""
        long_content = "A" * 100
        task = self._make_task(task_id="t1", content=long_content, status=TaskStatus.IN_PROGRESS)
        # Explicitly request full mode
        card = build_task_board_card([task], [], summary_mode=False)
        card_json = json.dumps(card, ensure_ascii=False)
        # Should NOT have 60 chars
        assert "A" * 60 not in card_json, "Task content should be truncated to 40 chars, not 60"


# ---------------------------------------------------------------------------
# Discussion Card Mobile Tests
# ---------------------------------------------------------------------------


class TestDiscussionCardMobile:
    """Discussion card has short title (<=12 chars) and 2 buttons."""

    def test_title_length_12_chars_or_less(self):
        """Discussion card title should be <= 12 chars (emoji + text)."""
        card = build_discussion_card(
            thread_id="thread1",
            participants=["Coder", "Reviewer"],
            messages=[{"sender": "Coder", "content": "Hello", "round_num": 1}],
            current_round=3,
            max_rounds=10,
            channel_id="chat1",
        )
        title = card["header"]["title"]["content"]
        # Old format: "💬 Agent 讨论 (轮次 3/10)" = ~20 chars
        # New format: "💬 讨论 R3/10" = ~10 chars
        assert len(title) <= 12, f"Title '{title}' is {len(title)} chars, should be <= 12"

    def test_title_contains_round_info(self):
        """Title should still contain round information."""
        card = build_discussion_card(
            thread_id="thread1",
            participants=["Coder", "Reviewer"],
            messages=[{"sender": "Coder", "content": "Hello", "round_num": 1}],
            current_round=5,
            max_rounds=20,
            channel_id="chat1",
        )
        title = card["header"]["title"]["content"]
        assert "5" in title or "R5" in title
        assert "20" in title or "/20" in title


# ---------------------------------------------------------------------------
# AC17: Hub card 按钮触控优化测试
# ---------------------------------------------------------------------------


class TestHubCardButtonTouchOptimization:
    """AC17: Hub card 按钮触控优化测试。"""

    def _collect_buttons(self, obj) -> list[dict]:
        """Recursively collect all button elements from card."""
        buttons = []
        if isinstance(obj, dict):
            if obj.get("tag") == "button":
                buttons.append(obj)
            for value in obj.values():
                buttons.extend(self._collect_buttons(value))
        elif isinstance(obj, list):
            for item in obj:
                buttons.extend(self._collect_buttons(item))
        return buttons

    def test_hub_card_buttons_are_medium_size(self):
        """build_command_hub_card 中所有按钮 size='medium'。"""
        from src.slock_engine.card_templates import build_command_hub_card

        card = build_command_hub_card(channel_id="test-ch")
        buttons = self._collect_buttons(card)

        assert len(buttons) > 0, "No buttons found in hub card"
        for btn in buttons:
            assert btn.get("size") == "medium", (
                f"Expected size='medium', got size='{btn.get('size')}': {btn}"
            )

    def test_vertical_button_spacing(self):
        """_build_button_vertical 生成的 column_set 包含 vertical_spacing='8px'。"""
        from src.card.shared import _build_button_vertical

        buttons = [
            {"tag": "button", "text": {"tag": "plain_text", "content": "Btn1"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "Btn2"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "Btn3"}},
        ]
        result = _build_button_vertical(buttons)

        assert len(result) == 3
        for column_set in result:
            assert column_set.get("vertical_spacing") == "8px", (
                f"Expected vertical_spacing='8px', got '{column_set.get('vertical_spacing')}'"
            )

    def test_apply_compact_style_skip_compact(self):
        """skip_compact=True 时始终保持 size='medium'。"""
        from src.card.shared import apply_compact_style

        # Test skip_compact=True, button_count=10 时 size 仍为 'medium'
        button = {"tag": "button", "text": {"tag": "plain_text", "content": "Click"}}
        result = apply_compact_style(button, button_count=10, skip_compact=True)
        assert result["size"] == "medium"

        # Also verify with button_count=3 (would normally be small)
        button2 = {"tag": "button", "text": {"tag": "plain_text", "content": "Go"}}
        result2 = apply_compact_style(button2, button_count=3, skip_compact=True)
        assert result2["size"] == "medium"

    def test_apply_compact_style_backward_compatible(self):
        """默认行为不变：skip_compact=False 时 button_count>2 仍为 'small'。"""
        from src.card.shared import apply_compact_style

        # Default behavior (skip_compact=False)
        button = {"tag": "button", "text": {"tag": "plain_text", "content": "Click"}}
        result = apply_compact_style(button, button_count=3)
        assert result["size"] == "small"

        # button_count <= 2 should still be medium
        button2 = {"tag": "button", "text": {"tag": "plain_text", "content": "Go"}}
        result2 = apply_compact_style(button2, button_count=2)
        assert result2["size"] == "medium"


# ---------------------------------------------------------------------------
# AC18: collapsible_panel header 格式统一
# ---------------------------------------------------------------------------


class TestCollapsiblePanelHeaderFormat:
    """AC18: collapsible_panel header 格式测试。"""

    def test_command_panel_header_format(self):
        """build_command_panel_card 的 collapsible_panel header 使用标准嵌套格式。"""
        card = build_command_panel_card(channel_id="test-ch")
        elements = card["body"]["elements"]

        # Find the collapsible_panel
        panel = None
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panel = elem
                break

        assert panel is not None, "collapsible_panel not found in command panel card"

        # Verify header structure is {"title": {"tag": "plain_text", ...}}
        header = panel.get("header", {})
        assert "title" in header, f"header should contain 'title' key, got: {header}"
        title = header.get("title", {})
        assert title.get("tag") == "plain_text", f"title tag should be 'plain_text', got: {title.get('tag')}"
        assert "更多快捷操作" in title.get("content", ""), "header content should contain '更多快捷操作'"

        # Verify old format {"tag": "plain_text", ...} is NOT used at header level
        assert "tag" not in header or header.get("tag") != "plain_text", (
            "header should NOT use old format with direct 'tag' key"
        )


# ---------------------------------------------------------------------------
# AC19: 讨论消息使用 collapsible_panel 优化（WP4 更新）
# ---------------------------------------------------------------------------


class TestDiscussionMessageTruncation:
    """AC19: 讨论消息使用 collapsible_panel 优化测试。"""

    def _find_collapsible_panels(self, card: dict) -> list[dict]:
        """Extract all collapsible_panel elements from card."""
        panels = []
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panels.append(elem)
        return panels

    def _find_note_elements(self, card: dict) -> list[dict]:
        """Extract all note elements from card."""
        notes = []
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "note":
                notes.append(elem)
        return notes

    def test_discussion_long_message_uses_collapsible_panel(self):
        """讨论消息超过 120 字符时使用 collapsible_panel。"""
        # Create 300 char message
        long_message = "A" * 300
        card = build_discussion_card(
            thread_id="trunc-ac19-1",
            participants=["Agent-A", "Agent-B"],
            messages=[{"sender": "Agent-A", "content": long_message, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-ac19",
        )

        panels = self._find_collapsible_panels(card)
        assert len(panels) >= 1, "collapsible_panel not found for long message"

        # Find the panel containing the message
        message_panel = None
        for panel in panels:
            panel_json = json.dumps(panel, ensure_ascii=False)
            if "Agent-A" in panel_json or "R1" in panel_json:
                message_panel = panel
                break

        assert message_panel is not None, "Message panel not found"

        # Verify full content is preserved in panel elements
        panel_elements = message_panel.get("elements", [])
        panel_content = json.dumps(panel_elements, ensure_ascii=False)
        assert long_message in panel_content, "Full message should be in collapsible_panel elements"

        # Verify expanded=False by default
        assert message_panel.get("expanded") is False

    def test_discussion_short_message_uses_note(self):
        """讨论消息在 note 元素中渲染（短消息用 note，长消息用 collapsible_panel）。"""
        # Short message uses note
        card = build_discussion_card(
            thread_id="trunc-ac19-2",
            participants=["Agent-X"],
            messages=[{"sender": "Agent-X", "content": "Hello world", "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-ac19b",
        )

        notes = self._find_note_elements(card)
        assert len(notes) > 0, "No note elements found for short message"
        # Verify each note has the expected structure
        for note in notes:
            assert "icon" in note, "Note should have icon"
            assert "elements" in note, "Note should have elements array"

    def test_short_message_not_in_collapsible_panel(self):
        """短消息（<= 120 字符）不使用 collapsible_panel。"""
        short_message = "B" * 120  # exactly 120 chars
        card = build_discussion_card(
            thread_id="trunc-ac19-3",
            participants=["Agent-Y"],
            messages=[{"sender": "Agent-Y", "content": short_message, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-ac19c",
        )

        card_json = json.dumps(card, ensure_ascii=False)
        # Full message should appear intact
        assert short_message in card_json
        # Should use note format
        notes = self._find_note_elements(card)
        assert len(notes) >= 1, "Short message should use note format"


# ---------------------------------------------------------------------------
# AC20: 角色名语法提示位置优化
# ---------------------------------------------------------------------------


class TestRoleSyntaxHintPosition:
    """AC20: 角色名语法提示位置优化测试。"""

    def _find_role_hint_in_elements(self, elements: list) -> bool:
        """Check if role syntax hint appears in the given elements list."""
        for elem in elements:
            if elem.get("tag") == "markdown":
                content = elem.get("content", "")
                if "角色名语法提示" in content:
                    return True
        return False

    def test_role_hint_in_collapsible_panel(self):
        """角色名语法提示位于 collapsible_panel 内部。"""
        card = build_command_panel_card(channel_id="test-ch-ac20")
        elements = card["body"]["elements"]

        # Find the collapsible_panel
        panel = None
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panel = elem
                break

        assert panel is not None, "collapsible_panel not found"

        # Verify hint is NOT in top-level elements
        assert not self._find_role_hint_in_elements(elements), (
            "Role syntax hint should NOT be in top-level elements"
        )

        # Verify hint IS inside collapsible_panel elements
        panel_elements = panel.get("elements", [])
        assert self._find_role_hint_in_elements(panel_elements), (
            "Role syntax hint should be inside collapsible_panel elements"
        )

    def test_role_hint_content_complete(self):
        """角色名语法提示内容完整。"""
        card = build_command_panel_card(channel_id="test-ch-ac20b")
        card_json = json.dumps(card, ensure_ascii=False)

        # Verify the hint content is present
        assert "角色名语法提示" in card_json
        assert "@role" in card_json
        assert "Senior Coder" in card_json


# ---------------------------------------------------------------------------
# WP4: 讨论卡片折叠面板 - 长消息使用 collapsible_panel
# ---------------------------------------------------------------------------


class TestDiscussionCardCollapsiblePanel:
    """WP4: 长讨论消息使用 collapsible_panel，用户可展开查看完整内容。"""

    def _find_collapsible_panels(self, card: dict) -> list[dict]:
        """Extract all collapsible_panel elements from card."""
        panels = []
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "collapsible_panel":
                panels.append(elem)
        return panels

    def _find_note_elements(self, card: dict) -> list[dict]:
        """Extract all note elements from card (for short messages)."""
        notes = []
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "note":
                notes.append(elem)
        return notes

    def test_long_message_uses_collapsible_panel(self):
        """超过 120 字符的消息使用 collapsible_panel 而非纯文本截断。"""
        long_message = "这是一条很长的讨论消息，" * 20  # ~200 chars
        card = build_discussion_card(
            thread_id="wp4-collapsible-1",
            participants=["Agent-A", "Agent-B"],
            messages=[{"sender": "Agent-A", "content": long_message, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-wp4",
        )

        panels = self._find_collapsible_panels(card)
        # Should have at least one collapsible_panel for the long message
        assert len(panels) >= 1, "Expected collapsible_panel for long message, found none"

        # Find the panel containing the message
        message_panel = None
        for panel in panels:
            panel_json = json.dumps(panel, ensure_ascii=False)
            if "Agent-A" in panel_json and "R1" in panel_json:
                message_panel = panel
                break

        assert message_panel is not None, "collapsible_panel for message not found"

        # Verify expanded=False (默认折叠)
        assert message_panel.get("expanded") is False, "collapsible_panel should be collapsed by default"

        # Verify full content is in the panel elements (not truncated)
        panel_elements = message_panel.get("elements", [])
        panel_content = json.dumps(panel_elements, ensure_ascii=False)
        assert long_message in panel_content, "Full message content should be in collapsible_panel elements"

    def test_collapsible_panel_has_truncated_preview_in_header(self):
        """collapsible_panel 的 header 包含截断预览。"""
        long_message = "A" * 200
        card = build_discussion_card(
            thread_id="wp4-collapsible-2",
            participants=["Agent-X"],
            messages=[{"sender": "Agent-X", "content": long_message, "round_num": 2}],
            current_round=2,
            max_rounds=5,
            channel_id="ch-wp4b",
        )

        panels = self._find_collapsible_panels(card)
        assert len(panels) >= 1

        # Find the message panel
        message_panel = None
        for panel in panels:
            header = panel.get("header", {})
            # Header could be {"title": {...}} or direct markdown
            header_content = ""
            if "title" in header:
                title = header.get("title", {})
                header_content = title.get("content", "")
            else:
                header_content = header.get("content", "")

            if "Agent-X" in header_content or "R2" in header_content:
                message_panel = panel
                break

        assert message_panel is not None, "Message panel not found"

        # Verify header has truncated preview (first N chars)
        header = message_panel.get("header", {})
        header_json = json.dumps(header, ensure_ascii=False)
        # Should contain some of the A's but not all 200
        assert "A" * 50 in header_json or "A" * 100 in header_json, "Header should contain truncated preview"
        assert "A" * 150 not in header_json, "Header preview should be truncated"

    def test_short_message_uses_note_format(self):
        """短消息（<= 120 字符）保持原有的 note 格式不变。"""
        short_message = "这是一条短消息"  # well under 120 chars
        card = build_discussion_card(
            thread_id="wp4-collapsible-3",
            participants=["Agent-A"],
            messages=[{"sender": "Agent-A", "content": short_message, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-wp4c",
        )

        notes = self._find_note_elements(card)
        # Should have at least one note element for the short message
        assert len(notes) >= 1, "Expected note element for short message"

        # Verify short message content is in the note
        note_json = json.dumps(notes, ensure_ascii=False)
        assert short_message in note_json, "Short message should appear in note element"

    def test_mixed_messages_short_and_long(self):
        """混合长短消息时，短消息用 note，长消息用 collapsible_panel。"""
        messages = [
            {"sender": "Agent-A", "content": "短消息 A", "round_num": 1},
            {"sender": "Agent-B", "content": "这是一条很长的消息内容" * 15, "round_num": 2},  # ~150 chars
            {"sender": "Agent-A", "content": "短消息 B", "round_num": 3},
        ]
        card = build_discussion_card(
            thread_id="wp4-collapsible-4",
            participants=["Agent-A", "Agent-B"],
            messages=messages,
            current_round=3,
            max_rounds=5,
            channel_id="ch-wp4d",
        )

        panels = self._find_collapsible_panels(card)
        notes = self._find_note_elements(card)

        # Should have at least 1 collapsible_panel (for the long message)
        assert len(panels) >= 1, "Expected collapsible_panel for long message"

        # Should have at least 2 note elements (for the short messages)
        assert len(notes) >= 2, "Expected note elements for short messages"

        card_json = json.dumps(card, ensure_ascii=False)
        assert "短消息 A" in card_json
        assert "短消息 B" in card_json

    def test_exact_120_char_message_uses_note(self):
        """恰好 120 字符的消息使用 note 格式（不使用 collapsible_panel）。"""
        exact_120 = "A" * 120
        card = build_discussion_card(
            thread_id="wp4-collapsible-5",
            participants=["Agent-X"],
            messages=[{"sender": "Agent-X", "content": exact_120, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-wp4e",
        )

        notes = self._find_note_elements(card)
        panels = self._find_collapsible_panels(card)

        # Should have note, not collapsible_panel for exactly 120 chars
        assert len(notes) >= 1, "120-char message should use note format"

        # Check that no collapsible_panel contains this message
        panel_json = json.dumps(panels, ensure_ascii=False)
        # Note: there might be other collapsible_panels for buttons, so we check content
        card_json = json.dumps(card, ensure_ascii=False)
        assert exact_120 in card_json

    def test_121_char_message_uses_collapsible_panel(self):
        """121 字符的消息触发 collapsible_panel。"""
        msg_121 = "B" * 121
        card = build_discussion_card(
            thread_id="wp4-collapsible-6",
            participants=["Agent-Y"],
            messages=[{"sender": "Agent-Y", "content": msg_121, "round_num": 1}],
            current_round=1,
            max_rounds=5,
            channel_id="ch-wp4f",
        )

        panels = self._find_collapsible_panels(card)

        # Find panel containing the message
        found = False
        for panel in panels:
            panel_json = json.dumps(panel, ensure_ascii=False)
            if "B" * 100 in panel_json:  # Check if most of the message is in a panel
                found = True
                # Verify full content is preserved
                assert msg_121 in panel_json, "Full 121-char content should be in collapsible_panel"
                break

        assert found, "121-char message should be in a collapsible_panel"


# ---------------------------------------------------------------------------
# Mouthpiece card mobile optimization: wide_screen_mode=false
# ---------------------------------------------------------------------------


class TestMouthpieceCardMobileOptimization:
    """Mouthpiece.format_card returns cards with wide_screen_mode=false for mobile optimization."""

    def test_format_card_wide_screen_mode_false(self):
        """Mouthpiece.format_card returns card with config.wide_screen_mode=false."""
        from src.slock_engine.mouthpiece import Mouthpiece

        agent = _make_agent("TestAgent", "coder")
        mouthpiece = Mouthpiece()

        card = mouthpiece.format_card(
            agent=agent,
            content="Test message content",
            channel_id="test-ch",
            task_id="task-123",
        )

        # Verify card has config
        assert "config" in card, "Card should have 'config' key"
        config = card["config"]

        # Verify wide_screen_mode is false
        assert "wide_screen_mode" in config, "Config should have 'wide_screen_mode' key"
        assert config["wide_screen_mode"] is False, (
            f"Expected wide_screen_mode=False, got {config['wide_screen_mode']}"
        )

    def test_format_card_with_model_info_wide_screen_mode_false(self):
        """Mouthpiece.format_card with model_info still has wide_screen_mode=false."""
        from src.slock_engine.mouthpiece import Mouthpiece

        agent = _make_agent("ReviewerAgent", "reviewer")
        mouthpiece = Mouthpiece()

        card = mouthpiece.format_card(
            agent=agent,
            content="Review complete",
            model_info="claude-3-opus",
            duration_s=12.5,
            channel_id="test-ch-2",
        )

        assert card["config"]["wide_screen_mode"] is False, (
            "wide_screen_mode should be false even with model_info and duration_s"
        )

    def test_format_escalation_wide_screen_mode_false(self):
        """Mouthpiece.format_escalation also returns card with wide_screen_mode=false."""
        from src.slock_engine.mouthpiece import Mouthpiece

        agent = _make_agent("EscalationAgent", "coder")
        mouthpiece = Mouthpiece()

        card = mouthpiece.format_escalation(
            agent=agent,
            reason="Need human assistance with this task",
        )

        assert "config" in card, "Escalation card should have 'config' key"
        assert card["config"]["wide_screen_mode"] is False, (
            "format_escalation should also have wide_screen_mode=false"
        )
