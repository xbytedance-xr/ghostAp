from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.project.context import ProjectContext


def _write_traex_model_cache(home, models):
    cache_dir = home / ".trae" / "cli"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")


@patch("src.acp.providers.subprocess.run")
def test_normalize_acp_model_name_resolves_traex_compound_variant(mock_run, tmp_path):
    from src.acp.providers import _reset_providers_for_testing, normalize_acp_model_name

    _reset_providers_for_testing()
    mock_run.return_value = SimpleNamespace(
        stdout='Usage: traecli acp serve [OPTIONS]\n  -c, --config <key=value>\n',
        stderr="",
    )
    _write_traex_model_cache(
        tmp_path,
        [{"slug": "Test-O-New-Thinking", "config_name": "c_o_new_thinking"}],
    )

    with patch("pathlib.Path.home", return_value=tmp_path):
        assert normalize_acp_model_name("traex", "c_o_new_thinking/max/max") == "Test-O-New-Thinking"


def test_codex_model_selection_round_trips_reasoning_effort():
    from src.acp.model_selection import (
        compose_codex_model_selection,
        split_codex_model_selection,
    )

    assert split_codex_model_selection("gpt-5.6-sol") == ("gpt-5.6-sol", None)
    assert split_codex_model_selection("gpt-5.6-sol/high") == ("gpt-5.6-sol", "high")
    assert split_codex_model_selection("gpt-5.6-sol/max") == ("gpt-5.6-sol", "max")
    assert split_codex_model_selection("gpt-5.6-sol/ultra") == ("gpt-5.6-sol", "ultra")
    assert split_codex_model_selection("gpt-5.4-mini/minimal") == (
        "gpt-5.4-mini",
        "minimal",
    )
    assert split_codex_model_selection("gpt-5.4-mini/none") == (
        "gpt-5.4-mini",
        "none",
    )
    assert split_codex_model_selection("anthropic/claude-sonnet") == (
        "anthropic/claude-sonnet",
        None,
    )
    assert compose_codex_model_selection("gpt-5.6-sol", "ultra") == "gpt-5.6-sol/ultra"


def test_normalize_codex_model_name_preserves_composite_selection_for_acp_boundary():
    from src.acp.providers import normalize_acp_model_name

    assert normalize_acp_model_name("codex", "gpt-5.6-sol/max") == "gpt-5.6-sol/max"


def test_traex_switch_model_uses_normalized_backend_model():
    from src.feishu.handlers.programming import TraexModeHandler

    ctx = MagicMock()
    ctx.settings.thread_programming_enabled = False
    ctx.settings.acp_startup_timeout = 10
    ctx.mode_manager.is_traex_mode.return_value = True
    ctx.project_manager.get_active_project.return_value = None
    ctx.working_dirs = {}

    handler = TraexModeHandler(ctx)
    handler.reply_card = MagicMock()
    handler.reply_error = MagicMock()
    handler.get_working_dir = MagicMock(return_value="/tmp")

    fake_session = MagicMock()
    fake_session.set_model = MagicMock(return_value=True)
    mgr_mock = MagicMock()
    mgr_mock.get_session.return_value = fake_session

    project = MagicMock(spec=ProjectContext)
    project.project_id = "p1"
    project.theme_color = "blue"
    project.root_path = "/tmp/p1"
    project.project_name = "P1"

    with (
        patch.object(handler, "_get_session_manager", return_value=mgr_mock),
        patch(
            "src.feishu.handlers.programming.normalize_acp_model_name",
            return_value="Test-O-New-Thinking",
        ) as normalize,
    ):
        handler.switch_model("msg1", "chat1", "c_o_new_thinking/max/max", project=project)

    normalize.assert_called_once_with("traex", "c_o_new_thinking/max/max")
    fake_session.set_model.assert_called_once_with("Test-O-New-Thinking")
    mgr_mock.end_session.assert_not_called()


def test_create_engine_session_normalizes_traex_model_before_start(monkeypatch):
    from src.agent_session import factory

    calls: dict[str, object] = {}
    fake_session = MagicMock()

    class _Settings:
        acp_startup_timeout = 20
        rate_limit_retry_enabled = False

    def fake_start_session_with_retry(**kwargs):
        calls.update(kwargs)
        return fake_session

    monkeypatch.setattr(factory, "get_settings", lambda: _Settings())
    monkeypatch.setattr("src.acp.sync_adapter.start_session_with_retry", fake_start_session_with_retry)
    monkeypatch.setattr(
        factory,
        "normalize_acp_model_name",
        lambda agent_type, model_name: "Test-O-New-Thinking"
        if agent_type == "traex"
        else model_name,
    )
    monkeypatch.setattr(factory, "ModelFailureAwareSession", lambda inner, **_: inner)

    result = factory.create_engine_session(
        agent_type="traex",
        cwd="/tmp/p1",
        model_name="c_o_new_thinking/max/max",
    )

    assert result is fake_session
    assert calls["model_name"] == "Test-O-New-Thinking"
