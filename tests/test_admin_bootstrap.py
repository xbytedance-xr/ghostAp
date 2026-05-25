from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.admin_bootstrap import AdminBootstrapService
from src.feishu.handlers.system import SystemHandler
from src.feishu.slash_command_parser import SlashCommandParser
from src.thread import set_current_sender_id


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """Clear class-level rate limit state between tests."""
    AdminBootstrapService._last_attempt.clear()
    yield
    AdminBootstrapService._last_attempt.clear()


def test_setadmin_bootstraps_sender_as_only_admin(tmp_path):
    settings = SimpleNamespace(admin_user_ids=frozenset())
    env_path = tmp_path / ".env"

    result = AdminBootstrapService(
        env_path=env_path,
        settings_getter=lambda: settings,
    ).set_admin("ou_first", "ou_other")

    assert result.success is True
    assert result.code == "bootstrap"
    assert result.target_id == "ou_first"
    assert env_path.read_text(encoding="utf-8") == "ADMIN_USER_IDS=ou_first\n"
    assert settings.admin_user_ids == frozenset({"ou_first"})


def test_setadmin_non_admin_cannot_replace_existing_admin(tmp_path):
    settings = SimpleNamespace(admin_user_ids=frozenset({"ou_admin"}))
    env_path = tmp_path / ".env"
    env_path.write_text("ADMIN_USER_IDS=ou_admin\n", encoding="utf-8")

    result = AdminBootstrapService(
        env_path=env_path,
        settings_getter=lambda: settings,
    ).set_admin("ou_other", "ou_other")

    assert result.success is False
    assert result.code == "not_admin"
    assert env_path.read_text(encoding="utf-8") == "ADMIN_USER_IDS=ou_admin\n"
    assert settings.admin_user_ids == frozenset({"ou_admin"})


def test_setadmin_existing_admin_can_replace_single_admin(tmp_path):
    settings = SimpleNamespace(admin_user_ids=frozenset({"ou_admin"}))
    env_path = tmp_path / ".env"
    env_path.write_text("APP_ID=app\nADMIN_USER_IDS=ou_admin\n", encoding="utf-8")

    result = AdminBootstrapService(
        env_path=env_path,
        settings_getter=lambda: settings,
    ).set_admin("ou_admin", "ou_next")

    assert result.success is True
    assert result.code == "updated"
    assert result.target_id == "ou_next"
    assert env_path.read_text(encoding="utf-8") == "APP_ID=app\nADMIN_USER_IDS=ou_next\n"
    assert settings.admin_user_ids == frozenset({"ou_next"})


def test_setadmin_replaces_export_style_env_line(tmp_path):
    settings = SimpleNamespace(admin_user_ids=frozenset({"ou_admin"}))
    env_path = tmp_path / ".env"
    env_path.write_text("APP_ID=app\nexport ADMIN_USER_IDS = ou_admin\n", encoding="utf-8")

    result = AdminBootstrapService(
        env_path=env_path,
        settings_getter=lambda: settings,
    ).set_admin("ou_admin", "ou_next")

    assert result.success is True
    assert env_path.read_text(encoding="utf-8") == "APP_ID=app\nADMIN_USER_IDS=ou_next\n"


def test_setadmin_first_sender_blocks_other_service_instances(tmp_path):
    settings = SimpleNamespace(admin_user_ids=frozenset())
    env_path = tmp_path / ".env"

    first = AdminBootstrapService(env_path=env_path, settings_getter=lambda: settings)
    second = AdminBootstrapService(env_path=env_path, settings_getter=lambda: settings)

    assert first.set_admin("ou_first").success is True
    result = second.set_admin("ou_other")

    assert result.success is False
    assert result.code == "not_admin"
    assert env_path.read_text(encoding="utf-8") == "ADMIN_USER_IDS=ou_first\n"


def test_setadmin_accepts_legacy_comma_string_admins(tmp_path):
    settings = SimpleNamespace(admin_user_ids="ou_admin,ou_backup")
    env_path = tmp_path / ".env"

    result = AdminBootstrapService(
        env_path=env_path,
        settings_getter=lambda: settings,
    ).set_admin("ou_backup", "ou_next")

    assert result.success is True
    assert result.target_id == "ou_next"
    assert settings.admin_user_ids == frozenset({"ou_next"})


def test_setadmin_rejects_invalid_target_after_bootstrap(tmp_path):
    settings = SimpleNamespace(admin_user_ids=frozenset({"ou_admin"}))
    env_path = tmp_path / ".env"
    env_path.write_text("ADMIN_USER_IDS=ou_admin\n", encoding="utf-8")

    result = AdminBootstrapService(
        env_path=env_path,
        settings_getter=lambda: settings,
    ).set_admin("ou_admin", "bad,target")

    assert result.success is False
    assert result.code == "invalid_target"
    assert env_path.read_text(encoding="utf-8") == "ADMIN_USER_IDS=ou_admin\n"


def test_system_handler_recognizes_setadmin_command():
    m = SlashCommandParser.parse("/setadmin")
    assert SystemHandler.is_interceptable_command_match(m) is True

    m = SlashCommandParser.parse("/setadmin ou_next")
    assert SystemHandler.is_interceptable_command_match(m) is True


def test_system_handler_routes_setadmin_with_sender():
    ctx = MagicMock()
    handler = SystemHandler(ctx)
    handler.reply_text = MagicMock()
    handler.reply_error = MagicMock()

    service = MagicMock()
    service.set_admin.return_value = SimpleNamespace(
        success=True,
        code="bootstrap",
        target_id="ou_first",
    )

    set_current_sender_id("ou_first")
    try:
        with patch("src.admin_bootstrap.AdminBootstrapService", return_value=service):
            handler.handle_intercepted_command(
                "msg_1",
                "chat_1",
                "/setadmin",
                command_match=SlashCommandParser.parse("/setadmin"),
            )
    finally:
        set_current_sender_id(None)

    service.set_admin.assert_called_once_with("ou_first", "", chat_type="group")
    handler.reply_text.assert_called_once()
    handler.reply_error.assert_not_called()
