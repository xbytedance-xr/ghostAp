"""Unified error formatting and base exception for user-facing messages.

All user-facing error messages should use these helpers to ensure consistent
formatting (emoji prefix, Chinese language, action-based structure).

Convention:
    ❌  Hard failure (action failed, cannot proceed)
    ⚠️  Warning / partial failure (action completed with issues)
    ⏱️  Timeout (action took too long)
"""


class GhostAPError(Exception):
    """Base exception for GhostAP domain errors.

    Carries a user-facing message (Chinese) and an optional machine-readable
    ``action`` tag for logging/metrics.
    """

    def __init__(self, message: str, *, action: str = ""):
        super().__init__(message)
        self.action = action


class SessionExpiredError(GhostAPError):
    """AI session (Coco/Claude) has expired or is unreachable."""
    pass


class ProjectNotFoundError(GhostAPError):
    """Requested project does not exist."""
    pass


class SafetyCheckError(GhostAPError):
    """Command blocked by safety checks."""
    pass


# ---------------------------------------------------------------------------
# User-facing message formatters
# ---------------------------------------------------------------------------

def fmt_error(action: str, detail: str = "") -> str:
    """Format a hard-failure message.

    >>> fmt_error("创建项目", "目录不存在")
    '❌ 创建项目失败: 目录不存在'
    >>> fmt_error("创建项目")
    '❌ 创建项目失败'
    """
    if detail:
        return f"❌ {action}失败: {detail}"
    return f"❌ {action}失败"


def fmt_exception(action: str, exc: BaseException) -> str:
    """Format an unexpected exception for the user.

    >>> fmt_exception("执行命令", ValueError("bad arg"))
    '❌ 执行命令异常: bad arg'
    """
    return f"❌ {action}异常: {str(exc)}"


def fmt_warning(message: str) -> str:
    """Format a warning / partial-failure message.

    >>> fmt_warning("部分任务失败")
    '⚠️ 部分任务失败'
    """
    return f"⚠️ {message}"


def fmt_timeout(action: str, seconds: int) -> str:
    """Format a timeout message.

    >>> fmt_timeout("命令执行", 30)
    '⏱️ 命令执行超时（30秒）'
    """
    return f"⏱️ {action}超时（{seconds}秒）"


def fmt_not_found(resource: str, name: str = "") -> str:
    """Format a resource-not-found message.

    >>> fmt_not_found("项目", "my_project")
    '❌ 未找到项目: my_project'
    >>> fmt_not_found("claude 命令")
    '❌ 未找到 claude 命令'
    """
    if name:
        return f"❌ 未找到{resource}: {name}"
    return f"❌ 未找到 {resource}"
