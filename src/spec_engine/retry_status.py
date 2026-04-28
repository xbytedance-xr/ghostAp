"""Structured retry status types for spec-engine retry subsystem.

Decouples engine layer from UI text — engine returns RetryStatus enums,
renderer layer maps them to user-facing strings with emoji.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RetryStatus(Enum):
    """Retry lifecycle states returned by the engine layer."""

    WAITING = "waiting"  # 等待重试延迟倒计时中
    EXECUTING = "executing"  # 正在执行重试请求
    SUCCEEDED = "succeeded"  # 重试成功，审查恢复正常
    EXHAUSTED = "exhausted"  # 所有重试次数已耗尽仍未成功
    NO_RETRY = "no_retry"  # 决定不重试（配置禁用或非全超时场景）


@dataclass(frozen=True, slots=True)
class RetryEvent:
    """Structured retry event emitted by the retry loop.

    Attributes:
        status: Current retry lifecycle state.
        attempt: Current attempt number (1-based), 0 if not applicable.
        max_attempts: Total configured retry attempts.
        delay_sec: Delay in seconds before this attempt (for WAITING status).
        message: Human-readable context message (for terminal states).
        detail: Deprecated — kept for backward compat, prefer structured fields.
    """

    status: RetryStatus
    attempt: int = 0
    max_attempts: int = 0
    delay_sec: float = 0.0
    message: str = ""
    detail: str = ""  # deprecated
