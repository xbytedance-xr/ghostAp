import subprocess
from unittest.mock import MagicMock, patch

import pytest

import src.ttadk.manager as ttadk_manager_module
from src.ttadk.manager import auto_update_ttadk


@pytest.fixture(autouse=True)
def _reset_attempted_flag():
    ttadk_manager_module._ttadk_update_attempted = False
    yield
    ttadk_manager_module._ttadk_update_attempted = False


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_success(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings

    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

    result = auto_update_ttadk()

    assert result is True
    mock_run.assert_called_once_with(
        ["ttadk", "update"],
        capture_output=True,
        text=True,
        timeout=120,
    )


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_already_attempted(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings

    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

    first = auto_update_ttadk()
    assert first is True

    second = auto_update_ttadk()
    assert second is False
    assert mock_run.call_count == 1


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_disabled_by_config(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = False
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings

    result = auto_update_ttadk()

    assert result is False
    mock_run.assert_not_called()


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_timeout(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ttadk update", timeout=120)

    result = auto_update_ttadk()

    assert result is False


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_command_not_found(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings

    mock_run.side_effect = FileNotFoundError("ttadk not found")

    result = auto_update_ttadk()

    assert result is False
