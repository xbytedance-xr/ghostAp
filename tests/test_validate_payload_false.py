"""Tests verifying production behavior when VALIDATE_PAYLOAD=False.

In production, per-element validation is skipped for performance.
These tests ensure malformed payloads don't raise exceptions in that mode.
"""

import pytest


def _disable_validation(monkeypatch):
    """Helper to disable validation in both modules (factories delegates to worktree)."""
    import src.card.events.factories as factories_mod
    import src.card.events.worktree as worktree_mod
    monkeypatch.setattr(factories_mod, "VALIDATE_PAYLOAD", False)
    monkeypatch.setattr(worktree_mod, "VALIDATE_PAYLOAD", False)


class TestValidatePayloadFalseWorktreeProgress:
    """Worktree progress with malformed units should not raise when validation is off."""

    def test_unit_without_status_passes_silently(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_progress
        # Unit missing 'status' — would fail with _VALIDATE_PAYLOAD=True
        e = worktree_progress(units=[{"name": "u1"}], project_id="p1")
        assert e.payload["units"] == [{"name": "u1"}]

    def test_unit_non_dict_passes_silently(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_progress
        # Unit is a string — per-element check skipped
        e = worktree_progress(units=["not_a_dict"], project_id="p1")
        assert e.payload["units"] == ["not_a_dict"]


class TestValidatePayloadFalseWorktreeToolSelect:
    """Tool select with non-dict tools should not raise when validation is off."""

    def test_non_dict_tool_passes_silently(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_tool_select
        e = worktree_tool_select(tools=["not_a_dict"])
        assert e.payload["tools"] == ["not_a_dict"]


class TestValidatePayloadFalseWorktreeCleanup:
    """Cleanup with malformed merge_notes should not raise when validation is off."""

    def test_merge_note_missing_branch_passes_silently(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_cleanup
        # merge_note missing 'branch' — would fail with _VALIDATE_PAYLOAD=True
        e = worktree_cleanup(
            merge_notes=[{"status": "ok"}],
            cleanup_phase="summary",
        )
        assert e.payload["merge_notes"] == [{"status": "ok"}]


class TestValidatePayloadFalseWorktreeMerge:
    """Merge with malformed merge_notes should not raise when validation is off."""

    def test_merge_note_missing_status_passes_silently(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_merge
        e = worktree_merge(merge_notes=[{"branch": "feat-1"}])
        assert e.payload["merge_notes"] == [{"branch": "feat-1"}]


class TestTopLevelTypeChecksStillEnforced:
    """Top-level isinstance checks should still raise even with _VALIDATE_PAYLOAD=False."""

    def test_units_non_list_still_raises(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_progress
        with pytest.raises(TypeError, match="units must be a list"):
            worktree_progress(units="bad", project_id="p1")

    def test_tools_non_list_still_raises(self, monkeypatch):
        _disable_validation(monkeypatch)

        from src.card.events.worktree import worktree_tool_select
        with pytest.raises(TypeError, match="tools must be a list"):
            worktree_tool_select(tools="bad")
