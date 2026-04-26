"""Common utilities shared by lock card builders.

Formatting helpers and signing re-exports used by both repo-lock and
chat-lock card modules.
"""

from __future__ import annotations

import logging
from ..styles import UI_TEXT

# -- Re-exported signing utilities (canonical implementation in src.utils.signing) --
from src.utils.signing import (  # noqa: F401 — re-export for backward compatibility
    _compute_command_sig,
    _get_signing_key,
    _verify_legacy_sha256_fallback,
    verify_command_sig,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_COMMAND_TEXT_LENGTH",
    "_build_p2p_multi_url",
    "_compute_command_sig",
    "_get_signing_key",
    "_verify_legacy_sha256_fallback",
    "verify_command_sig",
    "format_elapsed_ago",
    "format_lock_duration",
]

# Maximum length for command_text in retry button value payload.
# Feishu card action value has ~2KB limit; we leave headroom for other fields.
MAX_COMMAND_TEXT_LENGTH = 1000


def _build_p2p_multi_url(app_id: str) -> dict:
    """Build multi_url dict for P2P deeplink with Web fallback.

    Uses ``https://applink.feishu.cn/...`` for url/pc_url (Web-compatible)
    and ``lark://applink/...`` for android_url/ios_url (native client).
    """
    _https = f"https://applink.feishu.cn/client/bot/open?appId={app_id}"
    _native = f"lark://applink/client/bot/open?appId={app_id}"
    return {
        "url": _https,
        "pc_url": _https,
        "android_url": _native,
        "ios_url": _native,
    }


def format_elapsed_ago(elapsed_seconds: float) -> str:
    """Format an elapsed duration into a human-readable Chinese string.

    Four tiers:
    - < 60s   → "X 秒前"
    - < 3600s → "X 分钟前"
    - < 86400s → "X 小时 Y 分钟前"
    - >= 86400s → "X 天 Y 小时前"
    """
    elapsed = max(0, elapsed_seconds)
    if elapsed < 5:
        return UI_TEXT["lock_duration_just_now"]
    if elapsed < 60:
        return UI_TEXT["lock_duration_seconds"].format(n=int(elapsed))
    if elapsed < 3600:
        return UI_TEXT["lock_duration_minutes"].format(n=int(elapsed // 60))
    if elapsed < 86400:
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        return UI_TEXT["lock_duration_hours"].format(h=hours, m=minutes)
    days = int(elapsed // 86400)
    hours = int((elapsed % 86400) // 3600)
    return UI_TEXT["lock_duration_days"].format(d=days, h=hours)


def format_lock_duration(locked_at_mono: float) -> str:
    """Format how long a chat lock has been held (e.g. '已锁定 2 小时 15 分钟').

    Uses monotonic clock delta for accuracy.
    """
    import time as _time
    elapsed = max(0, _time.monotonic() - locked_at_mono)
    if elapsed < 60:
        return UI_TEXT["lock_held_seconds"].format(n=int(elapsed))
    if elapsed < 3600:
        return UI_TEXT["lock_held_minutes"].format(n=int(elapsed // 60))
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    if minutes:
        return UI_TEXT["lock_held_hours_minutes"].format(h=hours, m=minutes)
    return UI_TEXT["lock_held_hours"].format(h=hours)
