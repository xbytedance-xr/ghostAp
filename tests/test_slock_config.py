"""Tests for slock_default_roles configuration and warning logging."""

from __future__ import annotations

import logging

import pytest


class TestSlockDefaultRolesDefaultValue:
    """Test slock_default_roles default value."""

    def test_slock_default_roles_default_is_empty_string(self, monkeypatch):
        """Settings().slock_default_roles default value is empty string."""
        # Ensure no env var override
        monkeypatch.delenv("SLOCK_DEFAULT_ROLES", raising=False)

        from src.config.settings import Settings

        s = Settings(_env_file=None)
        assert s.slock_default_roles == ""


class TestSlockDefaultRolesWarningLog:
    """Test warning log when slock_default_roles is empty."""

    def test_empty_slock_default_roles_emits_warning(self, caplog, monkeypatch):
        """Creating Settings instance with empty slock_default_roles outputs WARNING log."""
        monkeypatch.delenv("SLOCK_DEFAULT_ROLES", raising=False)

        from src.config.settings import Settings

        with caplog.at_level(logging.WARNING, logger="src.config.settings"):
            Settings(_env_file=None)

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "slock_default_roles is empty" in r.message
        ]
        assert len(warning_records) == 1, (
            f"Expected 1 WARNING log for empty slock_default_roles. "
            f"Records: {[r.message for r in caplog.records]}"
        )

    def test_slock_default_roles_set_via_env_no_warning(self, caplog, monkeypatch):
        """Setting SLOCK_DEFAULT_ROLES env var prevents warning log."""
        monkeypatch.setenv(
            "SLOCK_DEFAULT_ROLES",
            "planner:claude,coder:codex,reviewer:claude,tester:codex",
        )

        from src.config.settings import Settings

        with caplog.at_level(logging.WARNING, logger="src.config.settings"):
            Settings(_env_file=None)

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "slock_default_roles is empty" in r.message
        ]
        assert len(warning_records) == 0, (
            f"Expected no WARNING log when SLOCK_DEFAULT_ROLES is set. "
            f"Records: {[r.message for r in caplog.records]}"
        )
