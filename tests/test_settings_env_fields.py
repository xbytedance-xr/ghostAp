"""Tests for newly added Settings fields (spec_failed_task_id_override, acp_max_file_chars, ttadk_interactive_max_models)."""

from __future__ import annotations


class TestSettingsEnvFields:
    """Verify new fields have correct defaults and can be overridden via env."""

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("SPEC_FAILED_TASK_ID_OVERRIDE", raising=False)
        monkeypatch.delenv("ACP_MAX_FILE_CHARS", raising=False)
        monkeypatch.delenv("TTADK_INTERACTIVE_MAX_MODELS", raising=False)

        from src.config import Settings
        s = Settings()
        assert s.spec_failed_task_id_override == ""
        assert s.acp_max_file_chars == 200_000
        assert s.ttadk_interactive_max_models == 12

    def test_env_override_spec_failed_task_id_override(self, monkeypatch):
        monkeypatch.setenv("SPEC_FAILED_TASK_ID_OVERRIDE", "some_task_123")
        from src.config import Settings
        s = Settings()
        assert s.spec_failed_task_id_override == "some_task_123"

    def test_env_override_acp_max_file_chars(self, monkeypatch):
        monkeypatch.setenv("ACP_MAX_FILE_CHARS", "500000")
        from src.config import Settings
        s = Settings()
        assert s.acp_max_file_chars == 500_000

    def test_env_override_ttadk_interactive_max_models(self, monkeypatch):
        monkeypatch.setenv("TTADK_INTERACTIVE_MAX_MODELS", "25")
        from src.config import Settings
        s = Settings()
        assert s.ttadk_interactive_max_models == 25
