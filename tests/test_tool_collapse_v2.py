from src.card.render.tools import render_tool_panel
from src.card.state.models import ContentBlock


def _tool(name, status, *, latest=False):
    return ContentBlock(
        kind="tool_call",
        block_id=f"{name}-{status}",
        tool_name=name,
        status=status,
        tool_input='{"path": "src/app.py"}',
        tool_output="ok",
        tool_summary="ok",
        is_latest_active=latest,
    )


def test_latest_active_tool_is_expanded():
    panel = render_tool_panel(_tool("Edit", "active", latest=True))

    assert panel["expanded"] is True


def test_completed_tool_is_collapsed_even_if_stale_latest_flag_remains():
    panel = render_tool_panel(_tool("Read", "completed", latest=True))

    assert panel["expanded"] is False
    assert "✅" in panel["header"]["title"]["content"]


def test_failed_tool_is_collapsed_with_red_marker():
    panel = render_tool_panel(_tool("Bash", "failed", latest=True))

    assert panel["expanded"] is False
    assert "❌" in panel["header"]["title"]["content"]
