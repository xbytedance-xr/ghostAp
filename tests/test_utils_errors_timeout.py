
import asyncio
import pytest
from src.utils.errors import fmt_error, fmt_exception

def test_fmt_error_with_timeout_error():
    # Test with builtin TimeoutError
    err = TimeoutError()
    result = fmt_error("测试操作", err)
    assert "操作超时，请稍后重试" in result
    assert "测试操作失败" in result

def test_fmt_error_with_asyncio_timeout_error():
    # Test with asyncio.TimeoutError
    err = asyncio.TimeoutError()
    result = fmt_error("测试操作", err)
    assert "操作超时，请稍后重试" in result
    assert "测试操作失败" in result

def test_fmt_error_with_normal_exception():
    # Test with normal Exception
    err = ValueError("Something wrong")
    result = fmt_error("测试操作", err)
    assert "Something wrong" in result
    assert "测试操作失败" in result

def test_fmt_error_with_string():
    # Test with string detail
    result = fmt_error("测试操作", "具体错误")
    assert "具体错误" in result
    assert "测试操作失败" in result

def test_fmt_exception_with_timeout_error():
    # Test fmt_exception with builtin TimeoutError
    err = TimeoutError()
    result = fmt_exception("测试操作", err)
    assert "测试操作超时" in result
    assert "操作耗时过长" in result

def test_fmt_exception_with_asyncio_timeout_error():
    # Test fmt_exception with asyncio.TimeoutError
    err = asyncio.TimeoutError()
    result = fmt_exception("测试操作", err)
    assert "测试操作超时" in result
    assert "操作耗时过长" in result

def test_fmt_exception_with_normal_exception():
    # Test fmt_exception with normal Exception
    err = ValueError("Something wrong")
    result = fmt_exception("测试操作", err)
    assert "Something wrong" in result
    assert "测试操作异常" in result
