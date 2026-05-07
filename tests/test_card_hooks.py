"""Tests for CardSession lifecycle hook implementations (EmojiHook, ContextPersistenceHook)."""

from unittest.mock import MagicMock, call

import pytest

from src.card.events import CardEvent, CardEventType
from src.card.hooks import ContextPersistenceHook, EmojiHook, SessionHook
from src.card.session.config import SessionCallbacks, SessionConfig
from src.card.state.models import CardMetadata, CardState


def _make_terminal_state(reason: str = "completed") -> CardState:
    """Create a minimal terminal CardState for testing."""
    from src.card.state.reducer import reduce_card_state

    state = reduce_card_state(None, CardEvent.started(), CardMetadata(engine_type="deep"))
    if reason == "completed":
        state = reduce_card_state(state, CardEvent.completed(), CardMetadata(engine_type="deep"))
    elif reason == "failed":
        state = reduce_card_state(state, CardEvent.failed("test error"), CardMetadata(engine_type="deep"))
    return state


class TestSessionHookProtocol:
    """Verify that SessionHook protocol works as expected."""

    def test_protocol_isinstance_check(self):
        """Classes implementing both methods satisfy the protocol."""
        class MyHook:
            def on_dispatched(self, event, state):
                pass
            def on_terminal(self, state, reason):
                pass

        assert isinstance(MyHook(), SessionHook)


class TestEmojiHook:
    """Tests for EmojiHook."""

    def test_completed_adds_success_emoji(self):
        """on_terminal(COMPLETED) adds success emoji."""
        add_reaction = MagicMock()
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_123",
            success_emoji="PARTY",
            error_emoji="SOB",
        )
        state = _make_terminal_state("completed")
        hook.on_terminal(state, "completed")

        add_reaction.assert_called_once_with("msg_123", "PARTY")

    def test_failed_adds_error_emoji(self):
        """on_terminal(FAILED) adds error emoji."""
        add_reaction = MagicMock()
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_456",
            success_emoji="PARTY",
            error_emoji="SOB",
        )
        state = _make_terminal_state("failed")
        hook.on_terminal(state, "failed")

        add_reaction.assert_called_once_with("msg_456", "SOB")

    def test_cancelled_does_nothing(self):
        """on_terminal(CANCELLED) adds stop emoji."""
        add_reaction = MagicMock()
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_789",
            success_emoji="PARTY",
            error_emoji="SOB",
            stop_emoji="STOP",
        )
        state = _make_terminal_state("completed")
        hook.on_terminal(state, "cancelled")

        add_reaction.assert_called_once_with("msg_789", "STOP")

    def test_cancelled_adds_stop_emoji(self):
        """on_terminal with reason='cancelled' adds stop emoji."""
        add_reaction = MagicMock()
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_cancel",
            success_emoji="PARTY",
            error_emoji="SOB",
            stop_emoji="STOP",
        )
        state = _make_terminal_state("completed")
        hook.on_terminal(state, "cancelled")

        add_reaction.assert_called_once_with("msg_cancel", "STOP")

    def test_ttl_expired_adds_stop_emoji(self):
        """on_terminal with reason='ttl_expired' adds stop emoji."""
        add_reaction = MagicMock()
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_ttl",
            success_emoji="PARTY",
            error_emoji="SOB",
            stop_emoji="STOP",
        )
        state = _make_terminal_state("completed")
        hook.on_terminal(state, "ttl_expired")

        add_reaction.assert_called_once_with("msg_ttl", "STOP")

    def test_on_dispatched_is_noop(self):
        """on_dispatched should not trigger any reaction."""
        add_reaction = MagicMock()
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_1",
            success_emoji="PARTY",
            error_emoji="SOB",
        )
        state = _make_terminal_state("completed")
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"text": "hi"})
        hook.on_dispatched(event, state)

        add_reaction.assert_not_called()

    def test_default_emojis_from_emoji_reaction(self):
        """Default emojis come from class constants (PARTY for success)."""
        add_reaction = MagicMock()
        hook = EmojiHook(add_reaction=add_reaction, message_id="msg_x")

        state = _make_terminal_state("completed")
        hook.on_terminal(state, "completed")

        add_reaction.assert_called_once_with("msg_x", "PARTY")


class TestContextPersistenceHook:
    """Tests for ContextPersistenceHook."""

    def test_completed_calls_update_fn(self):
        """on_terminal(COMPLETED) calls the update function with state."""
        update_fn = MagicMock()
        hook = ContextPersistenceHook(update_fn=update_fn)
        state = _make_terminal_state("completed")

        hook.on_terminal(state, "completed")

        update_fn.assert_called_once_with(state)

    def test_failed_does_not_call_update_fn(self):
        """on_terminal(FAILED) does NOT call the update function."""
        update_fn = MagicMock()
        hook = ContextPersistenceHook(update_fn=update_fn)
        state = _make_terminal_state("failed")

        hook.on_terminal(state, "failed")

        update_fn.assert_not_called()

    def test_cancelled_does_not_call_update_fn(self):
        """on_terminal(CANCELLED) does NOT call the update function."""
        update_fn = MagicMock()
        hook = ContextPersistenceHook(update_fn=update_fn)
        state = _make_terminal_state("completed")

        hook.on_terminal(state, "cancelled")

        update_fn.assert_not_called()

    def test_on_dispatched_is_noop(self):
        """on_dispatched does not trigger persistence."""
        update_fn = MagicMock()
        hook = ContextPersistenceHook(update_fn=update_fn)
        state = _make_terminal_state("completed")
        event = CardEvent(type=CardEventType.STARTED)

        hook.on_dispatched(event, state)

        update_fn.assert_not_called()


class TestHookExceptionIsolation:
    """Verify that a failing hook does not prevent subsequent hooks from running."""

    def test_first_hook_exception_does_not_block_second(self):
        """When the first hook raises, the second hook is still called.

        This tests the _fire_hooks_terminal wrapper in CardSession which
        wraps each hook call in try/except.
        """
        from src.card.session import CardSession
        from src.card.delivery.engine import CardDelivery

        class BrokenHook:
            def on_dispatched(self, event, state):
                pass
            def on_terminal(self, state, reason):
                raise RuntimeError("first hook explodes")

        second_calls = []

        class GoodHook:
            def on_dispatched(self, event, state):
                pass
            def on_terminal(self, state, reason):
                second_calls.append(reason)

        class MockClient:
            def create_card(self, chat_id, card_json, *, reply_to=None):
                return ("msg_1", "card_1")
            def update_card(self, card_id, card_json, *, sequence=0):
                pass

        delivery = CardDelivery(MockClient())
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔍")
        config = SessionConfig(metadata=metadata)
        session = CardSession(
            chat_id="c1", config=config, delivery=delivery,
            session_id="s1", hooks=(BrokenHook(), GoodHook()),
        )
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.completed())

        # Second hook should still have been called despite first hook raising
        assert second_calls == ["completed"]


class TestEmojiHookAddReactionFailure:
    """Verify EmojiHook add_reaction exceptions don't leak through CardSession."""

    def test_add_reaction_connection_error_isolated_by_session(self):
        """When add_reaction raises ConnectionError inside EmojiHook,
        CardSession's _fire_hooks_terminal catches it — no leak to dispatch caller."""
        from src.card.session import CardSession
        from src.card.delivery.engine import CardDelivery

        add_reaction = MagicMock(side_effect=ConnectionError("network down"))

        class MockClient:
            def create_card(self, chat_id, card_json, *, reply_to=None):
                return ("msg_1", "card_1")
            def update_card(self, card_id, card_json, *, sequence=0):
                pass

        delivery = CardDelivery(MockClient())
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_err",
            success_emoji="PARTY",
            error_emoji="SOB",
        )
        metadata = CardMetadata(engine_type="deep", mode_name="Deep", mode_emoji="🔍")
        config = SessionConfig(metadata=metadata)
        session = CardSession(
            chat_id="c1", config=config, delivery=delivery,
            session_id="s1", hooks=(hook,),
        )
        # Should NOT raise despite add_reaction throwing ConnectionError
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.completed())

        assert session.closed is True
        add_reaction.assert_called_once_with("msg_err", "PARTY")

    def test_add_reaction_runtime_error_isolated_by_session(self):
        """When add_reaction raises RuntimeError, dispatch still completes normally."""
        from src.card.session import CardSession
        from src.card.delivery.engine import CardDelivery

        add_reaction = MagicMock(side_effect=RuntimeError("API rate limited"))

        class MockClient:
            def create_card(self, chat_id, card_json, *, reply_to=None):
                return ("msg_1", "card_1")
            def update_card(self, card_id, card_json, *, sequence=0):
                pass

        delivery = CardDelivery(MockClient())
        hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_rate",
            success_emoji="PARTY",
            error_emoji="SOB",
        )
        metadata = CardMetadata(engine_type="loop", mode_name="Loop", mode_emoji="🔄")
        config = SessionConfig(metadata=metadata)
        session = CardSession(
            chat_id="c2", config=config, delivery=delivery,
            session_id="s2", hooks=(hook,),
        )
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.failed("some error"))

        assert session.closed is True
        add_reaction.assert_called_once_with("msg_rate", "SOB")


class TestOnDispatchedExceptionIsolation:
    """Verify that on_dispatched hook exceptions do not block subsequent dispatches."""

    def test_broken_dispatched_hook_does_not_block_pipeline(self):
        """A hook raising in on_dispatched should not prevent state updates."""
        from unittest.mock import MagicMock
        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession
        from src.card.state.models import CardMetadata
        from src.card.events import CardEvent, CardEventType

        class BrokenDispatchedHook:
            def on_dispatched(self, event, state):
                raise RuntimeError("hook exploded in on_dispatched!")

            def on_terminal(self, state, reason):
                pass

        client = MagicMock()
        delivery = CardDelivery(client)
        config = SessionConfig(metadata=CardMetadata(engine_type="deep"))
        session = CardSession(
            delivery=delivery,
            chat_id="test_hook_err",
            config=config,
            hooks=(BrokenDispatchedHook(),),
        )

        # This should NOT raise despite the broken hook
        session.dispatch(CardEvent(type=CardEventType.STARTED, payload={}))
        session.dispatch(CardEvent(type=CardEventType.TEXT_STARTED, payload={"block_id": "t1"}))
        session.dispatch(CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "t1", "text": "hello"}))

        # State should still be updated correctly
        assert session._state is not None
        text_blocks = [b for b in session._state.blocks if b.kind == "text"]
        assert len(text_blocks) == 1
        assert "hello" in text_blocks[0].content
        session.close()

    def test_multiple_hooks_one_broken_dispatched(self):
        """If first hook raises in on_dispatched, second hook should still be called."""
        from unittest.mock import MagicMock
        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession
        from src.card.state.models import CardMetadata
        from src.card.events import CardEvent, CardEventType

        class BrokenHook:
            def on_dispatched(self, event, state):
                raise ValueError("broken")

            def on_terminal(self, state, reason):
                pass

        class GoodHook:
            dispatched_count = 0

            def on_dispatched(self, event, state):
                GoodHook.dispatched_count += 1

            def on_terminal(self, state, reason):
                pass

        GoodHook.dispatched_count = 0
        client = MagicMock()
        delivery = CardDelivery(client)
        config = SessionConfig(metadata=CardMetadata(engine_type="deep"))
        session = CardSession(
            delivery=delivery,
            chat_id="test_multi_hook",
            config=config,
            hooks=(BrokenHook(), GoodHook()),
        )

        session.dispatch(CardEvent(type=CardEventType.STARTED, payload={}))
        # fire_dispatched is now fire-and-forget; give the executor time to run hooks
        import time
        time.sleep(0.1)
        # GoodHook should still get called despite BrokenHook raising
        assert GoodHook.dispatched_count >= 1
        session.close()


class TestHookConcurrentStress:
    """Stress test: many sessions firing terminal hooks concurrently.

    Verifies that the shared _HookExecutorManager handles concurrent
    submissions without deadlock, data corruption, or lost callbacks.
    """

    def test_concurrent_terminal_hooks_no_lost_callbacks(self):
        """20 sessions completing concurrently: every hook is called exactly once."""
        import threading
        import time
        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession

        call_log: list[str] = []
        lock = threading.Lock()

        class TrackerHook:
            def __init__(self, session_id: str):
                self._sid = session_id

            def on_dispatched(self, event, state):
                pass

            def on_terminal(self, state, reason):
                with lock:
                    call_log.append(self._sid)

        class MockClient:
            def create_card(self, chat_id, card_json, *, reply_to=None):
                return ("msg_1", "card_1")
            def update_card(self, card_id, card_json, *, sequence=0):
                pass

        n_sessions = 20
        sessions = []
        for i in range(n_sessions):
            delivery = CardDelivery(MockClient())
            hook = TrackerHook(f"s{i}")
            config = SessionConfig(metadata=CardMetadata(engine_type="deep", mode_name="Deep"))
            s = CardSession(
                chat_id=f"c{i}",
                config=config,
                delivery=delivery,
                session_id=f"stress_{i}",
                hooks=(hook,),
            )
            s.dispatch(CardEvent.started())
            sessions.append(s)

        # Allow fire_dispatched hooks to complete and release semaphore slots
        time.sleep(0.2)

        errors = []

        def complete_session(s):
            try:
                s.dispatch(CardEvent.completed())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=complete_session, args=(s,)) for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert errors == [], f"Hook stress errors: {errors}"
        assert len(call_log) == n_sessions, f"Expected {n_sessions} hook calls, got {len(call_log)}"

    def test_concurrent_hooks_with_slow_hook(self):
        """Slow hooks should not block other sessions' hooks (thread pool isolation)."""
        import threading
        import time
        from src.card.delivery.engine import CardDelivery
        from src.card.session import CardSession

        fast_calls: list[str] = []
        lock = threading.Lock()

        class SlowHook:
            def on_dispatched(self, event, state):
                pass

            def on_terminal(self, state, reason):
                time.sleep(0.5)

        class FastHook:
            def __init__(self, sid):
                self._sid = sid

            def on_dispatched(self, event, state):
                pass

            def on_terminal(self, state, reason):
                with lock:
                    fast_calls.append(self._sid)

        class MockClient:
            def create_card(self, chat_id, card_json, *, reply_to=None):
                return ("msg_1", "card_1")
            def update_card(self, card_id, card_json, *, sequence=0):
                pass

        # One session with a slow hook
        delivery_slow = CardDelivery(MockClient())
        config_slow = SessionConfig(metadata=CardMetadata(engine_type="deep", mode_name="Deep"))
        slow_session = CardSession(
            chat_id="c_slow",
            config=config_slow,
            delivery=delivery_slow,
            session_id="slow_sess",
            hooks=(SlowHook(),),
        )
        slow_session.dispatch(CardEvent.started())

        # 5 sessions with fast hooks
        fast_sessions = []
        for i in range(5):
            delivery = CardDelivery(MockClient())
            config_fast = SessionConfig(metadata=CardMetadata(engine_type="loop", mode_name="Loop"))
            s = CardSession(
                chat_id=f"cf{i}",
                config=config_fast,
                delivery=delivery,
                session_id=f"fast_{i}",
                hooks=(FastHook(f"f{i}"),),
            )
            s.dispatch(CardEvent.started())
            fast_sessions.append(s)

        # Complete all concurrently
        all_sessions = [slow_session] + fast_sessions
        threads = [threading.Thread(target=lambda s: s.dispatch(CardEvent.completed()), args=(s,)) for s in all_sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # All fast hooks should have been called
        assert len(fast_calls) == 5


class TestHookFirerBackpressure:
    """Task 36: Verify backpressure behavior of _HookExecutorManager."""

    def test_backpressure_returns_none(self):
        """When semaphore exhausted, submit() returns None."""
        from src.card.hooks import _HookExecutorManager
        import threading

        mgr = _HookExecutorManager()
        # Exhaust all semaphore slots
        slots = mgr._get_max_workers() * 4
        blockers = []
        barrier = threading.Event()

        def block():
            barrier.wait(timeout=5)

        for _ in range(slots):
            f = mgr.submit(block)
            assert f is not None
            blockers.append(f)

        # Next submission should return None (backpressure)
        result = mgr.submit(lambda: None)
        assert result is None

        # Release all blockers
        barrier.set()
        for f in blockers:
            f.result(timeout=5)

    def test_semaphore_released_after_completion(self):
        """After task completes, semaphore slot is freed."""
        from src.card.hooks import _HookExecutorManager

        mgr = _HookExecutorManager()
        # Submit and wait
        future = mgr.submit(lambda: "done")
        assert future is not None
        assert future.result(timeout=5) == "done"

        # Should still be able to submit
        future2 = mgr.submit(lambda: "also_done")
        assert future2 is not None
        assert future2.result(timeout=5) == "also_done"

    def test_semaphore_released_on_exception(self):
        """Semaphore is released even if the submitted function raises."""
        from src.card.hooks import _HookExecutorManager

        mgr = _HookExecutorManager()

        def raiser():
            raise ValueError("boom")

        future = mgr.submit(raiser)
        assert future is not None
        with pytest.raises(ValueError):
            future.result(timeout=5)

        # Slot should be freed — can still submit
        f2 = mgr.submit(lambda: "ok")
        assert f2 is not None
        assert f2.result(timeout=5) == "ok"


class TestHookFirerFilterNone:
    """Task 36: HookFirer.fire_terminal filters None futures from backpressure."""

    def test_fire_terminal_skips_none_futures_gracefully(self):
        """Even if all hooks hit backpressure, fire_terminal does not crash."""
        from unittest.mock import patch
        from src.card.hooks import HookFirer, _hook_executor_manager

        class DummyHook:
            called = False
            def on_dispatched(self, event, state):
                pass
            def on_terminal(self, state, reason):
                DummyHook.called = True

        DummyHook.called = False
        firer = HookFirer(hooks=(DummyHook(),), session_id="test_bp")
        state = _make_terminal_state("completed")

        # Patch submit to always return None (simulating backpressure)
        with patch.object(_hook_executor_manager, "submit", return_value=None):
            # Should not raise
            firer.fire_terminal(state, "completed")

        # Hook was NOT called because submit returned None
        assert DummyHook.called is False


class TestHookExecutorRebuild:
    """Task 36: Executor rebuild on consecutive timeouts."""

    def test_rebuild_after_consecutive_timeouts(self):
        """After _MAX_CONSECUTIVE_TIMEOUTS, executor is rebuilt."""
        from src.card.hooks import _HookExecutorManager, _MAX_CONSECUTIVE_TIMEOUTS

        mgr = _HookExecutorManager()
        old_executor = mgr._executor

        for _ in range(_MAX_CONSECUTIVE_TIMEOUTS):
            mgr.record_timeout()

        # Executor should have been rebuilt
        assert mgr._executor is not old_executor
        assert mgr._consecutive_timeouts == 0

    def test_success_resets_timeout_counter(self):
        """record_success() resets consecutive timeout counter."""
        from src.card.hooks import _HookExecutorManager, _MAX_CONSECUTIVE_TIMEOUTS

        mgr = _HookExecutorManager()
        mgr.record_timeout()  # 1 timeout
        mgr.record_success()  # reset
        old_executor = mgr._executor
        mgr.record_timeout()  # 1 timeout again (not threshold)
        assert mgr._executor is old_executor  # not rebuilt


class TestOnDispatchedHookTimeout:
    """Verify that a slow on_dispatched hook does not block the session pipeline."""

    def test_slow_hook_does_not_block_dispatch(self):
        """A hook that sleeps should not prevent dispatch from completing within timeout."""
        import time
        import threading
        from src.card.hooks import HookFirer, DISPATCHED_HOOK_TIMEOUT

        state = _make_terminal_state("completed")
        cancel = threading.Event()

        class SlowHook:
            def on_dispatched(self, event, state):
                cancel.wait(timeout=DISPATCHED_HOOK_TIMEOUT + 1.0)  # Longer than timeout
            def on_terminal(self, state, reason):
                pass

        firer = HookFirer((SlowHook(),), "test-session")
        event = CardEvent.started()

        start = time.monotonic()
        firer.fire_dispatched(event, state)
        elapsed = time.monotonic() - start

        # Should return within DISPATCHED_HOOK_TIMEOUT + small margin
        assert elapsed < DISPATCHED_HOOK_TIMEOUT + 1.0, f"fire_dispatched blocked for {elapsed:.1f}s"
        # Signal the hook to exit immediately for fast teardown
        cancel.set()


class TestResetHookExecutorShutdown:
    """Verify _reset_hook_executor shuts down old instance before creating new."""

    def test_reset_shuts_down_old_executor(self):
        """_reset_hook_executor should call shutdown() on the old manager before replacing."""
        from unittest.mock import patch, MagicMock
        import src.card.hooks as hooks_mod

        # Capture the old manager
        old_manager = hooks_mod._hook_executor_manager
        old_manager_shutdown = MagicMock()
        old_manager.shutdown = old_manager_shutdown

        hooks_mod._reset_hook_executor()

        # Old manager should have been shut down
        old_manager_shutdown.assert_called_once()

        # New manager should be a different instance
        assert hooks_mod._hook_executor_manager is not old_manager


class TestEmojiHookEmptyMessageId:
    """Verify EmojiHook gracefully skips when message_id is empty."""

    def test_empty_message_id_on_terminal_no_exception(self):
        """EmojiHook(message_id='') on_terminal('completed') does not raise and does not call API."""
        add_reaction = MagicMock()
        hook = EmojiHook(add_reaction=add_reaction, message_id="")
        state = _make_terminal_state("completed")
        # Should not raise
        hook.on_terminal(state, "completed")
        # Should NOT call add_reaction
        add_reaction.assert_not_called()

    def test_empty_message_id_on_terminal_failed(self):
        """EmojiHook(message_id='') on_terminal('failed') does not raise and does not call API."""
        add_reaction = MagicMock()
        hook = EmojiHook(add_reaction=add_reaction, message_id="")
        state = _make_terminal_state("failed")
        hook.on_terminal(state, "failed")
        add_reaction.assert_not_called()
