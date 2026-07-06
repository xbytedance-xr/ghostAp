from __future__ import annotations

from unittest.mock import MagicMock


def test_workflow_model_lookup_uses_short_ttl_cache(monkeypatch):
    from src.feishu.handlers.workflow import WorkflowHandler

    calls: list[tuple[str, str, str]] = []

    class FakeDiscovery:
        def get_available_tools(self):
            return [{"tool_name": "traex", "provider": "acp"}]

        def get_models_for_tool(self, tool_name, provider="ttadk", cwd=None, current_model=None):
            calls.append((tool_name, provider, cwd or ""))
            return [{"name": "openrouter-3o/low", "display_name": "openrouter-3o/low"}]

    monkeypatch.setattr(
        "src.worktree_engine.tool_discovery.WorktreeToolDiscovery",
        FakeDiscovery,
    )

    handler = WorkflowHandler.__new__(WorkflowHandler)
    first = handler._get_workflow_models_for_tool("traex", "/repo")
    second = handler._get_workflow_models_for_tool("traex", "/repo")

    assert first == second
    assert calls == [("traex", "acp", "/repo")]


def test_workflow_ttdak_model_selection_does_not_force_refresh(monkeypatch):
    from src.worktree_engine.tool_discovery import WorktreeToolDiscovery

    captured = {}

    class FakeManager:
        def get_models(self, *, tool_name, cwd=None, force_refresh=False):
            captured["force_refresh"] = force_refresh
            model = MagicMock()
            model.name = "doubao-seed"
            model.friendly_name = None
            model.display_name = None
            model.is_default = False
            result = MagicMock()
            result.models = [model]
            result.warnings = []
            result.source = "cache"
            return result

    monkeypatch.setattr(
        "src.worktree_engine.tool_discovery.get_ttadk_manager",
        lambda: FakeManager(),
    )

    models = WorktreeToolDiscovery().get_models_for_tool(
        "doubao",
        provider="ttadk",
        cwd="/repo",
    )

    assert captured["force_refresh"] is False
    assert models == [{"name": "doubao-seed", "display_name": "doubao-seed", "is_default": False}]
