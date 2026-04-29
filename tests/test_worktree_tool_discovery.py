"""Unit tests for WorktreeToolDiscovery (extracted from WorktreeManager)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.worktree_engine.tool_discovery import WorktreeToolDiscovery


def _make_acp_tool(name, description="test"):
    return SimpleNamespace(name=name, description=description)


def _make_ttadk_tool(name, description=None, skip_model_selection=False):
    return SimpleNamespace(name=name, description=description, skip_model_selection=skip_model_selection)


def test_returns_acp_tools_when_available():
    """ACP tools with shutil.which available should appear in results."""
    discovery = WorktreeToolDiscovery()
    tools = [_make_acp_tool("coco", "ACP Coco")]

    with patch("src.worktree_engine.tool_discovery.list_acp_tools", return_value=tools), \
         patch("src.worktree_engine.tool_discovery.shutil.which", return_value="/usr/bin/coco"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    assert len(result) >= 1
    coco = next(t for t in result if t["tool_name"] == "coco")
    assert coco["provider"] == "acp"
    assert coco["display_name"] == "Coco"


def test_skips_duplicate_tool_names():
    """Tools with the same name from different providers should be deduplicated."""
    discovery = WorktreeToolDiscovery()
    acp_tools = [_make_acp_tool("mytool")]

    ttadk_mgr = MagicMock()
    ttadk_result = SimpleNamespace(tools=[_make_ttadk_tool("mytool", "TTADK mytool")])
    ttadk_mgr.get_tools.return_value = ttadk_result

    with patch("src.worktree_engine.tool_discovery.list_acp_tools", return_value=acp_tools), \
         patch("src.worktree_engine.tool_discovery.shutil.which", return_value="/usr/bin/mytool"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    mytool_entries = [t for t in result if t["tool_name"] == "mytool"]
    assert len(mytool_entries) == 1, "Duplicate tool names should be deduplicated"


def test_returns_ttadk_as_single_aggregate_entry():
    """TTADK tools should be hidden behind a single aggregate entry in the top-level list."""
    discovery = WorktreeToolDiscovery()
    acp_tools = [_make_acp_tool("coco", "ACP Coco")]

    ttadk_mgr = MagicMock()
    ttadk_mgr.get_tools.return_value = SimpleNamespace(
        tools=[
            _make_ttadk_tool("coco", "TTADK coco"),
            _make_ttadk_tool("claude", "TTADK claude"),
        ]
    )

    with patch("src.worktree_engine.tool_discovery.list_acp_tools", return_value=acp_tools), \
         patch("src.worktree_engine.tool_discovery.shutil.which", return_value="/usr/bin/tool"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    assert any(
        t["provider"] == "ttadk" and t["tool_name"] == "ttadk"
        for t in result
    )
    assert not any(
        t["provider"] == "ttadk" and t["tool_name"] in {"coco", "claude"}
        for t in result
    )


def test_get_ttadk_tools_returns_concrete_ttadk_tools():
    """TTADK concrete tools should still be available from the TTADK-only discovery path."""
    discovery = WorktreeToolDiscovery()
    ttadk_mgr = MagicMock()
    ttadk_mgr.get_tools.return_value = SimpleNamespace(
        tools=[
            _make_ttadk_tool("coco", "TTADK coco"),
            _make_ttadk_tool("claude", "TTADK claude", skip_model_selection=True),
        ]
    )

    with patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        result = discovery.get_ttadk_tools()

    assert [tool["tool_name"] for tool in result] == ["coco", "claude"]
    assert all(tool["provider"] == "ttadk" for tool in result)
    assert result[1]["skip_model_selection"] is True


def test_get_models_returns_empty_on_error():
    """get_models_for_tool should return [] when the provider raises."""
    discovery = WorktreeToolDiscovery()

    with patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=RuntimeError("fail")):
        result = discovery.get_models_for_tool("coco", provider="ttadk")

    assert result == []


def test_get_models_acp_returns_model_dicts():
    """ACP models should be returned as dicts with name/display_name/is_default."""
    discovery = WorktreeToolDiscovery()
    mock_models = [
        SimpleNamespace(name="gpt-4o", description="GPT-4o", is_default=True),
        SimpleNamespace(name="gpt-3.5", description="GPT-3.5", is_default=False),
    ]

    with patch("src.worktree_engine.tool_discovery.fetch_acp_models", return_value=mock_models):
        result = discovery.get_models_for_tool("coco", provider="acp")

    assert len(result) == 2
    assert result[0]["name"] == "gpt-4o"
    assert result[0]["is_default"] is True
