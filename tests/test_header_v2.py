import re
from pathlib import Path

import pytest

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

    assert result["title"]["content"] == "第 3/10 轮"
    assert result["subtitle"]["content"].startswith(
        "任务 2: 修复紧凑工具摘要 · #3.2 · 页 2/3"
    )
    assert "Coco" not in result["title"]["content"]


def test_header_v2_spec_iteration_includes_total_elapsed():
    state = CardState(
        header=HeaderState(title="legacy", template="green"),
        metadata=CardMetadata(
            engine_type="spec",
            mode_name="Spec",
            mode_emoji="📋",
            tool_name="coco",
            model_name="Test-O-New-Thinking",
            iteration_index=5,
            iteration_total=500,
            card_sequence=6,
            session_started_at=100.0,
        ),
        runtime_stats=RuntimeStats(elapsed_seconds=83.0),
    )

    result = render_header(state)

    assert result["title"]["content"] == "第 5/500 轮 · Test-O-New-Thinking · 总耗时 1m23s"
    assert result["subtitle"]["content"] == "#6"


def test_header_v2_spec_archived_iteration_hides_total_elapsed():
    state = CardState(
        header=HeaderState(title="legacy", template="green"),
        terminal="archived",
        metadata=CardMetadata(
            engine_type="spec",
            mode_name="Spec",
            mode_emoji="📋",
            tool_name="coco",
            model_name="Test-O-New-Thinking",
            iteration_index=4,
            iteration_total=500,
            frozen=True,
            frozen_total_elapsed=82.0,
            session_started_at=100.0,
        ),
        runtime_stats=RuntimeStats(elapsed_seconds=83.0),
    )

    result = render_header(state)

    assert result["title"]["content"] == "第 4/500 轮 · 已封存"
    assert "总耗时" not in result["title"]["content"]


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

    assert result["title"]["content"] == "第 3 轮"
    assert "子任务 2a: 排查渲染边界" in result["subtitle"]["content"]
    assert "#3.a" in result["subtitle"]["content"]
    assert result["template"] == "orange"


def test_header_v2_omits_redundant_elapsed_subtitle():
    state = CardState(
        header=HeaderState(title="legacy", template="blue"),
        metadata=CardMetadata(
            project_name="ghostAp",
            tool_name="coco",
            model_name="Test-O-New-Thinking",
            working_dir=str(Path.home() / "workspaces/aiwork/ghostAp"),
        ),
    )

    result = render_header(state)

    assert result["title"]["content"] == "📁 ghostAp · 🤖 Coco · #1 · Test-O-New-Thinking"
    assert "subtitle" not in result


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
    assert "subtitle" not in result


def test_header_v2_frozen_keeps_elapsed_out_of_header():
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

    assert "subtitle" not in result


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


def test_header_v2_continuation_keeps_elapsed_out_of_header():
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

    result = render_header(state)

    assert "subtitle" not in result


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
        runtime_stats=RuntimeStats(elapsed_seconds=3723.0),
    )

    result = render_header(state)

    assert "#1" in result["title"]["content"]
    assert result["subtitle"]["content"] == "总耗时 1时02分03秒"


def test_deep_task_header_subtitle_uses_elapsed_instead_of_repeating_task():
    state = CardState(
        header=HeaderState(title="legacy", template="purple"),
        metadata=CardMetadata(
            engine_type="deep",
            project_name="ghostAp",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            unit_kind="task",
            unit_id="2",
            unit_label="实现 Deep 卡片优化",
            session_started_at=100.0,
        ),
        runtime_stats=RuntimeStats(elapsed_seconds=83.0),
    )

    result = render_header(state)

    assert "任务 2: 实现 Deep 卡片优化" in result["title"]["content"]
    assert result["subtitle"]["content"] == "总耗时 0时01分23秒"


def test_deep_iteration_header_uses_question_summary_and_keeps_page_context():
    state = CardState(
        header=HeaderState(title="legacy", template="violet"),
        metadata=CardMetadata(
            engine_type="deep",
            project_name="ghostAp",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            question_title="优化Deep卡片标题",
            iteration_index=1,
            iteration_total=1,
            session_started_at=100.0,
        ),
        runtime_stats=RuntimeStats(elapsed_seconds=83.0),
    )

    result = render_header(state, page_index=1, total_pages=2)

    assert result["title"]["content"] == "优化Deep卡片标题"
    assert "第 1 轮" not in result["title"]["content"]
    assert result["subtitle"]["content"] == "总耗时 0时01分23秒 · #1 · 页 2/2"


def test_deep_iteration_header_without_question_uses_stable_fallback():
    state = CardState(
        header=HeaderState(title="legacy", template="violet"),
        metadata=CardMetadata(
            engine_type="deep",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            iteration_index=1,
            iteration_total=1,
            session_started_at=100.0,
        ),
    )

    result = render_header(state)

    assert result["title"]["content"] == "Deep 任务"
    assert "第 1 轮" not in result["title"]["content"]


def test_deep_header_uses_question_summary_before_iteration_starts():
    state = CardState(
        header=HeaderState(title="legacy", template="violet"),
        metadata=CardMetadata(
            engine_type="deep",
            project_name="ghostAp",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            question_title="优化Deep卡片标题",
            session_started_at=100.0,
        ),
    )

    result = render_header(state)

    assert result["title"]["content"] == "优化Deep卡片标题"


def test_deep_failed_header_keeps_question_summary_as_title():
    state = CardState(
        header=HeaderState(title="legacy", template="red"),
        terminal="failed",
        metadata=CardMetadata(
            engine_type="deep",
            project_name="ghostAp",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            question_title="优化Deep卡片标题",
            iteration_index=1,
            iteration_total=1,
            session_started_at=100.0,
        ),
    )

    result = render_header(state)

    assert result["title"]["content"] == "优化Deep卡片标题"
    assert len(result["title"]["content"]) <= 15
    assert result["template"] == "red"


def test_deep_frozen_page_does_not_repeat_question_in_subtitle():
    state = CardState(
        header=HeaderState(title="legacy", template="grey"),
        terminal="archived",
        metadata=CardMetadata(
            engine_type="deep",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            question_title="优化Deep卡片标题",
            iteration_index=1,
            iteration_total=1,
            frozen=True,
            session_started_at=100.0,
        ),
    )

    result = render_header(state, page_index=0, total_pages=2)

    assert result["title"]["content"] == "优化Deep卡片标题 · 已封存"
    assert result["subtitle"]["content"] == "#1 · 页 1/2"


def test_deep_header_rebounds_restored_question_title_to_15_chars():
    state = CardState(
        header=HeaderState(title="legacy", template="violet"),
        metadata=CardMetadata(
            engine_type="deep",
            mode_name="Deep",
            mode_emoji="🧠",
            tool_name="Coco",
            question_title="优化Deep模式消息卡片标题并展示用户问题",
            session_started_at=100.0,
        ),
    )

    result = render_header(state)

    assert result["title"]["content"] == "优化Deep模式消息卡片标题…"
    assert len(result["title"]["content"]) <= 15


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
