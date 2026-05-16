"""Tests for group naming validation and formatting."""
from src.project_chat.group_naming import format_group_name, validate_name_part


class TestFormatGroupName:
    def test_basic(self):
        assert format_group_name("myproject", "dev") == "myproject-dev"

    def test_strips_whitespace(self):
        assert format_group_name("  myproject  ", "  dev  ") == "myproject-dev"


class TestValidateNamePart:
    def test_valid_ascii(self):
        assert validate_name_part("myproject") is None

    def test_valid_chinese(self):
        assert validate_name_part("我的项目") is None

    def test_valid_with_dash_underscore(self):
        assert validate_name_part("my-project_1") is None

    def test_invalid_whitespace(self):
        err = validate_name_part("my project")
        assert err is not None
        assert "空格" in err or "whitespace" in err.lower()

    def test_invalid_empty(self):
        err = validate_name_part("")
        assert err is not None

    def test_invalid_too_long(self):
        err = validate_name_part("a" * 100)
        assert err is not None
