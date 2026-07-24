"""Tests for display-safe tool-call metadata."""

import pytest

from src.acp.models import ToolCallInfo
from src.card import tool_display
from src.card.tool_display import extract_tool_call_label


def test_agent_tool_name_rejects_escaped_source_fragment():
    call = ToolCallInfo(
        id="call_internal",
        title="agent",
        kind="other",
        status="in_progress",
        content='子代理：\\" not in ordinary_output\\",\\n',
    )

    assert tool_display.extract_agent_tool_name(call) == "agent"


def test_task_label_rejects_opaque_call_identifier():
    call = ToolCallInfo(
        id="call_internal",
        title="task",
        kind="other",
        status="in_progress",
        content="call_usOANvwWFgpuBkmHB",
    )

    assert extract_tool_call_label(call, generic_labels={"task"}) == "子任务"


def test_agent_tool_name_keeps_clean_marker_before_escaped_newline():
    call = ToolCallInfo(
        id="call_internal",
        title="agent",
        kind="other",
        status="in_progress",
        content="子代理：Explore\\nignored metadata",
    )

    assert tool_display.extract_agent_tool_name(call) == "Explore"


def test_agent_tool_name_uses_safe_non_generic_title_before_fallback():
    call = ToolCallInfo(
        id="call_internal",
        title="Review Agent",
        kind="other",
        status="in_progress",
        content="",
    )

    assert tool_display.extract_agent_tool_name(call) == "Review Agent"


def test_agent_tool_name_splits_actual_control_whitespace():
    call = ToolCallInfo(
        id="call_internal",
        title="agent",
        kind="other",
        status="in_progress",
        content="子代理：Explore\tignored metadata",
    )

    assert tool_display.extract_agent_tool_name(call) == "Explore"


def test_agent_tool_name_rejects_inline_raw_json_fragment():
    call = ToolCallInfo(
        id="call_internal",
        title="agent",
        kind="other",
        status="in_progress",
        content='子代理：Explore raw JSON: {"model":"x"}',
    )

    assert tool_display.extract_agent_tool_name(call) == "agent"


def test_task_label_rejects_prefixed_opaque_call_identifier():
    call = ToolCallInfo(
        id="call_internal",
        title="task",
        kind="other",
        status="in_progress",
        content="prefix-call_secret",
    )

    assert extract_tool_call_label(call, generic_labels={"task"}) == "子任务"


def test_task_label_uses_only_first_line_of_json_description():
    call = ToolCallInfo(
        id="call_internal",
        title="task",
        kind="other",
        status="in_progress",
        content='{"description":"修复路由\\nassert false"}',
    )

    assert extract_tool_call_label(call, generic_labels={"task"}) == "修复路由"


def test_task_label_rejects_unterminated_json_fragment():
    call = ToolCallInfo(
        id="call_internal",
        title="task",
        kind="other",
        status="in_progress",
        content='{"description":"Fix card"',
    )

    assert extract_tool_call_label(call, generic_labels={"task"}) == "子任务"


@pytest.mark.parametrize(
    "label",
    [
        "[P0] 修复安全回归",
        "[1] 修复安全回归",
        "支持 raw JSON 输入",
    ],
)
def test_task_label_keeps_bracketed_or_json_named_human_text(label):
    call = ToolCallInfo(
        id="call_internal",
        title="task",
        kind="other",
        status="in_progress",
        content=label,
    )

    assert extract_tool_call_label(call, generic_labels={"task"}) == label
