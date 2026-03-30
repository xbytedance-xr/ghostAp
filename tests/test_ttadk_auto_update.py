import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

import src.ttadk.manager as ttadk_manager_module
from src.ttadk.manager import auto_update_ttadk


@pytest.fixture(autouse=True)
def _reset_attempted_flag():
    ttadk_manager_module._ttadk_update_attempted = False
    yield
    ttadk_manager_module._ttadk_update_attempted = False


@patch("src.ttadk.manager.threading.Thread")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_spawns_thread(mock_settings, mock_thread_cls):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings
    mock_thread = MagicMock()
    mock_thread_cls.return_value = mock_thread

    auto_update_ttadk()

    mock_thread_cls.assert_called_once()
    kwargs = mock_thread_cls.call_args
    assert kwargs[1]["daemon"] is True
    assert kwargs[1]["name"] == "ttadk-auto-upgrade"
    mock_thread.start.assert_called_once()


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_thread_runs_upgrade(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

    auto_update_ttadk()

    for t in threading.enumerate():
        if t.name == "ttadk-auto-upgrade":
            t.join(timeout=5)

    mock_run.assert_called_once_with(
        ["ttadk", "upgrade"],
        capture_output=True,
        text=True,
        timeout=120,
    )


@patch("src.ttadk.manager.threading.Thread")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_already_attempted(mock_settings, mock_thread_cls):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings
    mock_thread = MagicMock()
    mock_thread_cls.return_value = mock_thread

    auto_update_ttadk()
    auto_update_ttadk()

    assert mock_thread_cls.call_count == 1


@patch("src.ttadk.manager.threading.Thread")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_disabled_by_config(mock_settings, mock_thread_cls):
    settings = MagicMock()
    settings.ttadk_auto_update = False
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings

    auto_update_ttadk()

    mock_thread_cls.assert_not_called()


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_timeout_does_not_crash(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="ttadk upgrade", timeout=120)

    auto_update_ttadk()

    for t in threading.enumerate():
        if t.name == "ttadk-auto-upgrade":
            t.join(timeout=5)


@patch("src.ttadk.manager.subprocess.run")
@patch("src.ttadk.manager.get_settings")
def test_auto_update_ttadk_command_not_found_does_not_crash(mock_settings, mock_run):
    settings = MagicMock()
    settings.ttadk_auto_update = True
    settings.ttadk_update_timeout = 120
    mock_settings.return_value = settings
    mock_run.side_effect = FileNotFoundError("ttadk not found")

    auto_update_ttadk()

    for t in threading.enumerate():
        if t.name == "ttadk-auto-upgrade":
            t.join(timeout=5)
