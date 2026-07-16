"""Tests for SLOCK_TEAM_NAME_PREFIX → slock_team_name_suffix compatibility migration (AC-17)."""

from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings, _reset_settings_for_testing

pytestmark = pytest.mark.filterwarnings(
    "ignore:SLOCK_TEAM_NAME_PREFIX is deprecated, use SLOCK_TEAM_NAME_SUFFIX instead:DeprecationWarning"
)


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

        with patch("src.config.settings._emit_slock_prefix_deprecation_warning") as warn:
            s = Settings(_env_file=None)

        assert s.slock_team_name_suffix == "TestTeam"
        warn.assert_called_once_with()

    def test_new_suffix_env_takes_priority(self, monkeypatch):
        """When both PREFIX and SUFFIX are set, SUFFIX wins."""
        monkeypatch.setenv("SLOCK_TEAM_NAME_PREFIX", "OldValue")
        monkeypatch.setenv("SLOCK_TEAM_NAME_SUFFIX", "NewValue")

        with patch("src.config.settings._emit_slock_prefix_deprecation_warning") as warn:
            s = Settings(_env_file=None)

        assert s.slock_team_name_suffix == "NewValue"
        warn.assert_called_once_with()

    def test_prefix_emits_deprecation_warning(self, monkeypatch):
        """SLOCK_TEAM_NAME_PREFIX triggers DeprecationWarning."""
        monkeypatch.setenv("SLOCK_TEAM_NAME_PREFIX", "DeprecatedVal")
        monkeypatch.delenv("SLOCK_TEAM_NAME_SUFFIX", raising=False)

        with patch("src.config.settings._emit_slock_prefix_deprecation_warning") as warn:
            Settings(_env_file=None)
        warn.assert_called_once_with()

    def test_no_prefix_no_warning(self, monkeypatch):
        """Without SLOCK_TEAM_NAME_PREFIX, no deprecation warning is emitted."""
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        monkeypatch.delenv("SLOCK_TEAM_NAME_SUFFIX", raising=False)

        with patch("src.config.settings._emit_slock_prefix_deprecation_warning") as warn:
            Settings(_env_file=None)

        warn.assert_not_called()

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

    @pytest.mark.parametrize("configured_parallelism", [2, 8])
    def test_engine_dispatch_and_executor_share_configured_parallelism(
        self,
        monkeypatch,
        tmp_path,
        configured_parallelism,
    ):
        """Default dispatch batches and executor workers use one Settings value."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import AgentIdentity, SlockTask

        monkeypatch.setenv("SLOCK_MAX_PARALLEL_AGENTS", str(configured_parallelism))
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        settings = Settings(_env_file=None)

        with patch("src.slock_engine.engine.get_settings", return_value=settings):
            engine = SlockEngine(
                chat_id="parallelism-chat",
                root_path=str(tmp_path),
                memory_base_path=str(tmp_path),
            )

        agents = [
            AgentIdentity(
                agent_id=f"agent-{index}",
                name=f"Agent {index}",
                owner_group="parallelism-chat",
            )
            for index in range(8)
        ]
        engine._registry = MagicMock()
        engine._registry.list_agents.return_value = agents
        engine._tasks.extend(SlockTask(content=f"Task {index}") for index in range(8))

        try:
            executor = engine._get_executor()
            with (
                patch.object(
                    engine._router,
                    "route_message",
                    side_effect=lambda _content, available: available[0],
                ),
                patch.object(engine, "execute_parallel", return_value={}) as execute_parallel,
            ):
                engine.dispatch_pending_tasks()

            assignments = execute_parallel.call_args.args[0]
            assert len(assignments) == configured_parallelism
            assert executor._executor._max_workers == configured_parallelism
        finally:
            engine.cleanup()

    @pytest.mark.parametrize("invalid_override", [0, -1, 1.5, True])
    def test_engine_rejects_invalid_explicit_parallelism(
        self,
        monkeypatch,
        tmp_path,
        invalid_override,
    ):
        """Explicit dispatch overrides must be positive integers, not truthy values."""
        from src.slock_engine.engine import SlockEngine
        monkeypatch.delenv("SLOCK_TEAM_NAME_PREFIX", raising=False)
        settings = Settings(_env_file=None)
        with patch("src.slock_engine.engine.get_settings", return_value=settings):
            engine = SlockEngine(
                chat_id="invalid-parallelism-chat",
                root_path=str(tmp_path),
                memory_base_path=str(tmp_path),
            )

        engine._registry = MagicMock()
        engine._registry.list_agents.return_value = []
        assert engine._tasks == []
        try:
            with pytest.raises(ValueError, match="positive integer"):
                engine.dispatch_pending_tasks(max_concurrent=invalid_override)
        finally:
            engine.cleanup()
