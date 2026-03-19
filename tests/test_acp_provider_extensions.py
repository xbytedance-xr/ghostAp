from unittest.mock import patch

from src.acp.providers.aiden import AidenProvider, _get_aiden_acp_serve_help_blob
from src.acp.providers.codex import CodexProvider, _get_codex_acp_serve_help_blob


def test_aiden_provider_name():
    assert AidenProvider().name == "aiden"


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_aiden_provider_availability(mock_probe):
    mock_probe.return_value = (True, 0, "Usage: aiden acp serve [OPTIONS]", "")
    assert AidenProvider().check_availability() is True

    mock_probe.return_value = (False, 1, "", "")
    assert AidenProvider().check_availability() is False


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_aiden_provider_serve_command_model_style_config_c(mock_probe):
    # help 中包含 model.name 或 -c
    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_probe.return_value = (True, 0, "Usage: aiden acp serve -c model.name=...", "")
    cmd, args = AidenProvider().get_serve_command("gpt-4")
    assert cmd == "aiden"
    assert args == ["acp", "serve", "-c", "model.name=gpt-4"]


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_aiden_provider_serve_command_model_style_long(mock_probe):
    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_probe.return_value = (True, 0, "Usage: aiden acp serve --model MODEL", "")
    cmd, args = AidenProvider().get_serve_command("gpt-4")
    assert cmd == "aiden"
    assert args == ["acp", "serve", "--model", "gpt-4"]


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_aiden_provider_serve_command_model_style_short(mock_probe):
    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_probe.return_value = (True, 0, "Usage: aiden acp serve -m MODEL", "")
    cmd, args = AidenProvider().get_serve_command("gpt-4")
    assert cmd == "aiden"
    assert args == ["acp", "serve", "-m", "gpt-4"]


def test_codex_provider_name():
    assert CodexProvider().name == "codex"


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_codex_provider_availability(mock_probe):
    mock_probe.return_value = (True, 0, "Usage: codex acp serve", "")
    assert CodexProvider().check_availability() is True

    mock_probe.return_value = (False, 1, "", "")
    assert CodexProvider().check_availability() is False


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_codex_provider_serve_command_model_style_long(mock_probe):
    _get_codex_acp_serve_help_blob.cache_clear()
    mock_probe.return_value = (True, 0, "Usage: codex acp serve --model MODEL", "")
    cmd, args = CodexProvider().get_serve_command("gpt-4")
    assert cmd == "codex"
    assert args == ["acp", "serve", "--model", "gpt-4"]


@patch("src.acp.sync_adapter._probe_acp_serve_help")
def test_codex_provider_serve_command_model_style_short(mock_probe):
    _get_codex_acp_serve_help_blob.cache_clear()
    mock_probe.return_value = (True, 0, "Usage: codex acp serve -m MODEL", "")
    cmd, args = CodexProvider().get_serve_command("gpt-4")
    assert cmd == "codex"
    assert args == ["acp", "serve", "-m", "gpt-4"]
