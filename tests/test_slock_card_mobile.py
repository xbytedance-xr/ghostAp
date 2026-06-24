"""Mobile UX and card rendering tests for Slock card templates.

Tests AC-FIX-03, AC-FIX-09, AC-FIX-10, AC-FIX-11.
"""

import json
import uuid

import pytest

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
        card = build_discussion_card(
            thread_id="test-thread-1", current_round=3, max_rounds=10,
            participants=["Agent-A", "Agent-B"], messages=[],
            trigger_reason="Uncertainty detected", channel_id="test-ch",
        )
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "column_set":
                columns = elem.get("columns", [])
                assert not any(
                    col.get("background_style") in ("purple", "grey") for col in columns
                )

    def test_progress_markdown_format(self):
        card = build_discussion_card(
            thread_id="test-thread-2", current_round=4, max_rounds=7,
            participants=["A", "B"], messages=[], channel_id="ch",
        )
        elements = card["body"]["elements"]
        progress_elem = next(
            (e for e in elements if "进度" in e.get("content", "") and "●" in e.get("content", "")),
            None,
        )
        assert progress_elem is not None
        content = progress_elem["content"]
        assert content.count("●") == 4
        assert content.count("○") == 3
        assert "(4/7)" in content
        # Single line, reasonable length
        assert "\n" not in content
        assert len(content) < 40


class TestCommandPanelCompact:
    """AC-FIX-11: Command panel is compact with ≤8 first-level elements."""

    def test_main_panel_compact_and_has_core_actions(self):
        card = build_command_panel_card(channel_id="test-ch")
        elements = card["body"]["elements"]
        assert len(elements) <= 8
        card_json = json.dumps(card, ensure_ascii=False)
        for action in ["更多操作", "slock_cmd_team_list", "slock_cmd_role_list", "slock_cmd_task_list", "slock_cmd_discuss"]:
            assert action in card_json

    def test_extended_panel_has_forms(self):
        card = build_command_panel_extended_card(channel_id="test-ch")
        card_json = json.dumps(card, ensure_ascii=False)
        for form in ["slock_form_new_team", "slock_form_new_role", "slock_form_council"]:
            assert form in card_json


class TestCrashRecoveryCardShowsTitle:
    """AC-FIX-09: Crash recovery card displays task content, not just IDs."""

    def test_card_shows_task_content_and_truncates(self):
        long_content = "这是一个非常长的任务描述" * 20
        tasks = [
            SlockTask(task_id=str(uuid.uuid4()), content="修复登录页面的验证逻辑 bug", status=TaskStatus.TODO),
            SlockTask(task_id=str(uuid.uuid4()), content=long_content, status=TaskStatus.TODO),
        ]
        card = build_crash_recovery_card(tasks)
        card_json = json.dumps(card, ensure_ascii=False)
        assert "修复登录页面的验证逻辑" in card_json
        assert long_content not in card_json
        assert "..." in card_json
        assert card["schema"] == "2.0"


class TestChitchatHintImport:
    """AC-FIX-03: build_chitchat_hint_card is importable via absolute path."""

    def test_import_and_render(self):
        from src.slock_engine.card_templates import build_chitchat_hint_card
        assert callable(build_chitchat_hint_card)
        card = build_chitchat_hint_card("今天天气不错", channel_id="test-ch")
        assert isinstance(card, dict)
        assert "schema" in card or "body" in card or "elements" in card


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_agent(name: str = "TestBot", role: str = "coder") -> AgentIdentity:
    return AgentIdentity(name=name, role=role, agent_type="coco")


# ---------------------------------------------------------------------------
# AC23: Button simplification — at most 2 buttons visible at top level
# ---------------------------------------------------------------------------

class TestAC23ButtonSimplification:
    def test_top_level_buttons_and_collapsible_panel(self):
        """At most 3 top-level buttons; collapsible_panel holds secondary buttons."""
        agent = _make_agent("Reviewer")
        card = build_agent_message_card(
            agent=agent, content="Review done.",
            channel_id="ch-3", task_id="task-3", discussion_enabled=True,
        )
        elements = card["body"]["elements"]

        # Count top-level buttons
        top_level_buttons = 0
        for elem in elements:
            tag = elem.get("tag", "")
            if tag == "button":
                top_level_buttons += 1
            elif tag == "column_set":
                for col in elem.get("columns", []):
                    for child in col.get("elements", []):
                        if child.get("tag") == "button":
                            top_level_buttons += 1
        assert top_level_buttons <= 3

        # Collapsible panel exists with secondary buttons
        panel = next((e for e in elements if e.get("tag") == "collapsible_panel"), None)
        assert panel is not None
        panel_json = json.dumps(panel, ensure_ascii=False)
        assert "slock_agent_show_reasoning" in panel_json or "查看推理" in panel_json


# ---------------------------------------------------------------------------
# AC24: Progress bar — ≤30 chars when max_rounds is large
# ---------------------------------------------------------------------------

class TestAC24ProgressBarLength:
    def test_progress_bar_percentage_format_for_large_rounds(self):
        card = build_discussion_card(
            thread_id="test-ac24", current_round=5, max_rounds=20,
            participants=["Agent-X"], messages=[], channel_id="ch-ac24",
        )
        elements = card["body"]["elements"]
        progress_elem = next((e for e in elements if "进度" in e.get("content", "")), None)
        assert progress_elem is not None
        content = progress_elem["content"]
        assert len(content) <= 30
        assert "●" not in content  # Uses percentage format
        assert "%" in content
        assert "5/20" in content


# ---------------------------------------------------------------------------
# AC25: Truncation — task preview ≤40 chars + ellipsis
# ---------------------------------------------------------------------------

class TestAC25TaskPreviewTruncation:
    def test_long_task_content_truncated_to_25_chars(self):
        agent = _make_agent("ExactBot")
        content_50 = "X" * 50
        task = SlockTask(content=content_50, status=TaskStatus.TODO)
        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)], channel_id="ch-ac25c",
            current_tasks={agent.agent_id: task},
        )
        card_json = json.dumps(card, ensure_ascii=False)
        assert "X" * 25 in card_json
        assert "X" * 26 not in card_json
        assert "…" in card_json or "..." in card_json

    def test_short_task_content_not_truncated(self):
        agent = _make_agent("ShortTaskBot")
        short_content = "Fix login bug"
        task = SlockTask(content=short_content, status=TaskStatus.TODO)
        card = build_status_panel_card(
            agents=[(agent, AgentStatus.RUNNING)], team_name="TestTeam",
            channel_id="ch-ac25b", current_tasks={agent.agent_id: task},
        )
        card_json = json.dumps(card, ensure_ascii=False)
        assert short_content in card_json


# ---------------------------------------------------------------------------
# AC26: No tag=action — queue/transfer cards avoid legacy action nodes
# ---------------------------------------------------------------------------

class TestAC26NoTagAction:
    def _find_action_tags(self, obj) -> list:
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

    @pytest.mark.parametrize("card_builder,kwargs", [
        (build_queue_waiting_card, {"agent": _make_agent("BusyBot"), "channel_id": "ch", "position": 2, "current_status": "running"}),
        (build_transfer_suggestion_card, {"busy_agent": _make_agent("Busy"), "idle_agent": _make_agent("Idle"), "channel_id": "ch", "original_message": "help"}),
    ])
    def test_no_action_tag(self, card_builder, kwargs):
        card = card_builder(**kwargs)
        action_nodes = self._find_action_tags(card)
        assert action_nodes == []


# ---------------------------------------------------------------------------
# AC23: No bare action tags in chitchat/status cards
# ---------------------------------------------------------------------------


def _find_bare_action_nodes(obj) -> list:
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
    @pytest.mark.parametrize("card_fn,kwargs", [
        (build_chitchat_hint_card, {"original_message": "今天天气不错", "channel_id": "ch"}),
        (build_status_panel_card, {"agents": [(_make_agent("A"), AgentStatus.IDLE)], "channel_id": "ch"}),
    ])
    def test_no_bare_action_nodes(self, card_fn, kwargs):
        card = card_fn(**kwargs)
        bare_actions = _find_bare_action_nodes(card)
        assert bare_actions == []


# ---------------------------------------------------------------------------
# AC24: Task board uses flex_mode='none' not 'bisect'
# ---------------------------------------------------------------------------


class TestAC24TaskBoardFlexMode:
    def _collect_flex_modes(self, obj) -> list[str]:
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

    def test_task_board_no_bisect_uses_none(self):
        tasks = [
            SlockTask(content="Fix the login bug", status=TaskStatus.TODO),
            SlockTask(content="Deploy v2.1", status=TaskStatus.IN_PROGRESS),
        ]
        agents = [_make_agent("DevBot")]
        card = build_task_board_card(tasks=tasks, agents=agents, channel_id="ch-ac24", summary_mode=False)
        flex_modes = self._collect_flex_modes(card)
        assert "bisect" not in flex_modes
        assert "none" in flex_modes


# ---------------------------------------------------------------------------
# AC25: Dynamic agent name buttons truncation
# ---------------------------------------------------------------------------


class TestAC25DynamicNameTruncation:
    @pytest.mark.parametrize("text,max_len,expected_len", [
        ("Hello", 20, 5),
        ("A" * 20, 20, 20),
        ("A" * 25, 20, 20),
        ("B" * 21, 20, 20),
        ("Hello World!", 10, 10),
    ])
    def test_truncate_dynamic_label(self, text, max_len, expected_len):
        result = _truncate_dynamic_label(text, max_len=max_len)
        assert len(result) <= expected_len
        if len(text) > max_len:
            assert result.endswith("…")

    def test_transfer_card_button_labels_within_limit(self):
        busy_agent = _make_agent("BusyBot")
        idle_agent = _make_agent("一个超级超级长的Agent名字用来测试截断功能")
        card = build_transfer_suggestion_card(
            busy_agent=busy_agent, idle_agent=idle_agent,
            channel_id="ch-ac25", original_message="Check this.",
        )
        card_json = json.dumps(card, ensure_ascii=False)
        assert "…" in card_json


# ---------------------------------------------------------------------------
# Discussion card message truncation with collapsible_panel (consolidated)
# ---------------------------------------------------------------------------


class TestDiscussionMessageCollapsiblePanel:
    """Long discussion messages use collapsible_panel; short ones use note."""

    def _find_collapsible_panels(self, card: dict) -> list[dict]:
        return [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]

    def _find_note_elements(self, card: dict) -> list[dict]:
        return [e for e in card["body"]["elements"] if e.get("tag") == "note"]

    def test_long_message_uses_collapsible_panel(self):
        long_message = "x" * 500
        card = build_discussion_card(
            thread_id="t1", participants=["A", "B"],
            messages=[{"sender": "A", "content": long_message, "round_num": 1}],
            current_round=1, max_rounds=5, channel_id="ch1",
        )
        panels = self._find_collapsible_panels(card)
        assert len(panels) >= 1
        panel_json = json.dumps(panels, ensure_ascii=False)
        assert long_message in panel_json

    def test_short_message_uses_note(self):
        card = build_discussion_card(
            thread_id="t1", participants=["A", "B"],
            messages=[{"sender": "A", "content": "short msg", "round_num": 1}],
            current_round=1, max_rounds=5, channel_id="ch1",
        )
        notes = self._find_note_elements(card)
        assert len(notes) >= 1

    def test_exact_boundary_120_uses_note_121_uses_panel(self):
        """120 chars -> note; 121 chars -> collapsible_panel."""
        card_120 = build_discussion_card(
            thread_id="t-120", participants=["X"],
            messages=[{"sender": "X", "content": "A" * 120, "round_num": 1}],
            current_round=1, max_rounds=5, channel_id="ch",
        )
        notes_120 = self._find_note_elements(card_120)
        assert len(notes_120) >= 1

        card_121 = build_discussion_card(
            thread_id="t-121", participants=["Y"],
            messages=[{"sender": "Y", "content": "B" * 121, "round_num": 1}],
            current_round=1, max_rounds=5, channel_id="ch",
        )
        panels_121 = self._find_collapsible_panels(card_121)
        found = any("B" * 100 in json.dumps(p, ensure_ascii=False) for p in panels_121)
        assert found

    def test_mixed_messages_short_and_long(self):
        messages = [
            {"sender": "A", "content": "短消息 A", "round_num": 1},
            {"sender": "B", "content": "这是一条很长的消息内容" * 15, "round_num": 2},
            {"sender": "A", "content": "短消息 B", "round_num": 3},
        ]
        card = build_discussion_card(
            thread_id="mixed", participants=["A", "B"],
            messages=messages, current_round=3, max_rounds=5, channel_id="ch",
        )
        panels = self._find_collapsible_panels(card)
        notes = self._find_note_elements(card)
        assert len(panels) >= 1
        assert len(notes) >= 2


# ---------------------------------------------------------------------------
# Escalation cards: no code blocks
# ---------------------------------------------------------------------------


class TestEscalationNoCodeBlock:
    @pytest.mark.parametrize("builder,extra_kwargs", [
        ("build_escalation_card", {}),
        ("build_resolved_escalation_card", {"resolved_by": "Op", "resolution": "Retry"}),
    ])
    def test_no_code_block_in_card(self, builder, extra_kwargs):
        from src.slock_engine.card_templates import build_escalation_card, build_resolved_escalation_card
        from src.slock_engine.models import EscalationLevel, EscalationRequest
        esc = EscalationRequest(
            escalation_id="esc1", agent_id="a1", agent_name="TestAgent",
            level=EscalationLevel.WARNING, reason="test",
            context="some long error context " * 30,
        )
        fn = build_escalation_card if builder == "build_escalation_card" else build_resolved_escalation_card
        card = fn(esc, channel_id="ch1", **extra_kwargs)
        card_str = json.dumps(card)
        assert "```" not in card_str


# ---------------------------------------------------------------------------
# AC20: Memory display card
# ---------------------------------------------------------------------------


class TestMemoryCardCollapsible:
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


# ---------------------------------------------------------------------------
# AC21: Discussion subtitle truncation
# ---------------------------------------------------------------------------


class TestDiscussionSubtitleTruncation:
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
# AC22: Task board vertical layout
# ---------------------------------------------------------------------------


class TestTaskBoardVerticalLayout:
    def test_single_column_per_status_row(self):
        tasks = [SlockTask(task_id="t1", content="Test task", status=TaskStatus.TODO, created_in="ch1")]
        agents = [AgentIdentity(agent_id="a1", name="Bot", emoji="\U0001f916", owner_group="ch1")]
        card = build_task_board_card(tasks, agents, channel_id="ch1")
        elements = card["body"]["elements"]
        for elem in elements:
            if elem.get("tag") == "column_set" and elem.get("background_style") not in (None, "default"):
                assert elem.get("flex_mode") == "none"
                assert len(elem["columns"]) == 1


# ---------------------------------------------------------------------------
# Button sizing (apply_compact_style)
# ---------------------------------------------------------------------------


class TestApplyCompactStyleButtonSizing:
    @pytest.mark.parametrize("button_count,expected_size", [(3, "small"), (5, "small"), (2, "medium"), (1, "medium")])
    def test_button_sizing_threshold(self, button_count, expected_size):
        from src.card.shared import apply_compact_style
        button = {"tag": "button", "text": {"tag": "plain_text", "content": "Click"}}
        result = apply_compact_style(button, button_count=button_count)
        assert result["size"] == expected_size


# ---------------------------------------------------------------------------
# Discussion expand card pagination
# ---------------------------------------------------------------------------


class TestDiscussionExpandCardPagination:
    def test_page_size_10_and_pagination_indicator(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [{"sender": "A", "content": f"Message {i}", "round_num": i} for i in range(1, 16)]
        card = build_discussion_expand_card(
            thread_id="page-test", messages=messages,
            participants=["A"], channel_id="ch", page=0,
        )
        card_json = json.dumps(card, ensure_ascii=False)
        assert "第 1-10 条，共 15 条" in card_json
        assert "Message 10" in card_json
        assert "Message 11" not in card_json
        assert "加载更多" in card_json

    def test_no_pagination_when_all_fit(self):
        from src.slock_engine.card_templates import build_discussion_expand_card
        messages = [{"sender": "C", "content": f"Msg {i}", "round_num": i} for i in range(1, 11)]
        card = build_discussion_expand_card(
            thread_id="page-test-3", messages=messages,
            participants=["C"], channel_id="ch", page=0,
        )
        card_json = json.dumps(card, ensure_ascii=False)
        assert "加载更多" not in card_json


# ---------------------------------------------------------------------------
# Status panel two-row layout
# ---------------------------------------------------------------------------


class TestStatusPanelTwoRowLayout:
    def test_two_rows_per_agent_with_task(self):
        agent = AgentIdentity(agent_id="a1", name="Alice", emoji="🔧", role="coder")
        task = SlockTask(task_id="t1", content="Implement feature XYZ", status=TaskStatus.IN_PROGRESS)
        card = build_status_panel_card([(agent, AgentStatus.RUNNING)], current_tasks={"a1": task})
        md_elements = [e for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        agent_md = [e for e in md_elements if "Alice" in e.get("content", "") or "Implement" in e.get("content", "")]
        assert len(agent_md) >= 2


# ---------------------------------------------------------------------------
# Task board default summary mode
# ---------------------------------------------------------------------------


class TestTaskBoardDefaultSummaryMode:
    def test_default_summary_mode(self):
        card = build_task_board_card([], [], team_name="Test")
        column_sets = [
            e for e in card["body"]["elements"]
            if e.get("tag") == "column_set" and e.get("background_style") in {"green", "yellow", "blue", "grey"}
        ]
        assert len(column_sets) == 0


# ---------------------------------------------------------------------------
# Discussion card mobile: short title
# ---------------------------------------------------------------------------


class TestDiscussionCardMobile:
    def test_title_short_with_round_info(self):
        card = build_discussion_card(
            thread_id="thread1", participants=["Coder", "Reviewer"],
            messages=[{"sender": "Coder", "content": "Hello", "round_num": 1}],
            current_round=5, max_rounds=20, channel_id="chat1",
        )
        title = card["header"]["title"]["content"]
        assert len(title) <= 12
        assert "5" in title
        assert "20" in title


# ---------------------------------------------------------------------------
# AC17: Hub card button touch optimization
# ---------------------------------------------------------------------------


class TestHubCardButtonTouchOptimization:
    def test_hub_card_buttons_medium_and_vertical(self):
        from src.card.shared import _build_button_vertical
        from src.slock_engine.card_templates import build_command_hub_card

        card = build_command_hub_card(channel_id="test-ch")
        # Collect all buttons recursively
        def _collect_buttons(obj):
            buttons = []
            if isinstance(obj, dict):
                if obj.get("tag") == "button":
                    buttons.append(obj)
                for v in obj.values():
                    buttons.extend(_collect_buttons(v))
            elif isinstance(obj, list):
                for item in obj:
                    buttons.extend(_collect_buttons(item))
            return buttons

        buttons = _collect_buttons(card)
        assert len(buttons) > 0
        for btn in buttons:
            assert btn.get("size") == "medium"

        # Vertical layout test
        test_buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": f"Btn{i}"}} for i in range(3)]
        result = _build_button_vertical(test_buttons)
        assert len(result) == 3
        for cs in result:
            assert cs["tag"] == "column_set"
            assert cs["flex_mode"] == "none"

    def test_apply_compact_style_skip_compact(self):
        from src.card.shared import apply_compact_style
        button = {"tag": "button", "text": {"tag": "plain_text", "content": "Click"}}
        result = apply_compact_style(button, button_count=10, skip_compact=True)
        assert result["size"] == "medium"


# ---------------------------------------------------------------------------
# AC18: collapsible_panel header format
# ---------------------------------------------------------------------------


class TestCollapsiblePanelHeaderFormat:
    def test_command_panel_header_format(self):
        card = build_command_panel_card(channel_id="test-ch")
        elements = card["body"]["elements"]
        panel = next((e for e in elements if e.get("tag") == "collapsible_panel"), None)
        assert panel is not None
        header = panel.get("header", {})
        assert "title" in header
        title = header.get("title", {})
        assert title.get("tag") == "plain_text"
        assert "更多快捷操作" in title.get("content", "")


# ---------------------------------------------------------------------------
# AC20: Role syntax hint in collapsible_panel
# ---------------------------------------------------------------------------


class TestRoleSyntaxHintPosition:
    def test_role_hint_in_collapsible_panel_not_top_level(self):
        card = build_command_panel_card(channel_id="test-ch-ac20")
        elements = card["body"]["elements"]
        panel = next((e for e in elements if e.get("tag") == "collapsible_panel"), None)
        assert panel is not None

        # Not in top-level
        top_level_has_hint = any(
            "角色名语法提示" in e.get("content", "")
            for e in elements if e.get("tag") == "markdown"
        )
        assert not top_level_has_hint

        # Is inside panel
        panel_has_hint = any(
            "角色名语法提示" in e.get("content", "")
            for e in panel.get("elements", []) if e.get("tag") == "markdown"
        )
        assert panel_has_hint


# ---------------------------------------------------------------------------
# Mouthpiece card mobile optimization
# ---------------------------------------------------------------------------


class TestMouthpieceCardMobileOptimization:
    @pytest.mark.parametrize("method,kwargs", [
        ("format_card", {"agent": _make_agent("TestAgent"), "content": "msg", "channel_id": "ch", "task_id": "t"}),
        ("format_card", {"agent": _make_agent("Reviewer"), "content": "done", "model_info": "claude", "duration_s": 12.5, "channel_id": "ch"}),
        ("format_escalation", {"agent": _make_agent("Esc"), "reason": "Need help"}),
    ])
    def test_wide_screen_mode_false(self, method, kwargs):
        from src.slock_engine.mouthpiece import Mouthpiece
        mouthpiece = Mouthpiece()
        card = getattr(mouthpiece, method)(**kwargs)
        assert card["config"]["wide_screen_mode"] is False
