from types import SimpleNamespace
from unittest.mock import patch

from src.acp.providers.aiden import AidenProvider, _get_aiden_acp_serve_help_blob
from src.acp.providers.codex import CodexProvider, _get_codex_acp_serve_help_blob
from src.acp.providers.gemini import GeminiProvider, _get_gemini_acp_serve_help_blob


def test_aiden_provider_name():
    assert AidenProvider().name == "aiden"


@patch("src.acp.providers.aiden.subprocess.run")
def test_aiden_provider_availability(mock_run):
    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(
        stdout="Usage: aiden acp [options]\nRun Aiden CLI as an ACP agent for editors like Zed\n",
        stderr="",
    )
    assert AidenProvider().check_availability() is True

    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="", stderr="")
    assert AidenProvider().check_availability() is False


@patch("src.acp.providers.aiden.subprocess.run")
def test_aiden_provider_serve_command_model_style_long(mock_run):
    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="Usage: aiden acp --model MODEL", stderr="")
    cmd, args = AidenProvider().get_serve_command("gpt-4")
    assert cmd == "aiden"
    assert args == ["acp", "--model", "gpt-4"]


@patch("src.acp.providers.aiden.subprocess.run")
def test_aiden_provider_serve_command_model_style_short(mock_run):
    _get_aiden_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="Usage: aiden acp -m MODEL", stderr="")
    cmd, args = AidenProvider().get_serve_command("gpt-4")
    assert cmd == "aiden"
    assert args == ["acp", "-m", "gpt-4"]


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


def test_gemini_provider_name():
    assert GeminiProvider().name == "gemini"


@patch("src.acp.providers.gemini.subprocess.run")
def test_gemini_provider_availability(mock_run):
    _get_gemini_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="Usage: gemini [options]\n  --acp  Starts the agent in ACP mode\n", stderr="")
    assert GeminiProvider().check_availability() is True

    _get_gemini_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="Usage: gemini [options]\n", stderr="")
    assert GeminiProvider().check_availability() is False


@patch("src.acp.providers.gemini.subprocess.run")
def test_gemini_provider_serve_command_model_style_long(mock_run):
    _get_gemini_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="Usage: gemini [options]\n  --model MODEL\n  --acp\n", stderr="")
    cmd, args = GeminiProvider().get_serve_command("gemini-2.5-pro")
    assert cmd == "gemini"
    assert args == ["--acp", "--model", "gemini-2.5-pro"]


@patch("src.acp.providers.gemini.subprocess.run")
def test_gemini_provider_serve_command_model_style_short(mock_run):
    _get_gemini_acp_serve_help_blob.cache_clear()
    mock_run.return_value = SimpleNamespace(stdout="Usage: gemini [options]\n  -m MODEL\n  --acp\n", stderr="")
    cmd, args = GeminiProvider().get_serve_command("gemini-2.5-pro")
    assert cmd == "gemini"
    assert args == ["--acp", "-m", "gemini-2.5-pro"]
