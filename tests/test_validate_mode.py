"""--validate / --check-config pre-check mode tests.

Covers:
- AC-R12: --validate exits 0 when config is valid
- AC-R12: --validate exits 1 with error message when config is invalid
- AC-R12: --check-config is an alias for --validate
- main() accepts argv parameter for testability
- AC-R28: --validate calls validate_feishu_config() and exits 1 when it returns False
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.config import ConfigurationError, _reset_settings_for_testing


@pytest.fixture(autouse=True)
def reset_singleton():
    _reset_settings_for_testing()
    yield
    _reset_settings_for_testing()


class TestValidateModeSuccess:
    """--validate exits 0 when configuration is valid."""

    def test_validate_exits_zero_on_valid_config(self, capsys):
        from src.main import main

        with patch("src.main.get_settings"):
            with pytest.raises(SystemExit) as exc_info:
                main(["--validate"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "配置校验通过" in captured.out

    def test_check_config_alias_exits_zero(self, capsys):
        from src.main import main

        with patch("src.main.get_settings"):
            with pytest.raises(SystemExit) as exc_info:
                main(["--check-config"])

        assert exc_info.value.code == 0


class TestValidateModeFailure:
    """--validate exits 1 with error details when configuration is invalid."""

    def test_validate_exits_one_on_invalid_config(self, capsys):
        from src.main import main

        with patch("src.main.get_settings", side_effect=ConfigurationError("test validation error")):
            with pytest.raises(SystemExit) as exc_info:
                main(["--validate"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "配置校验失败" in captured.err

    def test_validate_error_message_in_stderr(self, capsys):
        from src.main import main

        with patch("src.main.get_settings", side_effect=ConfigurationError("missing API key")):
            with pytest.raises(SystemExit) as exc_info:
                main(["--validate"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "missing API key" in captured.err


class TestMainAcceptsArgv:
    """main() properly accepts argv parameter for programmatic invocation."""

    def test_no_validate_flag_proceeds_to_application(self):
        from src.main import main

        with patch("src.main.Application") as mock_app:
            mock_instance = mock_app.return_value
            mock_instance.run = lambda: None
            main([])  # no --validate flag

        mock_app.assert_called_once()

    def test_parse_known_args_ignores_unknown_flags(self, capsys):
        """Unknown flags like pytest's -x -q should not cause errors."""
        from src.main import main

        with patch("src.main.get_settings"):
            with pytest.raises(SystemExit) as exc_info:
                main(["--validate", "-x", "-q"])

        assert exc_info.value.code == 0


class TestValidateFeishuCheck:
    """AC-R28: --validate calls validate_feishu_config() and exits 1 on False."""

    def test_validate_exits_one_when_feishu_config_invalid(self, capsys):
        from src.main import main

        mock_settings = MagicMock()
        mock_settings.validate_feishu_config.return_value = False

        with patch("src.main.get_settings", return_value=mock_settings):
            with pytest.raises(SystemExit) as exc_info:
                main(["--validate"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "配置校验失败" in captured.err
        assert "APP_ID" in captured.err or "APP_SECRET" in captured.err

    def test_validate_calls_validate_feishu_config(self):
        from src.main import main

        mock_settings = MagicMock()
        mock_settings.validate_feishu_config.return_value = True

        with patch("src.main.get_settings", return_value=mock_settings):
            with pytest.raises(SystemExit):
                main(["--validate"])

        mock_settings.validate_feishu_config.assert_called_once()
