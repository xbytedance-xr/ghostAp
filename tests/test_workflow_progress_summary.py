"""Tests for phase summary, collapsible panel headers, and completion card layout."""

from __future__ import annotations

from src.workflow_engine.models import (
    AgentProgress,
    AgentStatus,
    PhaseProgress,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.renderer import (
    _PHASE_COMPLETED_TAIL,
    WorkflowProgressRenderer,
    render_completion_card,
)


def _make_agent(
    label: str, status: AgentStatus, *, tool: str = "coco", error: str | None = None, duration_s: float = 1.0
) -> AgentProgress:
    return AgentProgress(
        label=label,
        tool=tool,
        status=status,
        duration_s=duration_s,
        error=error,
    )


def _make_project(phase_title: str, agents: list[AgentProgress]) -> WorkflowProject:
    return WorkflowProject(
        name="test",
        phases=[PhaseProgress(title=phase_title, agents=agents, started_at=1000.0)],
    )


def _flatten_text(elements: list[dict]) -> str:
    """Recursively extract all string content from card elements (including dict headers)."""
    parts: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        content = el.get("content")
        if isinstance(content, str):
            parts.append(content)
        header = el.get("header")
        if isinstance(header, str):
            parts.append(header)
        elif isinstance(header, dict):
            # Structured collapsible_panel header: {"title": {"tag": ..., "content": ...}, "template": ...}
            title = header.get("title")
            if isinstance(title, dict):
                tc = title.get("content")
                if isinstance(tc, str):
                    parts.append(tc)
        # Column sets / columns recurse into their elements
        nested = el.get("elements")
        if isinstance(nested, list):
            parts.append(_flatten_text(nested))
        columns = el.get("columns")
        if isinstance(columns, list):
            for col in columns:
                if isinstance(col, dict):
                    col_els = col.get("elements")
                    if isinstance(col_els, list):
                        parts.append(_flatten_text(col_els))
    return "\n".join(parts)


def test_phase_header_has_completed_summary_with_large_phase() -> None:
    """Large phase (25 agents) — 已完成 M/N appears near header."""
    agents: list[AgentProgress] = []
    agents += [_make_agent(f"running-{i}", AgentStatus.RUNNING) for i in range(2)]
    agents.append(_make_agent("failed-0", AgentStatus.FAILED, error="boom"))
    agents += [_make_agent(f"done-{i}", AgentStatus.DONE) for i in range(18)]
    agents += [_make_agent(f"cached-{i}", AgentStatus.CACHED) for i in range(3)]
    agents.append(_make_agent("pending-0", AgentStatus.PENDING))

    project = _make_project("Large Phase", agents)
    renderer = WorkflowProgressRenderer(project)
    card = renderer.render_progress_card()

    text = _flatten_text(card["elements"])
    # 18 DONE + 3 CACHED = 21 completed / 25 total
    assert "已完成 21/25" in text, f"Expected '已完成 21/25' in: {text[:800]}"


def test_phase_pagination_truncates_done_and_cached() -> None:
    """Ensure DONE/CACHED buckets truncated to _PHASE_COMPLETED_TAIL each."""
    agents: list[AgentProgress] = []
    agents += [_make_agent(f"done-{i}", AgentStatus.DONE) for i in range(20)]
    agents += [_make_agent(f"cached-{i}", AgentStatus.CACHED) for i in range(5)]
    agents.append(_make_agent("running-0", AgentStatus.RUNNING))

    project = _make_project("Big Phase", agents)
    renderer = WorkflowProgressRenderer(project)
    card = renderer.render_progress_card()

    text = _flatten_text(card["elements"])

    # Find "已完成 (N)" and "缓存 (M)" collapsible panel headers and verify N, M ≤ tail
    done_panel_header_text: str | None = None
    cached_panel_header_text: str | None = None
    for el in card["elements"]:
        if isinstance(el, dict) and el.get("tag") == "collapsible_panel":
            header = el.get("header", {})
            title_content = ""
            if isinstance(header, dict):
                title = header.get("title", {})
                if isinstance(title, dict):
                    title_content = str(title.get("content", ""))
            elif isinstance(header, str):
                title_content = header
            if title_content.startswith("已完成 ("):
                done_panel_header_text = title_content
            elif title_content.startswith("缓存 ("):
                cached_panel_header_text = title_content

    assert done_panel_header_text is not None, f"No 已完成 panel found. text: {text[:800]}"
    assert cached_panel_header_text is not None, f"No 缓存 panel found. text: {text[:800]}"

    # Parse the count N from "已完成 (N)"
    done_shown = int(done_panel_header_text.split("(")[1].rstrip(")"))
    cached_shown = int(cached_panel_header_text.split("(")[1].rstrip(")"))
    assert done_shown <= _PHASE_COMPLETED_TAIL, f"Expected ≤ {_PHASE_COMPLETED_TAIL} done shown, got {done_shown}"
    assert cached_shown <= _PHASE_COMPLETED_TAIL, f"Expected ≤ {_PHASE_COMPLETED_TAIL} cached shown, got {cached_shown}"

    # A "共 X 条" counter line should exist for hidden entries
    assert "共" in text and "条" in text and "已完成/缓存" in text, f"Expected counter line not found in: {text[:800]}"


def test_small_phase_renders_everything() -> None:
    """Small phase (5 agents, ≤ 8) — render everything unchanged."""
    agents: list[AgentProgress] = [
        _make_agent("a-0", AgentStatus.DONE),
        _make_agent("a-1", AgentStatus.DONE),
        _make_agent("a-2", AgentStatus.RUNNING),
        _make_agent("a-3", AgentStatus.PENDING),
        _make_agent("a-4", AgentStatus.FAILED, error="fail"),
    ]
    project = _make_project("Small Phase", agents)
    renderer = WorkflowProgressRenderer(project)
    card = renderer.render_progress_card()
    text = _flatten_text(card["elements"])

    assert "已完成 2/5" in text

    # Small phases should not have "条已完成/缓存（已折叠）" counter line
    assert "条已完成/缓存（已折叠）" not in text


def test_empty_phase_renders_summary_zero_over_zero() -> None:
    """Started empty phase — renders an explicit 0/0 in-progress state."""
    project = _make_project("Empty Phase", [])
    renderer = WorkflowProgressRenderer(project)
    card = renderer.render_progress_card()
    text = _flatten_text(card["elements"])

    assert "进行中 0/0" in text


# ---------------------------------------------------------------------------
# Completion card: stats column_set should be stretch + centered text
# ---------------------------------------------------------------------------


def _make_completion_project(status) -> WorkflowProject:
    return WorkflowProject(
        name="audit",
        status=status,
        started_at=1_700_000_000.0,
        finished_at=1_700_000_060.0,
        phases=[
            PhaseProgress(
                title="Analyze",
                agents=[
                    AgentProgress(label="scan", tool="coco", status=AgentStatus.DONE, duration_s=5.0),
                    AgentProgress(label="verify", tool="claude", status=AgentStatus.DONE, duration_s=5.0),
                ],
            )
        ],
    )


def test_completion_card_stats_use_stretch_flex_mode() -> None:
    """Stats column_sets in render_completion_card should use flex_mode='stretch'."""
    project = _make_completion_project(WorkflowStatus.COMPLETED)
    card = render_completion_card(project)
    stats_column_sets = [el for el in card["elements"] if isinstance(el, dict) and el.get("tag") == "column_set"]
    assert stats_column_sets, "Expected at least one column_set in completion card"
    for cs in stats_column_sets:
        assert cs.get("flex_mode") == "stretch", f"Expected flex_mode='stretch', got {cs.get('flex_mode')!r}"


def test_completion_card_stats_columns_centered_text() -> None:
    """Stat column markdown elements should set text_align='center'."""
    project = _make_completion_project(WorkflowStatus.COMPLETED)
    card = render_completion_card(project)
    stats_column_sets = [el for el in card["elements"] if isinstance(el, dict) and el.get("tag") == "column_set"]
    assert stats_column_sets, "Expected stats column_sets"
    for cs in stats_column_sets:
        for col in cs.get("columns", []):
            for inner in col.get("elements", []):
                if inner.get("tag") == "markdown":
                    assert inner.get("text_align") == "center", (
                        f"Expected text_align='center' on markdown element, got {inner}"
                    )


# ---------------------------------------------------------------------------
# Phase collapsible-panel headers: structured with per-status color
# ---------------------------------------------------------------------------


def test_phase_collapsible_panel_headers_are_structured_with_border_colors() -> None:
    """Feishu accepts colors on panel border, not collapsible header.template."""
    agents: list[AgentProgress] = [
        _make_agent("r-0", AgentStatus.RUNNING),
        _make_agent("f-0", AgentStatus.FAILED, error="err"),
        _make_agent("d-0", AgentStatus.DONE),
        _make_agent("c-0", AgentStatus.CACHED),
        _make_agent("p-0", AgentStatus.PENDING),
    ]
    project = _make_project("Mixed Phase", agents)
    renderer = WorkflowProgressRenderer(project)
    card = renderer.render_progress_card()

    panels = [el for el in card["elements"] if isinstance(el, dict) and el.get("tag") == "collapsible_panel"]
    assert len(panels) >= 5, f"Expected 5 collapsible panels, got {len(panels)}"

    expected_colors = {
        "执行中": "blue",
        "失败": "red",
        "已完成": "green",
        "缓存": "turquoise",
        "待执行": "grey",
    }
    found_labels: set[str] = set()
    for panel in panels:
        header = panel.get("header")
        assert isinstance(header, dict), f"Expected dict header, got {type(header)}: {header}"
        title = header.get("title")
        assert isinstance(title, dict), f"Expected dict title, got {title}"
        assert title.get("tag") == "plain_text"
        content = str(title.get("content", ""))
        assert "template" not in header, f"collapsible_panel.header.template is invalid: {header}"
        border = panel.get("border")
        assert isinstance(border, dict), f"Expected dict border, got {border}"
        # Match the prefix label to verify the color mapping
        for prefix, color in expected_colors.items():
            if content.startswith(prefix):
                found_labels.add(prefix)
                assert border.get("color") == color, (
                    f"Expected border color '{color}' for {prefix!r}, got {border.get('color')}"
                )
    for expected_prefix in expected_colors:
        assert expected_prefix in found_labels, f"Missing panel for {expected_prefix!r}"
