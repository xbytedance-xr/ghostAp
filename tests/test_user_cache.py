"""FS-15: Tests for src/feishu/user_cache.py — LRU user display name cache."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from src.feishu.user_cache import (
    _reset_user_cache_for_testing,
    resolve_display_name,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_user_cache_for_testing()
    yield
    _reset_user_cache_for_testing()


class TestResolveDisplayName:
    def test_empty_user_id_returns_empty(self):
        assert resolve_display_name("") == ""

    def test_none_factory_returns_fallback(self):
        result = resolve_display_name("ou_abc12345678", None)
        assert result == "ou_abc12(ID)"

    def test_cache_hit_returns_cached(self):
        factory = MagicMock()
        # First call — cache miss, triggers API.  Simulate API failure → fallback.
        result1 = resolve_display_name("ou_user1", None)
        assert "(ID)" in result1
        # Second call within TTL — should NOT call factory.
        result2 = resolve_display_name("ou_user1", factory)
        assert result2 == result1
        factory.assert_not_called()

    def test_successful_api_call_cached(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        mock_resp.data.user.name = "Alice"
        mock_client.contact.v3.user.get.return_value = mock_resp
        factory = MagicMock(return_value=mock_client)

        result = resolve_display_name("ou_alice", factory)
        assert result == "Alice"

        # Second call should hit cache — factory not called again.
        factory.reset_mock()
        result2 = resolve_display_name("ou_alice", factory)
        assert result2 == "Alice"
        factory.assert_not_called()

    def test_api_exception_returns_fallback(self):
        factory = MagicMock(side_effect=Exception("network error"))
        result = resolve_display_name("ou_fail123456", factory)
        assert result == "ou_fail1(ID)"

    def test_concurrent_access_no_error(self):
        errors: list[Exception] = []

        def worker(uid: str):
            try:
                for _ in range(20):
                    resolve_display_name(uid, None)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"ou_{i:010d}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors

    def test_capacity_eviction(self):
        """Inserting more than _CACHE_CAPACITY entries prunes oldest."""
        from src.feishu import user_cache

        original_cap = user_cache._CACHE_CAPACITY
        try:
            user_cache._CACHE_CAPACITY = 5
            for i in range(10):
                resolve_display_name(f"ou_{i:010d}", None)
            with user_cache._cache_lock:
                assert len(user_cache._cache) <= 5
        finally:
            user_cache._CACHE_CAPACITY = original_cap
