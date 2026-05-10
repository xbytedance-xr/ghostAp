from pathlib import Path

from src.card.render.header import render_header
from src.card.state.models import CardMetadata, CardState, HeaderState
from src.card.state.runtime_stats import RuntimeStats


def test_header_v2_first_row_contains_project_tool_sequence_and_model():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            model_name="claude-opus-4-7",
            card_sequence=3,
            working_dir=str(Path.home() / "workspaces/aiwork/ghostAp"),
        ),
    )

    result = render_header(state)

    assert result["title"]["content"] == "📁 ghostAp · 🤖 Coco · #3 · claude-opus-4-7"


def test_header_v2_second_row_contains_directory_and_elapsed_from_runtime_stats():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            working_dir=str(Path.home() / "workspaces/aiwork/ghostAp"),
        ),
    )
    object.__setattr__(state, "runtime_stats", RuntimeStats(elapsed_seconds=252.0))

    result = render_header(state)

    assert "~/workspaces/aiwork/ghostAp" in result["subtitle"]["content"]
    assert "4m12s" in result["subtitle"]["content"]


def test_header_v2_subagent_uses_parent_reference_instead_of_path():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="aiden",
            card_sequence="5.a",
            is_subagent=True,
            parent_card_seq="5",
            working_dir=str(Path.home() / "p"),
        ),
    )

    result = render_header(state)

    assert "↳ from #5" in result["subtitle"]["content"]
    assert "~/p" not in result["subtitle"]["content"]


def test_header_v2_frozen_shows_archived_state_and_final_elapsed():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            card_sequence=3,
            frozen=True,
            frozen_total_elapsed=422.0,
        ),
    )

    result = render_header(state)

    assert "已封存" in result["title"]["content"]
    assert result["template"] == "grey"
    assert "⏸ final 7m02s" in result["subtitle"]["content"]
