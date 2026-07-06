from __future__ import annotations


def test_workflow_model_lookup_uses_short_ttl_cache(monkeypatch):
    from src.feishu.handlers.workflow import WorkflowHandler

    calls: list[tuple[str, str, str, bool]] = []

    class FakeDiscovery:
        def get_models_for_tool(
            self,
            tool_name,
            provider="ttadk",
            cwd=None,
            current_model=None,
            force_refresh=True,
        ):
            calls.append((tool_name, provider, cwd or "", force_refresh))
            return [{"name": "openrouter-3o/low", "display_name": "openrouter-3o/low"}]

    monkeypatch.setattr(
        "src.worktree_engine.tool_discovery.WorktreeToolDiscovery",
        FakeDiscovery,
    )

    handler = WorkflowHandler.__new__(WorkflowHandler)
    first = handler._get_workflow_models_for_tool("traex", "/repo")
    second = handler._get_workflow_models_for_tool("traex", "/repo")

    assert first == second
    assert calls == [("traex", "acp", "/repo", False)]


def test_workflow_model_lookup_does_not_rescan_all_tools(monkeypatch):
    from src.feishu.handlers.workflow import WorkflowHandler

    scans: list[str] = []
    calls: list[tuple[str, str, str, bool]] = []

    class FakeDiscovery:
        def get_available_tools(self):
            scans.append("called")
            return [{"tool_name": "traex", "provider": "ttadk"}]

        def get_models_for_tool(
            self,
            tool_name,
            provider="ttadk",
            cwd=None,
            current_model=None,
            force_refresh=True,
        ):
            calls.append((tool_name, provider, cwd or "", force_refresh))
            return [{"name": "openrouter-3o/low", "display_name": "openrouter-3o/low"}]

    monkeypatch.setattr(
        "src.worktree_engine.tool_discovery.WorktreeToolDiscovery",
        FakeDiscovery,
    )

    handler = WorkflowHandler.__new__(WorkflowHandler)
    models = handler._get_workflow_models_for_tool("traex", "/repo")

    assert models == [{"name": "openrouter-3o/low", "display_name": "openrouter-3o/low", "description": ""}]
    assert scans == []
    assert calls == [("traex", "acp", "/repo", False)]


def test_workflow_ttdak_model_selection_does_not_force_refresh(monkeypatch):
    from src.feishu.handlers.workflow import WorkflowHandler

    calls: list[tuple[str, str, str, bool]] = []

    class FakeDiscovery:
        def get_models_for_tool(
            self,
            tool_name,
            provider="ttadk",
            cwd=None,
            current_model=None,
            force_refresh=True,
        ):
            calls.append((tool_name, provider, cwd or "", force_refresh))
            return [{"name": "doubao-seed", "display_name": "doubao-seed"}]

    monkeypatch.setattr(
        "src.worktree_engine.tool_discovery.WorktreeToolDiscovery",
        FakeDiscovery,
    )

    handler = WorkflowHandler.__new__(WorkflowHandler)
    models = handler._get_workflow_models_for_tool(
        "doubao",
        "/repo",
    )

    assert calls == [("doubao", "ttadk", "/repo", False)]
    assert models == [{"name": "doubao-seed", "display_name": "doubao-seed", "description": ""}]
