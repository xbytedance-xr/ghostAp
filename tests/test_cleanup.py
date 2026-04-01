import pytest

from src.utils.cleanup import _cleanup_fns, cleanup_count, register_cleanup, run_all_cleanups


@pytest.fixture(autouse=True)
def _clear_registry():
    _cleanup_fns.clear()
    yield
    _cleanup_fns.clear()


class TestCleanup:
    @pytest.mark.asyncio
    async def test_register_and_run(self):
        called = []

        async def fn_a():
            called.append("a")

        async def fn_b():
            called.append("b")

        register_cleanup(fn_a)
        register_cleanup(fn_b)
        await run_all_cleanups()
        assert sorted(called) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_unregister(self):
        called = []

        async def fn():
            called.append("x")

        unregister = register_cleanup(fn)
        unregister()
        await run_all_cleanups()
        assert called == []

    def test_cleanup_count(self):
        async def fn_a():
            pass

        async def fn_b():
            pass

        assert cleanup_count() == 0
        unreg = register_cleanup(fn_a)
        assert cleanup_count() == 1
        register_cleanup(fn_b)
        assert cleanup_count() == 2
        unreg()
        assert cleanup_count() == 1

    @pytest.mark.asyncio
    async def test_exception_isolation(self):
        called = []

        async def bad_fn():
            raise RuntimeError("boom")

        async def good_fn():
            called.append("ok")

        register_cleanup(bad_fn)
        register_cleanup(good_fn)
        await run_all_cleanups()
        assert called == ["ok"]
