import asyncio
import pytest
from src.utils.errors import fmt_error

class TestErrorFormatting:
    def test_fmt_error_with_string(self):
        """Test formatting a simple string error."""
        result = fmt_error("测试", "发生错误")
        assert result == "❌ 测试失败: 发生错误"

    def test_fmt_error_with_empty_string(self):
        """Test formatting an empty string (should just show action failed)."""
        result = fmt_error("测试")
        assert result == "❌ 测试失败"

    def test_fmt_error_with_exception(self):
        """Test formatting a general exception."""
        exc = ValueError("无效值")
        result = fmt_error("测试", exc)
        assert result == "❌ 测试失败: 无效值"

    def test_fmt_error_with_timeout_empty_message(self):
        """Test formatting a TimeoutError with empty message (the main fix)."""
        exc = asyncio.TimeoutError()
        result = fmt_error("测试", exc)
        assert "操作超时，请稍后重试" in result
        assert "❌ 测试失败" in result

    def test_fmt_error_with_timeout_custom_message(self):
        """Test formatting a TimeoutError with a custom message (should be preserved)."""
        # Note: asyncio.TimeoutError() doesn't usually take args in older python, 
        # but built-in TimeoutError does. asyncio.TimeoutError is an alias in 3.11+.
        # Let's test with built-in TimeoutError which is compatible.
        exc = TimeoutError("连接超时")
        result = fmt_error("测试", exc)
        # Expecting: ❌ 测试失败: 操作超时 (连接超时)
        assert "操作超时 (连接超时)" in result
