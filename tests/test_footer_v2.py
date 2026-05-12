import pytest

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


# --- render_now_tool_hint unit tests ---


def test_now_tool_hint_for_edit_path():
    line = render_now_tool_hint(_tool("Edit", tool_input='{"path": "src/router.py"}'))

    assert line == "⚙ **Edit** · 写入 src/router.py"


def test_now_tool_hint_for_grep_pattern():
    line = render_now_tool_hint(_tool("Grep", tool_input='{"pattern": "def route"}'))

    assert "⚙ **Grep**" in line
    assert "搜索" in line
    assert "def route" in line


def test_now_tool_hint_unknown_tool_falls_back_to_name():
    line = render_now_tool_hint(_tool("MysteryTool"))

    assert line == "⚙ **MysteryTool** · MysteryTool"


def test_now_tool_hint_none_when_no_running_tool():
    assert render_now_tool_hint(_tool("Edit", status="completed")) == ""
    assert render_now_tool_hint(_tool("Edit", status="running")) == ""
    assert render_now_tool_hint(_tool("Edit", status="in_progress")) == ""
    assert render_now_tool_hint(None) == ""


# --- Footer renders ⚙ tool hint for active tools (v2) ---


def test_footer_renders_now_tool_hint_for_running_tool():
    """Footer should show ⚙ **tool** hint when an active tool exists."""
    state = CardState(
        blocks=(_tool("Bash", tool_input='{"command": "uv run pytest"}'),),
        footer=FooterState(status="tool_running", status_text="执行中"),
    )

    elements = render_footer(state)

    assert any("⚙ **Bash**" in el.get("content", "") for el in elements)


def test_frozen_footer_continuation():
    """Frozen card with continuation_seq shows UX-aligned continuation hint."""
    state = CardState(
        footer=FooterState(status="thinking", status_text="执行中"),
        metadata=CardMetadata(frozen=True, continuation_seq=2),
    )

    elements = render_footer(state)

    assert any(
        "本卡已停止更新" in el.get("content", "")
        and "续接" in el.get("content", "")
        and "#3" in el.get("content", "")
        and "↓" in el.get("content", "")
        for el in elements
    )


@pytest.mark.parametrize("seq,expected_next", [(1, "#2"), (2, "#3"), (5, "#6")])
def test_frozen_footer_continuation_boundary(seq, expected_next):
    """Frozen continuation hint produces correct next_seq for boundary values."""
    state = CardState(
        footer=FooterState(status="thinking", status_text="执行中"),
        metadata=CardMetadata(frozen=True, continuation_seq=seq),
    )

    elements = render_footer(state)

    assert any(expected_next in el.get("content", "") for el in elements)


def test_frozen_footer_no_continuation_when_seq_zero():
    """Frozen card with continuation_seq=0 should NOT show continuation hint."""
    state = CardState(
        footer=FooterState(status="thinking", status_text="执行中"),
        metadata=CardMetadata(frozen=True, continuation_seq=0),
    )

    elements = render_footer(state)

    assert not any("本卡已停止更新" in el.get("content", "") for el in elements)


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


def test_build_footer_atoms_includes_tool_hint_and_subagent_badge():
    """build_footer_atoms should include both tool hint and subagent badge (AC-16)."""
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

    assert any("⚙ **Edit**" in atom.content for atom in atoms)
    assert any("🧬 sub" in atom.content for atom in atoms)


def test_build_footer_atoms_frozen_continuation():
    """build_footer_atoms should include frozen continuation hint (AC-11, AC-23)."""
    state = CardState(
        footer=FooterState(status="thinking", status_text="执行中"),
        metadata=CardMetadata(frozen=True, continuation_seq=2),
    )

    atoms = build_footer_atoms(state)

    assert any("本卡已停止更新" in atom.content and "续接" in atom.content and "#3" in atom.content for atom in atoms)


def test_build_footer_atoms_no_tool_hint_when_no_active_tool():
    """build_footer_atoms should not include tool hint when no active tool."""
    state = CardState(
        blocks=(_tool("Edit", status="completed", tool_input='{"path": "a.py"}'),),
        footer=FooterState(status="thinking", status_text="思考中"),
    )

    atoms = build_footer_atoms(state)

    assert not any("⚙" in atom.content for atom in atoms)


def test_footer_no_tool_hint_when_all_tools_completed():
    """Footer should NOT show ⚙ tool hint when all tools are completed."""
    state = CardState(
        blocks=(_tool("Edit", status="completed", tool_input='{"path": "a.py"}'),),
        footer=FooterState(status="thinking", status_text="思考中"),
    )

    elements = render_footer(state)

    assert not any("⚙ **Edit**" in el.get("content", "") for el in elements)


def test_footer_tool_hint_bash_shows_command():
    """Footer ⚙ hint for Bash shows the command."""
    state = CardState(
        blocks=(_tool("Bash", tool_input='{"command": "npm run build"}'),),
        footer=FooterState(status="tool_running", status_text="执行中"),
    )

    elements = render_footer(state)

    hints = [el.get("content", "") for el in elements if "⚙ **Bash**" in el.get("content", "")]
    assert len(hints) == 1
    assert "npm run build" in hints[0]


# --- Context line tests (working_dir + engine phase in footer) ---


def test_footer_context_line_with_working_dir():
    """Footer should show 📂 ~/path when working_dir is set."""
    state = CardState(
        footer=FooterState(status="thinking", status_text="思考中"),
        metadata=CardMetadata(working_dir="/home/user/projects/myapp", engine_type="spec"),
    )

    elements = render_footer(state)

    context_els = [el for el in elements if "📂" in el.get("content", "")]
    assert len(context_els) == 1
    assert "myapp" in context_els[0]["content"]


def test_footer_context_line_absent_when_no_working_dir():
    """Footer should NOT show 📂 context line when working_dir is None."""
    state = CardState(
        footer=FooterState(status="thinking", status_text="思考中"),
        metadata=CardMetadata(engine_type="spec"),
    )

    elements = render_footer(state)

    assert not any("📂" in el.get("content", "") for el in elements)


def test_footer_context_line_includes_engine_subtitle():
    """Footer context line includes engine phase from header subtitle."""
    from src.card.state.models import HeaderState

    state = CardState(
        header=HeaderState(subtitle="cycle 2 / Build"),
        footer=FooterState(status="thinking", status_text="思考中"),
        metadata=CardMetadata(
            working_dir="/home/user/work/ghostAp",
            engine_type="spec",
        ),
    )

    elements = render_footer(state)

    context_els = [el for el in elements if "📂" in el.get("content", "")]
    assert len(context_els) == 1
    assert "cycle 2 / Build" in context_els[0]["content"]


def test_spec_footer_uses_session_start_for_total_elapsed(monkeypatch):
    """Spec footer total runtime should not start at the first progress update."""
    monkeypatch.setattr("src.card.render.footer.time.monotonic", lambda: 220.0)
    state = CardState(
        metadata=CardMetadata(engine_type="spec", session_started_at=100.0),
        footer=FooterState(
            status="thinking",
            status_text="思考中",
            progress="1/3 通过",
            progress_started_at=190.0,
        ),
    )

    elements = render_footer(state)
    content = "\n".join(e.get("content", "") for e in elements)

    assert "已执行 2 分钟 0 秒" in content
    assert "30 秒" not in content


def test_spec_footer_renders_elapsed_even_without_status(monkeypatch):
    """Spec total runtime should make the footer visible by itself."""
    monkeypatch.setattr("src.card.render.footer.time.monotonic", lambda: 130.0)
    state = CardState(
        metadata=CardMetadata(engine_type="spec", session_started_at=100.0),
        footer=FooterState(),
    )

    elements = render_footer(state)
    content = "\n".join(e.get("content", "") for e in elements)

    assert elements[0] == {"tag": "hr"}
    assert "已执行 30 秒" in content
