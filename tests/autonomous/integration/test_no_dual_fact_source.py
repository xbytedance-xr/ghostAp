"""Integration tests: no dual fact source in manager_only mode."""

from __future__ import annotations

from src.autonomous.migration.slock_compat import (
    CompatibilityMode,
    SlockCompatLayer,
)


def test_manager_only_mode_never_writes_legacy() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.MANAGER_ONLY)

    # Intercept write should block
    assert layer.intercept_write("task.create", {"task_id": "t1"}) is True
    assert layer.intercept_write("plan.update", {"plan_id": "p1"}) is True

    # No writes recorded
    assert layer.write_log == []


def test_legacy_mode_allows_writes() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.LEGACY)

    assert layer.intercept_write("task.create", {"task_id": "t1"}) is False
    assert len(layer.write_log) == 1


def test_manager_only_forwards_slock_commands_to_manager() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.MANAGER_ONLY)

    result = layer.handle_command("create", "improve reliability", tenant_key="t1")
    assert result.handled is True
    assert result.forward_to_manager is True
    assert result.data is not None
    assert result.data["operation"] == "goal.create"


def test_manager_only_forwards_list_command() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.MANAGER_ONLY)

    result = layer.handle_command("list", "", tenant_key="t1")
    assert result.handled is True
    assert result.forward_to_manager is True
    assert result.data["operation"] == "goal.list"


def test_legacy_mode_does_not_intercept_commands() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.LEGACY)

    result = layer.handle_command("create", "something")
    assert result.handled is False


def test_disabled_mode_blocks_everything() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.DISABLED)

    result = layer.handle_command("create", "task")
    assert result.handled is True
    assert "disabled" in result.response

    assert layer.intercept_write("task.create", {}) is True


def test_shadow_read_does_not_block_commands() -> None:
    layer = SlockCompatLayer(mode=CompatibilityMode.SHADOW_READ)

    result = layer.handle_command("create", "task")
    assert result.handled is False  # passes through to legacy
