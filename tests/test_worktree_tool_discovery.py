"""Unit tests for WorktreeToolDiscovery (extracted from WorktreeManager)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.ttadk.models import ModelListResult, TTADKModel
from src.worktree_engine.tool_discovery import WorktreeToolDiscovery


def _make_ttadk_tool(name, description=None, skip_model_selection=False):
    return SimpleNamespace(name=name, description=description, skip_model_selection=skip_model_selection)


def test_returns_tools_when_binary_available():
    """Known tools with shutil.which available should appear in results."""
    discovery = WorktreeToolDiscovery()

    def _which(name):
        return f"/usr/bin/{name}" if name == "coco" else None

    with patch("src.worktree_engine.tool_discovery.shutil.which", side_effect=_which), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    assert len(result) == 1
    coco = result[0]
    assert coco["tool_name"] == "coco"
    assert coco["provider"] == "cli"  # no ACP provider registered
    assert coco["display_name"] == "Coco"


def test_get_available_tools_triggers_acp_provider_lazy_init():
    """tool_registry 是 lazy 注册的；worktree 入口必须先调用 get_providers() 触发，
    否则 ACP 工具会被错判为 CLI 模式（supports_model=False）。
    """
    discovery = WorktreeToolDiscovery()

    with patch("src.worktree_engine.tool_discovery.shutil.which", return_value=None), \
         patch("src.worktree_engine.tool_discovery.get_providers") as mock_get, \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.return_value = None
        discovery.get_available_tools()

    mock_get.assert_called_once()


def test_uses_acp_provider_when_registered():
    """Tools with registered ACP provider should use provider='acp' and support models."""
    discovery = WorktreeToolDiscovery()

    def _which(name):
        return f"/usr/bin/{name}" if name == "coco" else None

    mock_provider = MagicMock()
    mock_provider.skip_model_selection = False

    with patch("src.worktree_engine.tool_discovery.shutil.which", side_effect=_which), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.side_effect = lambda name: mock_provider if name == "coco" else None
        mock_reg.get_availability.side_effect = lambda name, **_kwargs: name == "coco"
        result = discovery.get_available_tools()

    assert len(result) == 1
    coco = result[0]
    assert coco["provider"] == "acp"
    assert coco["supports_model"] is True
    assert coco["model_optional"] is True


def test_uses_available_acp_provider_even_when_binary_not_on_path():
    """ACP-backed tools should appear when the provider is available without a direct binary path."""
    discovery = WorktreeToolDiscovery()

    def _provider_for(name):
        if name in {"aiden", "codex", "traex"}:
            provider = MagicMock()
            provider.get_fallback_command.return_value = None
            return provider
        return None

    def _available(name, **_kwargs):
        return name in {"aiden", "codex", "traex"}

    with patch("src.worktree_engine.tool_discovery.shutil.which", return_value=None), \
         patch("src.worktree_engine.tool_discovery.get_providers"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.side_effect = _provider_for
        mock_reg.get_availability.side_effect = _available
        result = discovery.get_available_tools()

    names = [tool["tool_name"] for tool in result]
    assert names == ["aiden", "codex", "traex"]
    assert all(tool["provider"] == "acp" for tool in result)
    assert all(tool["supports_model"] is True for tool in result)


def test_returns_ttadk_as_single_aggregate_entry():
    """TTADK tools should be hidden behind a single aggregate entry in the top-level list."""
    discovery = WorktreeToolDiscovery()

    ttadk_mgr = MagicMock()
    ttadk_mgr.get_tools.return_value = SimpleNamespace(
        tools=[
            _make_ttadk_tool("coco", "TTADK coco"),
            _make_ttadk_tool("claude", "TTADK claude"),
        ]
    )

    with patch("src.worktree_engine.tool_discovery.shutil.which", return_value="/usr/bin/tool"), \
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


def test_top_level_tools_keep_native_entries_and_ttadk_at_same_level():
    """Top-level list should keep native tool entries while exposing TTADK as a sibling entry."""
    discovery = WorktreeToolDiscovery()
    ttadk_mgr = MagicMock()
    ttadk_mgr.get_tools.return_value = SimpleNamespace(
        tools=[
            _make_ttadk_tool("coco", "TTADK coco"),
            _make_ttadk_tool("claude", "TTADK claude"),
        ]
    )

    with patch("src.worktree_engine.tool_discovery.shutil.which", return_value="/usr/bin/tool"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    names = [tool["tool_name"] for tool in result]
    assert names == ["coco", "aiden", "codex", "claude", "traex", "ttadk"]


def test_top_level_tools_prioritize_coco_aiden_codex_claude_before_ttadk():
    """Top-level tool ordering should follow product entry priority instead of discovery order."""
    discovery = WorktreeToolDiscovery()
    ttadk_mgr = MagicMock()
    ttadk_mgr.get_tools.return_value = SimpleNamespace(
        tools=[_make_ttadk_tool("claude", "TTADK claude")]
    )

    with patch("src.worktree_engine.tool_discovery.shutil.which", return_value="/usr/bin/tool"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    assert [tool["tool_name"] for tool in result] == [
        "coco",
        "aiden",
        "codex",
        "claude",
        "traex",
        "ttadk",
    ]


def test_gemini_binary_is_not_exposed_as_top_level_tool():
    """Gemini remains a programming backend but is not a WT/Spec review selector candidate."""
    discovery = WorktreeToolDiscovery()

    def _which(name):
        return "/usr/bin/gemini" if name == "gemini" else None

    with patch("src.worktree_engine.tool_discovery.shutil.which", side_effect=_which), \
         patch("src.worktree_engine.tool_discovery.get_providers"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    assert [tool["tool_name"] for tool in result] == []


def test_only_shows_tools_with_binary_in_path():
    """Tools whose binary is not found via shutil.which should be excluded unless ACP provider is available."""
    discovery = WorktreeToolDiscovery()

    def _which(name):
        # Only claude and codex binaries available
        return f"/usr/bin/{name}" if name in ("claude", "codex") else None

    with patch("src.worktree_engine.tool_discovery.shutil.which", side_effect=_which), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.return_value = None
        result = discovery.get_available_tools()

    names = [t["tool_name"] for t in result]
    assert names == ["codex", "claude"]


def test_traex_binary_is_exposed_as_top_level_acp_tool():
    """Traex is a first-class Worktree ACP tool candidate."""
    discovery = WorktreeToolDiscovery()

    def _which(name):
        return "/usr/bin/traex" if name == "traex" else None

    mock_provider = MagicMock()
    mock_provider.get_fallback_command.return_value = None

    with patch("src.worktree_engine.tool_discovery.shutil.which", side_effect=_which), \
         patch("src.worktree_engine.tool_discovery.get_providers"), \
         patch("src.worktree_engine.tool_discovery.tool_registry") as mock_reg, \
         patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=Exception("skip")):
        mock_reg.get_provider.side_effect = lambda name: mock_provider if name == "traex" else None
        mock_reg.get_availability.side_effect = lambda name, **_kwargs: name == "traex"
        result = discovery.get_available_tools()

    assert result == [
        {
            "provider": "acp",
            "tool_name": "traex",
            "display_name": "Traex",
            "description": "TRAE CLI",
            "supports_model": True,
            "model_optional": True,
            "skip_model_selection": False,
            "agent_name": "",
        }
    ]


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
    assert [tool["display_name"] for tool in result] == ["coco", "claude"]
    assert all(tool["agent_name"] == "ttadk" for tool in result)
    assert all(tool["provider"] == "ttadk" for tool in result)
    assert result[1]["skip_model_selection"] is True


def test_get_models_returns_empty_on_error():
    """get_models_for_tool should return [] when the provider raises."""
    discovery = WorktreeToolDiscovery()

    with patch("src.worktree_engine.tool_discovery.get_ttadk_manager", side_effect=RuntimeError("fail")):
        result = discovery.get_models_for_tool("coco", provider="ttadk")

    assert result == []


def test_get_models_ttadk_filters_untrusted_default_models():
    """Untrusted TTADK default fallback models should not be shown in /wt model selection."""
    discovery = WorktreeToolDiscovery()
    ttadk_mgr = MagicMock()
    ttadk_mgr.get_models.return_value = ModelListResult(
        models=[
            TTADKModel(name="gpt-5.2", description="GPT-5.2"),
            TTADKModel(name="claude-3-opus", description="Claude 3 Opus"),
        ],
        source="defaults",
        warnings=["models_untrusted"],
    )

    with patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        result = discovery.get_models_for_tool("gemini", provider="ttadk")

    assert result == []


def test_get_models_ttadk_forces_fresh_fetch_to_avoid_stale_cache():
    """Worktree TTADK model picker should force refresh instead of trusting stale cache names."""
    discovery = WorktreeToolDiscovery()
    ttadk_mgr = MagicMock()
    ttadk_mgr.get_models.return_value = ModelListResult(
        models=[TTADKModel(name="glm-5", description="glm-5")],
        source="probe",
        warnings=[],
    )

    with patch("src.worktree_engine.tool_discovery.get_ttadk_manager", return_value=ttadk_mgr):
        result = discovery.get_models_for_tool("claude", provider="ttadk", cwd="/tmp/demo")

    ttadk_mgr.get_models.assert_called_once_with(
        tool_name="claude",
        cwd="/tmp/demo",
        force_refresh=True,
    )
    assert result == [
        {"name": "glm-5", "display_name": "glm-5", "is_default": False}
    ]


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


def test_get_models_acp_preserves_codex_effort_capabilities_without_expansion():
    from src.ttadk.models import ACPModelOption

    discovery = WorktreeToolDiscovery()
    mock_models = [
        ACPModelOption(
            name="gpt-5.6-sol",
            description="GPT-5.6-Sol",
            is_default=True,
            reasoning_efforts=("low", "high", "max", "ultra"),
            adapted_reasoning_effort="high",
        )
    ]

    with patch(
        "src.worktree_engine.tool_discovery.fetch_acp_models",
        return_value=mock_models,
    ):
        result = discovery.get_models_for_tool("codex", provider="acp")

    assert result == [
        {
            "name": "gpt-5.6-sol",
            "display_name": "gpt-5.6-sol",
            "description": "GPT-5.6-Sol",
            "is_default": True,
            "reasoning_efforts": ["low", "high", "max", "ultra"],
            "adapted_reasoning_effort": "high",
        }
    ]
