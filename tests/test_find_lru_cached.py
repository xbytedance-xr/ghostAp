"""Unit tests for the module-level _find_lru_cached helper."""
from __future__ import annotations

import functools

from src.acp.providers import _find_lru_cached


class TestFindLruCached:
    """Cover the three main look-up paths of _find_lru_cached."""

    def test_direct_lru_cache_function(self) -> None:
        """A function decorated with @lru_cache should be found directly."""

        @functools.lru_cache(maxsize=1)
        def cached_fn() -> str:
            return "hello"

        result = _find_lru_cached(cached_fn)
        assert result is not None
        # Populate cache, then clear via returned handle
        cached_fn()
        assert cached_fn.cache_info().currsize == 1
        result()
        assert cached_fn.cache_info().currsize == 0

    def test_closure_containing_lru_cache(self) -> None:
        """A closure whose __closure__ captures an @lru_cache function."""

        @functools.lru_cache(maxsize=1)
        def inner() -> int:
            return 42

        def wrapper() -> int:
            return inner()

        # wrapper captures inner in its closure
        result = _find_lru_cached(wrapper)
        assert result is not None
        inner()
        assert inner.cache_info().currsize == 1
        result()
        assert inner.cache_info().currsize == 0

    def test_plain_function_returns_none(self) -> None:
        """A plain function with no cache should yield None."""

        def plain() -> str:
            return "no cache"

        assert _find_lru_cached(plain) is None

    def test_no_args_returns_none(self) -> None:
        """Calling with zero arguments should safely return None."""
        assert _find_lru_cached() is None

    def test_first_cached_wins(self) -> None:
        """When multiple functions have cache_clear, the first one wins."""
        call_count_1 = 0
        call_count_2 = 0

        @functools.lru_cache(maxsize=1)
        def first() -> int:
            nonlocal call_count_1
            call_count_1 += 1
            return 1

        @functools.lru_cache(maxsize=1)
        def second() -> int:
            nonlocal call_count_2
            call_count_2 += 1
            return 2

        first()
        second()
        result = _find_lru_cached(first, second)
        assert result is not None
        result()  # should clear first's cache
        assert first.cache_info().currsize == 0
        # second's cache should remain intact
        assert second.cache_info().currsize == 1

    def test_skips_plain_then_finds_cached(self) -> None:
        """A plain function is skipped; the next cached one is returned."""

        def plain() -> str:
            return "no cache"

        @functools.lru_cache(maxsize=1)
        def cached() -> str:
            return "cached"

        cached()
        result = _find_lru_cached(plain, cached)
        assert result is not None
        result()
        assert cached.cache_info().currsize == 0
