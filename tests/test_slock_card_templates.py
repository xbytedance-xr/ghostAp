"""Unit tests for slock_engine/card_templates.py — card building functions."""

from __future__ import annotations

from src.slock_engine.card_templates import (
    build_agent_message_card,
    build_status_panel_card,
    build_task_board_card,
)
from src.slock_engine.models import (
    AgentIdentity,
    AgentStatus,
    SlockTask,
    TaskStatus,
)


def _collect_buttons(node: object) -> list[dict]:
    if isinstance(node, dict):
        buttons = [node] if node.get("tag") == "button" else []
        for value in node.values():
            buttons.extend(_collect_buttons(value))
        return buttons
    if isinstance(node, list):
        buttons: list[dict] = []
        for item in node:
            buttons.extend(_collect_buttons(item))
        return buttons
    return []


def _collect_tags(node: object) -> list[str]:
    if isinstance(node, dict):
        tags = [node["tag"]] if isinstance(node.get("tag"), str) else []
        for value in node.values():
            tags.extend(_collect_tags(value))
        return tags
    if isinstance(node, list):
        tags: list[str] = []
        for item in node:
            tags.extend(_collect_tags(item))
        return tags
    return []


def _status_column_sets(card: dict) -> list[dict]:
    return [
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "column_set"
        and element.get("background_style") in {"green", "yellow", "blue", "grey"}
    ]


class TestBuildAgentMessageCard:
    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Coder", "emoji": "🔧", "role": "coder"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_basic_structure(self):
        agent = self._make_agent()
        card = build_agent_message_card(agent, "Hello world")
        assert card["schema"] == "2.0"
        assert card["header"]["title"]["content"] == "🔧 Coder"
        assert card["header"]["template"] == "blue"
        body_elements = card["body"]["elements"]
        assert any(e["tag"] == "markdown" and "Hello world" in e["content"] for e in body_elements)

    def test_footer_with_model_info(self):
        agent = self._make_agent(agent_type="claude", model_name="claude-3")
        card = build_agent_message_card(agent, "msg", model_info="claude-3-opus")
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 1
        note_text = note_elements[0]["elements"][0]["content"]
        assert "claude-3-opus" in note_text

    def test_footer_with_duration_seconds(self):
        agent = self._make_agent()
        card = build_agent_message_card(agent, "msg", duration_s=5.2)
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 1
        assert "5.2s" in note_elements[0]["elements"][0]["content"]

    def test_footer_with_duration_minutes(self):
        agent = self._make_agent()
        card = build_agent_message_card(agent, "msg", duration_s=125.3)
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert "2m" in note_elements[0]["elements"][0]["content"]

    def test_no_footer_when_no_metadata(self):
        agent = self._make_agent(agent_type="", model_name="")
        card = build_agent_message_card(agent, "msg")
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 0


class TestBuildStatusPanelCard:
    def test_empty_agents(self):
        card = build_status_panel_card([], team_name="Alpha")
        assert "Alpha" in card["header"]["title"]["content"]
        body = card["body"]["elements"]
        assert any("No agents" in e.get("content", "") for e in body)

    def test_with_agents_uses_column_set(self):
        """AC-6: Status panel uses column_set components instead of plain markdown list."""
        agent = AgentIdentity(agent_id="a1", name="Alice", emoji="🤖", role="coder")
        card = build_status_panel_card([(agent, AgentStatus.IDLE)], team_name="Team")
        column_sets = _status_column_sets(card)
        assert len(column_sets) == 1
        # Verify agent info is inside the first column
        col = column_sets[0]["columns"][0]
        md_content = col["elements"][0]["content"]
        assert "Alice" in md_content

    def test_status_color_idle_green(self):
        """IDLE status maps to green background."""
        agent = AgentIdentity(agent_id="a1", name="A", emoji="🤖")
        card = build_status_panel_card([(agent, AgentStatus.IDLE)])
        cs = [e for e in card["body"]["elements"] if e["tag"] == "column_set"]
        assert cs[0]["background_style"] == "green"

    def test_status_color_thinking_yellow(self):
        """THINKING status maps to yellow background."""
        agent = AgentIdentity(agent_id="a1", name="A", emoji="🤖")
        card = build_status_panel_card([(agent, AgentStatus.THINKING)])
        cs = [e for e in card["body"]["elements"] if e["tag"] == "column_set"]
        assert cs[0]["background_style"] == "yellow"

    def test_status_color_running_blue(self):
        """RUNNING status maps to blue background."""
        agent = AgentIdentity(agent_id="a1", name="A", emoji="🤖")
        card = build_status_panel_card([(agent, AgentStatus.RUNNING)])
        cs = [e for e in card["body"]["elements"] if e["tag"] == "column_set"]
        assert cs[0]["background_style"] == "blue"

    def test_status_color_sending_grey(self):
        """SENDING status maps to grey background."""
        agent = AgentIdentity(agent_id="a1", name="A", emoji="🤖")
        card = build_status_panel_card([(agent, AgentStatus.SENDING)])
        cs = [e for e in card["body"]["elements"] if e["tag"] == "column_set"]
        assert cs[0]["background_style"] == "grey"

    def test_multiple_agents_produce_multiple_column_sets(self):
        """Each agent gets its own column_set row."""
        a1 = AgentIdentity(agent_id="a1", name="Alice", emoji="🤖")
        a2 = AgentIdentity(agent_id="a2", name="Bob", emoji="🔧")
        card = build_status_panel_card(
            [(a1, AgentStatus.IDLE), (a2, AgentStatus.RUNNING)]
        )
        cs = _status_column_sets(card)
        assert len(cs) == 2
        assert cs[0]["background_style"] == "green"
        assert cs[1]["background_style"] == "blue"

    def test_refresh_button_present(self):
        card = build_status_panel_card([])
        buttons = _collect_buttons(card)
        assert any(button["text"]["content"] == "🔄 Refresh" for button in buttons)

    def test_status_panel_does_not_emit_schema_v2_unsupported_action_tag(self):
        card = build_status_panel_card([], team_name="Alpha", channel_id="ch_alpha")
        assert "action" not in _collect_tags(card)

    def test_default_title_without_team(self):
        card = build_status_panel_card([])
        assert "Slock Agent Status" in card["header"]["title"]["content"]

    def test_status_panel_dual_column(self):
        """Designer review fix: column_set uses 2 columns with flex_mode='bisect'."""
        a1 = AgentIdentity(agent_id="a1", name="Alice", emoji="🤖", role="coder")
        a2 = AgentIdentity(agent_id="a2", name="Bob", emoji="🔧", role="writer")
        card = build_status_panel_card(
            [(a1, AgentStatus.IDLE), (a2, AgentStatus.RUNNING)], team_name="Team"
        )
        column_sets = _status_column_sets(card)
        assert len(column_sets) == 2

        for cs in column_sets:
            # Must have 2 columns
            assert len(cs["columns"]) == 2
            # flex_mode must be bisect
            assert cs["flex_mode"] == "bisect"
            # Left column has weight 3, right column has weight 1
            assert cs["columns"][0]["weight"] == 3
            assert cs["columns"][1]["weight"] == 1
            # Right column content is right-aligned status label
            right_md = cs["columns"][1]["elements"][0]
            assert right_md["text_align"] == "right"


class TestBuildTaskBoardCard:
    def test_empty_tasks(self):
        card = build_task_board_card([], [], team_name="Dev")
        assert "Dev" in card["header"]["title"]["content"]
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("empty" in e["content"] for e in md_elements)

    def test_with_tasks(self):
        task = SlockTask(task_id="t1", content="Build feature X", status=TaskStatus.TODO)
        agent = AgentIdentity(agent_id="a1", name="Bob", emoji="🔧")
        card = build_task_board_card([task], [agent])
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("Build feature X" in e["content"] for e in md_elements)

    def test_task_with_assignee(self):
        task = SlockTask(
            task_id="t1", content="Fix bug", status=TaskStatus.IN_PROGRESS, claimed_by="a1"
        )
        agent = AgentIdentity(agent_id="a1", name="Bob", emoji="🔧")
        card = build_task_board_card([task], [agent])
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        # Should show assignee
        assert any("Bob" in e["content"] for e in md_elements)

    def test_board_groups_by_status(self):
        tasks = [
            SlockTask(task_id="t1", content="A", status=TaskStatus.TODO),
            SlockTask(task_id="t2", content="B", status=TaskStatus.DONE),
        ]
        card = build_task_board_card(tasks, [])
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        # Should have columns for each status
        assert any("Todo" in e["content"] for e in md_elements)
        assert any("Done" in e["content"] for e in md_elements)


class TestBuildAgentMessageCardJsonStructure:
    """R-01 mitigation: JSON Schema regression tests for build_agent_message_card.

    Ensures the four essential elements (header.template, header.title,
    body.elements[markdown], body.elements[note]) are always present and correctly
    structured to prevent Feishu card rendering failures.
    """

    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {
            "agent_id": "test-001",
            "name": "TestCoder",
            "emoji": "🔧",
            "role": "coder",
            "agent_type": "claude",
            "model_name": "claude-sonnet-4",
        }
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_top_level_schema_keys(self):
        """Card must have schema, config, header, body at top level."""
        agent = self._make_agent()
        card = build_agent_message_card(agent, "content")
        assert set(card.keys()) == {"schema", "config", "header", "body"}
        assert card["schema"] == "2.0"
        assert card["config"]["wide_screen_mode"] is True

    def test_header_has_template_and_title(self):
        """Header must contain template (color) and title with tag+content."""
        agent = self._make_agent(role="coder")
        card = build_agent_message_card(agent, "hello")
        header = card["header"]
        # template must be a valid Feishu color string
        assert header["template"] == "blue"  # coder -> blue
        # title must be a plain_text tag with content
        assert header["title"]["tag"] == "plain_text"
        assert "TestCoder" in header["title"]["content"]
        assert "🔧" in header["title"]["content"]

    def test_header_template_varies_by_role(self):
        """Different roles produce different header colors."""
        roles_colors = [
            ("coder", "blue"),
            ("writer", "green"),
            ("reviewer", "orange"),
            ("tester", "purple"),
            ("planner", "red"),
            ("custom", "grey"),
        ]
        for role, expected_color in roles_colors:
            agent = self._make_agent(role=role)
            card = build_agent_message_card(agent, "x")
            assert card["header"]["template"] == expected_color, f"role={role}"

    def test_body_contains_markdown_element(self):
        """Body elements must include at least one markdown element with the content."""
        agent = self._make_agent()
        card = build_agent_message_card(agent, "The quick brown fox")
        body_elements = card["body"]["elements"]
        md_elements = [e for e in body_elements if e.get("tag") == "markdown"]
        assert len(md_elements) >= 1
        assert "The quick brown fox" in md_elements[0]["content"]

    def test_body_contains_note_element_when_metadata_present(self):
        """Body elements must include a note element when model_info or duration is given."""
        agent = self._make_agent()
        card = build_agent_message_card(agent, "msg", model_info="gpt-4o", duration_s=3.5)
        body_elements = card["body"]["elements"]
        note_elements = [e for e in body_elements if e.get("tag") == "note"]
        assert len(note_elements) == 1
        note = note_elements[0]
        # note must have elements array with plain_text child
        assert note["elements"][0]["tag"] == "plain_text"
        note_content = note["elements"][0]["content"]
        assert "gpt-4o" in note_content
        assert "3.5s" in note_content

    def test_note_element_contains_agent_type(self):
        """Footer note should include agent_type when present."""
        agent = self._make_agent(agent_type="codex")
        card = build_agent_message_card(agent, "msg", duration_s=1.0)
        note_elements = [e for e in card["body"]["elements"] if e.get("tag") == "note"]
        assert len(note_elements) == 1
        note_content = note_elements[0]["elements"][0]["content"]
        assert "codex" in note_content

    def test_elements_ordering_markdown_before_note(self):
        """Markdown content must appear before the footer note in elements array."""
        agent = self._make_agent()
        card = build_agent_message_card(agent, "body text", model_info="model-x")
        body_elements = card["body"]["elements"]
        md_idx = next(i for i, e in enumerate(body_elements) if e["tag"] == "markdown")
        note_idx = next(i for i, e in enumerate(body_elements) if e["tag"] == "note")
        assert md_idx < note_idx
