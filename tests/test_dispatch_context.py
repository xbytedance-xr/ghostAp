from __future__ import annotations

from types import SimpleNamespace


class _ProjectManager:
    def __init__(self):
        self.calls = []

    def get_project_for_chat(self, project_id: str, chat_id: str):
        self.calls.append((project_id, chat_id))
        return SimpleNamespace(project_id=project_id, chat_id=chat_id)


def test_dispatch_context_resolves_project_without_action_registry_private_helper():
    from src.feishu.dispatch_context import DispatchContext

    pm = _ProjectManager()
    ctx = DispatchContext(project_manager=pm)

    assert ctx.resolve_project("proj-1", "chat-1").project_id == "proj-1"
    assert ctx.resolve_project(None, "chat-1") is None
    assert pm.calls == [("proj-1", "chat-1")]

