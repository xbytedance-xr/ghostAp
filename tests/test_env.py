"""Tests for src/utils/env.py"""
from __future__ import annotations

import os
from unittest.mock import patch

from src.utils.env import (
    _reset_env_for_testing,
    build_clean_env,
    get_test_environment_checker,
    is_test_environment,
    set_test_environment_checker,
)


class TestIsTestEnvironment:
    """Tests for is_test_environment() function."""

    def test_default_with_pytest_current_test_env_var(self):
        """Should return True when PYTEST_CURRENT_TEST is set."""
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "test_file.py::TestClass::test_func"}):
            assert is_test_environment() is True

    def test_default_with_pytest_in_sys_modules(self):
        """Should return True when pytest is in sys.modules (default case when running tests)."""
        # Note: Since we're running inside pytest, this should already be True
        assert is_test_environment() is True

    def test_default_with_testing_env_var_true(self):
        """Should return True when TESTING env var is set to a truthy value."""
        from src.utils.env import _default_is_test_environment

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.utils.env.sys.modules", {}):
                os.environ["TESTING"] = "1"
                assert _default_is_test_environment() is True

    def test_default_with_test_env_var_false(self):
        """Should return False when TEST env var is set to a falsy value."""
        from src.utils.env import _default_is_test_environment

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.utils.env.sys.modules", {}):
                os.environ["TEST"] = "0"
                assert _default_is_test_environment() is False

    def test_default_no_test_indicators(self):
        """Should return False when no test environment indicators are present."""
        from src.utils.env import _default_is_test_environment

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.utils.env.sys.modules", {}):
                assert _default_is_test_environment() is False


class TestTestEnvironmentCheckerInjection:
    """Tests for set_test_environment_checker() and get_test_environment_checker()."""

    def setup_method(self):
        """Reset the environment before each test."""
        _reset_env_for_testing()

    def test_set_and_get_custom_checker(self):
        """Should set and get a custom test environment checker."""
        def custom_checker() -> bool:
            return True

        set_test_environment_checker(custom_checker)
        assert get_test_environment_checker() is custom_checker

    def test_custom_checker_is_used(self):
        """is_test_environment() should use the custom checker when set."""
        custom_return = False

        def custom_checker() -> bool:
            return custom_return

        set_test_environment_checker(custom_checker)
        assert is_test_environment() is False

        custom_return = True
        assert is_test_environment() is True

    def test_set_none_restores_default(self):
        """Setting checker to None should restore the default behavior."""
        def custom_checker() -> bool:
            return True

        set_test_environment_checker(custom_checker)
        assert get_test_environment_checker() is custom_checker

        set_test_environment_checker(None)
        assert get_test_environment_checker() is None

    def test_reset_returns_to_default(self):
        """_reset_env_for_testing should clear the custom checker."""
        def custom_checker() -> bool:
            return True

        set_test_environment_checker(custom_checker)
        assert get_test_environment_checker() is not None

        _reset_env_for_testing()
        assert get_test_environment_checker() is None


class TestBuildCleanEnv:
    """Tests for build_clean_env() function."""

    def test_removes_claudecode_key(self):
        """Should remove CLAUDECODE key from environment."""
        base_env = {
            "PATH": "/usr/bin",
            "CLAUDECODE": "some_value",
            "HOME": "/home/user",
        }
        result = build_clean_env(base_env)
        assert "CLAUDECODE" not in result
        assert result["PATH"].startswith("/usr/bin")
        assert result["HOME"] == "/home/user"

    def test_copies_all_other_keys(self):
        """Should copy all keys except guard keys."""
        base_env = {
            "KEY1": "value1",
            "KEY2": "value2",
            "CLAUDECODE": "value3",
        }
        result = build_clean_env(base_env)
        assert result["KEY1"] == "value1"
        assert result["KEY2"] == "value2"
        assert "CLAUDECODE" not in result

    def test_uses_os_environ_when_no_base_provided(self):
        """Should use os.environ when base is None."""
        with patch.dict(os.environ, {"CLAUDECODE": "test_value", "OTHER": "keep"}):
            result = build_clean_env()
            assert "CLAUDECODE" not in result
            assert result["OTHER"] == "keep"

    def test_handles_empty_base_env(self):
        """Should handle empty base environment gracefully."""
        result = build_clean_env({})
        # PATH may be added by _ensure_npm_global_in_path
        non_path_keys = {k: v for k, v in result.items() if k != "PATH"}
        assert non_path_keys == {}

    def test_base_env_not_modified(self):
        """Should not modify the original base environment dict."""
        base_env = {"CLAUDECODE": "value"}
        original = base_env.copy()
        build_clean_env(base_env)
        assert base_env == original

    def test_explicit_employee_home_never_injects_manager_user_bins(
        self,
        tmp_path,
        monkeypatch,
    ):
        manager_home = tmp_path / "manager"
        employee_home = tmp_path / "employee"
        for home in (manager_home, employee_home):
            (home / ".npm-global" / "bin").mkdir(parents=True)
            (home / ".local" / "bin").mkdir(parents=True)
        monkeypatch.setattr(
            "src.utils.env.os.path.expanduser",
            lambda _value: str(manager_home),
        )

        result = build_clean_env({"HOME": str(employee_home), "PATH": "/usr/bin"})

        assert str(employee_home / ".npm-global" / "bin") in result["PATH"]
        assert str(employee_home / ".local" / "bin") in result["PATH"]
        assert str(manager_home) not in result["PATH"]


class TestResetEnvForTesting:
    """Tests for _reset_env_for_testing() function."""

    def test_reset_in_test_environment_works(self):
        """Should work fine in test environment (default case)."""
        # Since we're running in pytest, this should just work
        _reset_env_for_testing()
        assert get_test_environment_checker() is None
