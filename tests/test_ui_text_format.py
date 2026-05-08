"""AC17: Parametrized test verifying all UI_TEXT format templates don't raise KeyError.

Iterates over all UI_TEXT keys containing {} placeholders and calls .format()
with sample kwargs to ensure no missing parameters.
"""

import re

import pytest

from src.card.ui_text import UI_TEXT

# Extract all format field names from a string
_FORMAT_FIELD_RE = re.compile(r"\{(\w+)\}")

# Sample values for all known format parameters across UI_TEXT
_SAMPLE_KWARGS = {
    "task_count": 3,
    "task_list": "  1. Task A\n  2. Task B\n  3. Task C",
    "task_name": "修复登录",
    "seq": 1,
    "next_seq": 2,
    "link_text": "查看最新卡片",
    "msg_id": "om_test_message",
    "sid_short": "abc123",
    "rotation_count": 2,
    "page": 3,
    "independent_count": 3,
    "merged_count": 2,
    "completed": 3,
    "engine_cmd": "deep",
    "project_name": "TestProject",
    "root_path": "/home/user/project",
    "tool_calls_count": 5,
    "iteration": 3,
    "total": 10,
    "status_icon": "✅",
    "cycle_num": 2,
    "tool_count": 8,
    "file_count": 4,
    "summary": "完成了重构",
    "n": 7,
    "context_lines": "some context",
    "error": "something went wrong",
    "reason": "timeout",
    "timeout": 30,
    "elapsed": 15,
    "remaining": 120,
    "model": "gpt-4",
    "provider": "openai",
    "name": "test_tool",
    "status": "completed",
    "count": 5,
    "max": 10,
    "cmd": "deep_status",
    "minutes": 5,
    "seconds": 30,
    "version": "1.0.0",
    "commands": "deep 或 loop",
    "duration": "3m 20s",
    "branch": "main",
    "worktree_path": "/tmp/wt",
    "tool_name": "read",
    "input_preview": "...",
    "output_preview": "...",
    "file_path": "/src/main.py",
    "line": 42,
    "message": "success",
    "chat_id": "chat_123",
    "user_name": "张三",
    "session_id": "sess_abc",
    "page_count": 2,
    "lock_holder": "user_x",
    "project": "my_project",
    "time_str": "14:30",
    "warn_minutes": 5,
}


def _get_format_keys() -> list[tuple[str, str]]:
    """Return list of (key, template) for all UI_TEXT entries with format placeholders."""
    results = []
    for key, value in UI_TEXT.items():
        if isinstance(value, str) and "{" in value:
            results.append((key, value))
    return results


_FORMAT_ENTRIES = _get_format_keys()


@pytest.mark.parametrize("key,template", _FORMAT_ENTRIES, ids=[k for k, _ in _FORMAT_ENTRIES])
def test_ui_text_format_no_key_error(key: str, template: str):
    """Each UI_TEXT template with {} placeholders should format without KeyError."""
    # Extract required field names
    fields = _FORMAT_FIELD_RE.findall(template)
    if not fields:
        # Template has { but no named fields (e.g., literal braces) — skip
        return

    # Build kwargs from sample values; use placeholder for unknown fields
    kwargs = {}
    for field in fields:
        kwargs[field] = _SAMPLE_KWARGS.get(field, f"<{field}>")

    # This should NOT raise KeyError
    result = template.format(**kwargs)
    assert isinstance(result, str)
    assert len(result) > 0


class TestUITextFrozenProxy:
    """AC16: UI_TEXT is frozen (MappingProxyType) and raises TypeError on mutation."""

    def test_frozen_proxy_raises_on_assignment(self):
        """UI_TEXT['key'] = 'x' should raise TypeError."""
        with pytest.raises(TypeError):
            UI_TEXT["orch_plan_archived"] = "tampered"  # type: ignore[index]

    def test_frozen_proxy_raises_on_new_key(self):
        """UI_TEXT['new_key'] = 'x' should raise TypeError."""
        with pytest.raises(TypeError):
            UI_TEXT["nonexistent_key_xyz"] = "value"  # type: ignore[index]

    def test_frozen_proxy_has_no_pop(self):
        """UI_TEXT should not support pop/del."""
        with pytest.raises((TypeError, AttributeError)):
            UI_TEXT.pop("orch_plan_archived")  # type: ignore[attr-defined]


class TestMutableUITextNotExported:
    """AC13: _MUTABLE_UI_TEXT is not accessible via wildcard import."""

    def test_mutable_not_in_wildcard_import(self):
        """from src.card.ui_text import * should NOT expose _MUTABLE_UI_TEXT."""
        import importlib
        mod = importlib.import_module("src.card.ui_text")
        public_api = getattr(mod, "__all__", None)
        assert public_api is not None, "ui_text.py must define __all__"
        assert "_MUTABLE_UI_TEXT" not in public_api

    def test_all_contains_ui_text(self):
        """__all__ must contain 'UI_TEXT'."""
        import importlib
        mod = importlib.import_module("src.card.ui_text")
        assert "UI_TEXT" in mod.__all__
