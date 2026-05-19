"""Tests for SLOCK_TEAM_NAME_PREFIX → slock_team_name_suffix compatibility migration (AC-17)."""

import warnings

import pytest

from src.config import Settings, _reset_settings_for_testing


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure config singleton is reset before and after each test."""
    _reset_settings_for_testing()
    yield
    _reset_settings_for_testing()


class TestSlockTeamNamePrefixCompat:
    """AC-17: SLOCK_TEAM_NAME_PREFIX env compat mapping."""

    def test_prefix_env_maps_to_suffix(self, monkeypatch):
        """Old SLOCK_TEAM_NAME_PREFIX value maps to slock_team_name_suffix."""
        monkeypatch.setenv("SLOCK_TEAM_NAME_PREFIX", "TestTeam")
        # Clear any existing suffix env to test pure prefix migration
        monkeypatch.delenv("SLOCK_TEAM_NAME_SUFFIX", raising=False)

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            s = Settings(_env_file=None)

        assert s.slock_team_name_suffix == "TestTeam"

    def test_new_suffix_env_takes_priority(self, monkeypatch):
        """When both PREFIX and SUFFIX are set, SUFFIX wins."""
        monkeypatch.setenv("SLOCK_TEAM_NAME_PREFIX", "OldValue")
        monkeypatch.setenv("SLOCK_TEAM_NAME_SUFFIX", "NewValue")

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            s = Settings(_env_file=None)

        assert s.slock_team_name_suffix == "NewValue"

    def test_prefix_emits_deprecation_warning(self, monkeypatch):
        """SLOCK_TEAM_NAME_PREFIX triggers DeprecationWarning."""
        monkeypatch.setenv("SLOCK_TEAM_NAME_PREFIX", "DeprecatedVal")
        monkeypatch.delenv("SLOCK_TEAM_NAME_SUFFIX", raising=False)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(_env_file=None)

        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) >= 1
        assert "SLOCK_TEAM_NAME_PREFIX" in str(deprecation_warnings[0].message)

    def test_no_prefix_no_warning(self, monkeypatch):
        """Without SLOCK_TEAM_NAME_PREFIX, no deprecation warning is emitted."""
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        monkeypatch.delenv("SLOCK_TEAM_NAME_SUFFIX", raising=False)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Settings(_env_file=None)

        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        prefix_warnings = [x for x in deprecation_warnings if "SLOCK_TEAM_NAME_PREFIX" in str(x.message)]
        assert len(prefix_warnings) == 0

    def test_default_suffix_without_env(self, monkeypatch):
        """Without any env, slock_team_name_suffix defaults to '[Slock]'."""
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        monkeypatch.delenv("SLOCK_TEAM_NAME_SUFFIX", raising=False)

        s = Settings(_env_file=None)
        assert s.slock_team_name_suffix == "[Slock]"


class TestSlockEngineSettings:
    """AC-17/NFR6: Slock engine settings field validation."""

    def test_default_values(self, monkeypatch):
        """New slock engine settings have correct defaults."""
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        s = Settings(_env_file=None)
        assert s.slock_max_parallel_agents == 4
        assert s.slock_max_queue_size == 8
        assert s.slock_queue_wait_timeout == 60
        assert s.slock_max_open_tasks == 50

    def test_max_parallel_agents_zero_raises(self, monkeypatch):
        """slock_max_parallel_agents=0 fails Pydantic validation (ge=1)."""
        monkeypatch.setenv("SLOCK_MAX_PARALLEL_AGENTS", "0")
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        with pytest.raises(Exception):  # ValidationError
            Settings(_env_file=None)

    def test_max_queue_size_zero_raises(self, monkeypatch):
        """slock_max_queue_size=0 fails Pydantic validation (ge=1)."""
        monkeypatch.setenv("SLOCK_MAX_QUEUE_SIZE", "0")
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        with pytest.raises(Exception):
            Settings(_env_file=None)

    def test_queue_wait_timeout_zero_raises(self, monkeypatch):
        """slock_queue_wait_timeout=0 fails Pydantic validation (ge=1)."""
        monkeypatch.setenv("SLOCK_QUEUE_WAIT_TIMEOUT", "0")
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        with pytest.raises(Exception):
            Settings(_env_file=None)

    def test_max_open_tasks_zero_raises(self, monkeypatch):
        """slock_max_open_tasks=0 fails Pydantic validation (ge=1)."""
        monkeypatch.setenv("SLOCK_MAX_OPEN_TASKS", "0")
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        with pytest.raises(Exception):
            Settings(_env_file=None)

    def test_custom_values_applied(self, monkeypatch):
        """Custom env values are properly loaded into settings."""
        monkeypatch.setenv("SLOCK_MAX_PARALLEL_AGENTS", "8")
        monkeypatch.setenv("SLOCK_MAX_QUEUE_SIZE", "16")
        monkeypatch.setenv("SLOCK_QUEUE_WAIT_TIMEOUT", "120")
        monkeypatch.setenv("SLOCK_MAX_OPEN_TASKS", "100")
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        s = Settings(_env_file=None)
        assert s.slock_max_parallel_agents == 8
        assert s.slock_max_queue_size == 16
        assert s.slock_queue_wait_timeout == 120
        assert s.slock_max_open_tasks == 100
