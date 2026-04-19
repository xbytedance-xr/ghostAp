import asyncio

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


# ---------------------------------------------------------------------------
# Exception chain: get_error_detail / fmt_error with wrapped TimeoutError
# ---------------------------------------------------------------------------

from src.utils.errors import get_error_detail


def test_get_error_detail_wrapped_timeout_via_cause():
    """RuntimeError wrapping TimeoutError via __cause__ → contains '超时'."""
    outer = RuntimeError("wrapper")
    outer.__cause__ = TimeoutError("inner timeout")
    result = get_error_detail(outer)
    assert "超时" in result


def test_get_error_detail_wrapped_timeout_via_context():
    """Exception wrapping TimeoutError via __context__ → contains '超时'."""
    outer = Exception("context wrap")
    outer.__context__ = TimeoutError()
    result = get_error_detail(outer)
    assert "超时" in result


def test_get_error_detail_multi_level_chain():
    """Deep chain: Exception → RuntimeError → TimeoutError → contains '超时'."""
    deep = TimeoutError("deep")
    mid = RuntimeError("mid")
    mid.__cause__ = deep
    outer = Exception("outer")
    outer.__context__ = mid
    result = get_error_detail(outer)
    assert "超时" in result


def test_get_error_detail_no_timeout_in_chain():
    """No TimeoutError in chain → should NOT contain '超时'."""
    outer = RuntimeError("wrapper")
    outer.__cause__ = ValueError("inner")
    result = get_error_detail(outer)
    assert "超时" not in result
    assert "wrapper" in result


def test_fmt_error_wrapped_timeout_via_cause():
    """fmt_error with wrapped TimeoutError → timeout formatting."""
    outer = RuntimeError("rte wrap")
    outer.__cause__ = TimeoutError("inner")
    result = fmt_error("测试", outer)
    assert "超时" in result
    assert "rte wrap" in result


def test_fmt_error_wrapped_bare_timeout_empty_outer():
    """fmt_error with wrapping exc that has empty str → fallback timeout text."""
    outer = RuntimeError()
    outer.__cause__ = TimeoutError()
    result = fmt_error("测试", outer)
    assert "超时" in result
    assert "操作超时，请稍后重试" in result


def test_get_error_detail_direct_timeout_regression():
    """Regression: direct TimeoutError() still returns '超时' text."""
    result = get_error_detail(TimeoutError())
    assert "超时" in result
    assert result == "操作超时，请稍后重试"


# ---------------------------------------------------------------------------
# _has_timeout_in_chain unified behaviour tests
# ---------------------------------------------------------------------------

from src.utils.errors import _has_timeout_in_chain


def test_chain_detects_asyncio_timeout_error():
    """asyncio.TimeoutError in chain is detected."""
    outer = RuntimeError("wrap")
    outer.__cause__ = asyncio.TimeoutError()
    assert _has_timeout_in_chain(outer) is True


def test_chain_detects_builtin_timeout_error():
    """Built-in TimeoutError in chain is detected."""
    outer = RuntimeError("wrap")
    outer.__context__ = TimeoutError("t")
    assert _has_timeout_in_chain(outer) is True


def test_chain_detects_timeout_expired_by_name():
    """Third-party 'TimeoutExpired' detected via class-name matching."""
    class TimeoutExpired(Exception):
        pass
    outer = RuntimeError("wrap")
    outer.__cause__ = TimeoutExpired("cmd timed out")
    assert _has_timeout_in_chain(outer) is True


def test_chain_detects_read_timeout_by_name():
    """Third-party 'ReadTimeout' detected via class-name matching."""
    class ReadTimeout(Exception):
        pass
    outer = RuntimeError()
    outer.__cause__ = ReadTimeout()
    assert _has_timeout_in_chain(outer) is True


def test_chain_detects_connect_timeout_by_name():
    """Third-party 'ConnectTimeout' detected via class-name matching."""
    class ConnectTimeout(IOError):
        pass
    outer = RuntimeError()
    outer.__context__ = ConnectTimeout()
    assert _has_timeout_in_chain(outer) is True


def test_chain_no_timeout_returns_false():
    """Non-timeout chain returns False."""
    outer = RuntimeError("wrap")
    outer.__cause__ = ValueError("v")
    assert _has_timeout_in_chain(outer) is False


def test_chain_depth_limit():
    """Chain deeper than _CHAIN_MAX_DEPTH (10) does not recurse infinitely."""
    # Build a 15-level chain ending with TimeoutError
    current = TimeoutError("deep")
    for _ in range(15):
        parent = RuntimeError("level")
        parent.__cause__ = current
        current = parent
    # Might or might not find it depending on depth — just must not hang/crash
    _has_timeout_in_chain(current)


def test_chain_consistency_with_review_diagnostics():
    """_has_timeout_in_chain from errors.py is the same function used by review_diagnostics."""
    from src.utils.review_diagnostics import _has_timeout_in_chain as rd_fn
    assert rd_fn is _has_timeout_in_chain

