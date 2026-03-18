import asyncio
import logging
from typing import Union

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


class SessionExpiredError(GhostAPError):
    """AI session (Coco/Claude) has expired or is unreachable."""

    def __init__(self, message: str = "会话已过期", **kwargs):
        super().__init__(message, quick_actions=["retry", "stop"], **kwargs)


class ProjectNotFoundError(GhostAPError):
    """Requested project does not exist."""

    def __init__(self, message: str = "项目不存在", **kwargs):
        super().__init__(message, quick_actions=["new_project_prompt", "list_projects"], **kwargs)


class SafetyCheckError(GhostAPError):
    """Command blocked by safety checks."""

    def __init__(self, message: str = "操作被安全策略拦截", **kwargs):
        super().__init__(message, quick_actions=["stop"], **kwargs)


# ---------------------------------------------------------------------------
# User-facing message formatters
# ---------------------------------------------------------------------------


def fmt_error(action: str, detail: Union[str, Exception] = "") -> str:
    """Format a hard-failure message."""
    if isinstance(detail, Exception):
        if isinstance(detail, (TimeoutError, asyncio.TimeoutError)):
            msg = str(detail)
            if not msg:
                detail = "操作超时，请稍后重试"
            else:
                detail = f"操作超时 ({msg})"
        else:
            detail = str(detail)

    if detail:
        return f"❌ {action}失败: {detail}"
    return f"❌ {action}失败"


def unwrap_fmt_error(formatted: str, exc: Exception | None = None, *, default: str = "未知错误") -> str:
    """从 `fmt_error("", exc)` 的输出中提取可展示的 detail。

    历史上多处调用会：
    1) `formatted = fmt_error("", e)`
    2) 手动剥离 "❌ 失败: " 前缀

    本函数把该逻辑下沉到 `src/utils/errors.py`，避免重复实现。
    """
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


def get_error_detail(exc: Exception, *, default: str = "未知错误") -> str:
    """统一把异常转成可展示的 detail（不含 "❌ 失败" 前缀）。"""
    try:
        formatted = fmt_error("", exc)
    except Exception:
        formatted = ""
    return unwrap_fmt_error(formatted, exc, default=default)


def fmt_exception(action: str, exc: BaseException) -> str:
    """Format an unexpected exception for the user."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return f"❌ {action}超时: 操作耗时过长，请重试"
    return f"❌ {action}异常: {str(exc)}"


def fmt_warning(message: str) -> str:
    """Format a warning / partial-failure message."""
    return f"⚠️ {message}"


def fmt_timeout(action: str, seconds: int) -> str:
    """Format a timeout message."""
    return f"⏱️ {action}超时（{seconds}秒）"


def fmt_not_found(resource: str, name: str = "") -> str:
    """Format a resource-not-found message."""
    if name:
        return f"❌ 未找到{resource}: {name}"
    return f"❌ 未找到 {resource}"


def log_exception(logger: logging.Logger, msg: str, exc: Exception, level: int = logging.ERROR):
    """Log an exception with appropriate level.

    Downgrades known business logic exceptions (GhostAPError) to WARNING.
    """
    if isinstance(exc, GhostAPError):
        logger.warning(f"{msg}: {exc}")
    else:
        logger.log(level, msg, exc_info=exc)
