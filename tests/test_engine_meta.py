"""Tests for src.card.engine_meta — centralized engine metadata."""

import pytest

from src.card.engine_meta import (
    ENGINE_CMD_MAP,
    ENGINE_LABELS,
    ENGINE_LABEL_DEFAULT,
    ENGINE_NAME_MAP,
    engine_type_to_cmd,
    engine_type_to_name,
)


class TestEngineMeta:
    """Verify engine metadata consistency and helper behavior."""

    def test_all_maps_have_same_keys(self):
        """ENGINE_CMD_MAP, ENGINE_NAME_MAP, ENGINE_LABELS must cover the same engine types."""
        assert set(ENGINE_CMD_MAP.keys()) == set(ENGINE_NAME_MAP.keys())
        assert set(ENGINE_CMD_MAP.keys()) == set(ENGINE_LABELS.keys())

    def test_known_engine_types(self):
        """All known engine types are present."""
        expected = {"deep", "spec", "worktree"}
        assert set(ENGINE_CMD_MAP.keys()) == expected

    def test_cmd_map_values_start_with_slash(self):
        """All commands should be slash commands."""
        for cmd in ENGINE_CMD_MAP.values():
            assert cmd.startswith("/"), f"Command '{cmd}' does not start with /"

    def test_engine_type_to_cmd_known(self):
        """Known engine types return their mapped command."""
        assert engine_type_to_cmd("deep") == "/deep"
        assert engine_type_to_cmd("spec") == "/spec"
        assert engine_type_to_cmd("worktree") == "/wt"

    def test_engine_type_to_cmd_unknown_returns_fallback(self):
        """Unknown engine types return the fallback value."""
        assert engine_type_to_cmd("unknown") == "命令"
        assert engine_type_to_cmd("") == "命令"
        assert engine_type_to_cmd(None) == "命令"

    def test_engine_type_to_cmd_custom_fallback(self):
        """Custom fallback is respected."""
        assert engine_type_to_cmd("unknown", fallback="对应命令") == "对应命令"
        assert engine_type_to_cmd(None, fallback="") == ""

    def test_engine_type_to_name_known(self):
        """Known engine types return their display name."""
        assert engine_type_to_name("deep") == "Deep"
        assert engine_type_to_name("spec") == "Spec"
        assert engine_type_to_name("worktree") == "Worktree"

    def test_engine_type_to_name_unknown_returns_fallback(self):
        """Unknown engine types return empty string by default."""
        assert engine_type_to_name("unknown") == ""
        assert engine_type_to_name(None) == ""

    def test_engine_type_to_name_custom_fallback(self):
        """Custom fallback is respected."""
        assert engine_type_to_name(None, fallback="Engine") == "Engine"

    def test_engine_labels_contain_restart_prefix(self):
        """All engine labels should contain the restart prefix."""
        for label in ENGINE_LABELS.values():
            assert "🔄" in label

    def test_engine_label_default_is_string(self):
        """Default label is a non-empty string."""
        assert isinstance(ENGINE_LABEL_DEFAULT, str)
        assert len(ENGINE_LABEL_DEFAULT) > 0
