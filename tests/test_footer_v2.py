from src.card.render.footer import (
    build_footer_atoms,
    render_footer,
    render_now_tool_hint,
    render_subagent_badge,
)
from src.card.state.models import CardMetadata, CardState, ContentBlock, FooterState


def _tool(name, status="active", tool_input=None):
    return ContentBlock(
        kind="tool_call",
        block_id=name,
        tool_name=name,
        status=status,
        tool_input=tool_input or "{}",
        tool_output="",
    )


def test_now_tool_hint_for_edit_path():
    line = render_now_tool_hint(_tool("Edit", tool_input='{"path": "src/router.py"}'))

    assert line == "⚙ Edit · 写入 src/router.py"


def test_now_tool_hint_for_grep_pattern():
    line = render_now_tool_hint(_tool("Grep", tool_input='{"pattern": "def route"}'))

    assert "搜索" in line
    assert "def route" in line


def test_now_tool_hint_unknown_tool_falls_back_to_name():
    line = render_now_tool_hint(_tool("MysteryTool"))

    assert line == "⚙ MysteryTool · MysteryTool"


def test_now_tool_hint_none_when_no_running_tool():
    assert render_now_tool_hint(_tool("Edit", status="completed")) == ""
    assert render_now_tool_hint(None) == ""


def test_footer_renders_now_tool_hint_for_running_tool():
    state = CardState(
        blocks=(_tool("Bash", tool_input='{"command": "uv run pytest"}'),),
        footer=FooterState(status="tool_running", status_text="执行中"),
    )

    elements = render_footer(state)

    assert any("⚙ Bash · 执行 uv run pytest" in el.get("content", "") for el in elements)


def test_footer_renders_subagent_badge_when_enabled():
    state = CardState(
        footer=FooterState(status="thinking", status_text="执行中"),
        metadata=CardMetadata(
            tool_name="Aiden",
            model_name="claude-haiku-4-5",
            is_subagent=True,
            parent_card_seq="5",
        ),
    )

    elements = render_footer(state)

    assert any("🧬 sub · model: claude-haiku-4-5 · tool: Aiden · from #5" in el.get("content", "") for el in elements)


def test_subagent_badge_helper_is_blank_for_main_card():
    assert render_subagent_badge(CardMetadata(tool_name="Codex")) == ""


def test_build_footer_atoms_includes_running_tool_and_subagent_badge():
    state = CardState(
        blocks=(_tool("Edit", tool_input='{"path": "src/card/render/footer.py"}'),),
        footer=FooterState(status="tool_running", status_text="执行中"),
        metadata=CardMetadata(
            tool_name="Aiden",
            model_name="claude-haiku-4-5",
            is_subagent=True,
            parent_card_seq="5",
        ),
    )

    atoms = build_footer_atoms(state)

    assert [atom.content for atom in atoms] == [
        "⚙ Edit · 写入 src/card/render/footer.py",
        "🧬 sub · model: claude-haiku-4-5 · tool: Aiden · from #5",
    ]
