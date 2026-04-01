from src.utils.hooks import HookEvent, clear_hooks, fire_hooks, register_hook


class TestRegisterAndFire:
    def setup_method(self):
        clear_hooks()

    def teardown_method(self):
        clear_hooks()

    def test_register_and_fire(self):
        calls = []
        register_hook(HookEvent.ENGINE_START, lambda **kw: calls.append(kw))
        fire_hooks(HookEvent.ENGINE_START, engine="spec")
        assert calls == [{"engine": "spec"}]

    def test_unregister(self):
        calls = []
        unsub = register_hook(HookEvent.SESSION_START, lambda **kw: calls.append(1))
        unsub()
        fire_hooks(HookEvent.SESSION_START)
        assert calls == []

    def test_multiple_hooks(self):
        results = []
        register_hook(HookEvent.ITERATION_DONE, lambda **kw: results.append("a"))
        register_hook(HookEvent.ITERATION_DONE, lambda **kw: results.append("b"))
        fire_hooks(HookEvent.ITERATION_DONE)
        assert results == ["a", "b"]

    def test_fire_unknown_event_no_error(self):
        fire_hooks(HookEvent.ENGINE_STOP)

    def test_exception_in_hook_does_not_propagate(self):
        calls = []

        def bad(**kw):
            raise ValueError("boom")

        register_hook(HookEvent.PRE_SHELL_EXECUTE, bad)
        register_hook(HookEvent.PRE_SHELL_EXECUTE, lambda **kw: calls.append("ok"))
        fire_hooks(HookEvent.PRE_SHELL_EXECUTE)
        assert calls == ["ok"]

    def test_clear_hooks(self):
        calls = []
        register_hook(HookEvent.POST_SHELL_EXECUTE, lambda **kw: calls.append(1))
        clear_hooks()
        fire_hooks(HookEvent.POST_SHELL_EXECUTE)
        assert calls == []

    def test_unregister_idempotent(self):
        unsub = register_hook(HookEvent.SESSION_END, lambda **kw: None)
        unsub()
        unsub()

    def test_hook_event_values(self):
        assert HookEvent.PRE_SHELL_EXECUTE.value == "pre_shell_execute"
        assert HookEvent.ITERATION_DONE.value == "iteration_done"
