"""Process-level LRU cache for Feishu user display names.

Resolves ``user_id`` → ``display_name`` by calling the Feishu contact API
on cache miss, with a configurable capacity and TTL.  Thread-safe.

Degradation strategy: if the API call fails (permissions, network, etc.),
the result is cached as a negative entry for 60 s and the caller receives
``user_id[:8] + "(ID)"`` as a fallback.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_CACHE_CAPACITY = 500
_TTL_SECONDS = 3600  # 1 hour
_NEGATIVE_TTL_SECONDS = 60  # cache failures for 60 s


class _CacheEntry:
    __slots__ = ("display_name", "expires_at")

    def __init__(self, display_name: str, ttl: float):
        self.display_name = display_name
        self.expires_at = time.monotonic() + ttl


_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_cache_lock = threading.Lock()


def resolve_display_name(
    user_id: str,
    api_client_factory: Optional[Callable[[], Any]] = None,
) -> str:
    """Resolve a Feishu user_id to a human-readable display name.

    Returns the cached name if available and not expired.
    On cache miss, calls the Feishu contact API via *api_client_factory*.
    Falls back to ``user_id[:8] + "(ID)"`` on any failure.
    """
    if not user_id:
        return ""

    now = time.monotonic()

    # 1. Check cache
    with _cache_lock:
        entry = _cache.get(user_id)
        if entry is not None and entry.expires_at > now:
            _cache.move_to_end(user_id)
            return entry.display_name

    # 2. Cache miss or expired — try API
    display_name = _fetch_display_name(user_id, api_client_factory)

    # 3. Store in cache
    with _cache_lock:
        if display_name and not display_name.endswith("(ID)"):
            _cache[user_id] = _CacheEntry(display_name, _TTL_SECONDS)
        else:
            # Negative cache with shorter TTL
            _cache[user_id] = _CacheEntry(display_name, _NEGATIVE_TTL_SECONDS)
        _cache.move_to_end(user_id)
        # Prune oldest if over capacity
        while len(_cache) > _CACHE_CAPACITY:
            _cache.popitem(last=False)

    return display_name


def _fetch_display_name(
    user_id: str,
    api_client_factory: Optional[Callable[[], Any]] = None,
) -> str:
    """Call Feishu contact API to get display name.  Best-effort."""
    fallback = (user_id[:8] + "(ID)") if user_id else ""

    if api_client_factory is None:
        return fallback

    try:
        import lark_oapi as lark
        from lark_oapi.api.contact.v3 import GetUserRequest

        client = api_client_factory()
        req = GetUserRequest.builder().user_id(user_id).user_id_type("open_id").build()
        resp = client.contact.v3.user.get(req)
        if resp and resp.success() and resp.data and resp.data.user:
            name = resp.data.user.name or ""
            if name:
                return name
        return fallback
    except ImportError:
        logger.debug("lark_oapi not available for contact API; using fallback name")
        return fallback
    except Exception:
        logger.debug("Failed to fetch display name for user_id=%s", user_id[:12], exc_info=True)
        return fallback


def _reset_user_cache_for_testing() -> None:
    """Clear the user cache.  **Test-only.**"""
    with _cache_lock:
        _cache.clear()
