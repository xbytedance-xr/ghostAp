"""FS-15: Tests for src/feishu/user_cache.py — LRU user display name cache."""

from __future__ import annotations

import ast
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.feishu.user_cache import (
    _reset_user_cache_for_testing,
    resolve_display_name,
    resolve_display_name_nonblocking,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_user_cache_for_testing()
    yield
    _reset_user_cache_for_testing()


class TestResolveDisplayName:
    def test_feishu_handlers_never_block_on_contact_lookup(self):
        root = Path(__file__).resolve().parents[1]
        violations: list[str] = []

        for path in (root / "src" / "feishu" / "handlers").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "resolve_display_name":
                    continue
                if len(node.args) >= 2 or any(
                    keyword.arg == "api_client_factory"
                    for keyword in node.keywords
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")

        assert violations == []

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
        from src.feishu import user_cache

        factory = MagicMock(side_effect=Exception("network error"))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(user_cache.time, "monotonic", lambda: 100.0)
            result = resolve_display_name("ou_fail123456", factory)
        assert result == "ou_fail1(ID)"
        with user_cache._cache_lock:
            entry = user_cache._cache["ou_fail123456"]
            assert entry.expires_at == 100.0 + user_cache._NEGATIVE_TTL_SECONDS

    def test_unsuccessful_api_response_uses_long_negative_cache(self, monkeypatch):
        from src.feishu import user_cache

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = False
        mock_resp.code = 99991663
        mock_client.contact.v3.user.get.return_value = mock_resp
        monkeypatch.setattr(user_cache.time, "monotonic", lambda: 100.0)

        result = resolve_display_name("ou_denied123456", MagicMock(return_value=mock_client))

        assert result == "ou_denie(ID)"
        with user_cache._cache_lock:
            entry = user_cache._cache["ou_denied123456"]
            assert entry.expires_at == 100.0 + user_cache._API_FAILURE_TTL_SECONDS

    def test_transient_api_response_uses_short_negative_cache(self, monkeypatch):
        from src.feishu import user_cache

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = False
        mock_resp.code = 500
        mock_client.contact.v3.user.get.return_value = mock_resp
        monkeypatch.setattr(user_cache.time, "monotonic", lambda: 100.0)

        result = resolve_display_name("ou_transient123", MagicMock(return_value=mock_client))

        assert result == "ou_trans(ID)"
        with user_cache._cache_lock:
            entry = user_cache._cache["ou_transient123"]
            assert entry.expires_at == 100.0 + user_cache._NEGATIVE_TTL_SECONDS

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

    def test_nonblocking_miss_returns_fallback_and_refreshes_once(self, monkeypatch):
        from src.feishu import user_cache

        submitted: list[tuple[object, tuple[object, ...]]] = []
        monkeypatch.setattr(
            user_cache,
            "submit_io",
            lambda fn, *args: submitted.append((fn, args)),
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.success.return_value = True
        mock_resp.data.user.name = "Alice"
        mock_client.contact.v3.user.get.return_value = mock_resp
        factory = MagicMock(return_value=mock_client)

        first = resolve_display_name_nonblocking("ou_alice", factory)
        second = resolve_display_name_nonblocking("ou_alice", factory)

        assert first == "ou_alice(ID)"
        assert second == first
        assert len(submitted) == 1
        factory.assert_not_called()

        refresh, args = submitted[0]
        refresh(*args)

        assert resolve_display_name_nonblocking("ou_alice", factory) == "Alice"
        factory.assert_called_once()

    def test_nonblocking_reservation_rechecks_cache_after_concurrent_refresh(
        self,
        monkeypatch,
    ):
        from src.feishu import user_cache

        real_get_cached = user_cache._get_cached
        first_lookup = True

        def miss_then_complete_refresh(user_id: str, now: float):
            nonlocal first_lookup
            if first_lookup:
                first_lookup = False
                user_cache._store_cached(user_id, "Alice")
                return None
            return real_get_cached(user_id, now)

        submit = MagicMock()
        monkeypatch.setattr(user_cache, "_get_cached", miss_then_complete_refresh)
        monkeypatch.setattr(user_cache, "submit_io", submit)

        result = resolve_display_name_nonblocking("ou_alice", MagicMock())

        assert result == "Alice"
        submit.assert_not_called()

    def test_nonblocking_refresh_queue_is_bounded(self, monkeypatch):
        from src.feishu import user_cache

        submitted: list[tuple[object, tuple[object, ...]]] = []
        monkeypatch.setattr(user_cache, "_MAX_PENDING_REFRESHES", 1)
        monkeypatch.setattr(
            user_cache,
            "submit_io",
            lambda fn, *args: submitted.append((fn, args)),
        )

        first = resolve_display_name_nonblocking("ou_first", MagicMock())
        second = resolve_display_name_nonblocking("ou_second", MagicMock())

        assert first == "ou_first(ID)"
        assert second == "ou_secon(ID)"
        assert len(submitted) == 1
        assert resolve_display_name_nonblocking("ou_second", MagicMock()) == second
