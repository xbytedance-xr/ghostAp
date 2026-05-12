import re
from pathlib import Path

import pytest

import src.card.render.header as header_module
from src.card.render.header import render_header
from src.card.state.models import CardMetadata, CardState, HeaderState
from src.card.state.runtime_stats import RuntimeStats

# AC-1 pattern: 📁 {project} · 🤖 {tool} · #{seq} with optional model suffix and 已封存
_AC1_PATTERN = re.compile(r"^📁 .+ · 🤖 .+ · #\S+( · .+)?$")


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


def test_header_v2_includes_iteration_task_and_page_context():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            card_sequence="3.2",
            unit_kind="task",
            unit_id="2",
            unit_label="修复紧凑工具摘要",
            iteration_index=3,
            iteration_total=10,
            working_dir=str(Path.home() / "workspaces/aiwork/ghostAp"),
        ),
    )

    result = render_header(state, page_index=1, total_pages=3)

    assert result["title"]["content"] == (
        "📁 ghostAp · 🤖 Coco · 第 3/10 轮 · 任务 2: 修复紧凑工具摘要 · #3.2 · 页 2/3"
    )


def test_header_v2_includes_subagent_task_context():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="aiden",
            card_sequence="3.a",
            unit_kind="subagent",
            unit_id="2a",
            unit_label="排查渲染边界",
            iteration_index=3,
            is_subagent=True,
            parent_card_seq="3",
            working_dir=str(Path.home() / "workspaces/aiwork/ghostAp"),
        ),
    )

    result = render_header(state)

    assert "第 3 轮" in result["title"]["content"]
    assert "子任务 2a: 排查渲染边界" in result["title"]["content"]
    assert result["template"] == "orange"


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

    # working_dir is now in footer, not subtitle; subtitle only has marker + cumulative time
    assert "4m12s" in result["subtitle"]["content"]
    assert "~/workspaces/aiwork/ghostAp" not in result["subtitle"]["content"]


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
    assert result["template"] == "orange"


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


def test_header_v2_frozen_uses_frozen_frame_constant():
    """Frozen card subtitle marker comes from FROZEN_FRAME (⏸), not a hardcoded string."""
    from src.card.render.live_ticker import FROZEN_FRAME

    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            card_sequence=1,
            frozen=True,
            frozen_total_elapsed=60.0,
        ),
    )

    result = render_header(state)

    assert FROZEN_FRAME in result["subtitle"]["content"]


def test_header_v2_frozen_hides_model_name():
    """v2 design: frozen cards show 已封存 but hide model_name (mutual exclusion)."""
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            model_name="claude-opus-4-7",
            card_sequence=3,
            frozen=True,
            frozen_total_elapsed=100.0,
        ),
    )

    result = render_header(state)
    title = result["title"]["content"]

    assert "已封存" in title
    assert "claude-opus-4-7" not in title


def test_header_v2_continuation_shows_card_elapsed_and_cumulative(monkeypatch):
    monkeypatch.setattr(header_module.time, "monotonic", lambda: 550.0)
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            card_sequence=2,
            continuation_seq=1,
            session_started_at=100.0,
        ),
    )
    object.__setattr__(state, "runtime_stats", RuntimeStats(elapsed_seconds=12.0))

    result = render_header(state)

    # Subtitle now only shows cumulative time (from session_started_at)
    assert "7m30s" in result["subtitle"]["content"]
    # Per-card elapsed (0m12s) and "累计" label are no longer in subtitle
    assert "0m12s" not in result["subtitle"]["content"]
    assert "累计" not in result["subtitle"]["content"]


def test_engine_header_keeps_legacy_phase_when_only_tool_name_present():
    state = CardState(
        header=HeaderState(title="Deep · 执行中", subtitle="phase: analyze", template="purple"),
        metadata=CardMetadata(engine_type="deep", tool_name="DeepEngine"),
    )

    result = render_header(state)

    assert result["title"]["content"] == "Deep · 执行中"
    assert result["subtitle"]["content"] == "phase: analyze"
    assert result["template"] == "purple"


def test_engine_header_uses_v2_when_session_started_for_first_card():
    state = CardState(
        header=HeaderState(title="Deep · 执行中", subtitle="phase: analyze", template="purple"),
        metadata=CardMetadata(engine_type="deep", tool_name="DeepEngine", session_started_at=123.0),
    )

    result = render_header(state)

    assert "#1" in result["title"]["content"]
    # Engine subtitle (phase info) is now in footer, not header subtitle
    assert "phase: analyze" not in result["subtitle"]["content"]


# ---------------------------------------------------------------------------
# AC-1 regex pattern validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "project, tool, seq, model, frozen",
    [
        ("ghostAp", "coco", 3, "claude-opus-4-7", False),
        ("myProj", "claude", 1, None, False),
        ("demo", "gemini", 10, "gemini-2.5-pro", False),
        ("ghostAp", "coco", 3, "claude-opus-4-7", True),
    ],
    ids=["with-model", "no-model", "different-tool", "frozen"],
)
def test_ac1_title_matches_pattern(project, tool, seq, model, frozen):
    """AC-1: active card title.content matches 📁 {project} · 🤖 {tool} · #{seq} pattern."""
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name=project,
            tool_name=tool,
            model_name=model,
            card_sequence=seq,
            frozen=frozen,
            frozen_total_elapsed=100.0 if frozen else None,
            working_dir="/tmp" if not frozen else None,
        ),
    )

    result = render_header(state)
    title_content = result["title"]["content"]

    assert _AC1_PATTERN.match(title_content), (
        f"title.content does not match AC-1 pattern: {title_content!r}"
    )
