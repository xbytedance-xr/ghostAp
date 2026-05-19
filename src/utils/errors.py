import asyncio
import logging
import re
from typing import Union

__all__ = [
    "GhostAPError",
    "sanitize_futures_msg",
    "classify_timeout",
    "fmt_error",
    "get_error_detail",
    "fmt_exception",
    "log_exception",
    "safe_error_message",
]

"""Unified error formatting and base exception for user-facing messages.

All user-facing error messages should use these helpers to ensure consistent
formatting (emoji prefix, Chinese language, action-based structure).

Convention:
    ❌  Hard failure (action failed, cannot proceed)
    ⚠️  Warning / partial failure (action completed with issues)
    ⏱️  Timeout (action took too long)
"""

# ---------------------------------------------------------------------------
# Feishu API Error Codes
# ---------------------------------------------------------------------------
LARK_CODE_MESSAGE_NOT_FOUND = 230001
LARK_CODE_MESSAGE_RECALLED = 230020


class GhostAPError(Exception):
    """Base exception for GhostAP domain errors.

    Carries a user-facing message (Chinese) and an optional machine-readable
    ``action`` tag for logging/metrics.

    Can also carry structured data for QuickAction buttons.
    """

    def __init__(self, message: str, *, action: str = "", quick_actions: list[str] = None, context: dict = None):
        super().__init__(message)
        self.action = action
        self.quick_actions = quick_actions or []
        self.context = context or {}


# ---------------------------------------------------------------------------
# User-facing message formatters
# ---------------------------------------------------------------------------

_CHAIN_MAX_DEPTH = 10

# stdlib concurrent.futures 在 TimeoutError 中注入的内部诊断格式，不应暴露给用户。
# 示例: "1 (of 5) futures unfinished", "3 (of 5) futures unfinished"
_FUTURES_UNFINISHED_RE = re.compile(r"\d+\s*\(of\s*\d+\)\s*futures?\s*unfinished")


def sanitize_futures_msg(msg: str) -> str:
    """清洗 stdlib 的 \"N (of M) futures unfinished\" 内部诊断信息。

    当 concurrent.futures.as_completed(timeout) 超时时，会抛出包含该格式信息的 TimeoutError，
    这对用户没有帮助，应该被替换成干净的中文提示或直接去掉。
    """
    cleaned = _FUTURES_UNFINISHED_RE.sub("", str(msg)).strip()
    return cleaned if cleaned else "操作超时，请稍后重试"


def classify_timeout(exc: BaseException) -> bool:
    """Determine whether *exc* should be classified as a timeout error.

    Returns ``True`` if *exc* is itself a ``TimeoutError`` /
    ``asyncio.TimeoutError``, **or** if any exception in its
    ``__cause__`` / ``__context__`` chain is a timeout type (detected by
    :func:`_has_timeout_in_chain`).

    This is the single source of truth for timeout classification — callers
    should prefer this over hand-rolling ``isinstance`` + chain-walk.
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    return _has_timeout_in_chain(exc)


def _has_timeout_in_chain(err: BaseException, *, _depth: int = 0) -> bool:
    """Walk __cause__ / __context__ for a wrapped TimeoutError (max 10 levels).

    Detects:
    - ``TimeoutError`` / ``asyncio.TimeoutError`` via isinstance
    - Third-party timeout types (``TimeoutExpired``, ``ReadTimeout``,
      ``ConnectTimeout``) via class-name matching
    """
    if _depth >= _CHAIN_MAX_DEPTH:
        return False
    for attr in ("__cause__", "__context__"):
        chained = getattr(err, attr, None)
        if chained is None:
            continue
        if isinstance(chained, (TimeoutError, asyncio.TimeoutError)):
            return True
        try:
            tn = type(chained).__name__
        except Exception:
            tn = ""
        if tn in ("TimeoutExpired", "ReadTimeout", "ConnectTimeout"):
            return True
        if _has_timeout_in_chain(chained, _depth=_depth + 1):
            return True
    return False


def fmt_error(action: str, detail: Union[str, Exception] = "") -> str:
    """Format a hard-failure message."""
    if isinstance(detail, Exception):
        if isinstance(detail, (TimeoutError, asyncio.TimeoutError)):
            msg = _FUTURES_UNFINISHED_RE.sub("", str(detail)).strip()
            if not msg:
                detail = "操作超时，请稍后重试"
            else:
                detail = f"操作超时 ({msg})"
        elif _has_timeout_in_chain(detail):
            # Exception wraps a TimeoutError in its chain
            inner_msg = _FUTURES_UNFINISHED_RE.sub("", str(detail)).strip()
            if not inner_msg:
                detail = "操作超时，请稍后重试"
            else:
                detail = f"操作超时 ({inner_msg})"
        else:
            detail = str(detail)

    if detail:
        return f"❌ {action}失败: {detail}"
    return f"❌ {action}失败"


def get_error_detail(exc: Exception, *, default: str = "未知错误") -> str:
    """统一把异常转成可展示的 detail（不含 "❌ 失败" 前缀）。"""
    try:
        formatted = fmt_error("", exc)
    except Exception:
        formatted = ""
    s = str(formatted or "")

    # 典型输出："❌ 失败: xxx" / "❌ 失败"
    if s.startswith("❌ "):
        if "失败: " in s:
            tail = s.split("失败: ", 1)[1]
            tail = str(tail or "").strip()
            if tail:
                return tail
        if s.endswith("失败"):
            msg = str(exc or "").strip()
            return msg or default

    # 非标准格式：保持原样
    return s


def fmt_exception(action: str, exc: BaseException) -> str:
    """Format an unexpected exception for the user."""
    if classify_timeout(exc):
        return f"❌ {action}超时: 操作耗时过长，请重试"
    return f"❌ {action}异常: {str(exc) or repr(exc)}"


def log_exception(logger: logging.Logger, msg: str, exc: Exception, level: int = logging.ERROR) -> None:
    """Log an exception with appropriate level.

    Downgrades known business logic exceptions (GhostAPError or any exception
    carrying ``is_ghostap_error = True``) to WARNING.
    """
    if isinstance(exc, GhostAPError) or getattr(exc, "is_ghostap_error", False):
        logger.warning(f"{msg}: {get_error_detail(exc)}")
    else:
        logger.log(level, msg, exc_info=exc)


def safe_error_message(exc: Exception) -> str:
    """Return a user-safe error description without leaking internal details.

    Known exception types get specific Chinese messages; unknown exceptions
    get a generic message.  Full details should be logged separately via logger.
    """
    if isinstance(exc, TimeoutError):
        return "执行超时"
    if isinstance(exc, PermissionError):
        return "权限不足"
    if isinstance(exc, (ConnectionError, OSError)):
        return "连接失败"
    return "内部错误，请联系管理员"
