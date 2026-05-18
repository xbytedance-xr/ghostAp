"""Tests for src.card.engine_meta — centralized engine metadata."""

import pytest

from src.card.engine_meta import (
    ENGINE_CMD_MAP,
    ENGINE_LABEL_DEFAULT,
    ENGINE_LABELS,
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

    @pytest.mark.parametrize("engine_type, fallback, expected", [
        # Known engine types return their mapped command.
        ("deep", None, "/deep"),
        ("spec", None, "/spec"),
        ("worktree", None, "/wt"),
        # Unknown engine types return the fallback value.
        ("unknown", None, "命令"),
        ("", None, "命令"),
        (None, None, "命令"),
        # Custom fallback is respected.
        ("unknown", "对应命令", "对应命令"),
        (None, "", ""),
    ])
    def test_engine_type_to_cmd(self, engine_type, fallback, expected):
        """engine_type_to_cmd handles known/unknown types with default and custom fallbacks."""
        if fallback is None:
            assert engine_type_to_cmd(engine_type) == expected
        else:
            assert engine_type_to_cmd(engine_type, fallback=fallback) == expected

    @pytest.mark.parametrize("engine_type, fallback, expected", [
        # Known engine types return their display name.
        ("deep", None, "Deep"),
        ("spec", None, "Spec"),
        ("worktree", None, "Worktree"),
        # Unknown engine types return empty string by default.
        ("unknown", None, ""),
        (None, None, ""),
        # Custom fallback is respected.
        (None, "Engine", "Engine"),
    ])
    def test_engine_type_to_name(self, engine_type, fallback, expected):
        """engine_type_to_name handles known/unknown types with default and custom fallbacks."""
        if fallback is None:
            assert engine_type_to_name(engine_type) == expected
        else:
            assert engine_type_to_name(engine_type, fallback=fallback) == expected

    def test_engine_labels_contain_restart_prefix(self):
        """All engine labels should contain the restart prefix."""
        for label in ENGINE_LABELS.values():
            assert "🔄" in label

    def test_engine_label_default_is_string(self):
        """Default label is a non-empty string."""
        assert isinstance(ENGINE_LABEL_DEFAULT, str)
        assert len(ENGINE_LABEL_DEFAULT) > 0
